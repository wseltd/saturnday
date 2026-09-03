"""Read-only release signoff listing and completeness checking.

Reads signoff JSON files directly from the filesystem without
requiring entitlement. Used by ``release-approve list``,
``release-approve check``, ``evidence list``, and ``status``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SIGNOFF_SUBDIR = "release/signoffs"

# Default minimum approver threshold — matches the premium module default
DEFAULT_MINIMUM_APPROVERS = 2


def load_signoff_records(evidence_dir: Path) -> list[dict[str, Any]]:
    """Load all signoff records from a release evidence directory.

    Reads every ``*.json`` file under ``<evidence_dir>/release/signoffs/``.

    Args:
        evidence_dir: Root evidence directory for a release run.

    Returns:
        List of raw signoff dicts, possibly empty.
    """
    signoff_dir = evidence_dir / _SIGNOFF_SUBDIR
    if not signoff_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(signoff_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "signoff_id" in data:
                records.append(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping malformed signoff file %s: %s", path, exc)

    return records


def count_signoffs(evidence_dir: Path) -> dict[str, int]:
    """Count signoffs and distinct approvers in a release evidence dir.

    Returns dict with keys: total, distinct_approvers.
    """
    records = load_signoff_records(evidence_dir)
    distinct = {r.get("approver", "") for r in records if r.get("approver")}
    return {
        "total": len(records),
        "distinct_approvers": len(distinct),
    }


def check_signoff_completeness(
    evidence_dir: Path,
    minimum_approvers: int = DEFAULT_MINIMUM_APPROVERS,
) -> tuple[bool, str]:
    """Check whether the minimum approver threshold is satisfied.

    Reads signoff files directly — no entitlement required.
    Uses the same semantics as the premium check_signoff_requirement:
    count distinct approvers against a minimum threshold.

    Args:
        evidence_dir: Root evidence directory for a release run.
        minimum_approvers: Minimum distinct approvers required.

    Returns:
        (satisfied, reason) tuple.
    """
    records = load_signoff_records(evidence_dir)
    distinct = {r.get("approver", "") for r in records if r.get("approver")}
    count = len(distinct)

    if minimum_approvers < 1:
        return True, "No minimum approver requirement configured"

    if count >= minimum_approvers:
        return True, (
            f"Signoff requirement met: {count} distinct approver(s) "
            f"(minimum {minimum_approvers})"
        )

    return False, (
        f"Signoff requirement NOT met: {count} distinct approver(s) found, "
        f"need {minimum_approvers}"
    )


def format_signoff_list(records: list[dict[str, Any]]) -> str:
    """Format signoff records as human-readable output."""
    if not records:
        return "  No release signoffs found in this evidence bundle."

    distinct = {r.get("approver", "") for r in records if r.get("approver")}
    lines: list[str] = []
    lines.append(f"  Release signoffs: {len(records)} total ({len(distinct)} distinct approver(s))")
    lines.append("")

    for r in records:
        sid = r.get("signoff_id", "?")[:12]
        approver = r.get("approver", "?")
        approved_at = r.get("approved_at", "")
        sha = r.get("artefact_sha256", "")
        notes = r.get("notes", "")

        lines.append(f"    ✓ {sid}")
        lines.append(f"      Approver:    {approver}")
        if approved_at:
            lines.append(f"      Approved at: {approved_at}")
        if sha:
            lines.append(f"      Artefact:    {sha[:16]}...")
        if notes:
            lines.append(f"      Notes:       {notes[:100]}")
        lines.append("")

    return "\n".join(lines)
