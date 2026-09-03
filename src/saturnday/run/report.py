"""Human-readable failure remediation reports.

Generates a markdown report explaining what failed, why, what Saturnday
tried, and what the owner must do next.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saturnday.run.failure_classifier import classify_failure

logger = logging.getLogger(__name__)

# Remediation advice by failure category
_REMEDIATION: dict[str, str] = {
    "plan_defect": "Amend the plan notes or split the ticket into smaller units.",
    "policy_defect": "Review governance findings and adjust code to comply with policy.",
    "timeout_or_complexity_defect": "Increase the timeout or simplify the ticket scope.",
    "unsupported_environment_defect": "Change the backend or install the required framework.",
    "coder_non_compliance_defect": "Run `rerun-failed` — the coder may succeed on retry.",
    "framework_compatibility_defect": "Check framework version compatibility and update dependencies.",
    "git_state_unavailable_defect": (
        "Saturnday requires a valid git working tree to track changes. "
        "Run `git init` in the repo directory, make a baseline commit, then retry. "
        "This failure does not mean the coder backend failed to write files."
    ),
}


def generate_failure_report(
    ledger_path: str | Path,
    output_dir: str | Path | None = None,
) -> str:
    """Generate a human-readable remediation report from a ledger snapshot.

    Args:
        ledger_path: Path to ``evidence/run/ledger.json`` or the base output dir.
        output_dir: Directory to write ``report.md`` (default: same as ledger dir).

    Returns:
        The report text (also written to ``evidence/run/report.md``).
    """
    ledger_path = Path(ledger_path).resolve()

    # Accept either the ledger file or the base output dir
    if ledger_path.is_dir():
        ledger_file = ledger_path / "evidence" / "run" / "ledger.json"
    else:
        ledger_file = ledger_path

    if not ledger_file.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_file}")

    ledger_data = json.loads(ledger_file.read_text(encoding="utf-8"))
    ticket_statuses = ledger_data.get("ticket_statuses", {})

    lines: list[str] = []
    lines.append("# Saturnday Run Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("")

    # Summary
    total = len(ticket_statuses)
    passed = sum(1 for ts in ticket_statuses.values() if ts.get("disposition") == "PASS")
    failed = sum(1 for ts in ticket_statuses.values() if ts.get("disposition") == "FAIL")
    skipped = sum(1 for ts in ticket_statuses.values() if ts.get("disposition") == "SKIP")
    pending = total - passed - failed - skipped

    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Count |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total  | {total} |")
    lines.append(f"| Passed | {passed} |")
    lines.append(f"| Failed | {failed} |")
    lines.append(f"| Skipped | {skipped} |")
    if pending > 0:
        lines.append(f"| Pending | {pending} |")
    lines.append("")

    # Stop reason
    stop_reason = ledger_data.get("stop_reason", "")
    if stop_reason:
        lines.append(f"**Stop reason:** {stop_reason}")
        lines.append("")

    # DoD status
    dod = ledger_data.get("definition_of_done", [])
    if dod:
        dod_met = (
            ledger_data.get("total_executed", 0) == total
            and ledger_data.get("total_failed", 0) == 0
        )
        status = "MET" if dod_met else "NOT MET"
        lines.append(f"**Definition of done:** {status} (markers: {', '.join(dod)})")
        lines.append("")

    # Failed tickets detail
    failed_tickets = {
        tid: ts for tid, ts in ticket_statuses.items()
        if ts.get("disposition") == "FAIL"
    }

    if failed_tickets:
        lines.append("## Failed Tickets")
        lines.append("")

        for tid, ts in sorted(failed_tickets.items()):
            category = ts.get("failure_category", "")
            reasons = ts.get("reasons", [])

            # Classify for remediation advice
            classification = classify_failure(
                error="; ".join(reasons) if reasons else "Unknown failure",
                changed_files=(),
            )

            lines.append(f"### {tid}")
            lines.append("")
            if category:
                lines.append(f"- **Category:** {category}")
            if reasons:
                lines.append(f"- **Reasons:** {', '.join(reasons)}")
            lines.append(f"- **Classification:** {classification.governance_outcome}")
            lines.append(f"- **What to do:** {_REMEDIATION.get(classification.failure_category, 'Investigate manually.')}")
            lines.append("")

    # Phase statuses
    phase_statuses = ledger_data.get("phase_statuses", [])
    if phase_statuses:
        lines.append("## Phases")
        lines.append("")
        lines.append("| Phase | Status | Tickets |")
        lines.append("|-------|--------|---------|")
        for ps in phase_statuses:
            name = ps.get("name", ps.get("phase_id", "?"))
            status = ps.get("status", "PENDING")
            tids = ", ".join(ps.get("ticket_ids", []))
            lines.append(f"| {name} | {status} | {tids} |")
        lines.append("")

    report = "\n".join(lines)

    # Write report
    if output_dir is None:
        report_dir = ledger_file.parent
    else:
        report_dir = Path(output_dir).resolve() / "evidence" / "run"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    logger.info("Wrote remediation report: %s", report_path)

    return report
