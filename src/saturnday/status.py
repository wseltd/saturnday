"""Repo governance status — read-only introspection for ``saturnday status``.

Gathers governance state from existing repo-local evidence, policy,
and baseline files without running any scans or mutating state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def gather_status(repo_path: Path) -> dict[str, Any]:
    """Gather governance status from repo-local state.

    Reads only — does not run scans, create files, or mutate state.

    Args:
        repo_path: Absolute path to the repository root.

    Returns:
        Dict with status fields.  Missing information is represented
        as ``None``, never guessed.
    """
    repo_path = repo_path.resolve()
    status: dict[str, Any] = {
        "repo_path": str(repo_path),
        "policy_path": None,
        "baseline_path": None,
        "latest_governance": None,
        "latest_run": None,
        "premium_available": {
            "approval_workflow": False,
            "release_governance": False,
        },
    }

    # Policy file
    policy = repo_path / ".saturnday-policy.yaml"
    if policy.is_file():
        status["policy_path"] = str(policy)

    # Baseline file
    baseline = repo_path / ".saturnday-baseline.json"
    if baseline.is_file():
        status["baseline_path"] = str(baseline)

    # Latest governance evidence
    evidence_dir = repo_path / ".saturnday" / "evidence"
    if evidence_dir.is_dir():
        check_dirs = sorted(
            [d for d in evidence_dir.iterdir()
             if d.is_dir() and (d.name.startswith("check_") or d.name.startswith("review_"))],
            reverse=True,
        )
        if check_dirs:
            latest = check_dirs[0]
            gov_info: dict[str, Any] = {
                "evidence_dir": str(latest),
                "run_id": latest.name,
                "disposition": None,
                "checks_run": None,
                "total_findings": None,
                "report_path": None,
            }
            # Read disposition
            disp_file = latest / "final-disposition.json"
            if disp_file.is_file():
                try:
                    disp_data = json.loads(disp_file.read_text(encoding="utf-8"))
                    gov_info["disposition"] = disp_data.get("disposition")
                    # Use the authoritative check_count field if present
                    if "check_count" in disp_data:
                        gov_info["checks_run"] = disp_data["check_count"]
                    reasons = disp_data.get("reasons", [])
                    gov_info["total_findings"] = sum(
                        r.get("finding_count", 0) for r in reasons
                    )
                except Exception:
                    pass
            # Report path
            report = latest / "governance-report.md"
            if report.is_file():
                gov_info["report_path"] = str(report)
            status["latest_governance"] = gov_info

    # Latest run evidence
    run_dir = repo_path / ".saturnday" / "run"
    if run_dir.is_dir():
        run_dirs = sorted(
            [d for d in run_dir.iterdir() if d.is_dir() and d.name.startswith("run_")],
            reverse=True,
        )
        if run_dirs:
            latest_run = run_dirs[0]
            run_info: dict[str, Any] = {
                "evidence_dir": str(latest_run),
                "run_id": latest_run.name,
                "report_path": None,
            }
            report = latest_run / "run-report.md"
            if not report.is_file():
                # Some runs write run-report.md at the run_dir parent level
                alt_report = run_dir / "run-report.md"
                if alt_report.is_file():
                    report = alt_report
            if report.is_file():
                run_info["report_path"] = str(report)
            # Check for accepted DoD artifact
            dod_artifact = latest_run / "accepted-dod.json"
            if dod_artifact.is_file():
                run_info["accepted_dod_path"] = str(dod_artifact)
            status["latest_run"] = run_info

    # Approval summary
    approvals_path = repo_path / ".saturnday" / "document" / "approvals.json"
    if approvals_path.is_file():
        try:
            approvals_data = json.loads(approvals_path.read_text(encoding="utf-8"))
            if isinstance(approvals_data, list):
                doc_ids = {r.get("document_id") for r in approvals_data if isinstance(r, dict)}
                status["approval_summary"] = {
                    "total_records": len(approvals_data),
                    "document_count": len(doc_ids),
                }
        except Exception:
            pass

    # Release exception summary — latest release bundle only
    evidence_dir = repo_path / ".saturnday" / "evidence"
    if evidence_dir.is_dir():
        release_parent = evidence_dir / "release"
        if release_parent.is_dir():
            release_dirs = sorted(
                [rd for rd in release_parent.iterdir()
                 if rd.is_dir() and rd.name.startswith("release_")],
                reverse=True,
            )
            if release_dirs:
                from saturnday.release_exception_list import count_exceptions
                from saturnday.release_signoff_list import count_signoffs
                latest_release = release_dirs[0]
                exc_counts = count_exceptions(latest_release)
                if exc_counts["total"] > 0:
                    status["release_exception_summary"] = {
                        "total": exc_counts["total"],
                        "active": exc_counts["active"],
                        "expired": exc_counts["expired"],
                        "release_bundle": latest_release.name,
                    }
                sig_counts = count_signoffs(latest_release)
                if sig_counts["total"] > 0:
                    status["release_signoff_summary"] = {
                        "total": sig_counts["total"],
                        "distinct_approvers": sig_counts["distinct_approvers"],
                        "release_bundle": latest_release.name,
                    }

    # Premium capability detection
    try:
        from saturnday import capability_registry
        status["premium_available"]["approval_workflow"] = (
            capability_registry.is_available("doc_post_global")
        )
        status["premium_available"]["release_governance"] = (
            capability_registry.is_available("release_governance")
        )
    except Exception:
        pass

    # Approval and exception state
    if status["premium_available"]["approval_workflow"]:
        status["approval_state"] = "available"
    else:
        status["approval_state"] = "not enabled in this environment"

    if status["premium_available"]["release_governance"]:
        status["exception_state"] = "available"
    else:
        status["exception_state"] = "not enabled in this environment"

    return status


def format_status(status: dict[str, Any]) -> str:
    """Format status dict as a compact human-readable string.

    Args:
        status: Dict from ``gather_status``.

    Returns:
        Multi-line string ready for printing.
    """
    lines: list[str] = []
    lines.append(f"  Repo: {status['repo_path']}")
    lines.append("")

    # Policy
    if status["policy_path"]:
        lines.append(f"  Policy:   {status['policy_path']}")
    else:
        lines.append("  Policy:   none (no .saturnday-policy.yaml)")

    # Baseline
    if status["baseline_path"]:
        lines.append(f"  Baseline: {status['baseline_path']}")
    else:
        lines.append("  Baseline: none")

    lines.append("")

    # Latest governance
    gov = status.get("latest_governance")
    if gov:
        disp = gov.get("disposition", "unknown")
        checks = gov.get("checks_run")
        findings = gov.get("total_findings")
        lines.append(f"  Latest governance scan: {gov['run_id']}")
        disp_str = disp or "unknown"
        parts = [f"Disposition: {disp_str}"]
        if checks is not None:
            parts.append(f"Checks: {checks}")
        if findings is not None:
            parts.append(f"Findings: {findings}")
        lines.append(f"    {' | '.join(parts)}")
        if gov.get("report_path"):
            lines.append(f"    Report: {gov['report_path']}")
    else:
        lines.append("  Latest governance scan: none (run saturnday governance --repo . --full)")

    lines.append("")

    # Latest run
    run = status.get("latest_run")
    if run:
        lines.append(f"  Latest run: {run['run_id']}")
        if run.get("report_path"):
            lines.append(f"    Report: {run['report_path']}")
        if run.get("accepted_dod_path"):
            lines.append(f"    Accepted DoD: {run['accepted_dod_path']}")
    else:
        lines.append("  Latest run: none")

    lines.append("")

    # Release signoff summary (latest release bundle only)
    sig_summary = status.get("release_signoff_summary")
    if sig_summary:
        bundle = sig_summary.get("release_bundle", "")
        label = f" (latest: {bundle})" if bundle else ""
        lines.append(f"  Release signoffs{label}: {sig_summary['total']} ({sig_summary['distinct_approvers']} distinct approver(s))")
    else:
        lines.append("  Release signoffs: none")
    lines.append("")

    # Release exception summary (latest release bundle only)
    exc_summary = status.get("release_exception_summary")
    if exc_summary:
        bundle = exc_summary.get("release_bundle", "")
        label = f" (latest: {bundle})" if bundle else ""
        lines.append(f"  Release exceptions{label}: {exc_summary['total']} ({exc_summary['active']} active, {exc_summary['expired']} expired)")
    else:
        lines.append("  Release exceptions: none")
    lines.append("")

    # Approval summary
    approval_summary = status.get("approval_summary")
    if approval_summary:
        lines.append(f"  Approvals: {approval_summary['total_records']} record(s) across {approval_summary['document_count']} document(s)")
        lines.append("    Run: saturnday approve-list --repo . for details")
    else:
        lines.append("  Approvals: none")
    lines.append("")

    # Extended capabilities
    premium = status.get("premium_available", {})
    approval = premium.get("approval_workflow", False)
    release_gov = premium.get("release_governance", False)
    if approval or release_gov:
        lines.append("  Extended capabilities:")
        if approval:
            lines.append("    ✓ Document approval workflow")
        if release_gov:
            lines.append("    ✓ Release governance (approve / exception)")
    else:
        lines.append("  Extended capabilities: not enabled in this environment")

    lines.append("")

    # Approval and exception state
    approval_state = status.get("approval_state", "unknown")
    exception_state = status.get("exception_state", "unknown")
    lines.append(f"  Approval state:  {approval_state}")
    lines.append(f"  Exception state: {exception_state}")

    return "\n".join(lines)
