"""npm tarball unpacker and inventory generator.

Unpacks an npm ``.tgz`` tarball into a temporary directory, parses
``package.json`` from inside the tarball for metadata, and produces an
:class:`~saturnday.release._types.ArtefactInventory` with ``artefact_type =
"npm"`` containing per-file path, size, and SHA-256 hash.

npm tarballs are gzipped tar archives where every file lives under a
``package/`` prefix directory.  This module strips that prefix so that
inventory paths are relative to the npm package root (e.g.
``"package.json"``, ``"dist/index.js"``), consistent with the paths that
``npm pack --json`` reports.

Validation:
    When *npm_file_list* is supplied (from :mod:`saturnday.release.npm_builder`),
    every file reported by npm is checked against the unpacked contents and
    vice versa.  Discrepancies are logged as warnings — they do not raise
    exceptions.  The check results are reflected in
    :attr:`~saturnday.release._types.ArtefactInventory.record_entries` where
    missing or extra paths are recorded.

Usage::

    from pathlib import Path
    from saturnday.release.npm_inspector import inspect_npm_tarball

    inventory, unpack_dir = inspect_npm_tarball(Path("my-package-1.0.0.tgz"))
    for f in inventory.files:
        print(f.path, f.size, f.sha256)
"""
from __future__ import annotations

import hashlib
import json
import logging
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

from saturnday.release._types import ArtefactFile, ArtefactInventory

logger = logging.getLogger(__name__)

# npm always places files under a 'package/' prefix inside the tarball.
_NPM_PREFIX = "package/"


def inspect_npm_tarball(
    tarball_path: Path,
    target_dir: Optional[Path] = None,
    npm_file_list: Optional[list[str]] = None,
) -> tuple[ArtefactInventory, Path]:
    """Unpack an npm tarball and produce a full :class:`ArtefactInventory`.

    The function is the npm counterpart to :func:`saturnday.release.wheel_inspector.inspect_wheel`.

    Args:
        tarball_path:  Path to the ``.tgz`` npm tarball to inspect.
        target_dir:    Directory to unpack into.  If ``None``, a new temporary
                       directory is created.  The caller is responsible for
                       cleanup in both cases.
        npm_file_list: Optional list of relative file paths as reported by
                       ``npm pack --json``.  When provided, the unpacked
                       contents are validated against this list and any
                       discrepancies are logged.

    Returns:
        A two-tuple of ``(ArtefactInventory, unpack_dir)`` where *unpack_dir*
        is the :class:`~pathlib.Path` of the directory containing the unpacked
        package contents (with the ``package/`` prefix stripped, so the root
        of the unpack tree *is* the package root).

    Raises:
        ValueError: If *tarball_path* does not exist.
    """
    tarball_path = Path(tarball_path).resolve()

    if not tarball_path.exists():
        raise ValueError(f"Tarball does not exist: {tarball_path}")

    if target_dir is None:
        unpack_dir = Path(tempfile.mkdtemp(prefix="saturnday-npm-"))
    else:
        unpack_dir = Path(target_dir).resolve()
        unpack_dir.mkdir(parents=True, exist_ok=True)

    tarball_sha256 = _hash_file(tarball_path)

    logger.info(
        "Unpacking npm tarball %s (sha256=%s) → %s",
        tarball_path.name,
        tarball_sha256[:16] + "…",
        unpack_dir,
    )

    # ------------------------------------------------------------------
    # Unpack the tarball.
    # ------------------------------------------------------------------
    try:
        with tarfile.open(tarball_path, "r:gz") as tf:
            members = tf.getmembers()
            # Validate archive members before extraction to avoid path
            # traversal attacks (e.g. absolute paths or ``../`` sequences).
            safe_members = _filter_safe_members(members, tarball_path.name)
            # filter="data" is the safe default for Python 3.14+; fall back
            # gracefully on older Python versions that do not support it.
            try:
                tf.extractall(path=unpack_dir, members=safe_members, filter="data")
            except TypeError:
                # Python < 3.12 does not accept the filter kwarg.
                tf.extractall(path=unpack_dir, members=safe_members)  # noqa: S202
    except tarfile.TarError as exc:
        logger.error(
            "Failed to unpack npm tarball %s: %s", tarball_path, exc
        )
        # Return an empty inventory rather than raising, so callers can
        # record the failure in evidence.
        return (
            ArtefactInventory(
                artefact_type="npm",
                artefact_path=str(tarball_path),
                artefact_sha256=tarball_sha256,
                files=[],
                record_entries=[f"ERROR: {exc}"],
                metadata={},
            ),
            unpack_dir,
        )
    except OSError as exc:
        logger.error(
            "OS error while unpacking npm tarball %s: %s", tarball_path, exc
        )
        return (
            ArtefactInventory(
                artefact_type="npm",
                artefact_path=str(tarball_path),
                artefact_sha256=tarball_sha256,
                files=[],
                record_entries=[f"ERROR: {exc}"],
                metadata={},
            ),
            unpack_dir,
        )

    logger.debug("Extracted %d members from tarball", len(members))

    # ------------------------------------------------------------------
    # Strip the 'package/' prefix from unpack_dir so paths are relative
    # to the package root.
    # ------------------------------------------------------------------
    # npm always extracts into <unpack_dir>/package/...
    # We promote that subdirectory to be the effective root.
    package_root = unpack_dir / "package"
    if package_root.is_dir():
        effective_root = package_root
    else:
        # Unusual tarball structure — use unpack_dir directly and log warning.
        logger.warning(
            "Expected 'package/' prefix directory not found in %s; "
            "using raw unpack dir as root",
            unpack_dir,
        )
        effective_root = unpack_dir

    # ------------------------------------------------------------------
    # Build per-file inventory (paths relative to effective_root).
    # ------------------------------------------------------------------
    artefact_files: list[ArtefactFile] = []
    for file_path in sorted(effective_root.rglob("*")):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(effective_root)
        rel_str = str(rel_path).replace("\\", "/")
        try:
            sha256 = _hash_file(file_path)
            size = file_path.stat().st_size
        except OSError as exc:
            logger.warning(
                "Could not read extracted file %s: %s", file_path, exc
            )
            sha256 = ""
            size = 0
        artefact_files.append(
            ArtefactFile(path=rel_str, size=size, sha256=sha256)
        )

    logger.debug(
        "Built inventory with %d files from %s", len(artefact_files), effective_root
    )

    # ------------------------------------------------------------------
    # Parse package.json for metadata.
    # ------------------------------------------------------------------
    metadata = _parse_package_json(effective_root)

    # ------------------------------------------------------------------
    # Validate against npm_file_list if provided.
    # ------------------------------------------------------------------
    record_entries = _validate_against_npm_list(
        artefact_files, npm_file_list, tarball_path.name
    )

    inventory = ArtefactInventory(
        artefact_type="npm",
        artefact_path=str(tarball_path),
        artefact_sha256=tarball_sha256,
        files=artefact_files,
        record_entries=record_entries,
        metadata=metadata,
    )

    logger.info(
        "npm tarball inventory complete: %d files, metadata name=%r version=%r",
        len(artefact_files),
        metadata.get("name"),
        metadata.get("version"),
    )

    return inventory, unpack_dir


# ---------------------------------------------------------------------------
# Canonical public alias matching the spec function signature.
# ---------------------------------------------------------------------------


def unpack_npm_tarball(
    tarball_path: Path,
    target_dir: Path,
    npm_file_list: Optional[list[str]] = None,
) -> ArtefactInventory:
    """Unpack *tarball_path* into *target_dir* and return the inventory.

    This is the spec-prescribed function signature from RS-026.  It delegates
    to :func:`inspect_npm_tarball` which also returns the unpack directory;
    callers that need the directory should use that function directly.

    Args:
        tarball_path:  Path to the ``.tgz`` npm tarball.
        target_dir:    Directory to unpack into (created if it does not exist).
        npm_file_list: Optional file list from ``npm pack --json`` for
                       validation.

    Returns:
        :class:`~saturnday.release._types.ArtefactInventory` with
        ``artefact_type = "npm"``.
    """
    inventory, _ = inspect_npm_tarball(
        tarball_path=tarball_path,
        target_dir=target_dir,
        npm_file_list=npm_file_list,
    )
    return inventory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """Return the lowercase hex SHA-256 digest of *path*'s content.

    Args:
        path: File to hash (must exist and be readable).

    Returns:
        64-character lowercase hex string.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _filter_safe_members(
    members: list[tarfile.TarInfo], tarball_name: str
) -> list[tarfile.TarInfo]:
    """Filter tar members to exclude unsafe paths (path traversal, absolute).

    Args:
        members:      All members from the tarball.
        tarball_name: Name of the tarball file (for log messages only).

    Returns:
        Filtered list of safe :class:`tarfile.TarInfo` members.
    """
    safe: list[tarfile.TarInfo] = []
    for member in members:
        name = member.name
        # Reject absolute paths and directory-traversal sequences.
        if name.startswith("/") or ".." in name.split("/"):
            logger.warning(
                "[%s] Skipping unsafe tarball member: %r",
                tarball_name,
                name,
            )
            continue
        # Reject symlinks and hard links — on Python < 3.12 the fallback
        # extraction path lacks filter="data", so a crafted symlink could
        # escape the extraction directory.
        if member.issym() or member.islnk():
            logger.warning(
                "[%s] Skipping symlink/hard-link tarball member: %r",
                tarball_name,
                name,
            )
            continue
        safe.append(member)
    return safe


def _parse_package_json(package_root: Path) -> dict:
    """Parse the ``package.json`` from inside the unpacked tarball.

    The ``package.json`` found here is the *shipped* one (may differ from
    the one in the repository if the build step transforms it).

    Args:
        package_root: Root directory of the unpacked npm package.

    Returns:
        Parsed JSON dict.  Empty dict if ``package.json`` is missing or
        cannot be parsed.
    """
    pkg_json_path = package_root / "package.json"
    if not pkg_json_path.exists():
        logger.warning(
            "No package.json found in unpacked npm tarball at %s", package_root
        )
        return {}

    try:
        raw = pkg_json_path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(
            "Could not parse package.json at %s: %s", pkg_json_path, exc
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            "package.json at %s parsed to %s, not a dict — returning empty",
            pkg_json_path,
            type(data).__name__,
        )
        return {}

    logger.debug(
        "Parsed package.json: name=%r version=%r",
        data.get("name"),
        data.get("version"),
    )
    return data


def _validate_against_npm_list(
    artefact_files: list[ArtefactFile],
    npm_file_list: Optional[list[str]],
    tarball_name: str,
) -> list[str]:
    """Compare unpacked files against the npm pack --json file list.

    Discrepancies are logged as warnings.  The raw ``npm_file_list`` entries
    are also stored verbatim so that downstream checks can access them.

    Args:
        artefact_files:  Files found by unpacking the tarball.
        npm_file_list:   File paths from ``npm pack --json``, or ``None`` to
                         skip validation.
        tarball_name:    Name of the tarball (for log messages).

    Returns:
        A :class:`list` of strings:
        - All entries from *npm_file_list* (if provided), followed by any
          warning lines for discrepancies.
        - Empty list if *npm_file_list* is ``None``.
    """
    if npm_file_list is None:
        return []

    unpacked_paths: set[str] = {f.path for f in artefact_files}
    npm_paths: set[str] = set(npm_file_list)

    record_entries = list(npm_file_list)

    # Files in tarball but not declared by npm (suspicious additions).
    extra = unpacked_paths - npm_paths
    for path in sorted(extra):
        msg = f"EXTRA (in tarball but not in npm pack --json): {path}"
        logger.warning("[%s] %s", tarball_name, msg)
        record_entries.append(msg)

    # Files declared by npm but absent from tarball (corrupt/missing).
    missing = npm_paths - unpacked_paths
    for path in sorted(missing):
        msg = f"MISSING (in npm pack --json but not in tarball): {path}"
        logger.warning("[%s] %s", tarball_name, msg)
        record_entries.append(msg)

    if not extra and not missing:
        logger.debug(
            "[%s] Tarball contents match npm pack --json exactly (%d files)",
            tarball_name,
            len(npm_paths),
        )

    return record_entries


__all__ = ["NpmBuildResult", "inspect_npm_tarball", "unpack_npm_tarball"]
