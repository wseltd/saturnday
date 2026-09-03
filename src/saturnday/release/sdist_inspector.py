"""Sdist artefact unpacker and inventory generator.

Unpacks a Python source distribution (``.tar.gz``) into a temporary directory,
parses ``PKG-INFO`` for metadata, and produces an :class:`ArtefactInventory`
with per-file path, size, and SHA-256 hash.

Sdists are ``gzip``-compressed tar archives.  This module uses only stdlib
(``tarfile``, ``hashlib``, ``email.parser``).

The returned inventory uses ``artefact_type = "sdist"`` and has an empty
``record_entries`` list (sdists do not have a RECORD file).

Usage::

    from pathlib import Path
    from saturnday.release.sdist_inspector import inspect_sdist

    inventory, unpack_dir = inspect_sdist(Path("dist/saturnday-1.1.01.tar.gz"))
    for f in inventory.files:
        print(f.path, f.size, f.sha256)
"""
from __future__ import annotations

import email.parser
import hashlib
import logging
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

from saturnday.release._types import ArtefactFile, ArtefactInventory

logger = logging.getLogger(__name__)


def inspect_sdist(
    sdist_path: Path,
    target_dir: Optional[Path] = None,
) -> tuple[ArtefactInventory, Path]:
    """Unpack an sdist and produce a full :class:`ArtefactInventory`.

    Args:
        sdist_path:  Path to the ``.tar.gz`` sdist file to inspect.
        target_dir:  Directory to unpack into.  If ``None``, a new temporary
                     directory is created.  The caller is responsible for
                     cleanup when ``target_dir`` is ``None``; when it is
                     supplied, the caller owns it regardless.

    Returns:
        A two-tuple of ``(ArtefactInventory, unpack_dir)`` where
        ``unpack_dir`` is the :class:`~pathlib.Path` of the directory
        containing the unpacked sdist contents.

    Raises:
        ValueError: If *sdist_path* does not exist or does not end in
                    ``.tar.gz``.
        tarfile.TarError: If the archive is corrupt (re-raised with context).
        RuntimeError: On unexpected internal errors during inventory building.
    """
    sdist_path = Path(sdist_path).resolve()

    if not sdist_path.exists():
        raise ValueError(f"Sdist file does not exist: {sdist_path}")
    if not sdist_path.name.endswith(".tar.gz"):
        raise ValueError(
            f"Expected a .tar.gz file, got: {sdist_path.name}"
        )

    if target_dir is None:
        unpack_dir = Path(tempfile.mkdtemp(prefix="saturnday-sdist-"))
    else:
        unpack_dir = Path(target_dir).resolve()
        unpack_dir.mkdir(parents=True, exist_ok=True)

    sdist_sha256 = _hash_file(sdist_path)

    logger.info(
        "Unpacking sdist %s (sha256=%s) → %s",
        sdist_path.name,
        sdist_sha256[:16] + "…",
        unpack_dir,
    )

    try:
        with tarfile.open(sdist_path, "r:gz") as tf:
            # Filter for safety (Python 3.12+), fall back gracefully.
            _safe_extract(tf, unpack_dir)
            member_names: list[str] = tf.getnames()
    except tarfile.TarError:
        logger.error("Corrupt or invalid sdist archive: %s", sdist_path)
        raise

    logger.debug("Extracted %d entries from sdist", len(member_names))

    # Build per-file inventory from the unpacked tree.
    artefact_files = _build_file_inventory(unpack_dir)

    # Parse PKG-INFO.
    metadata = _parse_pkg_info(unpack_dir)

    inventory = ArtefactInventory(
        artefact_type="sdist",
        artefact_path=str(sdist_path),
        artefact_sha256=sdist_sha256,
        files=artefact_files,
        record_entries=[],  # sdists do not have a RECORD file
        metadata=metadata,
    )

    logger.info(
        "Sdist inventory complete: %d files",
        len(artefact_files),
    )

    return inventory, unpack_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract all members from *tf* to *dest*, guarding against path traversal.

    Uses the ``filter="data"`` argument introduced in Python 3.12 when
    available to strip unsafe attributes.  Falls back to manual member
    validation on older interpreters.

    Args:
        tf:   Open :class:`tarfile.TarFile` instance.
        dest: Destination directory (must already exist).

    Raises:
        tarfile.TarError: Propagated from the underlying extraction call.
    """
    import sys

    dest_str = str(dest.resolve())

    if sys.version_info >= (3, 12):
        try:
            tf.extractall(dest, filter="data")
            return
        except TypeError:
            # Some 3.12 builds may not yet support filter= on all platforms.
            pass

    # Manual path-traversal check for Python < 3.12.
    for member in tf.getmembers():
        member_path = (dest / member.name).resolve()
        if not str(member_path).startswith(dest_str):
            logger.warning(
                "Skipping potentially unsafe tar entry: %s", member.name
            )
            continue
        tf.extract(member, dest)  # noqa: S202 — we validated above


def _build_file_inventory(unpack_dir: Path) -> list[ArtefactFile]:
    """Walk *unpack_dir* and build :class:`ArtefactFile` entries.

    Directories are skipped; only regular files are included.

    Args:
        unpack_dir: Root of the unpacked sdist tree.

    Returns:
        List of :class:`ArtefactFile` instances, one per regular file found,
        with paths relative to *unpack_dir* using forward slashes.
    """
    result: list[ArtefactFile] = []
    for file_path in sorted(unpack_dir.rglob("*")):
        if not file_path.is_file():
            continue
        try:
            sha256 = _hash_file(file_path)
            size = file_path.stat().st_size
        except OSError as exc:
            logger.warning("Could not read unpacked file %s: %s", file_path, exc)
            sha256 = ""
            size = 0
        rel = file_path.relative_to(unpack_dir).as_posix()
        result.append(ArtefactFile(path=rel, size=size, sha256=sha256))
    return result


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


def _parse_pkg_info(unpack_dir: Path) -> dict:
    """Parse the sdist ``PKG-INFO`` file into a plain dict.

    ``PKG-INFO`` uses the same RFC 822-style format as wheel ``METADATA``.
    :mod:`email.parser` is the correct parser for this format.

    The ``PKG-INFO`` file is expected at the top of the first extracted
    directory (i.e., ``<name>-<version>/PKG-INFO``).

    Args:
        unpack_dir: Root of the unpacked sdist tree.

    Returns:
        Dict of metadata key → value (or list of values for repeated keys).
        Empty dict if ``PKG-INFO`` is not found.
    """
    pkg_info_files = sorted(unpack_dir.rglob("PKG-INFO"))
    if not pkg_info_files:
        logger.warning("No PKG-INFO file found in %s", unpack_dir)
        return {}

    # Prefer the top-level PKG-INFO (shortest path relative to unpack_dir).
    pkg_info_path = min(
        pkg_info_files,
        key=lambda p: len(p.relative_to(unpack_dir).parts),
    )

    try:
        raw = pkg_info_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("Could not read PKG-INFO at %s: %s", pkg_info_path, exc)
        return {}

    msg = email.parser.Parser().parsestr(raw)
    result: dict = {}
    for key in set(msg.keys()):
        values = msg.get_all(key)
        if values is None:
            continue
        result[key] = values[0] if len(values) == 1 else values

    logger.debug(
        "Parsed %d metadata keys from %s", len(result), pkg_info_path
    )
    return result


__all__ = ["inspect_sdist"]
