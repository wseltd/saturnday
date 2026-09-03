"""Role-mode orchestration for Saturnday.

Loads role-specific prompt files and invokes the coder backend
in distinct controlled modes (repo_analyst, coder, governance_judge,
evidence_gate, repair, definition_of_done).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from saturnday._exceptions import CoderAPIError

logger = logging.getLogger(__name__)

ROLE_MODES_DIR = Path(__file__).parent / "prompts" / "role_modes"

KNOWN_ROLES: frozenset[str] = frozenset({
    "coder", "repo_analyst", "governance_judge",
    "evidence_gate", "repair", "definition_of_done",
    "security_triage", "code_reviewer",
})

# Canonical classification of each role's runtime implementation path.
# prompt_pass: invoked via invoke_role() with .md prompt, produces RoleResult
# native_runtime: implemented by a dedicated subsystem, not via invoke_role()
# hybrid: prompt loaded via load_role_prompt() but invoked through native call_coder(), not invoke_role()
ROLE_CLASSIFICATIONS: dict[str, dict[str, str]] = {
    "repo_analyst": {
        "type": "prompt_pass",
        "path": "invoke_role → ticket_runner.py pre-run",
        "run": "automatic_pre_run",
        "repair": "not_used",
    },
    "coder": {
        "type": "native_runtime",
        "path": "context_assembler.build_system_prompt → call_coder",
        "run": "automatic_per_ticket",
        "repair": "not_used",
        "note": "coder.md exists as auxiliary role persona but primary execution uses context_assembler",
    },
    "governance_judge": {
        "type": "hybrid",
        "path": "run_governance_check (deterministic, per-ticket in run) + invoke_role (LLM advisory, post-repair)",
        "run": "automatic_per_ticket_deterministic",
        "repair": "automatic_post_repair_llm",
    },
    "definition_of_done": {
        "type": "prompt_pass",
        "path": "run_dod_check → invoke_role → ticket_runner.py post-run",
        "run": "automatic_post_run",
        "repair": "not_used",
    },
    "evidence_gate": {
        "type": "prompt_pass",
        "path": "invoke_role → ticket_runner.py post-run + repair_runner.py post-repair",
        "run": "automatic_post_run",
        "repair": "automatic_post_repair",
    },
    "repair": {
        "type": "hybrid",
        "path": "load_role_prompt('repair') as system prompt → call_coder (not via invoke_role)",
        "run": "not_used",
        "repair": "automatic_execution",
        "note": "Prompt loaded via role_modes loader but bypasses invoke_role pipeline",
    },
    "security_triage": {
        "type": "prompt_pass",
        "path": "invoke_role → security_triage.py post-governance",
        "run": "automatic_post_governance",
        "repair": "automatic_pre_repair",
        "note": "LLM-based false positive filter for security findings. Runs after pattern matching, before disposition.",
    },
    "code_reviewer": {
        "type": "prompt_pass",
        "path": "invoke_role → ticket_runner.py post-post-checks pre-contract",
        "run": "automatic_post_post_checks_warning",
        "repair": "not_used",
        "note": "LLM semantic review of code against ticket goal. WARNING only — does not block commits.",
    },
}

_OPERATOR_PREFS_FILE = "operator_preferences.md"


@lru_cache(maxsize=8)
def load_role_prompt(role: str) -> str:
    """Load operator_preferences.md + role-specific prompt.

    Args:
        role: One of the known role names.

    Returns:
        Combined prompt string (operator prefs + role prompt).

    Raises:
        ValueError: If ``role`` is not in :data:`KNOWN_ROLES`.
        FileNotFoundError: If operator preferences or role prompt file is missing.
    """
    if role not in KNOWN_ROLES:
        raise ValueError(f"Unknown role: {role!r}. Known roles: {sorted(KNOWN_ROLES)}")
    prefs_path = ROLE_MODES_DIR / _OPERATOR_PREFS_FILE
    role_path = ROLE_MODES_DIR / f"{role}.md"
    if not prefs_path.is_file():
        raise FileNotFoundError(f"Operator preferences not found: {prefs_path}")
    if not role_path.is_file():
        raise FileNotFoundError(f"Role prompt not found: {role_path}")
    prefs = prefs_path.read_text(encoding="utf-8")
    role_prompt = role_path.read_text(encoding="utf-8")
    return f"{prefs}\n\n---\n\n{role_prompt}"


def available_roles() -> list[str]:
    """List roles whose .md files exist on disk.

    Returns:
        Sorted list of role name strings.
    """
    return sorted(r for r in KNOWN_ROLES if (ROLE_MODES_DIR / f"{r}.md").is_file())


def missing_roles() -> list[str]:
    """List expected roles without prompt files.

    Returns:
        Sorted list of role name strings that have no matching .md file.
    """
    return sorted(r for r in KNOWN_ROLES if not (ROLE_MODES_DIR / f"{r}.md").is_file())


@dataclass
class RoleResult:
    """Result from a single role pass.

    Attributes:
        role: Name of the role that was invoked.
        task: Task payload that was sent to the role.
        output: Raw text output from the coder backend.
        success: ``True`` if the backend call succeeded without error.
        error: Error message string if the call failed, otherwise ``None``.
    """

    role: str
    task: str
    output: str
    success: bool
    error: str | None = None


def invoke_role(
    role: str,
    task: str,
    *,
    coder_config: Any,
    repo_path: Path,
    context: dict[str, Any] | None = None,
) -> RoleResult:
    """Run a single role pass against the coder backend.

    Args:
        role: Role name — must be in :data:`KNOWN_ROLES`.
        task: User-facing task payload text.
        coder_config: Coder backend configuration (passed through to
            :func:`~saturnday.coder_adapter.call_coder`).
        repo_path: Repository root directory (used as cwd by CLI backends).
        context: Optional dict appended to the task payload as JSON.

    Returns:
        :class:`RoleResult` with ``success=True`` on a clean backend call,
        ``success=False`` on any exception.
    """
    prompt = load_role_prompt(role)
    user_content = task
    if context:
        context_block = "\n\n## Context\n" + json.dumps(context, indent=2, default=str)
        user_content = task + context_block

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_content},
    ]

    logger.info("Role pass: %s", role)
    try:
        from saturnday.coder_adapter import call_coder  # lazy import

        output = call_coder(coder_config, messages, repo_path)
        logger.info("Role pass complete: %s (%d chars)", role, len(output))
        return RoleResult(role=role, task=task, output=output, success=True)
    except (CoderAPIError, Exception) as exc:  # noqa: BLE001
        logger.warning("Role pass failed: %s -- %s", role, exc)
        return RoleResult(role=role, task=task, output="", success=False, error=str(exc))


def run_role_sequence(
    roles: list[tuple[str, str]],
    *,
    coder_config: Any,
    repo_path: Path,
    on_role_start: Callable[[str, str], None] | None = None,
    on_role_complete: Callable[[str, RoleResult], None] | None = None,
    stop_on_failure: bool = False,
) -> list[RoleResult]:
    """Run multiple role passes in sequence.

    Args:
        roles: List of ``(role_name, task_payload)`` tuples to execute in order.
        coder_config: Coder backend configuration forwarded to each
            :func:`invoke_role` call.
        repo_path: Repository root directory forwarded to each
            :func:`invoke_role` call.
        on_role_start: Optional callback invoked before each role with
            ``(role_name, task_payload)``.
        on_role_complete: Optional callback invoked after each role with
            ``(role_name, result)``.
        stop_on_failure: If ``True``, abort after the first failed role result.

    Returns:
        List of :class:`RoleResult` instances, one per role executed.
    """
    results: list[RoleResult] = []
    for role_name, task_payload in roles:
        if on_role_start:
            on_role_start(role_name, task_payload)
        result = invoke_role(
            role_name, task_payload,
            coder_config=coder_config,
            repo_path=repo_path,
        )
        results.append(result)
        if on_role_complete:
            on_role_complete(role_name, result)
        if stop_on_failure and not result.success:
            break
    return results


def run_dod_check(
    plan_path: Path,
    evidence_dir: Path,
    repo_path: Path,
    *,
    coder_config: Any,
) -> RoleResult:
    """Run the Definition of Done evaluation pass.

    Loads plan JSON, ledger, and run summary from disk, constructs a
    structured task description, then invokes the ``definition_of_done`` role.

    Args:
        plan_path: Path to the plan JSON file.
        evidence_dir: Root directory for evidence output (contains
            ``evidence/run/ledger.json`` and ``run-summary.json``).
        repo_path: Repository root directory.
        coder_config: Coder backend configuration.

    Returns:
        :class:`RoleResult` from the ``definition_of_done`` role.
    """
    # Load plan
    plan_data: dict[str, Any] = {}
    if plan_path.is_file():
        try:
            plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Load ledger
    ledger_data: dict[str, Any] = {}
    ledger_path = evidence_dir / "evidence" / "run" / "ledger.json"
    if ledger_path.is_file():
        try:
            ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Load run summary
    summary_data: dict[str, Any] = {}
    summary_path = evidence_dir / "run-summary.json"
    if summary_path.is_file():
        try:
            summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # Build structured task
    tickets = plan_data.get("tickets", [])
    dod_markers = plan_data.get("definition_of_done", ["all_tickets_passed"])
    ticket_results = summary_data.get("ticket_results", [])

    # Plan-governance fields — full text, no truncation on governing_goal.
    # Fallback to notes (truncated) when no governance fields present.
    gov_goal = plan_data.get("governing_goal", "")
    req_outcomes: list[str] = plan_data.get("required_outcomes", [])
    scoped_cats: list[str] = plan_data.get("scoped_categories", [])
    exclusions: list[str] = plan_data.get("exclusions", [])
    constraints: list[str] = plan_data.get("constraints", [])
    proof_exp: list[str] = plan_data.get("proof_expectations", [])

    # Determine whether this plan carries governance fields at all.
    _has_governance = bool(gov_goal or req_outcomes or exclusions or constraints or proof_exp)

    # Brief section: use governing_goal (full, untruncated) when available,
    # otherwise fall back to notes[:500] for backward compatibility.
    if gov_goal:
        brief_section = f"\n## Governing Goal\n{gov_goal}\n"
    else:
        brief = plan_data.get("notes", "")
        brief_section = f"\n## Original Brief\n{brief[:500]}\n" if brief else ""

    # Include standards digest so DoD can judge code quality
    from saturnday.standards_digest import STANDARDS_DIGEST

    task_parts = [
        f"## {STANDARDS_DIGEST}",
        "## Plan Summary",
        f"Project: {plan_data.get('project_id', 'unknown')}",
        f"Total tickets: {len(tickets)}",
        f"DoD markers: {', '.join(dod_markers)}",
        brief_section,
    ]

    # Plan-governance sections — only emitted when fields are present.
    if req_outcomes:
        task_parts.append("## Required Outcomes")
        for i, outcome in enumerate(req_outcomes, 1):
            task_parts.append(f"  {i}. {outcome}")
    if exclusions:
        task_parts.append("## Exclusions (must NOT be changed)")
        for exc in exclusions:
            task_parts.append(f"  - {exc}")
    if scoped_cats:
        task_parts.append(f"## Scoped Categories: {', '.join(scoped_cats)}")
    if constraints:
        task_parts.append("## Constraints")
        for c in constraints:
            task_parts.append(f"  - {c}")
    if proof_exp:
        task_parts.append("## Proof Expectations")
        for p in proof_exp:
            task_parts.append(f"  - {p}")

    task_parts.append("## Ticket Dispositions")
    for tr in ticket_results:
        tid = tr.get("ticket_id", "?")
        disp = tr.get("disposition", "?")
        task_parts.append(f"  {tid}: {disp}")

    # Fix 39: surface executable verification results so the LLM cannot emit
    # DOD_MET based on text dispositions alone when real execution failed.
    _vc_lines: list[str] = []
    for tr in ticket_results:
        vc_passed = tr.get("verify_cmd_passed")
        if vc_passed is not None:
            tid = tr.get("ticket_id", "?")
            status = "PASS" if vc_passed else "FAIL"
            failure = tr.get("verify_cmd_failure", "")
            line = f"  {tid}: verify_cmd {status}"
            if failure:
                line += f" — {failure[:120]}"
            _vc_lines.append(line)
    if _vc_lines:
        task_parts.append("\n## Executable Verification Results (verify_cmd)")
        task_parts.extend(_vc_lines)
        task_parts.append(
            "NOTE: a verify_cmd FAIL is executable proof of incomplete implementation."
            " A verify_cmd FAIL ticket MUST be classified as FAILED or PARTIAL, not COMPLETE."
        )

    task_parts.append("")
    task_parts.append("## Acceptance Criteria and Scope")
    for t in tickets:
        tid = t.get("ticket_id", "?")
        criteria = t.get("acceptance_criteria", [])
        oos = t.get("out_of_scope", [])
        ev_req = t.get("evidence_required", [])
        if criteria:
            task_parts.append(f"  {tid} criteria: {'; '.join(criteria)}")
        if oos:
            task_parts.append(f"  {tid} out_of_scope: {'; '.join(oos)}")
        if ev_req:
            task_parts.append(f"  {tid} evidence_required: {'; '.join(ev_req)}")

    # Phase 7: surface the end-to-end proof-resolution story so the DoD
    # role can see whether the product was proven, proven by a demo path,
    # proven by a hand-edited operator proof, or never proven at all.
    _pr_status = summary_data.get("proof_resolution_status", "")
    _pr_source = summary_data.get("proof_resolution_source", "")
    _pr_narrative = summary_data.get("proof_resolution_narrative", "")
    if _pr_status or _pr_source or _pr_narrative:
        task_parts.append("\n## Proof Resolution")
        task_parts.append(
            f"  status: {_pr_status or 'not_attempted'}"
        )
        task_parts.append(
            f"  source: {_pr_source or 'none'}"
        )
        if _pr_narrative:
            task_parts.append(f"  narrative: {_pr_narrative}")
        task_parts.append(
            "NOTE: a proof_resolution_status of 'unresolved_gap' or 'failed' "
            "is authoritative — the product was NOT mechanically proven. "
            "Do not classify such a run as DOD_MET."
        )

    if not ledger_data:
        task_parts.append("\n## Note: Ledger not found -- assessment based on summary only.")
    if not summary_data:
        task_parts.append("\n## Note: Run summary not found -- assessment based on plan only.")

    task_parts.append("\n## Task")
    task_parts.append("Evaluate whether the Definition of Done is met.")
    task_parts.append(
        "Classify each ticket as: COMPLETE, PARTIAL, FAILED, UNVERIFIABLE, or BLOCKED."
    )
    task_parts.append(
        "Classify overall DoD as: DOD_MET, DOD_PARTIAL, or DOD_NOT_MET."
    )

    # Plan-governance evaluation section.
    if _has_governance:
        task_parts.append("\n=== PLAN GOVERNANCE ===")
        task_parts.append(
            f"Governing goal: {plan_data.get('governing_goal', plan_data.get('notes', ''))}"
        )
        task_parts.append(f"Required outcomes: {plan_data.get('required_outcomes', [])}")
        task_parts.append(f"Scoped categories: {plan_data.get('scoped_categories', [])}")
        task_parts.append(f"Exclusions: {plan_data.get('exclusions', [])}")
        task_parts.append(f"Constraints: {plan_data.get('constraints', [])}")
        task_parts.append(f"Proof expectations: {plan_data.get('proof_expectations', [])}")
        task_parts.append(
            "\nIn addition to evaluating the Definition of Done, evaluate whether "
            "the PLAN GOVERNANCE requirements were met:"
        )
        task_parts.append("  - Were the required outcomes achieved?")
        task_parts.append("  - Were exclusions respected?")
        task_parts.append("  - Were scoped categories preserved (no scope creep)?")
        task_parts.append("  - Were proof expectations satisfied?")
        task_parts.append(
            "\nIf all plan governance requirements are met, include "
            "\"PLAN_GOVERNANCE_MET\" in your response."
        )
        task_parts.append(
            "If any are not met, include \"PLAN_GOVERNANCE_NOT_MET\" followed by the reason."
        )
    else:
        # No governance fields — emit sentinel so caller can treat as met.
        task_parts.append(
            "\nThis plan has no governance fields. Include "
            "\"PLAN_GOVERNANCE_NOT_MET\" or \"PLAN_GOVERNANCE_MET\" based on "
            "whether the DoD is met, or include \"PLAN_GOVERNANCE_NO_GOVERNANCE\" "
            "to indicate no plan-level governance was configured."
        )

    task = "\n".join(task_parts)
    return invoke_role(
        "definition_of_done",
        task,
        coder_config=coder_config,
        repo_path=repo_path,
    )


def extract_classification(output: str, candidates: list[str]) -> str:
    """Extract the first matching classification string from LLM output.

    Scans ``output`` for the first occurrence of any string in
    ``candidates`` and returns it.  Falls back to a truncated first line
    if no candidate matches.

    Args:
        output: Raw text output from a role pass.
        candidates: Ordered list of classification strings to search for.

    Returns:
        Matched candidate string, or a truncated summary of ``output``.
    """
    for candidate in candidates:
        if candidate in output:
            return candidate
    # Fallback: first 80 chars of cleaned output
    clean = output.strip().replace("\n", " ")
    return clean[:80] + ("..." if len(clean) > 80 else "")


def _retry_pg_evaluation(
    plan_data: dict[str, Any],
    ticket_results: tuple[Any, ...],
    dod_classification: str,
    coder_config: Any,
    repo_path: Path,
    max_attempts: int = 3,
) -> tuple[bool, str]:
    """Focused plan-governance evaluation with bounded retry.

    Sends a compact PG-only prompt (not the full DoD task) and extracts
    PLAN_GOVERNANCE_MET or PLAN_GOVERNANCE_NOT_MET.

    This helper is only called when the initial DoD role pass failed to
    produce a PG verdict.  It does NOT re-run the DoD check — it issues a
    narrow, governance-only prompt to the same backend.

    Args:
        plan_data: Raw plan dict (already loaded from disk).
        ticket_results: Per-ticket :class:`~saturnday._types.TicketResult`
            instances from the completed run.
        dod_classification: DoD verdict already established by the DoD role
            pass (e.g. ``"DOD_MET"``).
        coder_config: Coder backend configuration.
        repo_path: Repository root directory.
        max_attempts: Maximum number of LLM calls before giving up.

    Returns:
        ``(pg_met, pg_reason)`` where ``pg_met`` is ``True`` when
        PLAN_GOVERNANCE_MET is extracted and ``False`` otherwise.
        On exhaustion returns ``(False, "PLAN_GOVERNANCE_NOT_EVALUATED")``.
    """
    gov_goal: str = plan_data.get("governing_goal", "")
    req_outcomes: list[str] = plan_data.get("required_outcomes", [])
    scoped_cats: list[str] = plan_data.get("scoped_categories", [])
    exclusions: list[str] = plan_data.get("exclusions", [])

    # Compact ticket-disposition summary — avoid sending full evidence.
    disposition_lines: list[str] = []
    for tr in ticket_results:
        # Support both TicketResult dataclass and dict (from summary JSON).
        if hasattr(tr, "ticket_id"):
            tid = tr.ticket_id
            disp = tr.disposition
        else:
            tid = tr.get("ticket_id", "?")
            disp = tr.get("disposition", "?")
        disposition_lines.append(f"{tid}: {disp}")
    ticket_summary = ", ".join(disposition_lines) if disposition_lines else "no ticket data"

    task = (
        "## Plan Governance Evaluation\n\n"
        "The Definition of Done evaluation has already been completed with the following verdict:\n"
        f"  DoD classification: {dod_classification}\n\n"
        "Your task is to evaluate ONLY whether the plan-level governance requirements were met.\n\n"
        f"Governing goal:\n  {gov_goal}\n\n"
        f"Required outcomes:\n  {chr(10).join(f'  - {o}' for o in req_outcomes) if req_outcomes else '  (none)'}\n\n"
        f"Scoped categories:\n  {', '.join(scoped_cats) if scoped_cats else '(none)'}\n\n"
        f"Exclusions:\n  {chr(10).join(f'  - {e}' for e in exclusions) if exclusions else '(none)'}\n\n"
        f"Ticket dispositions:\n  {ticket_summary}\n\n"
        "Based on the above, evaluate whether the governing goal and required outcomes were met,\n"
        "whether exclusions were respected, and whether scoped categories were preserved.\n\n"
        "You MUST emit exactly one of the following tokens on its own line:\n"
        "  PLAN_GOVERNANCE_MET\n"
        "  PLAN_GOVERNANCE_NOT_MET: <brief reason>\n\n"
        "Do not evaluate tickets individually. Do not repeat the DoD verdict. "
        "Emit the PG token and a one-line reason only."
    )

    _pg_candidates = ["PLAN_GOVERNANCE_NOT_MET", "PLAN_GOVERNANCE_MET"]

    for attempt in range(1, max_attempts + 1):
        logger.info("PG retry attempt %d/%d", attempt, max_attempts)
        try:
            result = invoke_role(
                "definition_of_done",
                task,
                coder_config=coder_config,
                repo_path=repo_path,
            )
            if not result.success:
                logger.warning("PG retry %d: role invocation failed — %s", attempt, result.error)
                continue

            verdict = extract_classification(result.output, _pg_candidates)
            if verdict == "PLAN_GOVERNANCE_MET":
                return True, "PLAN_GOVERNANCE_MET"
            if verdict == "PLAN_GOVERNANCE_NOT_MET":
                # Extract reason after the token (up to 200 chars).
                reason = "PLAN_GOVERNANCE_NOT_MET"
                if "PLAN_GOVERNANCE_NOT_MET" in result.output:
                    idx = result.output.index("PLAN_GOVERNANCE_NOT_MET")
                    reason = result.output[idx : idx + 200].strip()
                return False, reason

            logger.warning(
                "PG retry %d: no PG token in output — %s",
                attempt,
                result.output[:120],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("PG retry %d: unexpected error — %s", attempt, exc)

    logger.warning(
        "PG evaluation exhausted %d attempts — setting PLAN_GOVERNANCE_NOT_EVALUATED",
        max_attempts,
    )
    return False, "PLAN_GOVERNANCE_NOT_EVALUATED"
