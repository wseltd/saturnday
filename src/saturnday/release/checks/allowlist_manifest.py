"""Release check REL-004 — Artefact allowlist manifest.

Validates a packed release artefact against a developer-supplied
``.saturnday-release-manifest.yaml`` at the repo root.  The manifest is the
positive assertion: "these files SHOULD be present, these files must NEVER be
present."

Manifest schema
---------------
.. code-block:: yaml

    version: "1.0"

    # Files that MUST exist in the artefact (glob patterns supported)
    required_files:
      - "saturnday/__init__.py"
      - "saturnday-*.dist-info/METADATA"

    # Files that are always blocked regardless of any other rule
    deny_always:
      - "*.env"
      - "generate_licence*"

    # Explicitly allowed files that would otherwise be flagged by other checks
    allowed_source_maps: []
    allowed_secrets: []
    allowed_internal:
      - "tests/**"   # if tests are intentionally shipped

    # Premium-only exception workflow with audit trail
    exceptions:
      - path: "saturnday_premium/_jwt.py"
        reason: "Contains signing secret — private distribution only"
        approver: "onur"
        expires: "2027-01-01"

Check logic
-----------
1. If manifest is None → SKIPPED (info message, not an error).
2. Validate manifest schema: ``version`` field required → FAIL on missing.
3. Check ``deny_always`` patterns — any match is a hard FAIL.
4. Check ``required_files`` patterns — each must match at least one file in the
   inventory (glob matching against all artefact file paths).

Severity
--------
All findings produced by this check are ``"error"`` severity.  A missing
manifest is ``"info"`` severity and returns SKIPPED.

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
import time
from pathlib import Path
from typing import Any

from saturnday.release._types import ArtefactInventory
from saturnday.release.evidence import ReleaseCheckResult

try:
    import yaml as _yaml  # PyYAML — optional dep (graceful fallback)
    _YAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rule identifier — stable, never reused.
RULE_ID: str = "REL-004"

#: Human-readable check name.
CHECK_NAME: str = "allowlist_manifest"

# ---------------------------------------------------------------------------
# Public check entry point
# ---------------------------------------------------------------------------


def run_check(
    inventory: ArtefactInventory,
    unpack_dir: Path,
    manifest: dict | None = None,
) -> ReleaseCheckResult:
    """Validate the artefact inventory against a ``.saturnday-release-manifest.yaml``.

    Args:
        inventory:  Inventory of the unpacked artefact, as produced by
                    ``wheel_inspector``, ``sdist_inspector``, or
                    ``npm_inspector``.
        unpack_dir: Directory on disk where the artefact was unpacked.
                    Not read by this check but required by the canonical
                    module interface so the orchestrator can call all checks
                    uniformly.  The ticket spec (RS-010) named this parameter
                    ``unpacked_dir``; the canonical contract in all other checks
                    and the orchestrator uses ``unpack_dir``.
        manifest:   Optional dict parsed from ``.saturnday-release-manifest.yaml``.
                    When ``None`` the check returns SKIPPED.

    Returns:
        :class:`~saturnday.release.evidence.ReleaseCheckResult` with:

        - ``status="SKIPPED"`` / ``severity="info"`` when no manifest is present.
        - ``status="FAIL"`` / ``severity="error"`` when the manifest is invalid,
          required files are missing, or ``deny_always`` patterns match.
        - ``status="PASS"`` / ``severity="error"`` when the manifest validates and
          all required files are present with no denied files found.

        Every finding dict contains at minimum:
        ``{"kind": str, "file": str, "detail": str}``.
    """
    t0 = time.monotonic()

    # ------------------------------------------------------------------
    # 1. No manifest supplied → SKIPPED
    # ------------------------------------------------------------------
    if manifest is None:
        elapsed = time.monotonic() - t0
        logger.info("REL-004 allowlist_manifest: SKIPPED — No .saturnday-release-manifest.yaml found")
        return ReleaseCheckResult(
            name=CHECK_NAME,
            rule_id=RULE_ID,
            status="SKIPPED",
            severity="info",
            findings=[{
                "kind": "no_manifest",
                "file": ".saturnday-release-manifest.yaml",
                "detail": "No .saturnday-release-manifest.yaml found",
            }],
            files_checked=0,
            elapsed_s=elapsed,
        )

    findings: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 2. Validate manifest schema — version field required
    # ------------------------------------------------------------------
    schema_error = _validate_manifest_schema(manifest)
    if schema_error is not None:
        elapsed = time.monotonic() - t0
        logger.warning("REL-004 allowlist_manifest: FAIL — manifest invalid: %s", schema_error)
        return ReleaseCheckResult(
            name=CHECK_NAME,
            rule_id=RULE_ID,
            status="FAIL",
            severity="error",
            findings=[{
                "kind": "manifest_invalid",
                "file": ".saturnday-release-manifest.yaml",
                "detail": schema_error,
            }],
            files_checked=0,
            elapsed_s=elapsed,
        )

    # Collect all artefact paths once — used for both required and deny checks.
    artefact_paths: list[str] = [f.path for f in inventory.files]
    files_checked = len(artefact_paths)

    # ------------------------------------------------------------------
    # 3. Check deny_always — any match is FAIL regardless of other rules
    # ------------------------------------------------------------------
    deny_patterns: list[str] = _coerce_string_list(manifest.get("deny_always"), "deny_always")
    for path in artefact_paths:
        for pattern in deny_patterns:
            if _glob_matches(path, pattern):
                finding = {
                    "kind": "denied_always",
                    "file": path,
                    "detail": f"Matched deny_always pattern '{pattern}'",
                }
                findings.append(finding)
                logger.debug("REL-004: denied_always match — %s (pattern: %s)", path, pattern)
                break  # one finding per file is enough; avoid duplicates per file

    # ------------------------------------------------------------------
    # 4. Check required_files — each glob must match at least one file
    # ------------------------------------------------------------------
    required_patterns: list[str] = _coerce_string_list(
        manifest.get("required_files"), "required_files"
    )
    for pattern in required_patterns:
        matched = any(_glob_matches(p, pattern) for p in artefact_paths)
        if not matched:
            finding = {
                "kind": "missing_required",
                "file": pattern,
                "detail": f"Required pattern '{pattern}' matched no files in artefact",
            }
            findings.append(finding)
            logger.debug("REL-004: missing_required — pattern '%s' not matched", pattern)

    # ------------------------------------------------------------------
    # Compute overall status
    # ------------------------------------------------------------------
    has_fail = bool(findings)
    overall_status = "FAIL" if has_fail else "PASS"

    elapsed = time.monotonic() - t0
    logger.info(
        "REL-004 allowlist_manifest: %s — %d finding(s), %d file(s) checked, %.3fs",
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


def _validate_manifest_schema(manifest: dict[str, Any]) -> str | None:
    """Validate the top-level manifest dict.

    Currently enforces:
    - ``manifest`` must be a dict.
    - ``version`` key must be present and non-empty.

    Args:
        manifest: Parsed YAML document as a Python dict.

    Returns:
        An error message string if validation fails, or ``None`` if the
        manifest is structurally valid.
    """
    if not isinstance(manifest, dict):
        return f"Manifest must be a YAML mapping, got {type(manifest).__name__}"
    version = manifest.get("version")
    if version is None:
        return "Manifest missing required field 'version'"
    if not str(version).strip():
        return "Manifest 'version' field is empty"
    return None


def _coerce_string_list(value: Any, field_name: str) -> list[str]:
    """Extract a list of non-empty strings from a manifest field value.

    Handles ``None`` (returns ``[]``), plain lists, and single strings.
    Non-string entries are coerced via ``str()``.  Invalid types are logged
    and return an empty list.

    Args:
        value:      Raw value from the manifest dict.
        field_name: Field name used in log messages only.

    Returns:
        A list of non-empty strings.
    """
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = [str(item) for item in value if item is not None and str(item).strip()]
        return result
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    logger.warning(
        "REL-004: manifest field '%s' has unexpected type %r — ignored.",
        field_name, type(value).__name__,
    )
    return []


def _glob_matches(path: str, pattern: str) -> bool:
    """Return True if *path* matches *pattern* using fnmatch glob semantics.

    Matching is performed against:
    1. The full relative path (e.g. ``"saturnday/cli.py"`` vs ``"saturnday/*.py"``).
    2. The basename only (e.g. ``"cli.py"`` vs ``"*.py"``).

    This mirrors the convention used by :mod:`saturnday.release.checks.source_map_blocker`
    so that manifest authors can use the same intuitive glob syntax for both.

    Args:
        path:    Relative artefact path (forward-slash separated).
        pattern: Glob pattern from ``required_files`` or ``deny_always``.

    Returns:
        ``True`` if either the full path or the basename matches the pattern.
    """
    if fnmatch.fnmatch(path, pattern):
        return True
    basename = path.rsplit("/", 1)[-1]
    if fnmatch.fnmatch(basename, pattern):
        return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CHECK_NAME",
    "RULE_ID",
    "run_check",
]
