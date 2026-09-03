"""Release check REL-005 — Release diff (candidate vs baseline).

Compares the candidate artefact inventory against a baseline inventory and
reports added, removed, and changed files.  Suspicious additions (secrets,
executables, very large files) are reported as WARN findings.  Additions of
files that appear in the manifest's ``deny_always`` list are reported as FAIL.

The check is informational when no suspicious or denied files are found:
it returns PASS with diff statistics in the findings.  If no baseline is
provided, the check returns SKIPPED.

Usage
-----
Invoked automatically by ``saturnday.release.orchestrator`` via dynamic import.
The orchestrator passes ``baseline`` as a keyword argument when a baseline
artefact was specified by the caller.  May also be called directly::

    from saturnday.release.checks.release_diff import run_check

    result = run_check(
        inventory=candidate_inv,
        unpack_dir=unpack_dir,
        manifest=manifest,
        baseline=baseline_inv,
    )

Module interface contract (canonical for all release checks)::

    def run_check(
        inventory: ArtefactInventory,
        unpack_dir: Path,
        manifest: dict | None = None,
        *,
        baseline: ArtefactInventory | None = None,
    ) -> ReleaseCheckResult:

Note: this check accepts an extra *baseline* keyword-only argument beyond the
standard three-parameter contract.  The orchestrator detects this and passes
the baseline when one is available.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from saturnday.release._types import ArtefactInventory
from saturnday.release.diff_engine import compute_release_diff
from saturnday.release.evidence import ReleaseCheckResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Rule identifier — stable, never reused.
RULE_ID: str = "REL-005"

#: Human-readable check name.
CHECK_NAME: str = "release_diff"


# ---------------------------------------------------------------------------
# Public check entry point
# ---------------------------------------------------------------------------


def run_check(
    inventory: ArtefactInventory,
    unpack_dir: Path,
    manifest: dict | None = None,
    *,
    baseline: ArtefactInventory | None = None,
) -> ReleaseCheckResult:
    """Compare candidate artefact against a baseline and report the diff.

    Args:
        inventory:   Inventory of the candidate (new) artefact.
        unpack_dir:  Directory where the candidate artefact was unpacked.
                     Not used directly by this check but present for interface
                     conformance.
        manifest:    Optional dict parsed from ``.saturnday-release-manifest.yaml``.
                     When ``deny_always`` is present, any added file matching
                     those patterns produces a FAIL finding.
        baseline:    Inventory of the baseline (previous/reference) artefact.
                     When ``None`` the check returns SKIPPED.

    Returns:
        :class:`~saturnday.release.evidence.ReleaseCheckResult` with:

        - ``status="SKIPPED"`` when no baseline is provided.
        - ``status="FAIL"`` when any added file matches a ``deny_always``
          pattern from the manifest.
        - ``status="WARN"`` when suspicious additions are detected but none
          are in ``deny_always``.
        - ``status="PASS"`` when the diff is clean or only informational.

        All findings include ``"path"``, ``"kind"`` (``"added"`` / ``"removed"``
        / ``"changed"`` / ``"suspicious"`` / ``"denied"``), and ``"reason"``
        keys.
    """
    t0 = time.monotonic()

    # ------------------------------------------------------------------
    # Guard: no baseline → SKIPPED
    # ------------------------------------------------------------------
    if baseline is None:
        logger.debug("REL-005 release_diff: no baseline provided — SKIPPED")
        elapsed = time.monotonic() - t0
        return ReleaseCheckResult(
            name=CHECK_NAME,
            rule_id=RULE_ID,
            status="SKIPPED",
            severity="info",
            findings=[{"message": "No baseline artefact provided; diff skipped."}],
            files_checked=0,
            elapsed_s=elapsed,
        )

    # ------------------------------------------------------------------
    # Compute the diff
    # ------------------------------------------------------------------
    try:
        diff = compute_release_diff(baseline=baseline, candidate=inventory)
    except ValueError as exc:
        # Cross-type comparison attempted.
        elapsed = time.monotonic() - t0
        logger.warning("REL-005: diff failed — %s", exc)
        return ReleaseCheckResult(
            name=CHECK_NAME,
            rule_id=RULE_ID,
            status="WARN",
            severity="warning",
            findings=[{"message": str(exc)}],
            files_checked=0,
            elapsed_s=elapsed,
        )

    # ------------------------------------------------------------------
    # Extract deny_always patterns from manifest
    # ------------------------------------------------------------------
    deny_always_patterns: list[str] = _extract_deny_always(manifest)

    # ------------------------------------------------------------------
    # Build findings
    # ------------------------------------------------------------------
    findings: list[dict[str, Any]] = []
    has_fail = False
    has_warn = False

    # Suspicious additions → WARN (or FAIL if also in deny_always)
    seen_suspicious_paths: set[str] = set()
    for item in diff.suspicious_additions:
        path = item["path"]
        denied = _is_denied(path, deny_always_patterns)
        kind = "denied" if denied else "suspicious"
        finding: dict[str, Any] = {
            "path": path,
            "kind": kind,
            "reason": item["reason"],
            "size": item["size"],
            "sha256": item["sha256"],
        }
        findings.append(finding)
        seen_suspicious_paths.add(path)
        if denied:
            has_fail = True
            logger.warning("REL-005 FAIL: denied file added — %s (%s)", path, item["reason"])
        else:
            has_warn = True
            logger.warning("REL-005 WARN: suspicious addition — %s (%s)", path, item["reason"])

    # Added files that are denied but not already flagged as suspicious
    for path in diff.added_files:
        if path in seen_suspicious_paths:
            continue
        if _is_denied(path, deny_always_patterns):
            findings.append({
                "path": path,
                "kind": "denied",
                "reason": "file matches deny_always pattern from manifest",
                "size": _file_size(inventory, path),
                "sha256": _file_sha256(inventory, path),
            })
            has_fail = True
            logger.warning("REL-005 FAIL: deny_always file added — %s", path)

    # Informational: added, removed, changed counts
    findings.append({
        "kind": "summary",
        "added_count": len(diff.added_files),
        "removed_count": len(diff.removed_files),
        "changed_count": len(diff.changed_files),
        "size_delta_bytes": diff.size_delta,
        "suspicious_count": len(diff.suspicious_additions),
    })

    # ------------------------------------------------------------------
    # Determine status
    # ------------------------------------------------------------------
    if has_fail:
        status = "FAIL"
        severity = "error"
    elif has_warn:
        status = "WARN"
        severity = "warning"
    else:
        status = "PASS"
        severity = "info"

    elapsed = time.monotonic() - t0
    files_checked = len(inventory.files)

    logger.info(
        "REL-005 release_diff: %s — +%d -%d ~%d suspicious=%d %.3fs",
        status,
        len(diff.added_files),
        len(diff.removed_files),
        len(diff.changed_files),
        len(diff.suspicious_additions),
        elapsed,
    )

    return ReleaseCheckResult(
        name=CHECK_NAME,
        rule_id=RULE_ID,
        status=status,
        severity=severity,
        findings=findings,
        files_checked=files_checked,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_deny_always(manifest: dict | None) -> list[str]:
    """Extract ``deny_always`` glob patterns from the manifest dict.

    Returns an empty list when the manifest is absent or the key is missing.

    Args:
        manifest: Parsed ``.saturnday-release-manifest.yaml`` dict or ``None``.

    Returns:
        List of glob pattern strings.
    """
    if manifest is None:
        return []
    raw = manifest.get("deny_always")
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "REL-005: deny_always in manifest is not a list (%r) — ignored.",
            type(raw).__name__,
        )
        return []
    return [str(p) for p in raw if p]


def _is_denied(file_path: str, deny_patterns: list[str]) -> bool:
    """Return True if *file_path* matches any of the deny_always patterns.

    Both basename and full-path matching are attempted so that patterns like
    ``"*.secret"`` and ``"config/secrets.yaml"`` work as expected.

    Args:
        file_path:      Relative artefact path (forward-slash separated).
        deny_patterns:  List of glob patterns from ``deny_always`` in manifest.

    Returns:
        ``True`` if at least one pattern matches.
    """
    import fnmatch

    if not deny_patterns:
        return False
    basename = file_path.rsplit("/", 1)[-1]
    for pattern in deny_patterns:
        if fnmatch.fnmatch(file_path, pattern):
            return True
        if fnmatch.fnmatch(basename, pattern):
            return True
    return False


def _file_size(inventory: ArtefactInventory, path: str) -> int:
    """Look up the size of *path* in *inventory*.  Returns 0 if not found."""
    for f in inventory.files:
        if f.path == path:
            return f.size
    return 0


def _file_sha256(inventory: ArtefactInventory, path: str) -> str:
    """Look up the SHA-256 of *path* in *inventory*.  Returns '' if not found."""
    for f in inventory.files:
        if f.path == path:
            return f.sha256
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "CHECK_NAME",
    "RULE_ID",
    "run_check",
]
