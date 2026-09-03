"""Structured, versioned evidence pack writer for governance and generation modes."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from saturnday.shared.evidence_schema import SCHEMA_VERSION  # noqa: F401 — re-exported


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | WARN | SKIPPED
    severity: str  # error | warning | info
    findings: list[dict] = field(default_factory=list)
    files_checked: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None
    rule_id: str | None = None  # Stable ID (e.g. SEC-001), never reused
    confidence: str = "high"  # high | medium | low
    cwe: str | None = None  # e.g. "CWE-798"
    owasp: str | None = None  # e.g. "A02:2021"


@dataclass
class EvidencePack:
    schema_version: str
    run_id: str
    mode: str  # "check" | "generate"
    repo_path: str
    diff_range: str | None
    saturnday_version: str
    created_utc: str
    ended_utc: str | None = None
    check_results: list[CheckResult] = field(default_factory=list)
    disposition: str = "PASS"  # PASS | WARN | FAIL
    disposition_reasons: list[dict] = field(default_factory=list)
    policy_path: str | None = None
    # generation-mode extras (optional)
    model_used: str | None = None
    plan_file: str | None = None


def compute_disposition(results: list[CheckResult]) -> tuple[str, list[dict]]:
    """FAIL if any error-severity check failed, WARN if any warning-severity failed, else PASS."""
    reasons: list[dict] = []
    has_fail = False
    has_warn = False

    for r in results:
        if r.status == "FAIL" and r.severity == "error":
            has_fail = True
            reasons.append({
                "check": r.name,
                "status": r.status,
                "severity": r.severity,
                "finding_count": len(r.findings),
            })
        elif r.status == "FAIL" and r.severity == "warning":
            has_warn = True
            reasons.append({
                "check": r.name,
                "status": r.status,
                "severity": r.severity,
                "finding_count": len(r.findings),
            })
        elif r.status == "WARN":
            has_warn = True
            reasons.append({
                "check": r.name,
                "status": r.status,
                "severity": r.severity,
                "finding_count": len(r.findings),
            })

    if has_fail:
        return "FAIL", reasons
    if has_warn:
        return "WARN", reasons
    return "PASS", reasons


def generate_summary_md(pack: EvidencePack) -> str:
    """Markdown table of check results + warnings/failures section."""
    lines = [
        f"# Evidence Pack Summary",
        "",
        f"- **Run ID**: {pack.run_id}",
        f"- **Mode**: {pack.mode}",
        f"- **Repo**: {pack.repo_path}",
        f"- **Disposition**: {pack.disposition}",
        f"- **Created**: {pack.created_utc}",
    ]
    if pack.diff_range:
        lines.append(f"- **Diff range**: {pack.diff_range}")
    if pack.policy_path:
        lines.append(f"- **Policy**: {pack.policy_path}")

    lines.extend(["", "## Check Results", ""])
    lines.append("| Check | Status | Severity | Findings | Time (s) |")
    lines.append("|-------|--------|----------|----------|----------|")

    for r in pack.check_results:
        lines.append(
            f"| {r.name} | {r.status} | {r.severity} | {len(r.findings)} | {r.elapsed_s:.2f} |"
        )

    if pack.disposition_reasons:
        lines.extend(["", "## Issues", ""])
        for reason in pack.disposition_reasons:
            # Reasons may be a per-check dict (compute_disposition shape) or a
            # bare marker string (Fix 65 follow-up: ["all_findings_expected"]).
            # Both shapes must render without crashing the summary.
            if isinstance(reason, dict):
                lines.append(
                    f"- **{reason.get('check', '?')}**: {reason.get('status', '?')} "
                    f"(severity={reason.get('severity', '?')}, "
                    f"findings={reason.get('finding_count', 0)})"
                )
            else:
                lines.append(f"- {reason}")

    lines.append("")
    return "\n".join(lines)


def write_evidence_dir(pack: EvidencePack, output_dir: Path) -> Path:
    """Write: run-metadata.json, verification/*.json, final-disposition.json, summary.md"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # run-metadata.json
    metadata = {
        "schema_version": pack.schema_version,
        "run_id": pack.run_id,
        "mode": pack.mode,
        "repo_path": pack.repo_path,
        "diff_range": pack.diff_range,
        "saturnday_version": pack.saturnday_version,
        "created_utc": pack.created_utc,
        "ended_utc": pack.ended_utc,
        "policy_path": pack.policy_path,
        "model_used": pack.model_used,
        "plan_file": pack.plan_file,
    }
    (output_dir / "run-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )

    # verification/*.json — one per check
    verification_dir = output_dir / "verification"
    verification_dir.mkdir(parents=True, exist_ok=True)
    for r in pack.check_results:
        data = asdict(r)
        (verification_dir / f"{r.name}.json").write_text(
            json.dumps(data, indent=2) + "\n"
        )

    # final-disposition.json
    disposition_data = {
        "disposition": pack.disposition,
        "reasons": pack.disposition_reasons,
        "check_count": len(pack.check_results),
        "pass_count": sum(1 for r in pack.check_results if r.status == "PASS"),
        "fail_count": sum(1 for r in pack.check_results if r.status == "FAIL"),
        "warn_count": sum(1 for r in pack.check_results if r.status == "WARN"),
        "skip_count": sum(1 for r in pack.check_results if r.status == "SKIPPED"),
    }
    (output_dir / "final-disposition.json").write_text(
        json.dumps(disposition_data, indent=2) + "\n"
    )

    # summary.md
    (output_dir / "summary.md").write_text(generate_summary_md(pack))

    return output_dir
