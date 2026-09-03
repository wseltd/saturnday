"""Per-ticket evidence recording and run summary output.

Writes a JSON evidence file per ticket and a final run summary, providing
an audit trail of what each coder attempt produced and how governance
ruled on it.

Directory structure produced:
    <output_dir>/
      run-metadata.json          # run-level info (config, plan, start time)
      run-summary.json           # final run outcome
      analytics.json             # computed run analytics
      tickets/
        <ticket_id>/
          attempt_1.json
          attempt_2.json
      phases/
        phase-<n>.json           # per-phase ticket outcomes
      evidence/
        run/
          ledger.json
          report.md
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saturnday._types import CoderConfig, RunResult, TicketResult
from saturnday.capability_registry import is_available
from saturnday.shared.evidence_schema import (
    SCHEMA_VERSION,  # noqa: F401 — shared schema constant
    build_capability_state,
    build_skipped_stages,
)

if TYPE_CHECKING:
    from saturnday.run.run_ledger import RunLedger

logger = logging.getLogger(__name__)

_SATURNDAY_VERSION = "0.1.0"


@dataclass
class TicketEvidence:
    """Audit trail for a single ticket execution.

    Attributes:
        ticket_id: The ticket identifier.
        attempt: Attempt number (1-based).
        coder_response: Raw text from the coder (truncated for storage).
        changed_files: Files written by the coder.
        governance_disposition: ``PASS`` or ``FAIL`` from governance.
        governance_findings: Governance check findings.
        governance_evidence_path: Filesystem path to the raw governance
            evidence JSON produced by ``run_governance_check``. Empty string
            when governance did not run or produced no path (e.g. error paths).
        error: Error message if the attempt failed.
        timestamp: UTC timestamp of the attempt.
    """

    ticket_id: str
    attempt: int
    coder_response: str = ""
    changed_files: list[str] = field(default_factory=list)
    governance_disposition: str = ""
    governance_findings: list[dict[str, Any]] = field(default_factory=list)
    governance_evidence_path: str = ""
    error: str = ""
    timestamp: str = ""
    # Prompt-budget instrumentation (measurement only, no behaviour change)
    prompt_chars: int = 0
    prompt_budget_warning: bool = False
    prompt_budget_soft_threshold: int = 0
    prompt_budget_hard_threshold: int = 0
    context_compaction_applied: bool = False
    prompt_split_exempt: bool = False
    prompt_split_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_ticket_evidence(evidence: TicketEvidence, output_dir: Path) -> Path:
    """Write a ticket evidence JSON file.

    Writes to ``<output_dir>/tickets/<ticket_id>/attempt_<n>.json``.
    Intermediate directories are created automatically.

    Args:
        evidence: The ticket evidence to record.
        output_dir: Base output directory (not the tickets subdir).

    Returns:
        Path to the written evidence file.
    """
    ticket_dir = output_dir / "tickets" / evidence.ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    filename = f"attempt_{evidence.attempt}.json"
    path = ticket_dir / filename

    # Truncate coder response for storage
    data = asdict(evidence)
    if len(data.get("coder_response", "")) > 50_000:
        data["coder_response"] = data["coder_response"][:50_000] + "\n... (truncated)"

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote ticket evidence: %s", path)
    return path


def write_run_metadata(
    config: CoderConfig,
    plan_path: str | Path,
    output_dir: Path,
    auth_mode: str = "",
    backend_capabilities: dict[str, bool] | None = None,
) -> Path:
    """Write a JSON file with run-level metadata at run start.

    Written to ``<output_dir>/run-metadata.json``.  Called once before the
    ticket loop begins so that a run can be identified even if it crashes.

    Args:
        config: The coder configuration for this run.
        plan_path: Path to the plan JSON file that was loaded.
        output_dir: Base output directory.
        auth_mode: The detected auth mode for the backend (e.g. ``api_key``,
            ``local_interactive``).  Empty string when not provided.
        backend_capabilities: Capability flags for the backend as returned by
            ``capability_matrix``.  ``None`` is written as an empty dict.

    Returns:
        Path to the written metadata file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "run-metadata.json"

    data: dict[str, Any] = {
        "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plan_path": str(plan_path),
        "backend": config.backend,
        "auth_mode": auth_mode,
        "backend_capabilities": backend_capabilities if backend_capabilities is not None else {},
        "saturnday_version": _SATURNDAY_VERSION,
    }

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote run metadata: %s", path)
    return path


def write_phase_summary(ledger: "RunLedger", output_dir: Path) -> list[Path]:
    """Write per-phase outcome JSON files.

    Writes one file per phase to ``<output_dir>/phases/phase-<phase_id>.json``.
    If the ledger has no phases, returns an empty list and writes nothing.

    The phase file contains the phase metadata (id, name, status, timestamps)
    plus per-ticket outcomes drawn from the ledger's ticket statuses.

    Args:
        ledger: The completed run ledger with phase and ticket outcomes.
        output_dir: Base output directory.

    Returns:
        List of paths written (one per phase).
    """
    if not ledger.phase_statuses:
        return []

    phases_dir = output_dir / "phases"
    phases_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for phase in ledger.phase_statuses:
        ticket_outcomes: list[dict[str, Any]] = []
        for tid in phase.ticket_ids:
            ts = ledger.ticket_statuses.get(tid)
            if ts is not None:
                ticket_outcomes.append({
                    "ticket_id": ts.ticket_id,
                    "disposition": ts.disposition,
                    "failure_category": ts.failure_category,
                })
            else:
                ticket_outcomes.append({
                    "ticket_id": tid,
                    "disposition": "PENDING",
                    "failure_category": "",
                })

        data: dict[str, Any] = {
            "phase_id": phase.phase_id,
            "name": phase.name,
            "status": phase.status,
            "started_utc": phase.started_utc,
            "completed_utc": phase.completed_utc,
            "tickets": ticket_outcomes,
        }

        path = phases_dir / f"phase-{phase.phase_id}.json"
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("Wrote phase summary: %s", path)
        written.append(path)

    return written


def compute_run_analytics(
    run_result: RunResult,
    backend: str = "",
    auth_mode: str = "",
) -> dict[str, Any]:
    """Compute analytics from a completed RunResult.

    Returns a flat analytics dict suitable for JSON serialisation and
    downstream dashboards.

    Args:
        run_result: The completed run result.
        backend: Backend identifier (e.g. ``"claude-cli"``).  Callers should
            pass this from their CoderConfig so that usage accounting is
            correctly classified.
        auth_mode: Detected auth mode (e.g. ``"local_interactive"``).
            Callers should pass this from the auth detection result.

    Returns:
        Analytics dict with acceptance rate, retry stats, failure breakdown,
        senior quality verdict, and a structured ``usage_accounting`` block
        that covers Fix 14 accounting requirements (14.1–14.4).
    """
    from saturnday.run.cost_accounting import accounting_from_run_result

    total = run_result.total_tickets
    avg_retries = (
        sum(tr.attempts for tr in run_result.ticket_results) / total
        if total
        else 0.0
    )
    usage_accounting = accounting_from_run_result(
        run_result, backend=backend, auth_mode=auth_mode
    )
    return {
        "acceptance_rate": run_result.passed / total if total else 0.0,
        "avg_retries": avg_retries,
        "failure_category_distribution": _count_failure_categories(run_result),
        "total_tickets": total,
        "passed": run_result.passed,
        "failed": run_result.failed,
        "skipped": run_result.skipped,
        "stop_reason": run_result.stop_reason,
        "definition_of_done_met": run_result.definition_of_done_met,
        "backend": backend,
        "auth_mode": auth_mode,
        "usage_accounting": usage_accounting.to_dict(),
        "senior_quality_verdict": _compute_quality_verdict(run_result),
    }


def _count_failure_categories(run_result: RunResult) -> dict[str, int]:
    """Count occurrences of each failure_category across all ticket results.

    Only failed tickets with a non-empty failure_category are counted.
    Tickets without a category contribute to an ``"uncategorised"`` bucket
    when they have a FAIL disposition.

    Args:
        run_result: The completed run result.

    Returns:
        Mapping of category name to occurrence count.
    """
    counts: dict[str, int] = {}
    for tr in run_result.ticket_results:
        if tr.disposition != "FAIL":
            continue
        category = tr.failure_category or "uncategorised"
        counts[category] = counts.get(category, 0) + 1
    return counts


def _compute_quality_verdict(run_result: RunResult) -> dict[str, Any]:
    """Summarise senior quality gate outcomes across all ticket results.

    Post-check data is not stored directly on TicketResult (it is embedded in
    the per-attempt evidence JSON files).  This verdict is therefore derived
    from governance_disposition and error fields, which do capture post-check
    failures when they trigger a retry or final FAIL.

    Returns:
        Dict with:
        - ``governance_pass_count``: tickets with governance PASS
        - ``governance_fail_count``: tickets with governance FAIL
        - ``error_count``: tickets whose error field is non-empty
        - ``quality_level``: ``"green"``, ``"yellow"``, or ``"red"`` summary
    """
    gov_pass = sum(
        1 for tr in run_result.ticket_results if tr.governance_disposition == "PASS"
    )
    gov_fail = sum(
        1 for tr in run_result.ticket_results if tr.governance_disposition == "FAIL"
    )
    error_count = sum(1 for tr in run_result.ticket_results if tr.error)

    total = run_result.total_tickets
    pass_rate = run_result.passed / total if total else 0.0

    if pass_rate >= 0.9 and gov_fail == 0:
        quality_level = "green"
    elif pass_rate >= 0.5:
        quality_level = "yellow"
    else:
        quality_level = "red"

    return {
        "governance_pass_count": gov_pass,
        "governance_fail_count": gov_fail,
        "error_count": error_count,
        "quality_level": quality_level,
    }


def write_analytics(analytics: dict[str, Any], output_dir: Path) -> Path:
    """Write run analytics to ``<output_dir>/analytics.json``.

    Args:
        analytics: The analytics dict produced by ``compute_run_analytics``.
        output_dir: Base output directory.

    Returns:
        Path to the written analytics file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "analytics.json"
    path.write_text(json.dumps(analytics, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote run analytics: %s", path)
    return path


def write_run_summary(
    result: RunResult,
    output_dir: Path,
    ran_stages: "set[str] | None" = None,
    *,
    plan_data: "dict[str, Any] | None" = None,
    repo_path: "Path | None" = None,
) -> Path:
    """Write a JSON summary of the complete plan execution.

    Args:
        result: The run result to summarize.
        output_dir: Directory for the summary file.
        ran_stages: Optional set of premium stage names that executed during
            this run.  Passed through to :func:`build_skipped_stages` so the
            evidence pack can distinguish stages that were skipped because
            premium is not installed from stages that were registered but did
            not execute for this run.  Pass ``None`` (default) to preserve
            the pre-SPLIT-016 behaviour where registered stages are treated
            as having run.
        plan_data: Optional plan dict.  When provided, plan-governance context
            fields (``governing_goal``, ``required_outcomes``,
            ``scoped_categories``, ``exclusions``) are written to the summary.
            Callers that do not have a complete plan (partial writes, interrupt
            handlers) omit this argument — in that case the governance fields
            are absent from the summary, which is correct: no governance
            evaluation exists for an incomplete run.
        repo_path: Optional repository root.  When provided, agent governance
            state is read from ``.saturnday/session.json`` and discovered
            agents are included in the summary under ``agent_governance``.

    Returns:
        Path to the written summary file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "run-summary.json"

    data: dict[str, Any] = {
        "project_id": result.project_id,
        "total_tickets": result.total_tickets,
        "passed": result.passed,
        "failed": result.failed,
        "skipped": result.skipped,
        "definition_of_done_met": result.definition_of_done_met,
        "stop_reason": result.stop_reason,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ticket_results": [],
    }

    # Compute failure category breakdown
    failure_categories: dict[str, int] = {}
    for tr in result.ticket_results:
        data["ticket_results"].append({
            "ticket_id": tr.ticket_id,
            "disposition": tr.disposition,
            "attempts": tr.attempts,
            "changed_files": list(tr.changed_files),
            "governance_disposition": tr.governance_disposition,
            "governance_evidence_path": tr.governance_evidence_path,
            "error": tr.error,
            "failure_category": tr.failure_category,
            "verify_cmd_specified": tr.verify_cmd_specified,
            "verify_cmd_passed": tr.verify_cmd_passed,
            "verify_cmd_failure": tr.verify_cmd_failure,
        })
        if tr.failure_category:
            failure_categories[tr.failure_category] = (
                failure_categories.get(tr.failure_category, 0) + 1
            )

    if failure_categories:
        data["failure_categories"] = failure_categories

    # Capability state — additive fields recording premium availability at run time.
    cap_state = build_capability_state()
    data["premium_capabilities_enabled"] = cap_state["premium_capabilities_enabled"]
    data["available_premium_hooks"] = cap_state["available_premium_hooks"]
    data["skipped_premium_stages"] = build_skipped_stages(ran_stages)

    # Per-hook availability booleans.
    data["security_triage_available"] = is_available("security_triage")
    data["memory_available"] = is_available("memory_provider")
    data["impact_analysis_available"] = is_available("impact_analysis")
    data["code_reviewer_available"] = is_available("code_reviewer")

    # Plan-governance outcome fields — always written so consumers can rely on
    # the keys being present in a final summary.
    data["plan_governance_met"] = result.plan_governance_met
    data["plan_governance_reason"] = result.plan_governance_reason

    # Fix 40: cross-ticket consistency gate results.
    data["cross_ticket_consistency_failures"] = [
        dict(f) for f in result.cross_ticket_consistency_failures
    ]

    # Fix 41: plan-level final acceptance gate results (legacy path).
    data["acceptance_cmd_passed"] = result.acceptance_cmd_passed
    data["acceptance_cmd_failure"] = result.acceptance_cmd_failure
    # Fix 77: local + live proof outcomes.  Mutually exclusive with the
    # acceptance_cmd_* pair above per the plan's operating_mode (legacy plans
    # populate acceptance_cmd_*; declared-mode plans populate local_proof_*).
    # Live proof is supplementary and non-blocking.
    data["local_proof_attempted"] = result.local_proof_attempted
    data["local_proof_passed"] = result.local_proof_passed
    data["local_proof_failure"] = result.local_proof_failure
    data["live_proof_attempted"] = result.live_proof_attempted
    data["live_proof_passed"] = result.live_proof_passed
    data["live_proof_failure"] = result.live_proof_failure
    # Phase 1: honest proof-resolution accounting — tells operator whether
    # the proof was ever defined, how it was resolved, and the final outcome.
    data["proof_resolution_status"] = result.proof_resolution_status
    data["proof_resolution_source"] = result.proof_resolution_source
    data["proof_resolution_narrative"] = result.proof_resolution_narrative
    # α: record which surface approved the acceptance_setup steps so the
    # run-summary distinguishes "TTY approved", "CLI flag approved", and
    # "env var approved" from "blocked_noninteractive" after the fact.
    data["acceptance_setup_approval_source"] = getattr(
        result, "acceptance_setup_approval_source", ""
    )
    # I.4: record work-branch metadata for resume/rerun continuity and
    # operator audit.  All fields are empty strings / False when the
    # --work-branch flag was not used, so the summary shape is identical
    # for pre-I.4 runs.
    data["git_parent_branch"] = getattr(result, "git_parent_branch", "")
    data["git_parent_head_sha"] = getattr(result, "git_parent_head_sha", "")
    data["git_work_branch"] = getattr(result, "git_work_branch", "")
    data["git_work_branch_auto_created"] = getattr(
        result, "git_work_branch_auto_created", False,
    )
    data["git_final_head_sha"] = getattr(result, "git_final_head_sha", "")

    # Plan-governance context — only written when plan_data is available
    # (final writes).  Partial/interrupt summaries omit these fields so the
    # record does not claim governance context it hasn't computed.
    if plan_data is not None:
        data["governing_goal"] = plan_data.get("governing_goal", "")
        data["required_outcomes"] = plan_data.get("required_outcomes", [])
        data["scoped_categories"] = plan_data.get("scoped_categories", [])
        data["exclusions"] = plan_data.get("exclusions", [])
        # Fix 76 / 73: operator-visible mode framing.  Surfaces seeded_demo,
        # storage_only, external_dependencies disclaimers AND the proof
        # realism so the summary cannot be mistaken for full production
        # completion when only a demo path was proved.
        _op_mode = plan_data.get("operating_mode", "")
        _proof_realism = plan_data.get("proof_realism", "")
        _disclaimer = plan_data.get("operator_disclaimer", "")
        data["operating_mode"] = _op_mode
        data["proof_realism"] = _proof_realism
        data["operator_disclaimer"] = _disclaimer
        # Phase 1: testing_strategy visible in summary when present.
        _testing_strategy = plan_data.get("testing_strategy", "unspecified")
        if _testing_strategy and _testing_strategy != "unspecified":
            data["testing_strategy"] = _testing_strategy
        # Fix 73 explicit demo note — never let a seeded_demo run be
        # reported as full product completion in any operator-visible field.
        if _proof_realism == "seeded_demo":
            data["proof_completion_note"] = (
                "SEEDED_DEMO — the local proof exercised the operator path "
                "with seeded fixture data only.  This is NOT proof of full "
                "production-intent product completion."
            )

    # Agent governance evidence — only when repo_path is provided.
    if repo_path is not None:
        try:
            from saturnday.agent_discovery import discover_all_agents
            _ag_state: dict[str, Any] = {
                "enabled": False,
                "scope": "",
                "governed_agents": [],
                "discovered_agents": [],
            }
            _session_path = repo_path / ".saturnday" / "session.json"
            if _session_path.is_file():
                import json as _json_ag
                _sess = _json_ag.loads(_session_path.read_text(encoding="utf-8"))
                _ag_state["enabled"] = _sess.get("agent_governance_enabled", False)
                _ag_state["scope"] = _sess.get("agent_governance_scope", "")
                _ag_state["governed_agents"] = _sess.get("governed_agents", [])
            _ag_state["discovered_agents"] = [
                {"name": a["name"], "source": a["source"]}
                for a in discover_all_agents(repo_path)
            ]
            data["agent_governance"] = _ag_state
        except Exception as _ag_exc:
            logger.debug("Agent governance evidence failed: %s", _ag_exc)

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote run summary: %s", path)
    return path
