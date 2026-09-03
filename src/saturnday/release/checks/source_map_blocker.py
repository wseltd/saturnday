"""Release check REL-001 — Source-map blocker.

Detects source maps in packed release artefacts and blocks publication
by default.  Source maps expose original source code and internal file
structure to anyone who downloads the artefact; they must never appear
in production release artefacts unless explicitly exempted.

Detection targets
-----------------
1. Files with ``.map`` extension (e.g. ``bundle.js.map``, ``styles.css.map``).
2. Text files containing a ``//# sourceMappingURL=`` or
   ``/*# sourceMappingURL=`` directive.
3. Inline source maps: ``sourceMappingURL=data:application/json;base64,``
   embedded directly in a file's content.
4. Files containing the ``"sourcesContent"`` JSON key, which indicates that
   original source text is embedded in the map.
5. Pre-compiled source files with extensions ``.ts``, ``.tsx``, ``.coffee``,
   ``.elm``, ``.dart``, ``.purs`` present unexpectedly in the artefact.

Policy
------
By default all detections produce an ``"error"``-severity FAIL.

If the caller supplies a manifest dict with an ``allowed_source_maps`` key
whose value is a list of glob patterns, any file whose path matches one of
those patterns is marked ``status: "exempted"`` in findings and does **not**
contribute to a FAIL.  An artefact whose only findings are exempted files
still gets a PASS result.

Usage
-----
Invoked automatically by ``saturnday.release.orchestrator`` via dynamic import.
The function may also be called directly for testing::

    from pathlib import Path
    from saturnday.release.checks.source_map_blocker import run_check
    result = run_check(inventory, unpack_dir, manifest)

Module interface contract (canonical for all release checks)::

    def run_check(
        inventory: ArtefactInventory,
        unpack_dir: Path,
        manifest: dict | None = None,
    ) -> ReleaseCheckResult:
"""
from __future__ import annotations

import fnmatch
import logging
import re
import time
from pathlib import Path
from typing import Any

from saturnday.release._types import ArtefactInventory
from saturnday.release.evidence import ReleaseCheckResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rule identifier — stable, never reused.
RULE_ID: str = "REL-001"

#: Human-readable check name.
CHECK_NAME: str = "source_map_blocker"

#: File extensions that are unconditionally treated as source map files.
_MAP_EXTENSIONS: frozenset[str] = frozenset({".map"})

#: Pre-compiled source extensions that must not appear in a release artefact.
_PRECOMPILED_EXTENSIONS: frozenset[str] = frozenset({
    ".ts",
    ".tsx",
    ".coffee",
    ".elm",
    ".dart",
    ".purs",
})

#: Regex for ``sourceMappingURL`` directives in text files (both JS // and CSS /* forms).
_SOURCE_MAPPING_URL_RE: re.Pattern[bytes] = re.compile(
    rb"sourceMappingURL\s*=\s*\S+",
    re.IGNORECASE,
)

#: Regex for inline data: URI source maps (base64-encoded).
_INLINE_SOURCE_MAP_RE: re.Pattern[bytes] = re.compile(
    rb"sourceMappingURL\s*=\s*data:application/json",
    re.IGNORECASE,
)

#: Bytes marker for embedded ``sourcesContent`` JSON key.
_SOURCES_CONTENT_BYTES: bytes = b'"sourcesContent"'

#: File extensions eligible for content-pattern scanning.  Source-map
#: directives (``sourceMappingURL=``, ``sourcesContent``) only appear in
#: JavaScript, CSS, HTML, and JSON artefacts.  Scanning other file types
#: (e.g. ``.py``) produces false positives when the file contains the
#: detection patterns as string literals (see RS-020 self-scan findings).
_CONTENT_SCAN_EXTENSIONS: frozenset[str] = frozenset({
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".css", ".scss", ".less",
    ".html", ".htm",
    ".map",
    ".json",
})

#: Maximum file size to scan for content patterns (avoid reading huge binaries).
_MAX_SCAN_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MiB

# ---------------------------------------------------------------------------
# Public check entry point
# ---------------------------------------------------------------------------


def run_check(
    inventory: ArtefactInventory,
    unpack_dir: Path,
    manifest: dict | None = None,
) -> ReleaseCheckResult:
    """Detect source maps in a packed release artefact.

    Args:
        inventory:   Inventory of the unpacked artefact, as produced by
                     ``wheel_inspector``, ``sdist_inspector``, or
                     ``npm_inspector``.
        unpack_dir:  Directory on disk where the artefact was unpacked.
                     Used to read file contents for pattern scanning.
        manifest:    Optional dict parsed from ``.saturnday-release-manifest.yaml``.
                     When ``allowed_source_maps`` is present, those glob patterns
                     exempt matching files from blocking.

    Returns:
        :class:`~saturnday.release.evidence.ReleaseCheckResult` with:
        - ``status="FAIL"`` / ``severity="error"`` if any non-exempted source
          map or pre-compiled source is found.
        - ``status="PASS"`` / ``severity="error"`` if all findings are exempted
          or no findings were produced.
        Every finding dict contains at minimum:
        ``{"path": str, "reason": str, "status": "blocked" | "exempted"}``.
    """
    t0 = time.monotonic()
    allowed_patterns: list[str] = _extract_allowed_patterns(manifest)
    findings: list[dict[str, Any]] = []
    files_checked: int = 0

    for artefact_file in inventory.files:
        files_checked += 1
        file_path = artefact_file.path

        reasons = _detect_reasons(file_path, unpack_dir)
        if not reasons:
            continue

        exempted = _is_exempted(file_path, allowed_patterns)
        status_label = "exempted" if exempted else "blocked"

        for reason in reasons:
            findings.append({
                "path": file_path,
                "reason": reason,
                "status": status_label,
            })
            logger.debug(
                "REL-001 %s: %s — %s", status_label, file_path, reason,
            )

    # FAIL only if there is at least one blocked (non-exempted) finding.
    has_blocked = any(f["status"] == "blocked" for f in findings)
    overall_status = "FAIL" if has_blocked else "PASS"

    elapsed = time.monotonic() - t0
    logger.info(
        "REL-001 source_map_blocker: %s — %d finding(s), %d file(s) checked, %.3fs",
        overall_status, len(findings), files_checked, elapsed,
    )

    return ReleaseCheckResult(
        name=CHECK_NAME,
        rule_id=RULE_ID,
        status=overall_status,
        severity="error",
        findings=findings,
        files_checked=files_checked,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_allowed_patterns(manifest: dict | None) -> list[str]:
    """Extract ``allowed_source_maps`` glob patterns from the manifest dict.

    Returns an empty list when the manifest is absent or the key is missing.
    Non-list values are logged and ignored.

    Args:
        manifest: Parsed ``.saturnday-release-manifest.yaml`` dict or ``None``.

    Returns:
        List of glob pattern strings.
    """
    if manifest is None:
        return []
    raw = manifest.get("allowed_source_maps")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "REL-001: allowed_source_maps in manifest is not a list (%r) — ignored.",
            type(raw).__name__,
        )
        return []
    patterns: list[str] = [str(p) for p in raw if p]
    logger.debug("REL-001: allowed_source_maps patterns: %s", patterns)
    return patterns


def _is_exempted(file_path: str, allowed_patterns: list[str]) -> bool:
    """Return True if *file_path* matches any of the exemption glob patterns.

    Both ``fnmatch.fnmatch`` (basename only) and a full-path match are tested
    so that patterns like ``"*.map"`` and ``"dist/bundle.js.map"`` both work.

    Args:
        file_path:        Relative path within the artefact (forward-slash separated).
        allowed_patterns: List of glob patterns from the manifest.

    Returns:
        ``True`` if at least one pattern matches.
    """
    if not allowed_patterns:
        return False
    basename = file_path.rsplit("/", 1)[-1]
    for pattern in allowed_patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


def _detect_reasons(file_path: str, unpack_dir: Path) -> list[str]:
    """Return a list of human-readable detection reasons for *file_path*.

    An empty list means the file is clean.  Multiple reasons may be returned
    when a single file triggers more than one detection rule.

    Detection order:
    1. Extension-based: ``.map`` files.
    2. Extension-based: pre-compiled source files.
    3. Content-based: ``sourceMappingURL=`` directive.
    4. Content-based: inline data: URI source map.
    5. Content-based: ``"sourcesContent"`` JSON key.

    Content scanning is skipped for files whose extension is not in
    ``_CONTENT_SCAN_EXTENSIONS`` (source-map directives only appear in
    JS/CSS/HTML/JSON artefacts), for binary files (null bytes present in
    the first 8 KiB), and for files exceeding ``_MAX_SCAN_SIZE_BYTES``.

    Args:
        file_path:  Relative artefact path (forward-slash separated).
        unpack_dir: Directory where the artefact was unpacked on disk.

    Returns:
        List of reason strings; empty if no issues detected.
    """
    reasons: list[str] = []

    # --- Extension: .map ---
    lower = file_path.lower()
    if any(lower.endswith(ext) for ext in _MAP_EXTENSIONS):
        reasons.append(".map file detected")
        # Still continue — file could also contain sourcesContent or inline map.

    # --- Extension: pre-compiled source ---
    suffix = Path(file_path).suffix.lower()
    if suffix in _PRECOMPILED_EXTENSIONS:
        reasons.append(f"pre-compiled source file detected ({suffix})")

    # --- Content scanning (extension-gated) ---
    # Only scan file types that can legitimately contain source-map
    # directives.  This avoids false positives when detection-pattern
    # literals appear in other languages (e.g. Python check definitions).
    if suffix in _CONTENT_SCAN_EXTENSIONS:
        full_path = unpack_dir / file_path
        content = _read_file_bytes(full_path)
    else:
        content = None
    if content is not None:
        if _INLINE_SOURCE_MAP_RE.search(content):
            reasons.append("inline data: URI source map (sourceMappingURL=data:...)")
        elif _SOURCE_MAPPING_URL_RE.search(content):
            # Only add the generic directive reason if we haven't already added inline.
            reasons.append("sourceMappingURL= directive found")

        if _SOURCES_CONTENT_BYTES in content:
            reasons.append('"sourcesContent" key found — embedded original source')

    return reasons


def _read_file_bytes(path: Path) -> bytes | None:
    """Read *path* and return its contents, or ``None`` if it should be skipped.

    Skips:
    - Files that do not exist or cannot be read.
    - Files larger than ``_MAX_SCAN_SIZE_BYTES``.
    - Binary files (null byte present in the first 8 KiB probe).

    Args:
        path: Absolute or relative filesystem path to the file.

    Returns:
        File bytes on success; ``None`` if the file should be skipped.
    """
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > _MAX_SCAN_SIZE_BYTES:
            logger.debug("REL-001: skipping large file (%d bytes): %s", size, path)
            return None
        data = path.read_bytes()
        # Binary probe: if first 8 KiB contains a null byte, skip content scan.
        if b"\x00" in data[:8192]:
            return None
        return data
    except OSError as exc:
        logger.debug("REL-001: could not read %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CHECK_NAME",
    "RULE_ID",
    "run_check",
]
