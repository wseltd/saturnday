"""Operational recovery: resume, rerun-failed, and rerun-remaining.

``resume_plan`` continues a run from the last ledger checkpoint, skipping
tickets that already passed.  ``rerun_failed`` re-executes only tickets
whose code failed or was committed ungoverned.  ``rerun_remaining``
re-executes only tickets that were skipped or never attempted.

All three delegate to the existing ``_run_ticket_with_retries`` in
ticket_runner.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from saturnday._types import CoderConfig, RunResult

logger = logging.getLogger(__name__)

# Pattern matching Saturnday commit subjects: [T001], [T002.a], [REPAIR-003], etc.
_TICKET_ID_RE = re.compile(r"^\[([A-Za-z0-9._-]+)\]")


def _detect_committed_tickets(repo_path: str | Path) -> frozenset[str]:
    """Return ticket IDs found in git commit subjects on the current branch.

    Scans ``git log --format='%s'`` for the ``[TICKET_ID]`` prefix that
    ``_git_commit`` uses in ``ticket_runner.py``.  Returns a frozenset
    of ticket IDs (e.g. ``{"T001", "T002", "T003.a"}``).

    Returns an empty frozenset on any error (no git repo, empty history, etc.).
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return frozenset()
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()

    ids: set[str] = set()
    for line in result.stdout.splitlines():
        m = _TICKET_ID_RE.match(line.strip())
        if m:
            ids.add(m.group(1))
    return frozenset(ids)


def _reconcile_stale_ledger(
    ledger_data: dict[str, Any],
    committed: frozenset[str],
    repo_path: str | Path,
    evidence_dir: Path,
) -> frozenset[str]:
    """Reconcile stale PENDING tickets that already have git commits.

    For any ticket where the ledger says PENDING but git shows a commit,
    promotes the disposition to CODED_UNGOVERNED and persists the updated
    ledger.  Returns the set of reconciled ticket IDs.
    """
    ticket_statuses = ledger_data.get("ticket_statuses", {})
    reconciled: set[str] = set()

    for tid, ts in ticket_statuses.items():
        if ts.get("disposition") == "PENDING" and tid in committed:
            ts["disposition"] = "CODED_UNGOVERNED"
            reconciled.add(tid)

    if reconciled:
        # Persist the reconciled ledger
        ledger_path = evidence_dir / "evidence" / "run" / "ledger.json"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(
            json.dumps(ledger_data, indent=2) + "\n", encoding="utf-8",
        )
        logger.warning(
            "Fix 44.a: reconciled %d stale PENDING ticket(s) that already have "
            "git commits — promoted to CODED_UNGOVERNED and will be skipped: %s",
            len(reconciled), sorted(reconciled),
        )

    return frozenset(reconciled)


def _read_recorded_work_branch(evidence_dir: Path) -> str:
    """I.4: extract the work-branch name the prior run recorded, if any.

    Resume / rerun-failed / rerun-remaining use this so the operator
    doesn't have to re-pass ``--work-branch`` on every follow-up run.
    Returns an empty string when the prior run did not use the feature
    or when the summary cannot be read.
    """
    for name in ("run-summary.json",):
        for candidate in Path(evidence_dir).rglob(name):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                recorded = data.get("git_work_branch", "")
                if isinstance(recorded, str) and recorded:
                    return recorded
            except Exception:
                continue
    return ""


def _load_ledger(evidence_dir: Path) -> dict[str, Any]:
    """Load ledger.json from the evidence directory.

    Args:
        evidence_dir: Base output directory (parent of ``evidence/run/``).

    Returns:
        Parsed ledger dict.

    Raises:
        FileNotFoundError: If ledger.json does not exist.
    """
    ledger_path = evidence_dir / "evidence" / "run" / "ledger.json"
    if not ledger_path.exists():
        raise FileNotFoundError(f"Ledger not found: {ledger_path}")
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def resume_plan(
    evidence_dir: str | Path,
    plan_path: str | Path,
    repo_path: str | Path,
    coder_config: CoderConfig,
    standards_dir: str | Path,
    output_dir: str | Path | None = None,
    approve_acceptance_setup: bool = False,
    work_branch: str | None = None,
    proof_answers: dict[str, str] | None = None,
    max_consecutive_failures: int | None = None,
) -> RunResult:
    """Continue a run from the last ledger checkpoint.

    Tickets that already PASSED are skipped. Execution resumes from the
    first non-PASS ticket.

    Args:
        evidence_dir: Directory containing the prior run's evidence.
        plan_path: Path to the plan JSON file.
        repo_path: Path to the target repository.
        coder_config: Backend configuration.
        standards_dir: Path to engineering standards directory.
        output_dir: Output directory for the resumed run (default: evidence_dir).

    Returns:
        RunResult for the resumed execution.
    """
    evidence_dir = Path(evidence_dir).resolve()
    ledger_data = _load_ledger(evidence_dir)

    # Fix 44.a: reconcile stale PENDING tickets that have git commits
    committed = _detect_committed_tickets(repo_path)
    _reconcile_stale_ledger(ledger_data, committed, repo_path, evidence_dir)

    # Identify tickets that already passed
    ticket_statuses = ledger_data.get("ticket_statuses", {})
    passed_tickets = frozenset(
        tid for tid, ts in ticket_statuses.items()
        if ts.get("disposition") in ("PASS", "CODED_UNGOVERNED")
    )

    coded_in_skip = frozenset(
        tid for tid, ts in ticket_statuses.items()
        if ts.get("disposition") == "CODED_UNGOVERNED"
    )
    if coded_in_skip:
        logger.warning(
            "resume: %d ticket(s) will be skipped that were coded but not governed — "
            "code is on the branch but governance did not clear it; "
            "use 'saturnday rerun-failed' to revisit: %s",
            len(coded_in_skip), sorted(coded_in_skip),
        )

    logger.info(
        "Resuming plan: %d tickets already passed, will skip them",
        len(passed_tickets),
    )

    # I.4: if the operator passed a fresh --work-branch, prefer that.
    # Otherwise read the prior run's recorded branch and continue on it.
    recorded = _read_recorded_work_branch(Path(evidence_dir))
    return _run_with_filter(
        plan_path=plan_path,
        repo_path=repo_path,
        coder_config=coder_config,
        standards_dir=standards_dir,
        output_dir=output_dir or str(evidence_dir),
        skip_tickets=passed_tickets,
        approve_acceptance_setup=approve_acceptance_setup,
        work_branch=work_branch,
        work_branch_resume_recorded=recorded if work_branch is None else None,
        proof_answers=proof_answers,
        max_consecutive_failures=max_consecutive_failures,
    )


def rerun_failed(
    evidence_dir: str | Path,
    plan_path: str | Path,
    repo_path: str | Path,
    coder_config: CoderConfig,
    standards_dir: str | Path,
    output_dir: str | Path | None = None,
    approve_acceptance_setup: bool = False,
    work_branch: str | None = None,
    proof_answers: dict[str, str] | None = None,
    max_consecutive_failures: int | None = None,
) -> RunResult:
    """Re-execute only tickets that failed in a prior run.

    Args:
        evidence_dir: Directory containing the prior run's evidence.
        plan_path: Path to the plan JSON file.
        repo_path: Path to the target repository.
        coder_config: Backend configuration.
        standards_dir: Path to engineering standards directory.
        output_dir: Output directory for the rerun (default: evidence_dir).

    Returns:
        RunResult for the rerun execution.
    """
    evidence_dir = Path(evidence_dir).resolve()
    ledger_data = _load_ledger(evidence_dir)

    # Fix 44.a: reconcile stale PENDING tickets that have git commits
    committed = _detect_committed_tickets(repo_path)
    _reconcile_stale_ledger(ledger_data, committed, repo_path, evidence_dir)

    ticket_statuses = ledger_data.get("ticket_statuses", {})
    failed_tickets = frozenset(
        tid for tid, ts in ticket_statuses.items()
        if ts.get("disposition") in ("FAIL", "CODED_UNGOVERNED")
    )

    if not failed_tickets:
        logger.info("No failed tickets to rerun")
        return RunResult(project_id="unknown")

    # Skip everything except failed tickets
    all_tickets = frozenset(ticket_statuses.keys())
    skip_tickets = all_tickets - failed_tickets

    logger.info(
        "Rerunning %d failed tickets: %s",
        len(failed_tickets), sorted(failed_tickets),
    )

    recorded = _read_recorded_work_branch(Path(evidence_dir))
    return _run_with_filter(
        plan_path=plan_path,
        repo_path=repo_path,
        coder_config=coder_config,
        standards_dir=standards_dir,
        output_dir=output_dir or str(evidence_dir),
        skip_tickets=skip_tickets,
        approve_acceptance_setup=approve_acceptance_setup,
        work_branch=work_branch,
        work_branch_resume_recorded=recorded if work_branch is None else None,
        proof_answers=proof_answers,
        max_consecutive_failures=max_consecutive_failures,
    )


def rerun_remaining(
    evidence_dir: str | Path,
    plan_path: str | Path,
    repo_path: str | Path,
    coder_config: CoderConfig,
    standards_dir: str | Path,
    output_dir: str | Path | None = None,
    approve_acceptance_setup: bool = False,
    work_branch: str | None = None,
    proof_answers: dict[str, str] | None = None,
    max_consecutive_failures: int | None = None,
) -> RunResult:
    """Re-execute only tickets that were skipped or never attempted.

    Targets tickets with disposition SKIP or PENDING — work that was
    planned but never executed, typically because a stop condition fired
    or a parent ticket failed.  Does NOT re-execute FAIL or
    CODED_UNGOVERNED tickets (use ``rerun_failed`` for those).

    Args:
        evidence_dir: Directory containing the prior run's evidence.
        plan_path: Path to the plan JSON file.
        repo_path: Path to the target repository.
        coder_config: Backend configuration.
        standards_dir: Path to engineering standards directory.
        output_dir: Output directory for the rerun (default: evidence_dir).

    Returns:
        RunResult for the rerun execution.
    """
    evidence_dir = Path(evidence_dir).resolve()
    ledger_data = _load_ledger(evidence_dir)

    # Fix 44.a: reconcile stale PENDING tickets that have git commits
    committed = _detect_committed_tickets(repo_path)
    _reconcile_stale_ledger(ledger_data, committed, repo_path, evidence_dir)

    ticket_statuses = ledger_data.get("ticket_statuses", {})
    remaining_tickets = frozenset(
        tid for tid, ts in ticket_statuses.items()
        if ts.get("disposition") in ("SKIP", "PENDING")
    )

    if not remaining_tickets:
        logger.info("No remaining unattempted tickets to rerun")
        return RunResult(project_id="unknown")

    # Skip everything except remaining tickets
    all_tickets = frozenset(ticket_statuses.keys())
    skip_tickets = all_tickets - remaining_tickets

    logger.info(
        "Rerunning %d remaining unattempted tickets: %s",
        len(remaining_tickets), sorted(remaining_tickets),
    )

    recorded = _read_recorded_work_branch(Path(evidence_dir))
    return _run_with_filter(
        plan_path=plan_path,
        repo_path=repo_path,
        coder_config=coder_config,
        standards_dir=standards_dir,
        output_dir=output_dir or str(evidence_dir),
        skip_tickets=skip_tickets,
        approve_acceptance_setup=approve_acceptance_setup,
        work_branch=work_branch,
        work_branch_resume_recorded=recorded if work_branch is None else None,
        proof_answers=proof_answers,
        max_consecutive_failures=max_consecutive_failures,
    )


def _select_continuation_command(
    failed: list[str],
    coded_ungoverned: list[str],
    skipped: list[str],
    pending: list[str],
) -> str | None:
    """Return the correct recovery subcommand name, or None if no continuation needed.

    Decision matrix (evaluated in priority order):

    - FAIL + (SKIP or PENDING)                    → ``resume``
    - (FAIL or CODED_UNGOVERNED) + no remaining   → ``rerun-failed``
    - (SKIP or PENDING) + no FAIL                 → ``rerun-remaining``
    - Otherwise (all settled)                     → ``None``

    Args:
        failed: Ticket IDs with disposition FAIL.
        coded_ungoverned: Ticket IDs with disposition CODED_UNGOVERNED.
        skipped: Ticket IDs with disposition SKIP.
        pending: Ticket IDs with disposition PENDING.

    Returns:
        Subcommand name string (``"resume"``, ``"rerun-failed"``,
        ``"rerun-remaining"``) or ``None`` when no continuation is needed.
    """
    has_fail = bool(failed)
    has_coded = bool(coded_ungoverned)
    has_remaining = bool(skipped or pending)

    if has_fail and has_remaining:
        return "resume"
    if (has_fail or has_coded) and not has_remaining:
        return "rerun-failed"
    if has_remaining and not has_fail:
        return "rerun-remaining"
    return None


def describe_recovery_state(evidence_dir: str | Path) -> dict[str, Any]:
    """Describe the recovery state of a prior run for operator display.

    Returns a dict with ticket counts per disposition and which recovery
    command targets each class.

    Args:
        evidence_dir: Directory containing the prior run's evidence.

    Returns:
        Dict with keys: ``passed``, ``failed``, ``coded_ungoverned``,
        ``skipped``, ``pending``, ``total``, and ``recovery_paths``.
    """
    evidence_dir = Path(evidence_dir).resolve()
    ledger_data = _load_ledger(evidence_dir)
    ticket_statuses = ledger_data.get("ticket_statuses", {})

    counts: dict[str, list[str]] = {
        "PASS": [],
        "FAIL": [],
        "CODED_UNGOVERNED": [],
        "SKIP": [],
        "PENDING": [],
    }
    for tid, ts in ticket_statuses.items():
        disp = ts.get("disposition", "PENDING")
        if disp in counts:
            counts[disp].append(tid)
        else:
            counts["PENDING"].append(tid)

    return {
        "passed": sorted(counts["PASS"]),
        "failed": sorted(counts["FAIL"]),
        "coded_ungoverned": sorted(counts["CODED_UNGOVERNED"]),
        "skipped": sorted(counts["SKIP"]),
        "pending": sorted(counts["PENDING"]),
        "total": len(ticket_statuses),
        "recovery_paths": {
            "resume": "Re-runs FAIL + SKIP + PENDING tickets (skips PASS + CODED_UNGOVERNED)",
            "rerun-failed": "Re-runs FAIL + CODED_UNGOVERNED tickets only",
            "rerun-remaining": "Re-runs SKIP + PENDING tickets only (unattempted work)",
        },
    }


def detect_recovered_baseline_state(
    evidence_dir: "str | Path",
    repo_path: "str | Path",
) -> bool:
    """Return True when the repo is in a recovered-baseline state.

    A recovered-baseline state is the specific situation where:

    * the repo is now inside a valid git working tree (the operator has since
      run ``git init`` and made a baseline commit), AND
    * all FAIL tickets in the prior run ledger have
      ``failure_category == "git_state_unavailable_defect"``, meaning they
      failed only because git-based change detection was unavailable, not
      because the coder produced bad output.

    In this state, those FAILed tickets may already have their artefacts on
    disk and baselined.  Guiding the operator toward ``resume`` (which would
    re-run them) is misleading.  ``rerun-remaining`` — which targets only the
    still-unexecuted SKIP/PENDING tickets — is the correct continuation.

    This detection is intentionally narrow to avoid false positives:
    it only fires when ALL failures in the ledger share the git-state cause.

    Args:
        evidence_dir: Base output directory containing prior run evidence.
        repo_path: Path to the target repository.

    Returns:
        True only when both conditions above are confirmed; False otherwise.
    """
    import subprocess

    evidence_dir = Path(evidence_dir).resolve()
    repo_path = Path(repo_path)

    # Condition 1: repo is now in a valid git working tree.
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if not (proc.returncode == 0 and proc.stdout.strip() == "true"):
            return False
    except (OSError, FileNotFoundError):
        return False

    # Condition 2: ALL FAIL tickets have failure_category == git_state_unavailable_defect.
    try:
        ledger_data = _load_ledger(evidence_dir)
    except Exception:
        return False

    ticket_statuses = ledger_data.get("ticket_statuses", {})
    failed_tickets = [
        ts for ts in ticket_statuses.values()
        if ts.get("disposition") == "FAIL"
    ]
    if not failed_tickets:
        return False

    return all(
        ts.get("failure_category") == "git_state_unavailable_defect"
        for ts in failed_tickets
    )


def _run_with_filter(
    plan_path: str | Path,
    repo_path: str | Path,
    coder_config: CoderConfig,
    standards_dir: str | Path,
    output_dir: str | Path,
    skip_tickets: frozenset[str],
    approve_acceptance_setup: bool = False,
    work_branch: str | None = None,
    work_branch_resume_recorded: str | None = None,
    proof_answers: dict[str, str] | None = None,
    max_consecutive_failures: int | None = None,
) -> RunResult:
    """Run a plan, skipping specified tickets.

    This is a thin wrapper around ``run_plan`` that patches the plan to
    mark filtered tickets as pre-completed.
    """
    from saturnday.ticket_runner import run_plan

    return run_plan(
        plan_path=plan_path,
        repo_path=repo_path,
        coder_config=coder_config,
        standards_dir=standards_dir,
        output_dir=output_dir,
        skip_tickets=skip_tickets,
        approve_acceptance_setup=approve_acceptance_setup,
        work_branch=work_branch,
        work_branch_resume_recorded=work_branch_resume_recorded,
        proof_answers=proof_answers,
        max_consecutive_failures=max_consecutive_failures,
    )
