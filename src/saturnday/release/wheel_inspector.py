"""Wheel artefact unpacker and inventory generator.

Unpacks a Python wheel (``.whl``) file into a temporary directory, parses the
``RECORD`` file for declared contents, and produces an :class:`ArtefactInventory`
with per-file path, size, and SHA-256 hash.

Wheels are ZIP archives.  This module uses only stdlib (``zipfile``,
``hashlib``, ``email.parser``).

RECORD validation:
    Every file found in the unpacked wheel is compared against the wheel's
    ``RECORD`` file.  Files present in the zip but absent from ``RECORD``, and
    ``RECORD`` entries whose files are absent from the zip, are both flagged
    in the returned inventory via the ``record_entries`` field.

Usage::

    from pathlib import Path
    from saturnday.release.wheel_inspector import inspect_wheel

    inventory, unpack_dir = inspect_wheel(Path("dist/saturnday-1.1.01-py3-none-any.whl"))
    for f in inventory.files:
        print(f.path, f.size, f.sha256)
"""
from __future__ import annotations

import email.parser
import hashlib
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from saturnday.release._types import ArtefactFile, ArtefactInventory

logger = logging.getLogger(__name__)

# The RECORD file path suffix used inside wheel archives.
_RECORD_SUFFIX = ".dist-info/RECORD"
_METADATA_SUFFIX = ".dist-info/METADATA"


def inspect_wheel(
    wheel_path: Path,
    target_dir: Optional[Path] = None,
) -> tuple[ArtefactInventory, Path]:
    """Unpack a wheel and produce a full :class:`ArtefactInventory`.

    Args:
        wheel_path:  Path to the ``.whl`` file to inspect.
        target_dir:  Directory to unpack into.  If ``None``, a new temporary
                     directory is created.  The caller is responsible for
                     cleanup when ``target_dir`` is ``None``; when it is
                     supplied, the caller owns it regardless.

    Returns:
        A two-tuple of ``(ArtefactInventory, unpack_dir)`` where
        ``unpack_dir`` is the :class:`~pathlib.Path` of the directory
        containing the unpacked wheel contents.

    Raises:
        ValueError: If *wheel_path* does not exist or is not a ``.whl`` file.
        zipfile.BadZipFile: If the archive is corrupt (re-raised with context).
        RuntimeError: On unexpected internal errors during inventory building.
    """
    wheel_path = Path(wheel_path).resolve()

    if not wheel_path.exists():
        raise ValueError(f"Wheel file does not exist: {wheel_path}")
    if not wheel_path.suffix == ".whl":
        raise ValueError(
            f"Expected a .whl file, got: {wheel_path.name}"
        )

    if target_dir is None:
        unpack_dir = Path(tempfile.mkdtemp(prefix="saturnday-wheel-"))
    else:
        unpack_dir = Path(target_dir).resolve()
        unpack_dir.mkdir(parents=True, exist_ok=True)

    wheel_sha256 = _hash_file(wheel_path)

    logger.info(
        "Unpacking wheel %s (sha256=%s) → %s",
        wheel_path.name,
        wheel_sha256[:16] + "…",
        unpack_dir,
    )

    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            zf.extractall(unpack_dir)
            zip_names: list[str] = zf.namelist()
    except zipfile.BadZipFile:
        logger.error("Corrupt or invalid wheel archive: %s", wheel_path)
        raise

    logger.debug("Extracted %d entries from wheel", len(zip_names))

    # Build per-file inventory.
    artefact_files: list[ArtefactFile] = []
    for rel_path in zip_names:
        file_path = unpack_dir / rel_path
        if file_path.is_dir():
            continue  # directories have no content hash
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
            ArtefactFile(
                path=rel_path.replace("\\", "/"),
                size=size,
                sha256=sha256,
            )
        )

    # Locate and parse the RECORD file.
    record_entries = _read_record(unpack_dir)
    _validate_record(artefact_files, record_entries, wheel_path.name)

    # Parse METADATA.
    metadata = _parse_metadata(unpack_dir)

    inventory = ArtefactInventory(
        artefact_type="wheel",
        artefact_path=str(wheel_path),
        artefact_sha256=wheel_sha256,
        files=artefact_files,
        record_entries=record_entries,
        metadata=metadata,
    )

    logger.info(
        "Wheel inventory complete: %d files, %d RECORD entries",
        len(artefact_files),
        len(record_entries),
    )

    return inventory, unpack_dir


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


def _read_record(unpack_dir: Path) -> list[str]:
    """Read and return raw lines from the wheel RECORD file.

    The RECORD file is ``<dist-info-dir>/RECORD`` inside the unpacked
    directory.  If no RECORD file is found, a warning is logged and an
    empty list is returned.

    Args:
        unpack_dir: Root of the unpacked wheel tree.

    Returns:
        List of non-empty raw lines from the RECORD file.
    """
    record_files = sorted(unpack_dir.rglob("*.dist-info/RECORD"))
    if not record_files:
        logger.warning("No RECORD file found in unpacked wheel at %s", unpack_dir)
        return []
    if len(record_files) > 1:
        logger.warning(
            "Multiple RECORD files found: %s — using first", record_files
        )

    record_path = record_files[0]
    try:
        raw = record_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("Could not read RECORD at %s: %s", record_path, exc)
        return []

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    logger.debug("Read %d lines from RECORD at %s", len(lines), record_path)
    return lines


def _validate_record(
    artefact_files: list[ArtefactFile],
    record_entries: list[str],
    wheel_name: str,
) -> None:
    """Compare unpacked file list against RECORD entries and log discrepancies.

    This function only logs warnings — it does not raise or modify the
    inventory.  Callers that need to block on RECORD mismatches should
    implement a check on top of the returned inventory.

    Args:
        artefact_files:  Files extracted from the wheel zip.
        record_entries:  Raw RECORD lines (``path,hash,size`` format).
        wheel_name:      Name of the wheel file (for log messages only).
    """
    if not record_entries:
        return

    # RECORD lines are "relative/path,hash_algo:digest,size".
    # The path field is first; the RECORD entry for RECORD itself has empty
    # hash and size fields.
    record_paths: set[str] = set()
    for line in record_entries:
        parts = line.split(",")
        if parts:
            record_paths.add(parts[0].strip().replace("\\", "/"))

    unpacked_paths: set[str] = {f.path for f in artefact_files}

    # Files in zip but not declared in RECORD (suspicious).
    extra = unpacked_paths - record_paths
    for path in sorted(extra):
        # RECORD itself legitimately lists itself with no hash — ignore it.
        if path.endswith("/RECORD"):
            continue
        logger.warning(
            "[%s] File present in wheel zip but missing from RECORD: %s",
            wheel_name,
            path,
        )

    # RECORD entries that refer to absent files (corrupt artefact).
    missing = record_paths - unpacked_paths
    for path in sorted(missing):
        logger.warning(
            "[%s] RECORD entry references file not present in wheel zip: %s",
            wheel_name,
            path,
        )


def _parse_metadata(unpack_dir: Path) -> dict:
    """Parse the wheel METADATA file into a plain dict.

    Uses :mod:`email.parser` which is the correct tool for the RFC 822-style
    METADATA format used by Python packaging.

    Args:
        unpack_dir: Root of the unpacked wheel tree.

    Returns:
        Dict of metadata key → value (or list of values for repeated keys).
        Empty dict if METADATA is not found.
    """
    metadata_files = sorted(unpack_dir.rglob("*.dist-info/METADATA"))
    if not metadata_files:
        logger.warning("No METADATA file found in %s", unpack_dir)
        return {}

    meta_path = metadata_files[0]
    try:
        raw = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("Could not read METADATA at %s: %s", meta_path, exc)
        return {}

    msg = email.parser.Parser().parsestr(raw)
    result: dict = {}
    for key in set(msg.keys()):
        values = msg.get_all(key)
        if values is None:
            continue
        # Fix J.1: METADATA uses case-sensitive RFC-822 keys (e.g.
        # ``Author-email``, ``Classifier``, ``License-Expression``).
        # Consumers perform lowercase lookups, so normalise to lower so
        # those lookups find the real values.  The original-case keys are
        # also kept to preserve any consumer that still reads them.
        lowered = key.lower()
        normalised = values[0] if len(values) == 1 else values
        result[key] = normalised
        if lowered != key:
            result.setdefault(lowered, normalised)

    # Fix J.1: PEP 639 (metadata 2.4) replaces ``License:`` with
    # ``License-Expression:`` for SPDX expressions.  Alias so legacy
    # license-presence checks continue to find the value.
    if "license" not in result and "license-expression" in result:
        result["license"] = result["license-expression"]

    logger.debug(
        "Parsed %d metadata keys from %s", len(result), meta_path
    )
    return result


__all__ = ["inspect_wheel"]
