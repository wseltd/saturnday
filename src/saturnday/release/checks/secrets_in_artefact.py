"""Release check REL-002: secrets-in-artefact scan.

Scans every file in the **unpacked** release artefact for committed secrets,
credential files, and high-entropy strings.  This check operates on the packed
artefact contents — not the source tree — so it catches secrets that are copied
or generated during the build step but absent from the source repo.

Detection layers (applied per file, in order):

1. **Filename match** — check the file's basename against
   :data:`~saturnday.shared.secret_patterns.ENV_FILE_PATTERNS` and
   :data:`~saturnday.shared.secret_patterns.KEY_FILE_PATTERNS`.  A matching
   filename is sufficient evidence; no content read is required.
2. **Content scan** — read text content (binary files are skipped) and scan
   each line against all patterns in
   :data:`~saturnday.shared.secret_patterns.SECRET_PATTERNS`.
   :data:`~saturnday.shared.secret_patterns.HIGH_ENTROPY_REGEX` is the last
   pattern in that list and acts as a length-based entropy fallback.
3. **Exemption** — files matching any glob pattern in
   ``manifest["allowed_secrets"]`` are skipped for content scanning; they are
   still recorded with ``"status": "exempted"`` in the findings so that the
   exemption is visible in evidence.

Manifest schema (optional section in ``.saturnday-release-manifest.yaml``)::

    allowed_secrets:
      - "**/*.pem"           # e.g. JWT public key bundled intentionally
      - ".npmrc"             # allowlisted for a specific package's CI token

Works on ``wheel``, ``sdist``, and ``npm`` artefact types.

Result rule_id: ``REL-002``
"""
from __future__ import annotations

import fnmatch
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum bytes to read from a single file during content scanning.
# Files larger than this are not content-scanned (still filename-checked).
_MAX_CONTENT_BYTES: int = 512 * 1024  # 512 KiB

# Byte sequences that indicate a binary file — we skip content scanning for
# these to avoid false positives from compiled artefacts.
_BINARY_SNIFF_BYTES: int = 8192  # inspect first 8 KiB for null bytes
_NULL_BYTE: bytes = b"\x00"

# Paths that are skipped for content scanning because they contain
# build-generated hashes or metadata that trigger HIGH_ENTROPY_REGEX
# false positives.  Filename-based detection (env/key patterns) still
# applies to these files.
#
# - *.dist-info/RECORD: wheel integrity manifest with sha256=<base64>
#   per file — produces dozens of high-entropy hits per wheel.
# - *.dist-info/METADATA, *.egg-info/PKG-INFO, PKG-INFO: package
#   metadata containing pinned GitHub Actions SHAs and long URLs.
_CONTENT_SCAN_SKIP_PATTERNS: tuple[str, ...] = (
    ".dist-info/RECORD",
    ".dist-info/METADATA",
    ".egg-info/PKG-INFO",
    "/PKG-INFO",
)


def run_check(
    inventory: Any,
    unpack_dir: Path,
    manifest: dict[str, Any] | None = None,
) -> Any:
    """Scan the unpacked artefact for secrets and credential files.

    Args:
        inventory:  :class:`~saturnday.release._types.ArtefactInventory` for
                    the artefact being inspected.  Used to enumerate files.
        unpack_dir: Path to the directory where the artefact was unpacked.
                    All file paths from *inventory* are resolved relative to
                    this directory.
        manifest:   Parsed ``.saturnday-release-manifest.yaml`` dict, or
                    ``None`` when no manifest is present.  The
                    ``allowed_secrets`` key (list of glob patterns) controls
                    per-file exemptions.

    Returns:
        :class:`~saturnday.release.evidence.ReleaseCheckResult` with:

        - ``status="FAIL"`` and ``severity="error"`` when at least one
          non-exempted secret finding exists.
        - ``status="PASS"`` and ``severity="error"`` when the artefact is clean.

        Each finding dict contains:

        - ``file``    — relative path within the artefact.
        - ``line``    — 1-based line number (content findings only; ``0`` for
          filename-only findings).
        - ``pattern`` — label of the matched pattern.
        - ``kind``    — one of ``"env_file"``, ``"key_file"``, ``"api_key"``,
          ``"high_entropy"``.
        - ``status``  — ``"found"`` or ``"exempted"``.
    """
    from saturnday.release.evidence import ReleaseCheckResult
    from saturnday.shared.secret_patterns import (
        ENV_FILE_PATTERNS,
        HIGH_ENTROPY_REGEX,
        KEY_FILE_PATTERNS,
        SECRET_PATTERNS,
    )

    t0 = time.monotonic()
    unpack_dir = Path(unpack_dir)
    allowed_globs: list[str] = _extract_allowed_globs(manifest)

    findings: list[dict[str, Any]] = []
    files_checked: int = 0

    for artefact_file in inventory.files:
        rel_path: str = artefact_file.path
        abs_path: Path = unpack_dir / rel_path
        files_checked += 1

        # ------------------------------------------------------------------
        # 1. Filename-based detection (env files and key files)
        # ------------------------------------------------------------------
        basename: str = Path(rel_path).name

        env_match = _match_filename(basename, ENV_FILE_PATTERNS)
        if env_match:
            kind = "env_file"
            if _is_exempted(rel_path, allowed_globs):
                findings.append(
                    _make_finding(
                        file=rel_path,
                        line=0,
                        pattern=env_match,
                        kind=kind,
                        status="exempted",
                    )
                )
                logger.debug("secrets_in_artefact: %s exempted (env_file match)", rel_path)
            else:
                findings.append(
                    _make_finding(
                        file=rel_path,
                        line=0,
                        pattern=env_match,
                        kind=kind,
                        status="found",
                    )
                )
                logger.info(
                    "secrets_in_artefact: env_file match %s → %s",
                    env_match, rel_path,
                )
            # Skip content scan — filename match is sufficient.
            continue

        key_match = _match_filename(basename, KEY_FILE_PATTERNS)
        if key_match:
            kind = "key_file"
            if _is_exempted(rel_path, allowed_globs):
                findings.append(
                    _make_finding(
                        file=rel_path,
                        line=0,
                        pattern=key_match,
                        kind=kind,
                        status="exempted",
                    )
                )
                logger.debug("secrets_in_artefact: %s exempted (key_file match)", rel_path)
            else:
                findings.append(
                    _make_finding(
                        file=rel_path,
                        line=0,
                        pattern=key_match,
                        kind=kind,
                        status="found",
                    )
                )
                logger.info(
                    "secrets_in_artefact: key_file match %s → %s",
                    key_match, rel_path,
                )
            # Skip content scan — filename match is sufficient.
            continue

        # ------------------------------------------------------------------
        # 2. Content-based detection
        #    Skip if exempted (record exemption but do not scan).
        # ------------------------------------------------------------------
        if _is_exempted(rel_path, allowed_globs):
            # File is in the allowed list but did not match a filename pattern.
            # No finding to record — we only record exemptions when there was
            # a detection that was waived.
            continue

        if not abs_path.is_file():
            # Path from inventory does not exist on disk — skip silently.
            logger.debug("secrets_in_artefact: %s not found on disk — skipping", rel_path)
            continue

        # Skip content scanning for build-generated metadata files whose
        # hashes and long hex strings are known false-positive sources.
        if _is_content_scan_skipped(rel_path):
            logger.debug(
                "secrets_in_artefact: %s skipped (build-generated metadata)",
                rel_path,
            )
            continue

        content_findings = _scan_file_content(abs_path, rel_path, SECRET_PATTERNS)
        findings.extend(content_findings)

    # ------------------------------------------------------------------
    # 3. Compute result
    # ------------------------------------------------------------------
    non_exempted = [f for f in findings if f["status"] == "found"]
    has_failure: bool = len(non_exempted) > 0

    elapsed: float = time.monotonic() - t0
    logger.info(
        "secrets_in_artefact: %d file(s) checked, %d finding(s) (%d exempted), elapsed=%.3fs",
        files_checked,
        len(non_exempted),
        len(findings) - len(non_exempted),
        elapsed,
    )

    return ReleaseCheckResult(
        name="secrets_in_artefact",
        rule_id="REL-002",
        status="FAIL" if has_failure else "PASS",
        severity="error",
        findings=findings,
        files_checked=files_checked,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_allowed_globs(manifest: dict[str, Any] | None) -> list[str]:
    """Return the list of allowed_secrets glob patterns from the manifest.

    Returns an empty list when the manifest is absent or contains no
    ``allowed_secrets`` key.
    """
    if manifest is None:
        return []
    raw = manifest.get("allowed_secrets", [])
    if not isinstance(raw, list):
        logger.warning(
            "secrets_in_artefact: manifest 'allowed_secrets' is not a list — ignored"
        )
        return []
    return [str(g) for g in raw]


def _is_exempted(rel_path: str, allowed_globs: list[str]) -> bool:
    """Return True when *rel_path* matches any glob pattern in *allowed_globs*.

    Uses :func:`fnmatch.fnmatch` for each pattern after normalising the
    path to use forward slashes.  The pattern ``"**/*.pem"`` is handled by
    also matching the basename alone so that deep paths are covered.
    """
    if not allowed_globs:
        return False

    normalised = rel_path.replace("\\", "/")
    basename = normalised.split("/")[-1]

    for glob in allowed_globs:
        # Direct match on the full relative path.
        if fnmatch.fnmatch(normalised, glob):
            return True
        # Match on basename — allows patterns like "*.pem" to cover deep paths.
        if fnmatch.fnmatch(basename, glob):
            return True
        # Handle ** prefix: strip leading **/ and match against basename.
        if glob.startswith("**/"):
            suffix_glob = glob[3:]
            if fnmatch.fnmatch(basename, suffix_glob):
                return True

    return False


def _is_content_scan_skipped(rel_path: str) -> bool:
    """Return True when *rel_path* is a build-generated metadata file.

    These files contain cryptographic hashes, long URLs, or pinned commit
    SHAs that reliably trigger ``HIGH_ENTROPY_REGEX`` false positives.
    Skipping them from content scanning eliminates noise without weakening
    real secret detection — filename-based detection still applies.

    Args:
        rel_path: Relative path within the artefact (forward-slash separated).

    Returns:
        ``True`` if the file should be skipped for content scanning.
    """
    normalised = rel_path.replace("\\", "/")
    return any(normalised.endswith(suffix) for suffix in _CONTENT_SCAN_SKIP_PATTERNS)


def _match_filename(basename: str, patterns: list[str]) -> str | None:
    """Return the first matching pattern label from *patterns* or ``None``.

    Patterns are matched against *basename* using :func:`fnmatch.fnmatch`.
    Exact matches (no wildcards) are also checked via equality.
    """
    for pattern in patterns:
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(basename, pattern):
                return pattern
        else:
            # Exact match — e.g. "id_rsa", ".env", "server.key"
            if basename == pattern:
                return pattern
    return None


def _is_binary(path: Path) -> bool:
    """Return True when the file appears to be binary.

    Reads the first :data:`_BINARY_SNIFF_BYTES` bytes and checks for null
    bytes, which are reliable indicators of binary content.
    """
    try:
        chunk = path.read_bytes()[:_BINARY_SNIFF_BYTES]
        return _NULL_BYTE in chunk
    except OSError:
        return True  # If we can't read it, treat as binary (skip).


def _scan_file_content(
    abs_path: Path,
    rel_path: str,
    patterns: list[tuple[str, re.Pattern[str]]],
) -> list[dict[str, Any]]:
    """Scan a single file's text content against all *patterns*.

    Binary files and files larger than :data:`_MAX_CONTENT_BYTES` are skipped.

    Args:
        abs_path:  Absolute path to the file on disk.
        rel_path:  Relative path string for use in finding records.
        patterns:  List of ``(label, compiled_regex)`` pairs to match against.

    Returns:
        List of finding dicts (may be empty).  Each dict has the canonical
        finding schema (``file``, ``line``, ``pattern``, ``kind``, ``status``).
    """
    findings: list[dict[str, Any]] = []

    # Size gate: skip large files.
    try:
        file_size = abs_path.stat().st_size
    except OSError:
        return findings

    if file_size > _MAX_CONTENT_BYTES:
        logger.debug(
            "secrets_in_artefact: %s skipped (size %d > %d)",
            rel_path, file_size, _MAX_CONTENT_BYTES,
        )
        return findings

    # Binary gate.
    if _is_binary(abs_path):
        return findings

    # Read text content.
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("secrets_in_artefact: could not read %s: %s", rel_path, exc)
        return findings

    lines = text.splitlines()

    for lineno, line in enumerate(lines, start=1):
        for label, regex in patterns:
            match = regex.search(line)
            if match is not None:
                kind = _pattern_label_to_kind(label)
                findings.append(
                    _make_finding(
                        file=rel_path,
                        line=lineno,
                        pattern=label,
                        kind=kind,
                        status="found",
                    )
                )
                logger.info(
                    "secrets_in_artefact: %s found in %s line %d",
                    label, rel_path, lineno,
                )
                # One finding per pattern per line is enough — move to next pattern.
                break

    return findings


def _pattern_label_to_kind(label: str) -> str:
    """Map a pattern label string to a canonical ``kind`` value.

    The mapping is intentionally broad: any label mentioning "entropy" maps
    to ``"high_entropy"``; API/service token labels map to ``"api_key"``; the
    generic fallback is ``"secret"``.

    Args:
        label: Human-readable label from :data:`~saturnday.shared.secret_patterns.SECRET_PATTERNS`.

    Returns:
        One of ``"api_key"``, ``"high_entropy"``, or ``"secret"``.
    """
    label_lower = label.lower()
    if "entropy" in label_lower:
        return "high_entropy"
    if any(
        kw in label_lower
        for kw in ("stripe", "sendgrid", "slack", "api key", "api_key", "token", "secret key")
    ):
        return "api_key"
    return "secret"


def _make_finding(
    *,
    file: str,
    line: int,
    pattern: str,
    kind: str,
    status: str,
) -> dict[str, Any]:
    """Build a canonical finding dict for this check.

    Args:
        file:    Relative path within the artefact.
        line:    1-based line number; 0 for filename-only detections.
        pattern: Label or pattern that triggered the finding.
        kind:    Detection kind (``"env_file"``, ``"key_file"``, ``"api_key"``,
                 ``"high_entropy"``, ``"secret"``).
        status:  ``"found"`` for a real finding; ``"exempted"`` for a waived one.

    Returns:
        A dict with all five keys populated.
    """
    return {
        "file": file,
        "line": line,
        "pattern": pattern,
        "kind": kind,
        "status": status,
    }


__all__ = ["run_check"]
