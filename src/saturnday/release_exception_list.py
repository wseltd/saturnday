"""Read-only release exception listing and counting.

Reads exception JSON files directly from the filesystem without
requiring entitlement. Used by ``release-exception list``,
``evidence list``, and ``status``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_EXCEPTION_SUBDIR = "release/exceptions"


def load_exception_records(evidence_dir: Path) -> list[dict[str, Any]]:
    """Load all exception records from a release evidence directory.

    Reads every ``*.json`` file under ``<evidence_dir>/release/exceptions/``.
    Does not filter by expiry — that's the caller's choice.

    Args:
        evidence_dir: Root evidence directory for a release run.

    Returns:
        List of raw exception dicts, possibly empty.
    """
    exc_dir = evidence_dir / _EXCEPTION_SUBDIR
    if not exc_dir.is_dir():
        return []

    records: list[dict[str, Any]] = []
    for path in sorted(exc_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "exception_id" in data:
                records.append(data)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping malformed exception file %s: %s", path, exc)

    return records


def is_expired(record: dict[str, Any]) -> bool:
    """Check whether an exception record has expired."""
    expiry = record.get("expiry", "")
    if not expiry:
        return False
    try:
        expiry_dt = datetime.fromisoformat(expiry)
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        return expiry_dt <= datetime.now(tz=timezone.utc)
    except ValueError:
        return False


def count_exceptions(evidence_dir: Path) -> dict[str, int]:
    """Count active and expired exceptions in a release evidence dir.

    Returns dict with keys: total, active, expired.
    """
    records = load_exception_records(evidence_dir)
    expired_count = sum(1 for r in records if is_expired(r))
    return {
        "total": len(records),
        "active": len(records) - expired_count,
        "expired": expired_count,
    }


def format_exception_list(records: list[dict[str, Any]]) -> str:
    """Format exception records as human-readable output."""
    if not records:
        return "  No release exceptions found in this evidence bundle."

    lines: list[str] = []
    active = [r for r in records if not is_expired(r)]
    expired = [r for r in records if is_expired(r)]

    lines.append(f"  Release exceptions: {len(records)} total ({len(active)} active, {len(expired)} expired)")
    lines.append("")

    for r in records:
        exp = is_expired(r)
        status_str = "EXPIRED" if exp else "ACTIVE"
        eid = r.get("exception_id", "?")[:12]
        rules = ", ".join(r.get("rule_ids", []))
        approver = r.get("approver", "?")
        reason = r.get("reason", "")
        created = r.get("created_at", "")
        expiry = r.get("expiry", "")
        file_pats = r.get("file_patterns", [])

        icon = "○" if exp else "✓"
        lines.append(f"    {icon} {eid}  [{status_str}]")
        lines.append(f"      Rules:    {rules}")
        lines.append(f"      Approver: {approver}")
        if reason:
            lines.append(f"      Reason:   {reason[:100]}")
        if created:
            lines.append(f"      Created:  {created}")
        if expiry:
            lines.append(f"      Expiry:   {expiry}")
        if file_pats:
            lines.append(f"      Files:    {', '.join(file_pats)}")
        lines.append("")

    return "\n".join(lines)
