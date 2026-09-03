"""OCI/container image inspector for release-security checks.

Extracts and inventories container image contents for security inspection.
Supports two extraction methods:

1. ``docker save <image> | tar -x``  (requires Docker daemon)
2. ``skopeo copy docker://<image> oci:<dir>``  (daemon-less)

Falls back gracefully if neither tool is available.

For testing without a live Docker daemon, use :func:`inspect_oci_archive` with
a pre-saved ``.tar`` produced by ``docker save``.

Whiteout handling:
    OCI/Docker layers use a special convention to express file deletion.
    A file named ``.wh.<name>`` in an upper layer means ``<name>`` was
    deleted in that layer and must be excluded from the merged filesystem.
    A file named ``.wh..wh..opq`` means the entire directory is opaque
    (the lower layer's directory contents are completely replaced).  Both
    conventions are implemented here so the returned inventory reflects
    the final, merged filesystem rather than raw per-layer contents.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Optional

from saturnday.release._types import ArtefactFile, ArtefactInventory

logger = logging.getLogger(__name__)

# Whiteout prefix used by Docker/OCI layers.
_WH_PREFIX = ".wh."
# Opaque whiteout marker — marks an entire directory as replaced.
_WH_OPAQUE = ".wh..wh..opq"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inspect_oci_image(
    image_ref: str,
    target_dir: Optional[Path] = None,
) -> tuple[ArtefactInventory, Path]:
    """Inspect an OCI/container image pulled from a registry or local daemon.

    Tries the following extraction strategies in order:

    1. ``docker save <image_ref>`` piped to a tar extractor.
    2. ``skopeo copy docker://<image_ref> oci:<dir>``.

    Args:
        image_ref:  Image reference, e.g. ``"saturnday:latest"`` or
                    ``"ghcr.io/org/repo:tag"``.
        target_dir: Directory to use as the extraction root.  A new temporary
                    directory is created when ``None``.  The caller owns
                    cleanup for both cases.

    Returns:
        ``(ArtefactInventory, unpack_dir)`` where *unpack_dir* is the path
        that was used / created.

    Raises:
        RuntimeError: When neither ``docker`` nor ``skopeo`` is available and
                      no pre-saved archive is supplied.
    """
    if target_dir is None:
        unpack_dir = Path(tempfile.mkdtemp(prefix="saturnday-oci-"))
    else:
        unpack_dir = Path(target_dir).resolve()
        unpack_dir.mkdir(parents=True, exist_ok=True)

    # Strategy 1: docker save
    if shutil.which("docker") is not None:
        logger.info("Using 'docker save' to extract image %r", image_ref)
        archive_path = unpack_dir / "image.tar"
        try:
            _docker_save(image_ref, archive_path)
            return _inspect_tar_archive(
                archive_path=archive_path,
                unpack_dir=unpack_dir,
                image_ref=image_ref,
                artefact_path_override=None,
            )
        except Exception as exc:
            logger.warning(
                "docker save failed for %r: %s — will try skopeo", image_ref, exc
            )

    # Strategy 2: skopeo
    if shutil.which("skopeo") is not None:
        logger.info("Using 'skopeo copy' to extract image %r", image_ref)
        oci_layout_dir = unpack_dir / "oci-layout"
        try:
            _skopeo_copy(image_ref, oci_layout_dir)
            return _inspect_oci_layout(
                oci_layout_dir=oci_layout_dir,
                unpack_dir=unpack_dir,
                image_ref=image_ref,
            )
        except Exception as exc:
            logger.warning(
                "skopeo copy failed for %r: %s", image_ref, exc
            )

    raise RuntimeError(
        f"Cannot inspect OCI image {image_ref!r}: neither 'docker' nor "
        f"'skopeo' is available or both extraction attempts failed.  "
        f"Install Docker or Skopeo, or use inspect_oci_archive() with a "
        f"pre-saved archive."
    )


def inspect_oci_archive(
    archive_path: Path,
    target_dir: Optional[Path] = None,
) -> tuple[ArtefactInventory, Path]:
    """Inspect a pre-saved OCI archive (``.tar`` file from ``docker save``).

    This function does not require a Docker daemon and is the primary path
    used in tests.

    Args:
        archive_path: Path to the ``.tar`` file produced by ``docker save``.
        target_dir:   Directory to use as the extraction root.  A new temporary
                      directory is created when ``None``.  The caller owns
                      cleanup in both cases.

    Returns:
        ``(ArtefactInventory, unpack_dir)`` where *unpack_dir* is the path
        that was used / created.

    Raises:
        ValueError: When *archive_path* does not exist.
    """
    archive_path = Path(archive_path).resolve()
    if not archive_path.exists():
        raise ValueError(f"OCI archive does not exist: {archive_path}")

    if target_dir is None:
        unpack_dir = Path(tempfile.mkdtemp(prefix="saturnday-oci-"))
    else:
        unpack_dir = Path(target_dir).resolve()
        unpack_dir.mkdir(parents=True, exist_ok=True)

    return _inspect_tar_archive(
        archive_path=archive_path,
        unpack_dir=unpack_dir,
        image_ref=archive_path.name,
        artefact_path_override=str(archive_path),
    )


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def _docker_save(image_ref: str, dest: Path) -> None:
    """Run ``docker save <image_ref>`` and write output to *dest*.

    Args:
        image_ref: Docker image reference.
        dest:      Destination path for the saved ``.tar`` file.

    Raises:
        subprocess.CalledProcessError: When docker exits non-zero.
        OSError: When the output cannot be written.
    """
    logger.debug("docker save %s → %s", image_ref, dest)
    result = subprocess.run(  # noqa: S603 — shutil.which guard above
        ["docker", "save", image_ref],
        capture_output=True,
        check=True,
    )
    dest.write_bytes(result.stdout)
    logger.debug(
        "docker save wrote %d bytes for %r", len(result.stdout), image_ref
    )


def _skopeo_copy(image_ref: str, oci_layout_dir: Path) -> None:
    """Run ``skopeo copy docker://<image_ref> oci:<oci_layout_dir>``.

    Args:
        image_ref:      Docker image reference (without ``docker://`` prefix).
        oci_layout_dir: Output directory for the OCI image layout.

    Raises:
        subprocess.CalledProcessError: When skopeo exits non-zero.
    """
    src = f"docker://{image_ref}"
    dst = f"oci:{oci_layout_dir}"
    logger.debug("skopeo copy %s %s", src, dst)
    subprocess.run(  # noqa: S603 — shutil.which guard above
        ["skopeo", "copy", src, dst],
        capture_output=True,
        check=True,
    )


# ---------------------------------------------------------------------------
# docker-save format parser
# ---------------------------------------------------------------------------


def _inspect_tar_archive(
    archive_path: Path,
    unpack_dir: Path,
    image_ref: str,
    artefact_path_override: Optional[str],
) -> tuple[ArtefactInventory, Path]:
    """Parse a docker-save style ``.tar`` archive into an ArtefactInventory.

    The docker-save format is::

        manifest.json            — array of image manifests
        <config_sha>.json        — image config (env, entrypoint, cmd, labels)
        <layer_sha>/layer.tar    — one entry per layer

    Args:
        archive_path:           Path to the docker-save ``.tar``.
        unpack_dir:             Directory used as work area.
        image_ref:              Original image reference (for metadata).
        artefact_path_override: When not ``None``, used as ``artefact_path``
                                in the inventory instead of the derived value.

    Returns:
        ``(ArtefactInventory, unpack_dir)``
    """
    artefact_sha256 = _hash_file(archive_path)

    extract_dir = unpack_dir / "docker-save"
    extract_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Extracting docker-save archive %s (sha256=%s…)",
        archive_path.name,
        artefact_sha256[:16],
    )

    try:
        with tarfile.open(archive_path, "r") as outer:
            safe_members = _filter_safe_members(list(outer.getmembers()), archive_path.name)
            try:
                outer.extractall(path=extract_dir, members=safe_members, filter="data")
            except TypeError:
                outer.extractall(path=extract_dir, members=safe_members)  # noqa: S202
    except tarfile.TarError as exc:
        logger.error("Failed to open docker-save archive %s: %s", archive_path, exc)
        return _empty_inventory(
            artefact_path=artefact_path_override or str(archive_path),
            artefact_sha256=artefact_sha256,
            error=str(exc),
        ), unpack_dir

    # Parse manifest.json
    manifest_path = extract_dir / "manifest.json"
    if not manifest_path.exists():
        logger.warning(
            "No manifest.json found in archive %s; returning empty inventory",
            archive_path.name,
        )
        return _empty_inventory(
            artefact_path=artefact_path_override or str(archive_path),
            artefact_sha256=artefact_sha256,
            error="manifest.json not found",
        ), unpack_dir

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not parse manifest.json: %s", exc)
        return _empty_inventory(
            artefact_path=artefact_path_override or str(archive_path),
            artefact_sha256=artefact_sha256,
            error=f"manifest.json parse error: {exc}",
        ), unpack_dir

    if not isinstance(manifest_data, list) or not manifest_data:
        return _empty_inventory(
            artefact_path=artefact_path_override or str(archive_path),
            artefact_sha256=artefact_sha256,
            error="manifest.json is empty or not a list",
        ), unpack_dir

    # Use the first image entry (the common case)
    first_entry = manifest_data[0]
    config_file: str = first_entry.get("Config", "")
    layer_paths: list[str] = first_entry.get("Layers", [])

    # Parse image config for metadata
    metadata = _parse_image_config(extract_dir, config_file, image_ref)

    # Merge layers in order (bottom → top), resolving whiteouts
    merged: dict[str, ArtefactFile] = {}
    for layer_rel in layer_paths:
        layer_tar_path = extract_dir / layer_rel
        if not layer_tar_path.exists():
            logger.warning("Layer archive not found: %s", layer_tar_path)
            continue
        _apply_layer(layer_tar_path, merged)

    files = sorted(merged.values(), key=lambda f: f.path)

    logger.info(
        "OCI archive inventory complete: %d files, image_ref=%r",
        len(files),
        image_ref,
    )

    return ArtefactInventory(
        artefact_type="oci",
        artefact_path=artefact_path_override or str(archive_path),
        artefact_sha256=artefact_sha256,
        files=files,
        record_entries=[],
        metadata=metadata,
    ), unpack_dir


# ---------------------------------------------------------------------------
# OCI image layout (skopeo output) parser
# ---------------------------------------------------------------------------


def _inspect_oci_layout(
    oci_layout_dir: Path,
    unpack_dir: Path,
    image_ref: str,
) -> tuple[ArtefactInventory, Path]:
    """Parse an OCI image layout directory (skopeo output) into an inventory.

    OCI image layout spec (v1.0.0)::

        oci-layout/
          oci-layout          — ``{"imageLayoutVersion": "1.0.0"}``
          index.json          — top-level index pointing to manifests
          blobs/sha256/
            <digest>          — manifest JSON or config JSON or layer tar.gz

    Args:
        oci_layout_dir: Directory containing the OCI image layout.
        unpack_dir:     Work area for extracted layers.
        image_ref:      Original image reference (for metadata).

    Returns:
        ``(ArtefactInventory, unpack_dir)``
    """
    index_path = oci_layout_dir / "index.json"
    if not index_path.exists():
        logger.warning("No index.json in OCI layout dir %s", oci_layout_dir)
        return _empty_inventory(
            artefact_path=image_ref,
            artefact_sha256="",
            error="index.json not found in OCI layout",
        ), unpack_dir

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _empty_inventory(
            artefact_path=image_ref,
            artefact_sha256="",
            error=f"index.json parse error: {exc}",
        ), unpack_dir

    manifests = index.get("manifests", [])
    if not manifests:
        return _empty_inventory(
            artefact_path=image_ref,
            artefact_sha256="",
            error="index.json has no manifests",
        ), unpack_dir

    # Resolve the first manifest blob
    first_manifest_digest: str = manifests[0].get("digest", "")
    manifest_blob = _read_blob(oci_layout_dir, first_manifest_digest)
    if manifest_blob is None:
        return _empty_inventory(
            artefact_path=image_ref,
            artefact_sha256="",
            error=f"Cannot read manifest blob {first_manifest_digest!r}",
        ), unpack_dir

    try:
        manifest = json.loads(manifest_blob)
    except json.JSONDecodeError as exc:
        return _empty_inventory(
            artefact_path=image_ref,
            artefact_sha256="",
            error=f"Manifest blob is not valid JSON: {exc}",
        ), unpack_dir

    # Parse config for metadata
    config_digest: str = manifest.get("config", {}).get("digest", "")
    metadata: dict[str, Any] = {"image_ref": image_ref}
    if config_digest:
        config_blob = _read_blob(oci_layout_dir, config_digest)
        if config_blob is not None:
            try:
                config_data = json.loads(config_blob)
                metadata.update(_extract_config_metadata(config_data))
            except json.JSONDecodeError:
                pass

    # Merge layers
    merged: dict[str, ArtefactFile] = {}
    layers = manifest.get("layers", [])
    for layer_desc in layers:
        layer_digest: str = layer_desc.get("digest", "")
        layer_blob_path = _blob_path(oci_layout_dir, layer_digest)
        if layer_blob_path is None or not layer_blob_path.exists():
            logger.warning("Layer blob not found: %s", layer_digest)
            continue
        # OCI layers are tar.gz (application/vnd.oci.image.layer.v1.tar+gzip)
        _apply_compressed_layer(layer_blob_path, merged)

    files = sorted(merged.values(), key=lambda f: f.path)

    logger.info(
        "OCI layout inventory complete: %d files, image_ref=%r",
        len(files),
        image_ref,
    )

    return ArtefactInventory(
        artefact_type="oci",
        artefact_path=image_ref,
        artefact_sha256="",
        files=files,
        record_entries=[],
        metadata=metadata,
    ), unpack_dir


# ---------------------------------------------------------------------------
# Layer application — whiteout-aware
# ---------------------------------------------------------------------------


def _apply_layer(
    layer_tar_path: Path,
    merged: dict[str, ArtefactFile],
) -> None:
    """Apply a docker-save layer tar to the merged filesystem dict.

    Layer tars inside docker-save archives are uncompressed ``.tar`` files
    (the outer docker-save archive is also ``.tar``; compression happens
    at the outer level or not at all in modern formats).

    Args:
        layer_tar_path: Path to the layer ``.tar`` file.
        merged:         Accumulated filesystem state (path → ArtefactFile).
                        Modified in place.
    """
    logger.debug("Applying layer: %s", layer_tar_path.name)
    try:
        # Layer tars may be uncompressed or gzip compressed
        mode = "r:*"
        with tarfile.open(layer_tar_path, mode) as layer_tf:
            _process_layer_members(layer_tf, merged)
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Could not read layer tar %s: %s", layer_tar_path, exc)


def _apply_compressed_layer(
    blob_path: Path,
    merged: dict[str, ArtefactFile],
) -> None:
    """Apply a gzip-compressed OCI layer blob to the merged filesystem dict.

    OCI layers are ``application/vnd.oci.image.layer.v1.tar+gzip`` blobs.

    Args:
        blob_path: Path to the gzip-compressed layer blob.
        merged:    Accumulated filesystem state (path → ArtefactFile).
                   Modified in place.
    """
    logger.debug("Applying compressed layer blob: %s", blob_path.name)
    try:
        with tarfile.open(blob_path, "r:gz") as layer_tf:
            _process_layer_members(layer_tf, merged)
    except tarfile.TarError as exc:
        logger.warning("Could not read layer blob %s: %s", blob_path, exc)


def _process_layer_members(
    layer_tf: tarfile.TarFile,
    merged: dict[str, ArtefactFile],
) -> None:
    """Process all members from a layer TarFile into *merged*.

    Handles:
    - Regular files: added / overrides lower layers.
    - Whiteout files (``.wh.<name>``): removes ``<name>`` from merged.
    - Opaque whiteout (``.wh..wh..opq``): removes all files in the same
      directory from merged (entire directory contents replaced).
    - Directories and symlinks: traversed but not added to inventory.

    Args:
        layer_tf: Open TarFile for a single layer.
        merged:   Accumulated filesystem state.  Modified in place.
    """
    for member in layer_tf.getmembers():
        raw_name = member.name.lstrip("./")
        if not raw_name:
            continue

        base = os.path.basename(raw_name)
        dir_part = os.path.dirname(raw_name)

        # ----------------------------------------------------------------
        # Opaque whiteout: removes ALL entries under dir_part
        # ----------------------------------------------------------------
        if base == _WH_OPAQUE:
            prefix = dir_part + "/" if dir_part else ""
            to_remove = [k for k in merged if k.startswith(prefix)]
            for key in to_remove:
                del merged[key]
            logger.debug(
                "Opaque whiteout in %r — removed %d files",
                dir_part or "/",
                len(to_remove),
            )
            continue

        # ----------------------------------------------------------------
        # Regular whiteout: removes the named file from merged
        # ----------------------------------------------------------------
        if base.startswith(_WH_PREFIX):
            real_name = base[len(_WH_PREFIX):]
            target = (dir_part + "/" + real_name).lstrip("/")
            if target in merged:
                del merged[target]
                logger.debug("Whiteout removed %r from merged filesystem", target)
            continue

        # ----------------------------------------------------------------
        # Regular file: read content, compute hash
        # ----------------------------------------------------------------
        if not member.isfile():
            continue

        try:
            f = layer_tf.extractfile(member)
            if f is None:
                continue
            content = f.read()
        except (tarfile.TarError, OSError) as exc:
            logger.warning(
                "Could not read layer member %r: %s", raw_name, exc
            )
            continue

        sha256 = hashlib.sha256(content).hexdigest()
        # Normalise path separator
        norm_path = raw_name.replace("\\", "/")
        merged[norm_path] = ArtefactFile(
            path=norm_path,
            size=len(content),
            sha256=sha256,
        )


# ---------------------------------------------------------------------------
# Image config metadata extraction
# ---------------------------------------------------------------------------


def _parse_image_config(
    extract_dir: Path,
    config_file: str,
    image_ref: str,
) -> dict[str, Any]:
    """Parse an image config JSON file from a docker-save layout.

    Args:
        extract_dir: Root directory of the extracted docker-save archive.
        config_file: Relative path to the config JSON (from manifest.json).
        image_ref:   Image reference string (used as fallback metadata).

    Returns:
        Dict with keys: ``image_ref``, ``env``, ``entrypoint``, ``cmd``,
        ``labels``.  Keys are omitted when not present in the config.
    """
    metadata: dict[str, Any] = {"image_ref": image_ref}
    if not config_file:
        return metadata

    config_path = extract_dir / config_file
    if not config_path.exists():
        logger.debug("Config file %s not found in extracted archive", config_file)
        return metadata

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not parse image config %s: %s", config_path, exc)
        return metadata

    return {**metadata, **_extract_config_metadata(data)}


def _extract_config_metadata(config_data: dict[str, Any]) -> dict[str, Any]:
    """Extract security-relevant fields from an OCI/Docker image config dict.

    Args:
        config_data: Parsed JSON dict from image config blob or file.

    Returns:
        Flat dict with ``env``, ``entrypoint``, ``cmd``, and ``labels``
        keys where present.
    """
    result: dict[str, Any] = {}
    # Docker config is under "container_config" or "config" key
    cfg = config_data.get("config") or config_data.get("container_config") or config_data
    if isinstance(cfg, dict):
        if "Env" in cfg:
            result["env"] = cfg["Env"]
        if "Entrypoint" in cfg:
            result["entrypoint"] = cfg["Entrypoint"]
        if "Cmd" in cfg:
            result["cmd"] = cfg["Cmd"]
        if "Labels" in cfg and cfg["Labels"]:
            result["labels"] = cfg["Labels"]
    return result


# ---------------------------------------------------------------------------
# OCI blob helpers
# ---------------------------------------------------------------------------


def _blob_path(oci_layout_dir: Path, digest: str) -> Optional[Path]:
    """Resolve a digest string to a blob file path.

    Digests are in ``<algorithm>:<hex>`` format (e.g. ``sha256:abc123``).

    Args:
        oci_layout_dir: Root of the OCI image layout.
        digest:         Digest string from a manifest.

    Returns:
        Path to the blob file, or ``None`` when the digest is malformed.
    """
    if ":" not in digest:
        logger.warning("Malformed digest: %r", digest)
        return None
    algorithm, hex_val = digest.split(":", 1)
    return oci_layout_dir / "blobs" / algorithm / hex_val


def _read_blob(oci_layout_dir: Path, digest: str) -> Optional[bytes]:
    """Read a blob from an OCI layout directory.

    Args:
        oci_layout_dir: Root of the OCI image layout.
        digest:         Digest string (``<algorithm>:<hex>``).

    Returns:
        Raw bytes of the blob, or ``None`` when not found or unreadable.
    """
    path = _blob_path(oci_layout_dir, digest)
    if path is None or not path.exists():
        logger.warning("Blob not found: %r", digest)
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read blob %r: %s", digest, exc)
        return None


# ---------------------------------------------------------------------------
# Shared helpers
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
    members: list[tarfile.TarInfo],
    archive_name: str,
) -> list[tarfile.TarInfo]:
    """Filter tar members to exclude unsafe paths (path traversal, absolute).

    Args:
        members:      All members from the tarball.
        archive_name: Archive filename (for log messages only).

    Returns:
        Filtered list of safe :class:`tarfile.TarInfo` members.
    """
    safe: list[tarfile.TarInfo] = []
    for member in members:
        name = member.name
        if name.startswith("/") or ".." in name.split("/"):
            logger.warning(
                "[%s] Skipping unsafe archive member: %r", archive_name, name
            )
            continue
        # Reject symlinks and hard links — on Python < 3.12 the fallback
        # extraction path lacks filter="data", so a crafted symlink could
        # escape the extraction directory.
        if member.issym() or member.islnk():
            logger.warning(
                "[%s] Skipping symlink/hard-link archive member: %r",
                archive_name,
                name,
            )
            continue
        safe.append(member)
    return safe


def _empty_inventory(
    artefact_path: str,
    artefact_sha256: str,
    error: str = "",
) -> ArtefactInventory:
    """Return an empty :class:`ArtefactInventory` indicating a parse failure.

    Args:
        artefact_path:   Path or reference for the artefact.
        artefact_sha256: Hash of the artefact (empty string when unknown).
        error:           Human-readable error description stored in
                         ``record_entries``.

    Returns:
        :class:`ArtefactInventory` with zero files and the error recorded.
    """
    return ArtefactInventory(
        artefact_type="oci",
        artefact_path=artefact_path,
        artefact_sha256=artefact_sha256,
        files=[],
        record_entries=[f"ERROR: {error}"] if error else [],
        metadata={},
    )


__all__ = [
    "inspect_oci_image",
    "inspect_oci_archive",
]
