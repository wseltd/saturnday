"""Batch repair execution with stop conditions."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from saturnday.repair.finding_locality import is_file_local
from saturnday.repair.repair_executor import RepairResult, execute_repair
from saturnday.repair.repair_tickets import RepairTicket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fix 49.a — per-ticket durable persistence helpers
# ---------------------------------------------------------------------------

def _commit_repair_fix(
    repo_path: Path,
    ticket_id: str,
    finding_kind: str,
    scoped_files: list[str] | None = None,
) -> None:
    """Commit a successfully fixed repair ticket.

    Fix C: stages only the explicit list of files the ticket targets
    (``scoped_files``) instead of using ``git add -A``.  Untracked
    artefacts in the working tree (terraform plans, ``.env`` files,
    build outputs, etc.) are no longer swept into repair commits.

    Fix B: sets ``SATURNDAY_INTERNAL_COMMIT=1`` on the commit subprocess
    so Saturnday's own pre-commit hook skips governance for commits
    initiated by governed repair execution.

    Non-fatal: if staging or committing fails the repair result is still
    valid, just not yet committed.
    """
    if not scoped_files:
        # Nothing to stage — caller didn't provide a concrete file list.
        # Under Fix C we refuse to fall back to ``git add -A``; that was
        # the exact defect this change fixes.  Log and return so the
        # repair result stands on disk but is not silently committed
        # with an unknown scope.
        logger.warning(
            "_commit_repair_fix: no scoped_files provided for %s — refusing "
            "broad ``git add -A`` and skipping the commit.",
            ticket_id,
        )
        return
    _env = {**os.environ, "SATURNDAY_INTERNAL_COMMIT": "1"}
    try:
        add_cmd = ["git", "-C", str(repo_path), "add", "--"] + list(scoped_files)
        subprocess.run(
            add_cmd,
            capture_output=True, timeout=10, check=False, env=_env,
        )
        msg = f"[{ticket_id}] saturnday repair: fixed {finding_kind}"
        result = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", msg],
            capture_output=True, text=True, timeout=10, check=False, env=_env,
        )
        if result.returncode == 0:
            logger.info("Fix 49.a: committed repair fix for %s", ticket_id)
        else:
            logger.debug("Fix 49.a: nothing to commit for %s (%s)", ticket_id, result.stderr.strip()[:100])
    except Exception as exc:
        logger.debug("Fix 49.a: commit failed for %s: %s", ticket_id, exc)


def _write_incremental_summary(
    run_result: "RepairRunResult",
    output_dir: Path | None,
) -> None:
    """Persist the current repair progress to ``repair-summary.json``.

    Called after each processed ticket so that interrupted runs leave
    durable evidence of all completed work.  Non-fatal.
    """
    if output_dir is None:
        return
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "fixed": run_result.fixed,
            "partial": run_result.partial,
            "failed": run_result.failed,
            "stopped_early": run_result.stopped_early,
            "stop_reason": run_result.stop_reason,
            "tickets": [asdict(r) for r in run_result.results],
        }
        path = output_dir / "repair-summary.json"
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.debug("Fix 49.a: incremental summary write failed: %s", exc)


@dataclass
class RepairRunResult:
    """Aggregated outcome of a batch repair run.

    Attributes:
        results: Per-ticket ``RepairResult`` objects in execution order.
        total_tickets: Total number of tickets submitted.
        fixed: Count of tickets that reached ``status="fixed"``.
        partial: Count of tickets that reached ``status="partial"``.
        failed: Count of tickets that reached ``status="failed"``.
        stopped_early: ``True`` when the run was cut short by a stop condition.
        stop_reason: Human-readable explanation when ``stopped_early`` is set.
    """

    results: list[RepairResult] = field(default_factory=list)
    total_tickets: int = 0
    fixed: int = 0
    partial: int = 0
    failed: int = 0
    stopped_early: bool = False
    stop_reason: str = ""


_ENRICH_BATCH_SIZE = 4


def _build_repair_enrich_prompt(batch_tickets: list[RepairTicket]) -> str:
    """Build a prompt to enrich a batch of repair tickets with design decisions."""
    ticket_summaries = []
    for t in batch_tickets:
        ticket_summaries.append(
            f"- {t.ticket_id}: {t.finding_kind} in {t.file_path}\n"
            f"  Evidence: {'; '.join(t.evidence[:3])}"
        )
    return (
        "You are a senior engineer planning repair work. For each ticket below, "
        "add a DESIGN decision (approach and why), FOCUS (what to be careful about), "
        "and RESTRAINT (what NOT to change).\n\n"
        "Tickets to enrich:\n" + "\n".join(ticket_summaries) + "\n\n"
        "Output ONLY a JSON array with enriched tickets:\n"
        '[{"ticket_id": "REPAIR-001", "design": "...", "focus": "...", "restraint": "..."}, ...]\n'
        "No markdown fences. No explanation."
    )


def enrich_repair_tickets(
    tickets: list[RepairTicket],
    coder_config: Any,
    repo_path: Path,
) -> list[RepairTicket]:
    """Enrich repair tickets with design decisions in batches.

    Same pattern as the planner's batched enrichment. Each batch of 4
    tickets gets design/focus/restraint added to its remediation.
    """
    try:
        from saturnday.coder_adapter import call_coder
        from saturnday.run.planner import _extract_json
        from saturnday.context_assembler import assemble_messages
    except ImportError:
        return tickets

    enriched = list(tickets)

    for batch_start in range(0, len(tickets), _ENRICH_BATCH_SIZE):
        batch = tickets[batch_start:batch_start + _ENRICH_BATCH_SIZE]
        batch_ids = [t.ticket_id for t in batch]

        for attempt in range(1, 3):
            try:
                prompt = _build_repair_enrich_prompt(batch)
                if attempt > 1:
                    prompt += "\nIMPORTANT: Output ONLY raw JSON array. No markdown."
                messages = assemble_messages("You are a repair planner.", prompt, None)
                _t_enrich = time.monotonic()
                response = call_coder(coder_config, messages, repo_path)
                logger.info("TIMING %-30s %.2fs [batch=%s attempt=%d]", "enrich_llm_call", time.monotonic() - _t_enrich, batch_ids, attempt)
                result = _extract_json(response)

                if isinstance(result, list) and len(result) == len(batch):
                    for i, enrichment in enumerate(result):
                        idx = batch_start + i
                        design = enrichment.get("design", "")
                        focus = enrichment.get("focus", "")
                        restraint = enrichment.get("restraint", "")
                        if design or focus or restraint:
                            extra = f"\nDESIGN: {design}\nFOCUS: {focus}\nRESTRAINT: {restraint}"
                            if enriched[idx].remediation and isinstance(enriched[idx].remediation, dict):
                                enriched[idx].remediation["fix"] = enriched[idx].remediation.get("fix", "") + extra
                            else:
                                enriched[idx] = RepairTicket(
                                    ticket_id=enriched[idx].ticket_id,
                                    title=enriched[idx].title,
                                    severity=enriched[idx].severity,
                                    file_path=enriched[idx].file_path,
                                    line=enriched[idx].line,
                                    finding_kind=enriched[idx].finding_kind,
                                    evidence=enriched[idx].evidence,
                                    remediation={"fix": extra.strip()},
                                    group_key=enriched[idx].group_key,
                                )
                    logger.info("Enriched repair batch %s", batch_ids)
                    break
                else:
                    logger.warning("Repair enrichment batch %s attempt %d: parse failed", batch_ids, attempt)
            except Exception as exc:
                logger.warning("Repair enrichment batch %s attempt %d failed: %s", batch_ids, attempt, exc)

    return enriched


# ---------------------------------------------------------------------------
# Targeted scan factory
# ---------------------------------------------------------------------------

def _make_targeted_scan(target_file: str) -> Callable[[Path], list]:
    """Return a scan function scoped to a single *target_file*.

    The returned callable matches the ``scan_fn`` interface used by
    ``execute_repair`` (``Callable[[Path], list]``) but restricts the
    governance review to *target_file* only.  This is safe for file-local
    finding kinds where a single-file scan produces the same verdict as a
    full-repo scan.

    Args:
        target_file: Repo-relative path of the file to scan (e.g.
            ``"src/auth.py"``).

    Returns:
        A function that accepts a ``Path`` (repo root) and returns a list
        of ``Finding`` objects scoped to *target_file*.
    """
    def _targeted_scan(path: Path) -> list:
        from saturnday.interactive import _scan_for_repair
        findings, _ = _scan_for_repair(path, target_files=[target_file])
        return findings

    return _targeted_scan


def run_repair_batch(
    tickets: list[RepairTicket],
    skill_path: Path,
    coder_fn: Callable[[str, str, Path], str] | None = None,
    *,
    max_failures: int = 3,
    scan_fn: Callable[[Path], list] | None = None,
    cli_mode: bool = False,
    progress_fn: Callable[[str, str, str], None] | None = None,
    output_dir: Path | None = None,
) -> RepairRunResult:
    """Execute repair tickets in sequence with stop conditions.

    Iterates over *tickets* in order, calling ``execute_repair`` for each one.
    Stops early when consecutive failures reach *max_failures* to avoid
    wasting coder calls on a broken skill directory.

    Args:
        tickets: Ordered list of ``RepairTicket`` objects to execute.
        skill_path: Absolute path to the skill directory root.
        coder_fn: Callable with signature ``(prompt: str, file_path: str,
            repo_path: Path) -> str`` passed through to ``execute_repair``.
            If ``None`` all tickets will fail immediately (no coder available).
        max_failures: Maximum allowed consecutive failures before the run
            stops early.  Must be >= 1.  Default is 3.

    Returns:
        ``RepairRunResult`` with per-ticket outcomes and aggregate counters.
    """
    if max_failures < 1:
        max_failures = 1
    _t_batch = time.monotonic()
    run_result = RepairRunResult(total_tickets=len(tickets))
    # Track failures per finding kind so one broken kind doesn't block others
    kind_failures: dict[str, int] = {}
    skipped_kinds: set[str] = set()

    for ticket in tickets:
        # Skip kinds that have failed too many times
        if ticket.finding_kind in skipped_kinds:
            run_result.failed += 1
            skip_result = RepairResult(
                ticket_id=ticket.ticket_id,
                status="failed",
                findings_before=0,
                findings_after=0,
                error=f"Skipped — {ticket.finding_kind} failed {max_failures} times",
            )
            run_result.results.append(skip_result)
            if progress_fn:
                progress_fn(ticket.ticket_id, ticket.finding_kind, "SKIP")
            continue

        if progress_fn:
            progress_fn(ticket.ticket_id, ticket.finding_kind, "RUNNING")
        logger.info(
            "Repair %s: %s in %s",
            ticket.ticket_id,
            ticket.finding_kind,
            ticket.file_path,
        )

        # For file-local finding kinds, scope the governance scan to the
        # single target file instead of running the full-repo review.
        # This is safe because file-local checks produce verdicts from
        # individual file content alone; scanning the rest of the repo
        # adds no information and wastes considerable time.
        # Repo-level kinds always use the original full-repo scan_fn.
        effective_scan_fn = scan_fn
        if (
            scan_fn is not None
            and ticket.file_path
            and is_file_local(ticket.finding_kind)
        ):
            effective_scan_fn = _make_targeted_scan(ticket.file_path)
            logger.debug(
                "run_repair_batch: file-local kind=%s — using targeted scan for %s",
                ticket.finding_kind,
                ticket.file_path,
            )

        _t_ticket = time.monotonic()
        result = execute_repair(ticket, skill_path, coder_fn, scan_fn=effective_scan_fn, cli_mode=cli_mode)
        logger.info("TIMING %-30s %.2fs [%s]", "repair_ticket", time.monotonic() - _t_ticket, ticket.ticket_id)
        run_result.results.append(result)

        if result.status == "fixed":
            run_result.fixed += 1
            kind_failures.pop(ticket.finding_kind, None)
            # Fix 49.a: commit the fix immediately so interruption
            # cannot discard it.  Skip commit for pre-scan "already
            # clean" results (findings_before == 0) — no code changed.
            if result.findings_before > 0:
                # Fix C: commit only the ticket's target file(s) — never
                # ``git add -A``.  One ticket = one target file.
                scoped = [ticket.file_path] if ticket.file_path else []
                _commit_repair_fix(
                    skill_path, ticket.ticket_id, ticket.finding_kind,
                    scoped_files=scoped,
                )
        elif result.status == "partial":
            run_result.partial += 1
            kind_failures.pop(ticket.finding_kind, None)
        else:
            run_result.failed += 1
            kind_failures[ticket.finding_kind] = kind_failures.get(ticket.finding_kind, 0) + 1
            if kind_failures[ticket.finding_kind] >= max_failures:
                skipped_kinds.add(ticket.finding_kind)
                logger.warning(
                    "Skipping remaining %s tickets after %d failures",
                    ticket.finding_kind, max_failures,
                )

        # Fix 49.a: persist progress after every processed ticket
        _write_incremental_summary(run_result, output_dir)

        status_str = result.status.upper()
        if result.error:
            status_str = f"{status_str} ({result.error})"
        if progress_fn:
            progress_fn(ticket.ticket_id, ticket.finding_kind, status_str)
        logger.info(
            "  %s -> %s (before=%d, after=%d)",
            ticket.ticket_id,
            result.status,
            result.findings_before,
            result.findings_after,
        )

    logger.info("TIMING %-30s %.2fs", "repair_batch_total", time.monotonic() - _t_batch)
    return run_result


def run_repair_role_passes(
    run_result: RepairRunResult,
    output_dir: Path,
    skill_path: Path,
    *,
    coder_config: Any,
) -> None:
    """Run post-repair role passes: repo_analyst, governance_judge, definition_of_done, evidence_gate.

    Writes JSON artefacts for each role pass to *output_dir*.  All failures
    are caught and logged as warnings so callers are never blocked.

    Args:
        run_result: Aggregated result of the completed repair batch.
        output_dir: Directory to write role-pass JSON artefacts.
        skill_path: Path to the skill directory (used as repo_path).
        coder_config: :class:`~saturnday._types.CoderConfig` instance.
    """
    try:
        import json
        from saturnday.role_modes import invoke_role

        output_dir.mkdir(parents=True, exist_ok=True)

        # Repo analyst pass
        logger.info("--- stage: repo analysis ---")
        analyst_task = (
            f"Analyse the state of: {skill_path}\n"
            f"A repair run is about to be evaluated.\n"
            f"Fixed: {run_result.fixed}, Partial: {run_result.partial}, Failed: {run_result.failed}\n"
            f"Assess the repo state after these repairs."
        )
        _t_role = time.monotonic()
        analyst_result = invoke_role(
            "repo_analyst",
            analyst_task,
            coder_config=coder_config,
            repo_path=skill_path,
        )
        logger.info("TIMING %-30s %.2fs", "role_repo_analyst", time.monotonic() - _t_role)
        analyst_path = output_dir / "role-pass-repo-analyst.json"
        analyst_path.write_text(
            json.dumps(
                {
                    "role": analyst_result.role,
                    "success": analyst_result.success,
                    "output": analyst_result.output,
                    "error": analyst_result.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Governance judge pass
        logger.info("--- stage: governance review ---")
        gov_task = (
            f"Repair run completed on: {skill_path}\n"
            f"Fixed: {run_result.fixed}, Partial: {run_result.partial}, Failed: {run_result.failed}\n"
            f"Stopped early: {run_result.stopped_early}\n"
            f"Assess whether these repairs are governance-compliant. "
            f"Also check: do README.md and SKILL.md accurately describe what the code actually does? "
            f"Flag any mismatch between documented behaviour and actual code behaviour."
        )
        _t_role = time.monotonic()
        gov_result = invoke_role(
            "governance_judge",
            gov_task,
            coder_config=coder_config,
            repo_path=skill_path,
        )
        logger.info("TIMING %-30s %.2fs", "role_governance_judge", time.monotonic() - _t_role)
        gov_path = output_dir / "role-pass-governance-judge.json"
        gov_path.write_text(
            json.dumps(
                {
                    "role": gov_result.role,
                    "success": gov_result.success,
                    "output": gov_result.output,
                    "error": gov_result.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Definition of done pass
        logger.info("--- stage: completion check ---")
        dod_task = (
            f"Repair run on: {skill_path}\n"
            f"Fixed: {run_result.fixed}, Partial: {run_result.partial}, Failed: {run_result.failed}\n"
            f"Total tickets: {run_result.total_tickets}\n"
            f"Assess whether the repair work meets the definition of done."
        )
        _t_role = time.monotonic()
        dod_result = invoke_role(
            "definition_of_done",
            dod_task,
            coder_config=coder_config,
            repo_path=skill_path,
        )
        logger.info("TIMING %-30s %.2fs", "role_definition_of_done", time.monotonic() - _t_role)
        dod_path = output_dir / "role-pass-dod.json"
        dod_path.write_text(
            json.dumps(
                {
                    "role": dod_result.role,
                    "success": dod_result.success,
                    "output": dod_result.output,
                    "error": dod_result.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        # Evidence gate pass
        logger.info("--- stage: evidence check ---")
        evidence_files = [
            str(p.relative_to(output_dir))
            for p in output_dir.rglob("*.json")
        ]
        eg_task = (
            f"Repair evidence directory: {output_dir}\n"
            f"Files: {', '.join(evidence_files[:20])}\n"
            f"Fixed: {run_result.fixed}, Failed: {run_result.failed}\n"
            f"Assess whether evidence is sufficient for this repair run."
        )
        _t_role = time.monotonic()
        eg_result = invoke_role(
            "evidence_gate",
            eg_task,
            coder_config=coder_config,
            repo_path=skill_path,
        )
        logger.info("TIMING %-30s %.2fs", "role_evidence_gate", time.monotonic() - _t_role)
        eg_path = output_dir / "role-pass-evidence-gate.json"
        eg_path.write_text(
            json.dumps(
                {
                    "role": eg_result.role,
                    "success": eg_result.success,
                    "output": eg_result.output,
                    "error": eg_result.error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception as exc:  # noqa: BLE001
        logger.warning("Post-repair role passes failed: %s", exc)
