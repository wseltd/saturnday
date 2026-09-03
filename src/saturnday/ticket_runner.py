"""Main orchestrator: execute a project plan ticket by ticket.

For each ticket (in dependency order):
1. Skip if dependencies not all completed.
2. Build system prompt (standards + coder personality) — cached.
3. Build ticket prompt (goal + scope + project state + plan notes).
4. Call the coder backend.
5. Detect changed files (git status for CLI, FILE blocks for API).
6. Stage only the current ticket's changed files.
7. Run saturnday governance on staged changes.
8. Filter governance findings to only current ticket's files (ignore pre-existing).
9. If PASS → git commit, update project state, record evidence.
10. If FAIL → git reset, retry with governance findings (max 2 retries).
11. If exhausted → skip ticket, record failure.
"""

from __future__ import annotations

import json as _json_mod
import logging
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from saturnday.run.safe_shell import ShellPolicyViolation, safe_subprocess_run

from saturnday._exceptions import (
    GIT_STATE_ERROR_MARKER,
    CloudCoreError,
    CoderAPIError,
    GitStateError,
    GovernanceError,
    PatchApplicationError,
    PatchExtractionError,
    ScopeViolationError,
)
from saturnday._types import CoderConfig, RunResult, TicketResult, TicketSpec
from saturnday import capability_registry
from saturnday.coder_adapter import call_coder, is_cli_backend
from saturnday.shared.backend_auth import capability_matrix, detect_auth_mode
from saturnday.context_assembler import (
    assemble_messages,
    build_system_prompt,
    build_ticket_prompt,
)
from saturnday.run.evidence import (
    TicketEvidence,
    compute_run_analytics,
    write_analytics,
    write_phase_summary,
    write_run_metadata,
    write_run_summary,
    write_ticket_evidence,
)
from saturnday.run.run_ledger import (
    RunLedger,
    create_ledger_from_plan,
    write_ledger_snapshot,
)
from saturnday.patch_extractor import (
    apply_file_blocks,
    apply_unified_diff,
    extract_changes,
)
from saturnday.post_checks import run_post_checks
from saturnday.run.lessons import (
    Lesson,
    format_lessons_for_prompt,
    init_db,
    init_memory_db,
    load_lessons,
    store_lesson,
)
from saturnday.project_state import (
    ProjectState,
    generate_context_summary,
    load_state,
    save_state,
    update_state,
)

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2

# Module-level progress log path — set by run_plan(), used by _log_progress()
_progress_log_path: Path | None = None
# Fix 54: per-project gap log for Saturnday self-diagnostic signals (JSONL)
_gaps_log_path: Path | None = None


def _log_progress(msg: str) -> None:
    """Write a progress message to both logger and the progress log file."""
    logger.info(">> %s", msg)
    _path = _progress_log_path  # snapshot the global
    if _path and msg:
        try:
            with open(str(_path), "a", encoding="utf-8") as f:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                f.write(f"[{ts}] {msg}\n")
                f.flush()
        except Exception as exc:
            logger.debug("Progress log write failed: %s", exc)


def _log_gap(category: str, code: str, message: str, **fields: object) -> None:
    """Write a structured Saturnday self-diagnostic gap signal (JSONL).

    Only for genuine Saturnday limitations — not generic project events.
    Written to ``<project>/.saturnday/gaps.log``.  No-op if path not set.
    """
    logger.info("SATURNDAY-GAP [%s] [%s] %s", category, code, message)
    _path = _gaps_log_path
    if not _path:
        return
    try:
        import json as _gap_json
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "category": category,
            "code": code,
            "message": message,
        }
        entry.update(fields)
        with open(str(_path), "a", encoding="utf-8") as f:
            f.write(_gap_json.dumps(entry) + "\n")
            f.flush()
    except Exception as exc:
        logger.debug("Gap log write failed: %s", exc)


_PROGRESS_SYSTEM_PROMPT = (
    "You write brief progress messages for Saturnday, a governance tool that "
    "runs 50+ checks on AI-generated code. These checks catch things that a "
    "direct Claude Code, Codex, or Cursor session would NOT catch:\n\n"
    "SECURITY (19 Python + 19 TypeScript checks): SQL injection, auth bypass, "
    "CSRF, XSS, hardcoded secrets (AWS keys, OpenAI keys, GitHub tokens, "
    "private keys), WebSocket auth, OAuth flow integrity, token handling "
    "(expiry, revocation, weak randomness), rate limiting, IDOR, user "
    "enumeration, cookie security, frontend secret exposure.\n\n"
    "AI-SPECIFIC: Hallucinated imports (packages that don't exist on npm/PyPI), "
    "no-assert tests, tautological assertions, dead code (cross-file), "
    "placeholder stubs, dependency declaration verification.\n\n"
    "QUALITY: Syntax validation, import resolution, Python version compatibility, "
    "typosquat detection, project hygiene (README/LICENSE), blast radius.\n\n"
    "Rules:\n"
    "- Frame messages as: 'An AI coder without Saturnday would have...' then "
    "explain what the ungoverned AI coder typically does wrong in this situation.\n"
    "- One to two sentences, max 400 characters.\n"
    "- Be specific to the project. Reference actual files, functionality, and "
    "the ticket goal. Generic messages are useless.\n"
    "- No markdown formatting.\n"
    "- Vary your style. Sometimes lead with the consequence, sometimes with "
    "the contrast, sometimes with what was prevented.\n"
    "- Real-world consequences to reference when relevant: exposed API keys "
    "causing financial loss, SQL injection enabling data theft, missing auth "
    "allowing account takeover, fake tests giving false confidence, "
    "hallucinated imports crashing on deploy.\n"
    "- The user is waiting for a governed build. These messages should make "
    "the wait feel worth it."
)


def _generate_progress_message(
    coder_config,
    repo_path: Path,
    prompt: str,
) -> str:
    """Generate a contextual progress message via LLM.

    Returns empty string on any failure — never blocks the workflow.
    CLI backends are skipped entirely: spawning a full agent process
    for a cosmetic message is too expensive and wastes API quota.
    """
    if coder_config.backend in ("codex-cli", "cursor-cli"):
        return ""
    try:
        from saturnday.coder_adapter import call_coder
        messages = [
            {"role": "system", "content": _PROGRESS_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        response = call_coder(coder_config, messages, repo_path)
        return response.strip()[:500]
    except Exception as exc:
        logger.debug("Progress message generation failed: %s", exc)
        return ""


def _detect_project_languages(repo_path: Path) -> frozenset[str]:
    """Detect programming languages in the project from file extensions.

    Returns a frozenset of language names (e.g. ``{"python", "typescript"}``).
    Used to filter language-specific standards.
    """
    langs: set[str] = set()
    _LANG_MAP = {
        ".py": "python", ".pyi": "python",
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".mjs": "javascript", ".cjs": "javascript",
        ".sh": "shell", ".bash": "shell",
    }
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_path),
            capture_output=True, text=True, check=False, timeout=5,
        )
        if result.returncode == 0:
            for f in result.stdout.strip().splitlines():
                ext = Path(f).suffix.lower()
                if ext in _LANG_MAP:
                    langs.add(_LANG_MAP[ext])
    except Exception:
        pass
    # Also check for pyproject.toml, package.json, tsconfig.json
    if (repo_path / "pyproject.toml").is_file() or (repo_path / "setup.py").is_file():
        langs.add("python")
    if (repo_path / "package.json").is_file():
        langs.add("javascript")
    if (repo_path / "tsconfig.json").is_file():
        langs.add("typescript")
    logger.debug("Detected project languages: %s", langs)
    return frozenset(langs)


def _list_repo_files(repo_path: Path, limit: int = 50) -> list[str]:
    """List repo files via git ls-files or os.walk fallback.

    Args:
        repo_path: Path to the repository root.
        limit: Maximum number of file paths to return.

    Returns:
        List of relative file path strings, capped at ``limit``.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "ls-files"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            files = [f for f in r.stdout.strip().split("\n") if f]
            return files[:limit]
    except (subprocess.SubprocessError, OSError):
        pass
    # Fallback: walk directory tree
    result: list[str] = []
    for root, _, files in os.walk(repo_path):
        for f in files:
            if len(result) >= limit:
                return result
            rel = os.path.relpath(os.path.join(root, f), repo_path)
            if not rel.startswith("."):
                result.append(rel)
    return result


PROMPT_SOFT_THRESHOLD = 7500
PROMPT_HARD_THRESHOLD = 9000
# Fix 18: last-resort split threshold — clearly higher than PROMPT_HARD_THRESHOLD.
# A ticket whose prompt consistently exceeds this size is a candidate for
# conservative post-exhaustion splitting.
OVERSIZE_SPLIT_THRESHOLD = 15000



def _write_accepted_dod(evidence_dir: Path, plan_data: dict) -> None:
    """Write the accepted Definition of Done into the per-run evidence dir.

    Called once at the start of ``run_plan``, before any ticket executes,
    so the artifact survives even if the process crashes mid-run.
    """
    from datetime import datetime, timezone
    evidence_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project_id": plan_data.get("project_id", ""),
        "governing_goal": plan_data.get("governing_goal", ""),
        "required_outcomes": plan_data.get("required_outcomes", []),
        "proof_expectations": plan_data.get("proof_expectations", []),
        "exclusions": plan_data.get("exclusions", []),
        "constraints": plan_data.get("constraints", []),
        "definition_of_done_markers": plan_data.get("definition_of_done", []),
        "user_edited": plan_data.get("_dod_user_edited", False),
    }
    try:
        dod_path = evidence_dir / "accepted-dod.json"
        dod_path.write_text(
            __import__("json").dumps(artifact, indent=2), encoding="utf-8",
        )
        logger.info("Accepted DoD artifact written to %s", dod_path)
    except Exception as exc:
        logger.warning("Failed to write accepted DoD artifact: %s", exc)


def _generate_run_id() -> str:
    """Generate a collision-resistant run ID.

    Format: ``run_{YYYYMMDDTHHMMSSZ}_{pid}_{hex8}``

    The combination of UTC timestamp, process ID, and 8 random hex chars
    makes same-second collisions from parallel terminals negligible.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pid = os.getpid()
    rand = os.urandom(4).hex()  # 8 hex chars = 4 bytes
    return f"run_{ts}_{pid}_{rand}"


def _write_latest_pointer(parent_dir: Path, run_id: str, run_dir: Path) -> None:
    """Write a ``latest.json`` convenience pointer in *parent_dir*.

    This is an **operator convenience only** — it must never be used as an
    unvalidated source of truth for resume behaviour.  Resume logic must
    still check project_id and plan compatibility.

    Args:
        parent_dir: The parent directory (e.g. ``.saturnday/run/``).
        run_id: The run identifier string.
        run_dir: The full path to this run's evidence directory.
    """
    parent_dir.mkdir(parents=True, exist_ok=True)
    pointer = parent_dir / "latest.json"
    data = {
        "run_id": run_id,
        "path": str(run_dir),
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        pointer.write_text(
            _json_mod.dumps(data, indent=2) + "\n", encoding="utf-8",
        )
        logger.debug("Wrote latest pointer: %s", pointer)
    except OSError as exc:
        logger.warning("Could not write latest.json: %s", exc)


def _read_latest_pointer(parent_dir: Path) -> dict | None:
    """Read ``latest.json`` from *parent_dir*, returning the parsed dict or None."""
    pointer = parent_dir / "latest.json"
    if not pointer.is_file():
        return None
    try:
        return _json_mod.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, _json_mod.JSONDecodeError) as exc:
        logger.debug("Could not read latest.json: %s", exc)
        return None


def run_plan(
    plan_path: str | Path,
    repo_path: str | Path,
    coder_config: CoderConfig,
    standards_dir: str | Path,
    output_dir: str | Path | None = None,
    skip_tickets: frozenset[str] | None = None,
    auto_repair: bool = False,
    lessons_db: str | Path | None = None,
    progress_callback: Callable[[str, str, int, str], None] | None = None,
    role_passes: bool = True,
    approve_acceptance_setup: bool = False,
    work_branch: str | None = None,
    work_branch_resume_recorded: str | None = None,
    proof_answers: dict[str, str] | None = None,
    max_consecutive_failures: int | None = None,
) -> RunResult:
    """Execute a project plan end to end.

    Args:
        plan_path: Path to the plan JSON file.
        repo_path: Path to the target repository.
        coder_config: Backend configuration for the AI coder.
        standards_dir: Path to the engineering standards directory.
        output_dir: Directory for evidence output (default: ``<repo>/.saturnday/``).
        skip_tickets: Ticket IDs to treat as pre-completed (used by resume and
            rerun-failed).  Skipped tickets are added to the ``completed`` set
            so that downstream dependency resolution still works.
        auto_repair: If ``True``, attempt one repair pass after all retries
            for a ticket are exhausted.  The repair reuses the final governance
            findings as context for one additional coder attempt, then
            re-runs governance.  If that attempt passes, the ticket is
            committed as PASS.  No effect when a ticket passes normally.
        lessons_db: Path to the SQLite lessons database.  When provided,
            lessons from past failures are loaded and injected into the
            coder prompt before each ticket, and new lessons are recorded
            when a ticket exhausts all retries.  When ``None`` (the default),
            the lessons subsystem is not activated.
        progress_callback: Optional callable invoked at each ticket state
            transition.  Signature:
            ``(ticket_id: str, phase_name: str, attempt: int, status: str) -> None``
            where ``status`` is one of ``RUNNING``, ``PASS``, ``FAIL``,
            ``SKIP``, ``RETRY``.  ``attempt`` is 0 for the initial
            RUNNING/SKIP notification and the attempt number (1-based) for
            final dispositions.
        role_passes: If ``True`` and ``output_dir`` is set, run a DoD
            evaluation pass and an evidence-gate pass after the run
            completes.  Results are written as ``role-pass-dod.json`` and
            ``role-pass-evidence-gate.json`` inside ``output_dir``.
            Failures are logged as warnings and never raise.

    Returns:
        A ``RunResult`` summarizing the full execution.
    """
    from saturnday.plan_parser import load_plan

    _t_run_start = time.monotonic()
    _stage_timings: list[tuple[str, float]] = []

    repo_path = Path(repo_path).resolve()
    standards_dir = Path(standards_dir).resolve()
    plan = load_plan(plan_path)

    # I.4 — opt-in work-branch pre-flight.  Runs BEFORE any git writes so
    # a refusal cleanly aborts without touching repo state.  Populates
    # _work_branch_ctx which is folded into the final RunResult.
    _work_branch_ctx = None
    if work_branch is not None or work_branch_resume_recorded is not None:
        from saturnday.run.work_branch import (
            WorkBranchRefused, pre_flight_create, pre_flight_resume,
            resolve_work_branch_arg,
        )
        try:
            if work_branch is not None:
                # Explicit create path — this run owns the branch.
                resolved_name = resolve_work_branch_arg(work_branch, "run")
                _work_branch_ctx = pre_flight_create(repo_path, resolved_name)
            else:
                # Resume path — continue on the recorded branch (if the
                # operator didn't pass --work-branch themselves).
                _work_branch_ctx = pre_flight_resume(
                    repo_path, work_branch_resume_recorded,
                )
            logger.info(
                "I.4: active work branch %r (parent %s, auto_created=%s)",
                _work_branch_ctx.work_branch,
                _work_branch_ctx.parent_branch or "(resumed)",
                _work_branch_ctx.auto_created,
            )
        except WorkBranchRefused as exc:
            # Propagate upward — the CLI translates to a non-zero exit
            # with the operator-facing message.
            raise RuntimeError(str(exc)) from None

    # Load the raw plan dict for evidence recording — write_run_summary needs
    # the plan as a plain dict to embed governance context in run-summary.json.
    _plan_data_dict: dict | None = None
    try:
        import json as _plan_json
        _plan_data_dict = _plan_json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except Exception:
        pass  # Non-blocking — evidence fields simply absent if load fails

    _run_parent: Path | None = None
    _run_id: str | None = None
    if output_dir is None:
        # Each code-mode run gets its own evidence directory to prevent
        # overwrite collisions.  latest.json is written AFTER auto-resume
        # reads it (see below) so the pointer still refers to the prior run
        # during discovery.
        _run_id = _generate_run_id()
        _run_parent = repo_path / ".saturnday" / "run"
        output_dir = _run_parent / _run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        # NOTE: _write_latest_pointer is called AFTER auto-resume below
    else:
        output_dir = Path(output_dir).resolve()

    # Write accepted DoD artifact into the per-run evidence directory before
    # any ticket execution begins.  This ensures the artifact survives even
    # if the process crashes mid-run.
    if _plan_data_dict:
        _write_accepted_dod(output_dir, _plan_data_dict)

    # Add Saturnday entries to .git/info/exclude (repo-local, not committed)
    _t0 = time.monotonic()
    _ensure_git_exclude(repo_path)

    # Create project venv if none exists — isolates pip install from system python
    _ensure_project_venv(repo_path)
    _stage_timings.append(("setup_git_venv", time.monotonic() - _t0))
    logger.info("TIMING %-30s %.2fs", "setup_git_venv", _stage_timings[-1][1])

    # Load or initialize project state
    state = load_state(repo_path) or ProjectState(project_id=plan.project_id)
    state.project_id = plan.project_id

    # Detect project languages for language-aware standards loading
    _t0 = time.monotonic()
    _project_langs = _detect_project_languages(Path(repo_path))

    # Build system prompt once (cached, language-filtered)
    # compact_prompts=True (e.g. local-120b): use the digest (~2K) to stay
    # within the model's practical context window instead of the full corpus.
    if coder_config.compact_prompts:
        from saturnday.standards_digest import STANDARDS_DIGEST
        system_prompt = STANDARDS_DIGEST
    else:
        system_prompt = build_system_prompt(str(standards_dir), project_languages=_project_langs)

    # For CLI backends: write full standards to .saturnday/standards.md and
    # return a compact prompt that references the file.  API backends keep the
    # full inline prompt unchanged.
    if is_cli_backend(coder_config):
        from saturnday.context_assembler import write_standards_file
        _sd = repo_path / ".saturnday"
        _sd.mkdir(parents=True, exist_ok=True)
        system_prompt = write_standards_file(
            str(standards_dir),
            _sd / "standards.md",
            project_languages=_project_langs,
        )
        # Fix 17: write plan notes to file so retry prompts can reference it
        # instead of repeating the full inline text on every attempt.
        if plan.notes:
            try:
                (_sd / "plan-notes.md").write_text(plan.notes, encoding="utf-8")
                logger.debug("Wrote plan-notes.md (%d chars)", len(plan.notes))
            except OSError as exc:
                logger.warning("Failed to write plan-notes.md: %s", exc)

    _stage_timings.append(("setup_prompt_standards", time.monotonic() - _t0))
    logger.info("TIMING %-30s %.2fs", "setup_prompt_standards", _stage_timings[-1][1])

    # Create run ledger from plan fields
    all_ticket_ids = tuple(t.ticket_id for t in plan.tickets)
    ledger = create_ledger_from_plan(
        definition_of_done=plan.definition_of_done,
        stop_conditions=plan.stop_conditions,
        max_project_tickets=plan.max_project_tickets,
        phases=plan.phases,
        ticket_ids=all_ticket_ids,
        consecutive_failure_limit=max_consecutive_failures,
    )

    # Auto-resume: discover prior run via latest.json, then validate safety
    # conditions (project_id match) before skipping any tickets.
    # latest.json is a convenience pointer only — safety validation is mandatory.
    if output_dir and skip_tickets is None:
        _resume_parent = _run_parent or (repo_path / ".saturnday" / "run")
        _latest = _read_latest_pointer(_resume_parent)
        _prior_ledger: Path | None = None

        if _latest and _latest.get("path"):
            _prior_run_dir = Path(_latest["path"])
            _candidate = _prior_run_dir / "evidence" / "run" / "ledger.json"
            if _candidate.is_file():
                _prior_ledger = _candidate

        # Fallback: check the current output_dir itself (covers explicit
        # --output-dir passed by CLI resume, and legacy shared-path layout)
        if _prior_ledger is None:
            _candidate = Path(output_dir) / "evidence" / "run" / "ledger.json"
            if _candidate.is_file():
                _prior_ledger = _candidate

        if _prior_ledger is not None:
            try:
                _prior_data = _json_mod.loads(_prior_ledger.read_text(encoding="utf-8"))

                # Auto-resume: skip tickets that already passed in the prior run
                # ONLY if the prior run was for the SAME plan (matching project_id).
                # Different plans reuse ticket IDs (T001, T002...) so we must not
                # skip a new plan's T002 because an old plan's T002 passed.
                _prior_project_id = _prior_data.get("project_id", "")
                if _prior_project_id == plan.project_id:
                    # Fix 44.a: reconcile stale PENDING tickets that
                    # already have git commits before building skip set.
                    from saturnday.run.resume import (
                        _detect_committed_tickets,
                        _reconcile_stale_ledger,
                    )
                    _committed = _detect_committed_tickets(repo_path)
                    _reconciled = _reconcile_stale_ledger(
                        _prior_data, _committed, repo_path,
                        _prior_ledger.parent.parent.parent,  # evidence_dir
                    )

                    _prior_passed = frozenset(
                        tid for tid, ts in _prior_data.get("ticket_statuses", {}).items()
                        if ts.get("disposition") in ("PASS", "CODED_UNGOVERNED")
                    )
                    if _prior_passed:
                        skip_tickets = _prior_passed
                        _prior_coded_ungoverned = frozenset(
                            tid for tid, ts in _prior_data.get("ticket_statuses", {}).items()
                            if ts.get("disposition") == "CODED_UNGOVERNED"
                        )
                        if _prior_coded_ungoverned:
                            logger.warning(
                                "Auto-resume: %d ticket(s) will be skipped that were coded "
                                "but not governed — code is on the branch but governance did "
                                "not clear it; use 'saturnday rerun-failed' to revisit: %s",
                                len(_prior_coded_ungoverned), sorted(_prior_coded_ungoverned),
                            )
                        logger.info(
                            "Auto-resume: %d tickets already completed, skipping them",
                            len(_prior_passed),
                        )
                else:
                    logger.info(
                        "Auto-resume: prior ledger is for project '%s', current is '%s' — not resuming",
                        _prior_project_id, plan.project_id,
                    )

                # Backup the prior ledger in place
                _backup = _prior_ledger.with_suffix(".json.bak")
                _prior_ledger.rename(_backup)
                logger.info("Backed up prior ledger to %s", _backup)
            except Exception:
                pass

    # NOW write latest.json — after auto-resume has read the prior pointer
    if _run_parent is not None and _run_id is not None:
        _write_latest_pointer(_run_parent, _run_id, output_dir)

    # Write run-level metadata before first ticket executes
    _auth_mode = detect_auth_mode(coder_config)
    _backend_caps = capability_matrix(coder_config.backend)
    write_run_metadata(
        coder_config,
        plan_path,
        output_dir,
        auth_mode=_auth_mode,
        backend_capabilities=_backend_caps,
    )

    # Durable audit copy: write the full plan JSON into this run's evidence
    # directory so it survives even if the working-copy plan file is deleted.
    try:
        _plan_source = Path(plan_path)
        if _plan_source.is_file():
            _plan_copy_dest = Path(output_dir) / "plan.json"
            _plan_copy_dest.write_text(
                _plan_source.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            logger.info("Copied plan to run evidence: %s", _plan_copy_dest)
    except OSError as _plan_copy_err:
        logger.warning("Could not copy plan to evidence: %s", _plan_copy_err)

    # Pre-run: repo analyst pass
    if role_passes and output_dir:
        try:
            from saturnday.role_modes import invoke_role
            import json as _json

            logger.info("--- stage: repo analysis ---")
            repo_files = _list_repo_files(Path(repo_path), limit=50)
            analyst_task = (
                f"Repository: {repo_path}\n"
                f"Project: {plan.project_id}\n"
                f"Tickets: {len(plan.tickets)}\n"
                f"Files ({len(repo_files)}):\n"
                + "\n".join(f"  {f}" for f in repo_files)
                + "\n\nAnalyse this repository and produce a concise summary covering:\n"
                  "1. Architecture and module boundaries (key packages, layers, entry points)\n"
                  "2. Local code style and idioms (naming conventions, patterns in use)\n"
                  "3. Existing test patterns (test framework, fixture approach, coverage style)\n"
                  "4. Dependencies already in use (runtime and dev deps, versions where notable)\n"
                  "This output will guide the coder for all subsequent tickets."
            )
            analyst_result = invoke_role(
                "repo_analyst",
                analyst_task,
                coder_config=coder_config,
                repo_path=Path(repo_path),
            )
            analyst_path = Path(output_dir) / "role-pass-repo-analyst.json"
            analyst_path.parent.mkdir(parents=True, exist_ok=True)
            analyst_path.write_text(
                _json.dumps(
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
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pre-run repo_analyst pass failed: %s", exc)

    # Open the lessons DB if opt-in was requested
    _lessons_conn: sqlite3.Connection | None = None
    _memory_conn: sqlite3.Connection | None = None
    ran_stages: set[str] = set()
    if lessons_db is not None:
        try:
            _lessons_conn = init_db(Path(lessons_db))
            logger.info("Lessons DB active: %s", lessons_db)
        except Exception as _exc:  # pragma: no cover
            logger.warning(
                "Failed to open lessons DB %s: %s — continuing without lessons",
                lessons_db, _exc,
            )
            _lessons_conn = None

        # Phase 1: open memory DB (same file, adds memory_items + rules tables)
        try:
            _memory_conn = init_memory_db(Path(lessons_db))
            logger.debug("Memory DB (Phase 1) active: %s", lessons_db)
        except Exception as _mem_exc:  # pragma: no cover
            logger.warning(
                "Failed to open memory DB %s: %s — continuing without memory",
                lessons_db, _mem_exc,
            )
            _memory_conn = None

        # Phase 4: staleness cleanup — mark expired/missing items before tickets run
        if _memory_conn is not None and capability_registry.is_available("memory_provider"):
            try:
                _mem_provider = capability_registry.get("memory_provider")
                _stale_count = _mem_provider.cleanup(_memory_conn, repo_path)
                if _stale_count:
                    logger.info(
                        "Staleness cleanup marked %d item(s) stale before run",
                        _stale_count,
                    )
                ran_stages.add("memory_provider")
            except Exception as _sc_exc:
                logger.debug("Staleness cleanup failed (non-fatal): %s", _sc_exc)
        elif _memory_conn is not None:
            logger.debug("Stage skipped: premium not registered (memory_provider/staleness)")

        # EXT1: memory dedup pass — merge duplicate items once before tickets run
        if _memory_conn is not None:
            try:
                from saturnday.run.lessons import run_dedup_pass
                _dedup_count = run_dedup_pass(_memory_conn)
                if _dedup_count:
                    logger.info("Memory dedup: %d item(s) merged", _dedup_count)
            except Exception as _dd_exc:
                logger.debug("Memory dedup pass skipped: %s", _dd_exc)

    completed: set[str] = set()
    ticket_results: list[TicketResult] = []

    logger.info(
        "Starting plan execution: %s (%d tickets)",
        plan.project_id, len(plan.tickets),
    )

    # Set up progress log file for live monitoring
    global _progress_log_path, _gaps_log_path
    _sat_dir = Path(repo_path) / ".saturnday"
    _sat_dir.mkdir(parents=True, exist_ok=True)
    _progress_log_path = _sat_dir / "progress.log"
    _gaps_log_path = _sat_dir / "gaps.log"
    try:
        if _progress_log_path.exists():
            with open(_progress_log_path, "a", encoding="utf-8") as _pf:
                _pf.write(f"\n{'=' * 60}\n")
                _pf.write(f"Resumed: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
                _pf.write(f"{'=' * 60}\n\n")
        else:
            _progress_log_path.write_text(
                f"Saturnday governed build: {plan.project_id}\n"
                f"Tickets: {len(plan.tickets)}\n"
                f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
                f"{'=' * 60}\n\n",
                encoding="utf-8",
            )
        logger.info(
            "\n"
            "  To see live governance insights, open another terminal and run:\n"
            "    cd %s && tail -f .saturnday/progress.log\n"
            "\n"
            "  This shows what an AI coder without Saturnday would have done wrong.\n",
            repo_path,
        )
    except Exception as _log_exc:
        logger.warning("Failed to set up progress log: %s", _log_exc)
        _progress_log_path = None  # type: ignore[assignment]

    # Progress message: run start
    _start_msg = _generate_progress_message(
        coder_config, Path(repo_path),
        f"Project '{plan.project_id}' starting with {len(plan.tickets)} tickets. "
        f"Brief: {plan.notes[:200] if plan.notes else 'not provided'}. "
        f"An AI coder without Saturnday would build this in one shot with no checks. "
        f"Write one sentence about what typically goes wrong when this type of project is built ungoverned.",
    )
    if _start_msg:
        _log_progress(_start_msg)

    for ticket in plan.tickets:
        # Pre-completed skip (resume / rerun-failed native filtering)
        if skip_tickets and ticket.ticket_id in skip_tickets:
            completed.add(ticket.ticket_id)
            logger.info(
                "Skipping %s (pre-completed via resume filter)", ticket.ticket_id,
            )
            continue

        # Ledger-level skip check (max tickets, prior stop condition)
        skip, skip_reason = ledger.should_skip_ticket(ticket.ticket_id)
        if skip:
            logger.info("Skipping %s: %s", ticket.ticket_id, skip_reason)
            ledger.record_ticket_result(ticket.ticket_id, "SKIP")
            ticket_results.append(TicketResult(
                ticket_id=ticket.ticket_id,
                disposition="SKIP",
                error=skip_reason,
            ))
            if progress_callback:
                progress_callback(ticket.ticket_id, "", 0, "SKIP")
            write_ledger_snapshot(ledger, output_dir)
            continue

        # Check dependencies
        unmet = [d for d in ticket.dependencies if d not in completed]
        if unmet:
            logger.warning(
                "Skipping %s: unmet dependencies %s",
                ticket.ticket_id, unmet,
            )
            ledger.record_ticket_result(ticket.ticket_id, "SKIP")
            ticket_results.append(TicketResult(
                ticket_id=ticket.ticket_id,
                disposition="SKIP",
                error=f"Unmet dependencies: {', '.join(unmet)}",
            ))
            if progress_callback:
                progress_callback(ticket.ticket_id, "", 0, "SKIP")
            write_ledger_snapshot(ledger, output_dir)
            continue

        # Analyze ticket complexity and split if needed.
        # Fix 19: atomic tickets must never be split — skip analysis entirely.
        if ticket.atomic:
            logger.info(
                "Ticket %s: atomic=True — pre-execution split suppressed",
                ticket.ticket_id,
            )
            sub_tickets = [ticket]
        else:
            from saturnday.ticket_splitter import analyze_and_split
            sub_tickets = analyze_and_split(
                ticket, coder_config, repo_path, plan.notes,
            )

        all_subs_passed = True
        for sub in sub_tickets:
            # Resolve phase name for progress reporting
            _phase_name = ""
            for _phase in plan.phases:
                if sub.ticket_id in _phase.ticket_ids:
                    _phase_name = _phase.name
                    break

            # Ledger-level skip check for sub-tickets
            skip, skip_reason = ledger.should_skip_ticket(sub.ticket_id)
            if skip:
                logger.info("Skipping %s: %s", sub.ticket_id, skip_reason)
                ledger.record_ticket_result(sub.ticket_id, "SKIP", parent_ticket_id=ticket.ticket_id)
                ticket_results.append(TicketResult(
                    ticket_id=sub.ticket_id,
                    disposition="SKIP",
                    error=skip_reason,
                ))
                if progress_callback:
                    progress_callback(sub.ticket_id, _phase_name, 0, "SKIP")
                all_subs_passed = False
                write_ledger_snapshot(ledger, output_dir)
                continue

            # Check sub-ticket dependencies (prior subs in this split)
            sub_unmet = [d for d in sub.dependencies if d not in completed]
            if sub_unmet:
                logger.warning("Skipping %s: unmet deps %s", sub.ticket_id, sub_unmet)
                ledger.record_ticket_result(sub.ticket_id, "SKIP", parent_ticket_id=ticket.ticket_id)
                ticket_results.append(TicketResult(
                    ticket_id=sub.ticket_id,
                    disposition="SKIP",
                    error=f"Unmet dependencies: {', '.join(sub_unmet)}",
                ))
                if progress_callback:
                    progress_callback(sub.ticket_id, _phase_name, 0, "SKIP")
                all_subs_passed = False
                write_ledger_snapshot(ledger, output_dir)
                continue

            if progress_callback:
                progress_callback(sub.ticket_id, _phase_name, 0, "RUNNING")

            # Fix 44.b: callback that persists ledger immediately after
            # a ticket's code is committed, BEFORE returning outward.
            # This closes the atomicity window between git commit and
            # ledger flush that caused stale-PENDING on interrupt.
            def _on_committed(tid: str, disposition: str, failure_category: str) -> None:
                ledger.record_ticket_result(
                    tid, disposition,
                    failure_category=failure_category,
                    parent_ticket_id=ticket.ticket_id,
                )
                ledger.update_phase_status(tid)
                write_ledger_snapshot(ledger, output_dir)
                logger.debug("Fix 44.b: ledger flushed immediately after commit for %s (%s)", tid, disposition)

            try:
                result = _run_ticket_with_retries(
                    ticket=sub,
                    repo_path=repo_path,
                    coder_config=coder_config,
                    system_prompt=system_prompt,
                    state=state,
                    plan_notes=plan.notes,
                    output_dir=output_dir,
                    max_retries=plan.default_retry_limit,
                    auto_repair=auto_repair,
                    lessons_conn=_lessons_conn,
                    memory_conn=_memory_conn,
                    project_id=plan.project_id,
                    ran_stages=ran_stages,
                    on_committed=_on_committed,
                )
            except KeyboardInterrupt:
                logger.info("Run interrupted by user (Ctrl+C) during %s", sub.ticket_id)
                _log_progress(f"✗ Run interrupted by user during {sub.ticket_id}")
                _git_reset_changes(repo_path)
                write_ledger_snapshot(ledger, output_dir)
                # Save partial summary so progress isn't lost
                _int_passed = sum(1 for r in ticket_results if r.disposition == "PASS")
                _int_failed = sum(1 for r in ticket_results if r.disposition == "FAIL")
                _int_skipped = sum(1 for r in ticket_results if r.disposition == "SKIP")
                _int_ungoverned = sum(1 for r in ticket_results if r.disposition == "CODED_UNGOVERNED")
                try:
                    write_run_summary(RunResult(
                        project_id=plan.project_id,
                        total_tickets=len(ticket_results),
                        passed=_int_passed, failed=_int_failed,
                        skipped=_int_skipped, coded_ungoverned=_int_ungoverned,
                        ticket_results=tuple(ticket_results),
                        stop_reason="interrupted",
                    ), output_dir, repo_path=repo_path)
                except Exception:
                    pass
                raise

            ticket_results.append(result)

            # Record in ledger and persist.
            # Pass parent_ticket_id so split sub-ticket failures count as
            # one consecutive failure signal per parent, not per sub-ticket.
            ledger.record_ticket_result(
                sub.ticket_id,
                result.disposition,
                failure_category=result.failure_category,
                parent_ticket_id=ticket.ticket_id,
            )
            ledger.update_phase_status(sub.ticket_id)
            write_ledger_snapshot(ledger, output_dir)

            # Write partial run summary after every ticket — if the process
            # crashes, the summary reflects all completed work up to this point.
            _partial_passed = sum(1 for r in ticket_results if r.disposition == "PASS")
            _partial_failed = sum(1 for r in ticket_results if r.disposition == "FAIL")
            _partial_skipped = sum(1 for r in ticket_results if r.disposition == "SKIP")
            _partial_ungoverned = sum(1 for r in ticket_results if r.disposition == "CODED_UNGOVERNED")
            _partial_result = RunResult(
                project_id=plan.project_id,
                total_tickets=len(ticket_results),
                passed=_partial_passed,
                failed=_partial_failed,
                skipped=_partial_skipped,
                coded_ungoverned=_partial_ungoverned,
                ticket_results=tuple(ticket_results),
            )
            try:
                write_run_summary(_partial_result, output_dir, repo_path=repo_path)
            except Exception:
                logger.debug("Partial run summary write failed")

            if progress_callback:
                progress_callback(sub.ticket_id, _phase_name, result.attempts, result.disposition)

            if result.disposition == "PASS":
                completed.add(sub.ticket_id)
                state = update_state(
                    state, sub.ticket_id, list(result.changed_files), repo_path,
                )
                save_state(state, repo_path)
                logger.info("Ticket %s PASSED", sub.ticket_id)
                # Progress message on first pass and every 2nd pass after
                _pass_count = sum(1 for _r in ticket_results if _r.disposition == "PASS") + 1
                if _pass_count == 1 or _pass_count % 2 == 0:
                    _pass_msg = _generate_progress_message(
                        coder_config, Path(repo_path),
                        f"Ticket {sub.ticket_id} passed governance. "
                        f"Goal: {sub.goal[:150] if sub.goal else 'N/A'}. "
                        f"Files: {', '.join(result.changed_files[:5])}. "
                        f"Project brief: {plan.notes[:100] if plan.notes else 'N/A'}. "
                        f"Write one sentence starting with 'An AI coder without Saturnday would have...' "
                        f"explaining what typically goes wrong with this type of code when ungoverned.",
                    )
                    if _pass_msg:
                        _log_progress(_pass_msg)
            else:
                logger.warning(
                    "Ticket %s %s: %s",
                    sub.ticket_id, result.disposition, result.error,
                )
                all_subs_passed = False

            # Check stop conditions after each ticket
            should_stop, stop_reason = ledger.check_stop_conditions(
                ticket_id=sub.ticket_id,
            )
            if should_stop:
                logger.warning("Stopping run: %s", stop_reason)
                _log_gap("EXECUTION", "consecutive_stop",
                         stop_reason, count=ledger.consecutive_failures,
                         limit=ledger.consecutive_failure_limit)
                write_ledger_snapshot(ledger, output_dir)
                break

        # Break outer loop if ledger says stop
        if ledger.stopped:
            break

        # Mark the original ticket as completed if all subs passed
        if all_subs_passed:
            completed.add(ticket.ticket_id)
            if len(sub_tickets) > 1:
                logger.info(
                    "Ticket %s PASSED (split into %d sub-tickets, all passed)",
                    ticket.ticket_id, len(sub_tickets),
                )

    # Auto-repair failed and ungoverned tickets before DoD evaluation.
    #
    # Fix E: narrow the scan and subsequent repair batch to the files the
    # failing tickets actually touched.  The previous implementation did a
    # full-repo scan and generated a repair ticket for every finding in the
    # whole tree — one failed run-mode ticket could trigger a 200-ticket
    # sweep across unrelated code.  The per-ticket retry auto-repair (a
    # different path at ``_attempt_auto_repair``) stays unchanged; this
    # narrowing only applies to the post-run sweep.
    failed_results = [
        r for r in ticket_results
        if r.disposition in ("FAIL", "CODED_UNGOVERNED")
    ]
    repair_candidates = len(failed_results)
    if repair_candidates > 0:
        # Union of files that failing tickets actually modified.
        failed_files: list[str] = []
        _seen: set[str] = set()
        for r in failed_results:
            for cf in r.changed_files:
                if cf and cf not in _seen:
                    _seen.add(cf)
                    failed_files.append(cf)

        if not failed_files:
            logger.info(
                "--- stage: auto-repair skipped — %d failed ticket(s) but no "
                "changed_files to scope the repair to; per-ticket retry "
                "auto-repair already handled the early-failure case ---",
                repair_candidates,
            )
        else:
            logger.info(
                "--- stage: auto-repair %d ticket(s) scoped to %d file(s): %s ---",
                repair_candidates, len(failed_files),
                ", ".join(failed_files[:8]) + (" …" if len(failed_files) > 8 else ""),
            )
            try:
                from saturnday.interactive import _scan_for_repair
                from saturnday.repair.repair_tickets import generate_repair_tickets
                from saturnday.repair.repair_runner import run_repair_batch

                findings, _ = _scan_for_repair(
                    Path(repo_path), target_files=list(failed_files)
                )
                if findings:
                    # Filter out policy exemptions — Fix 53.e: shared loader
                    from saturnday.policy_loader import load_policy as _lp, validated_expected_findings as _vef
                    _pol = _lp(Path(repo_path))
                    exempt_kinds: set[str] = _vef(_pol)
                    _ENV_KINDS = {"declared_not_installed", "package_not_importable"}
                    code_findings = [
                        f for f in findings
                        if f.kind not in _ENV_KINDS and f.kind not in exempt_kinds
                    ]
                    if code_findings:
                        repair_tickets = generate_repair_tickets(code_findings, repo_path=repo_path)
                        logger.info("Auto-repair: %d tickets from %d findings", len(repair_tickets), len(code_findings))

                        cli_mode = is_cli_backend(coder_config)
                        def _auto_repair_coder_fn(prompt: str, file_path: str, rp: Path) -> str:
                            return call_coder(coder_config, [
                                {"role": "user", "content": f"Fix this issue in {file_path}:\n{prompt}\nEdit the file directly. SCOPE CONSTRAINT: Fix ONLY this specific finding. Do NOT refactor surrounding code, add new features, change unrelated logic, or make improvements beyond the exact fix. One finding, one surgical fix, nothing else."}
                            ], rp, agent_mode=True if cli_mode else False)

                        # Fix E: keep per-ticket post-scans scoped to the
                        # same failed-files surface so the repair batch
                        # does not oscillate between file-local and
                        # repo-wide views.
                        _failed_files_snapshot = list(failed_files)
                        def _auto_repair_scan_fn(path: Path) -> list:
                            f2, _ = _scan_for_repair(
                                path, target_files=_failed_files_snapshot
                            )
                            return f2

                        repair_result = run_repair_batch(
                            repair_tickets, Path(repo_path), _auto_repair_coder_fn,
                            scan_fn=_auto_repair_scan_fn,
                            cli_mode=cli_mode,
                        )
                        logger.info(
                            "Auto-repair: fixed=%d partial=%d failed=%d",
                            repair_result.fixed, repair_result.partial, repair_result.failed,
                        )
            except Exception as exc:
                logger.warning("Auto-repair failed: %s", exc)

    # Evaluate definition of done
    dod_met = ledger.evaluate_definition_of_done()

    # Compute totals — use len(ticket_results) as denominator so sub-tickets
    # are counted consistently in both numerator and denominator.
    passed = sum(1 for r in ticket_results if r.disposition == "PASS")
    failed = sum(1 for r in ticket_results if r.disposition == "FAIL")
    skipped = sum(1 for r in ticket_results if r.disposition == "SKIP")
    coded_ungoverned = sum(1 for r in ticket_results if r.disposition == "CODED_UNGOVERNED")
    actual_total = len(ticket_results)

    _init_pr_status, _init_pr_source, _init_pr_narrative = (
        _initial_proof_resolution(plan)
    )
    # I.4: capture work-branch metadata for evidence.  When the feature
    # is not active, every field stays as its empty-string default and
    # the summary shape is identical to pre-I.4 runs.
    _wb_parent_branch = _work_branch_ctx.parent_branch if _work_branch_ctx else ""
    _wb_parent_head = _work_branch_ctx.parent_head_sha if _work_branch_ctx else ""
    _wb_work_branch = _work_branch_ctx.work_branch if _work_branch_ctx else ""
    _wb_auto_created = _work_branch_ctx.auto_created if _work_branch_ctx else False
    if _work_branch_ctx is not None:
        from saturnday.run.work_branch import current_head_sha
        _wb_final_head = current_head_sha(repo_path)
    else:
        _wb_final_head = ""

    run_result = RunResult(
        project_id=plan.project_id,
        total_tickets=actual_total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        coded_ungoverned=coded_ungoverned,
        ticket_results=tuple(ticket_results),
        definition_of_done_met=dod_met,
        stop_reason=ledger.stop_reason,
        evidence_dir=str(output_dir),
        proof_resolution_status=_init_pr_status,
        proof_resolution_source=_init_pr_source,
        proof_resolution_narrative=_init_pr_narrative,
        git_parent_branch=_wb_parent_branch,
        git_parent_head_sha=_wb_parent_head,
        git_work_branch=_wb_work_branch,
        git_work_branch_auto_created=_wb_auto_created,
        git_final_head_sha=_wb_final_head,
    )

    write_phase_summary(ledger, output_dir)
    write_run_summary(run_result, output_dir, ran_stages=ran_stages, plan_data=_plan_data_dict, repo_path=repo_path)
    write_ledger_snapshot(ledger, output_dir)

    analytics = compute_run_analytics(run_result)
    analytics["backend"] = coder_config.backend
    write_analytics(analytics, output_dir)
    logger.info(
        "RUN ANALYTICS | acceptance_rate=%.2f | avg_retries=%.2f | quality=%s | dod_met=%s",
        analytics["acceptance_rate"],
        analytics["avg_retries"],
        analytics["senior_quality_verdict"]["quality_level"],
        analytics["definition_of_done_met"],
    )

    # Write run metrics and comparison report (never blocks the pipeline)
    if capability_registry.is_available("run_metrics"):
        try:
            from saturnday.run.metrics import (
                compare_metrics as _compare_metrics,
                compute_run_metrics,
                format_metrics_report as _format_metrics_report,
                load_metrics,
                write_metrics,
            )
            _metrics = compute_run_metrics(run_result, Path(output_dir))
            write_metrics(_metrics, Path(output_dir))
            logger.debug("Run metrics written to %s/metrics.json", output_dir)

            # T019: compare with history and log the trend report
            _history = load_metrics(Path(output_dir))
            # Exclude the record we just appended (last item)
            _history_only = _history[:-1] if len(_history) > 1 else []
            _comparison = _compare_metrics(_metrics, _history_only)
            _report = _format_metrics_report(_comparison)
            logger.info("Metrics comparison:\n%s", _report)

            # Append the performance report to lessons.md if it was written
            _lessons_md_path = Path(output_dir) / "lessons.md"
            if _lessons_md_path.exists():
                try:
                    existing_md = _lessons_md_path.read_text(encoding="utf-8")
                    _lessons_md_path.write_text(
                        existing_md + "\n\n" + _report + "\n",
                        encoding="utf-8",
                    )
                except OSError as _md_append_exc:
                    logger.debug("Could not append metrics to lessons.md: %s", _md_append_exc)
            ran_stages.add("run_metrics")
        except Exception as _metrics_exc:
            logger.warning("Run metrics write failed: %s", _metrics_exc)
    else:
        logger.debug("Stage skipped: premium not registered (run_metrics)")

    # Write structured lessons.md from memory DB (never blocks the pipeline)
    if _memory_conn is not None and capability_registry.is_available("memory_provider"):
        try:
            from saturnday.run.lessons import write_lessons_file
            write_lessons_file(_memory_conn, Path(output_dir))
            logger.debug("Lessons markdown written to %s/lessons.md", output_dir)
        except Exception as _lessons_md_exc:
            logger.warning("Lessons markdown write failed: %s", _lessons_md_exc)

        # Phase 5: promote candidate rules that meet threshold (never blocks)
        try:
            from saturnday.run.lessons import promote_rules as _promote_rules
            _promoted = _promote_rules(_memory_conn)
            if _promoted:
                logger.info("Phase 5: %d rule(s) promoted after run", _promoted)
        except Exception as _promote_exc:
            logger.warning("Rule promotion failed: %s", _promote_exc)

    logger.info(
        "Plan complete: %d passed, %d failed, %d skipped | DoD met: %s | stop_reason: %s",
        passed, failed, skipped, dod_met, ledger.stop_reason or "none",
    )

    # Progress message: run complete
    _finding_kinds_seen = set()
    for _tr in ticket_results:
        for _f in (_tr.governance_findings or ()):
            _finding_kinds_seen.add(_f.get("kind", _f.get("message", "")))
    _complete_msg = _generate_progress_message(
        coder_config, Path(repo_path),
        f"Project '{plan.project_id}' complete. {passed} passed governance, "
        f"{coded_ungoverned} need review, {failed} failed. "
        f"Brief: {plan.notes[:150] if plan.notes else 'N/A'}. "
        f"Governance caught these kinds: {', '.join(sorted(_finding_kinds_seen)) if _finding_kinds_seen else 'none'}. "
        f"Write one sentence about what this project would look like if an AI coder had built it without any governance.",
    )
    if _complete_msg:
        _log_progress(_complete_msg)

    if role_passes and output_dir:
        try:
            from saturnday.role_modes import run_dod_check, invoke_role
            import json as _json

            logger.info("Running post-run role passes...")

            # DoD pass
            dod_result = run_dod_check(
                plan_path=Path(plan_path),
                evidence_dir=Path(output_dir),
                repo_path=Path(repo_path),
                coder_config=coder_config,
            )
            dod_path = Path(output_dir) / "role-pass-dod.json"
            dod_path.write_text(
                _json.dumps(
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

            # If LLM says DOD_MET or DOD_PARTIAL, upgrade the mechanical
            # assessment.  DOD_PARTIAL means functional goals are met even
            # though a ticket failed (e.g. T001 failed but T002 solved the
            # same problem).  The mechanical gate is too blunt for this.
            from saturnday.role_modes import extract_classification
            dod_classification = extract_classification(
                dod_result.output,
                ["DOD_MET", "DOD_PARTIAL", "DOD_NOT_MET", "DOD_BLOCKED_BY_MISSING_EVIDENCE"],
            )
            if dod_classification == "DOD_MET" and not dod_met:
                logger.info(
                    "LLM DoD says DOD_MET — upgrading mechanical gate (was NOT MET)",
                )
                dod_met = True
                from dataclasses import replace as _replace
                run_result = _replace(run_result, definition_of_done_met=True)
                write_run_summary(run_result, output_dir, ran_stages=ran_stages, plan_data=_plan_data_dict, repo_path=repo_path)
            elif dod_classification == "DOD_PARTIAL" and not dod_met:
                # Fix 12.A / G13: DOD_PARTIAL must not mechanically upgrade to met.
                # Partial completion is not full completion.  The mechanical gate
                # stays False so the run result truthfully reflects that the
                # definition of done was not fully satisfied.
                logger.warning(
                    "LLM DoD says DOD_PARTIAL — functional goals partially met "
                    "but mechanical gate remains NOT MET; repair is required",
                )

            # Fix 39 + Fix 74: mechanical guard — failed executable proofs override
            # DOD_MET regardless of LLM classification or mechanical gate outcome.
            # Fix 39 closed the per-ticket verify_cmd gap; Fix 74 closes the
            # plan-level acceptance_cmd gap (a failed acceptance_cmd combined
            # with an LLM DOD_MET verdict could previously upgrade dod_met=True
            # because the guard only consulted per-ticket verify_cmd_passed).
            _new_dod_met, _new_classification, _guard_reasons = (
                _apply_dod_mechanical_guard(dod_met, dod_classification, run_result)
            )
            if _guard_reasons:
                logger.warning(
                    "Fix 39/74 mechanical guard: %s — downgrading DOD_MET to DOD_NOT_MET",
                    "; ".join(_guard_reasons),
                )
                dod_met = _new_dod_met
                dod_classification = _new_classification
                from dataclasses import replace as _replace  # noqa: F811
                run_result = _replace(run_result, definition_of_done_met=False)
                write_run_summary(run_result, output_dir, ran_stages=ran_stages, plan_data=_plan_data_dict, repo_path=repo_path)

            # DoD enforcement — only trigger repair for DOD_NOT_MET.
            # DOD_PARTIAL means the functional goals are met but a ticket
            # failed (e.g. T001 failed, T002 solved same problem).  Repairing
            # in that case chases governance false positives like blast_radius.
            if dod_classification == "DOD_NOT_MET":
                logger.info("DoD is %s — triggering repair to address gaps", dod_classification)
                try:
                    from saturnday.interactive import _scan_for_repair
                    from saturnday.repair.repair_tickets import generate_repair_tickets
                    from saturnday.repair.repair_runner import run_repair_batch

                    dod_findings, _ = _scan_for_repair(Path(repo_path))
                    if dod_findings:
                        _ENV_KINDS_DOD = {"declared_not_installed", "package_not_importable"}
                        _BLAST_KINDS = {"excessive_blast_radius"}
                        # Filter out policy exemptions (mirrors auto-repair path)
                        _dod_policy_path = Path(repo_path) / ".saturnday-policy.yaml"
                        _dod_exempt_kinds: set[str] = set()
                        if _dod_policy_path.is_file():
                            # Fix 53.e: shared loader
                            from saturnday.policy_loader import load_policy as _lp2, validated_expected_findings as _vef2
                            _dod_pol = _lp2(Path(repo_path))
                            _dod_exempt_kinds = _vef2(_dod_pol)
                        dod_code_findings = [
                            f for f in dod_findings
                            if f.kind not in _ENV_KINDS_DOD
                            and f.kind not in _BLAST_KINDS
                            and f.kind not in _dod_exempt_kinds
                        ]
                        if dod_code_findings:
                            dod_repair_tickets = generate_repair_tickets(dod_code_findings, repo_path=repo_path)
                            logger.info("DoD repair: %d tickets from %d findings", len(dod_repair_tickets), len(dod_code_findings))
                            cli_mode_dod = is_cli_backend(coder_config)
                            def _dod_repair_fn(prompt, file_path, rp):
                                return call_coder(coder_config, [
                                    {"role": "user", "content": f"Fix this issue in {file_path}:\n{prompt}\nFollow governance rules. Edit directly. SCOPE CONSTRAINT: Fix ONLY this specific finding. Do NOT refactor, add features, or change unrelated code."}
                                ], rp, agent_mode=True if cli_mode_dod else False)
                            def _dod_scan_fn(path):
                                f2, _ = _scan_for_repair(path)
                                return f2
                            dod_repair_result = run_repair_batch(
                                dod_repair_tickets, Path(repo_path), _dod_repair_fn,
                                scan_fn=_dod_scan_fn, cli_mode=cli_mode_dod,
                            )
                            logger.info("DoD repair: fixed=%d failed=%d", dod_repair_result.fixed, dod_repair_result.failed)
                except Exception as exc:
                    logger.warning("DoD enforcement repair failed: %s", exc)
            elif dod_classification and dod_classification != "DOD_MET":
                logger.info("DoD is %s — not triggering repair (functional goals likely met)", dod_classification)

            # Plan-governance verdict extraction.
            # The DoD role pass now evaluates plan-governance in addition to the
            # per-ticket DoD.  Extract the PG classification and store it on
            # run_result so it propagates to run-summary.json and the UX.
            #
            # Evaluation path:
            #   1. No governance fields on plan → PLAN_GOVERNANCE_NO_GOVERNANCE immediately (no LLM call)
            #   2. Governance exists + PG token in DoD output → extract it (first-try path)
            #   3. Governance exists + PG token missing → focused PG-only retry (up to 3 attempts)
            #   4. Retry exhaustion → PLAN_GOVERNANCE_NOT_EVALUATED (explicit degraded state, not silent pass)
            #
            # PLAN_GOVERNANCE_NOT_MET listed first in candidates: it is the more specific token
            # and would otherwise be shadowed by PLAN_GOVERNANCE_MET (substring match).
            from saturnday.role_modes import _retry_pg_evaluation
            _has_pg_fields = bool(
                (_plan_data_dict or {}).get("governing_goal")
                or (_plan_data_dict or {}).get("required_outcomes")
            )

            if not _has_pg_fields:
                # No governance expectations — explicit no-governance state, skip LLM entirely.
                _pg_met: bool = True
                _pg_reason: str = "PLAN_GOVERNANCE_NO_GOVERNANCE"
            else:
                # Try to extract PG verdict from the existing DoD output first.
                _pg_candidates = ["PLAN_GOVERNANCE_NOT_MET", "PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NO_GOVERNANCE"]
                _pg_verdict = extract_classification(dod_result.output, _pg_candidates)

                if _pg_verdict in _pg_candidates:
                    # DoD output contained a valid PG token — use it directly.
                    if _pg_verdict in ("PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NO_GOVERNANCE"):
                        _pg_met = True
                        _pg_reason = _pg_verdict
                    else:
                        # PLAN_GOVERNANCE_NOT_MET — extract reason up to 200 chars.
                        _pg_met = False
                        _pg_reason = "PLAN_GOVERNANCE_NOT_MET"
                        if "PLAN_GOVERNANCE_NOT_MET" in dod_result.output:
                            _pg_idx = dod_result.output.index("PLAN_GOVERNANCE_NOT_MET")
                            _pg_reason = dod_result.output[_pg_idx : _pg_idx + 200].strip()
                else:
                    # PG token missing from DoD output — run focused PG-only retry.
                    logger.warning(
                        "PG verdict missing from DoD output — retrying with focused evaluation"
                    )
                    _pg_met, _pg_reason = _retry_pg_evaluation(
                        plan_data=_plan_data_dict or {},
                        ticket_results=run_result.ticket_results,
                        dod_classification=dod_classification,
                        coder_config=coder_config,
                        repo_path=Path(repo_path),
                    )

            from dataclasses import replace as _replace  # noqa: F811 — may already be imported above
            run_result = _replace(
                run_result,
                definition_of_done_classification=dod_classification or "",
                plan_governance_met=_pg_met,
                plan_governance_reason=_pg_reason,
            )
            logger.info("Plan governance: %s — %s", "MET" if _pg_met else "NOT MET", _pg_reason)
            write_run_summary(run_result, output_dir, ran_stages=ran_stages, plan_data=_plan_data_dict, repo_path=repo_path)

            # Evidence gate pass
            evidence_files = [
                str(p.relative_to(output_dir))
                for p in Path(output_dir).rglob("*.json")
            ]
            eg_task = (
                f"Evidence directory: {output_dir}\n"
                f"Files present: {', '.join(evidence_files[:20])}\n"
                f"Run passed: {run_result.passed},"
                f" failed: {run_result.failed},"
                f" skipped: {run_result.skipped}\n"
                f"DoD met: {run_result.definition_of_done_met}\n\n"
                f"Assess whether evidence is sufficient for this run. "
                f"Also check: do README.md and SKILL.md accurately describe what the code actually does? "
                f"Flag any mismatch between documented behaviour and actual code behaviour."
            )
            eg_result = invoke_role(
                "evidence_gate",
                eg_task,
                coder_config=coder_config,
                repo_path=Path(repo_path),
            )
            eg_path = Path(output_dir) / "role-pass-evidence-gate.json"
            eg_path.write_text(
                _json.dumps(
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
            logger.warning("Post-run role passes failed: %s", exc)

    # Simplifier pass
    try:
        if is_cli_backend(coder_config):
            logger.info("--- stage: simplifier pass ---")
            from saturnday.standards_digest import STANDARDS_DIGEST
            simplify_prompt = (
                "You are a senior code reviewer. Review all changes made in this session.\n\n"
                f"## {STANDARDS_DIGEST}\n\n"
                "CRITICAL CONSTRAINTS — read these first:\n"
                "- NEVER delete entire files. Only edit code WITHIN existing source files.\n"
                "- NEVER touch: README.md, LICENSE, .md files, .yaml/.yml files, "
                ".json config files, log files, .gitignore, Dockerfile, CI configs.\n"
                "- NEVER delete test files. You may simplify test code but not remove test files.\n"
                "- Only modify .py, .js, .ts, .jsx, .tsx, .sh source files.\n\n"
                "SIMPLIFICATION (within source files only):\n"
                "1. Inline helper functions used only once.\n"
                "2. Remove comments that describe WHAT (noise). Keep only WHY comments.\n"
                "3. Inline premature abstractions.\n"
                "4. Prefer small edits over full rewrites.\n\n"
                "CONSTRAINT COMPLIANCE:\n"
                "5. Are tests distributed UNEVENLY? (many for risky logic, few for trivial) "
                "If tests are evenly spread, simplify the trivial ones.\n"
                "6. Were unnecessary dependencies, classes, or abstractions introduced? "
                "Simplify them within their files.\n"
                "7. Do trade-off comments exist for non-obvious design choices? "
                "If missing, add a brief WHY comment at the key decision point.\n\n"
                "Make the code simpler and more senior. Only inline or tighten within files. "
                "Add brief WHY comments at key design decisions if missing."
            )
            call_coder(coder_config, [
                {"role": "user", "content": simplify_prompt}
            ], Path(repo_path), agent_mode=True)
            logger.info("Simplifier pass complete.")
    except Exception as exc:
        logger.warning("Simplifier pass failed: %s", exc)

    # Post-completion governance re-scan
    post_pack = None
    try:
        from saturnday.governance import run_full_repo_review
        logger.info("--- stage: final governance scan ---")
        post_pack, _ = run_full_repo_review(Path(repo_path))
        logger.info("Final governance: %s", post_pack.disposition)
    except Exception as exc:
        logger.warning("Post-completion governance scan failed: %s", exc)

    # Final contract sweep — verify all plan contracts against completed repo state.
    # This is the enforcement path that replaces per-ticket blocking during run_mode.
    # Failures are surfaced clearly; the sweep itself is non-blocking at plan level
    # so partial results are never silently dropped.
    try:
        from saturnday.run.contract_checker import (
            extract_contracts,
            format_contract_results,
            verify_contracts,
        )
        logger.info("--- stage: final contract sweep ---")
        _sweep_criteria: tuple[str, ...] = tuple(
            c for t in plan.tickets for c in t.acceptance_criteria
        )
        if _sweep_criteria:
            _sweep_contracts = extract_contracts(_sweep_criteria)
            if _sweep_contracts:
                _sweep_results = verify_contracts(_sweep_contracts, Path(repo_path))
                _sweep_failed = [
                    r for r in _sweep_results if not r.verified and r.severity == "error"
                ]
                if _sweep_failed:
                    logger.warning(
                        "Final contract sweep: %d/%d blocking contract(s) FAILED"
                        " against completed repo state",
                        len(_sweep_failed), len(_sweep_results),
                    )
                    logger.warning(
                        "Failed contracts:\n%s", format_contract_results(_sweep_failed)
                    )
                    _log_gap("EXECUTION", "contract_sweep_failed",
                             "blocking plan contracts failed post-run",
                             failed=len(_sweep_failed), total=len(_sweep_results))
                else:
                    logger.info(
                        "Final contract sweep: all %d contract(s) verified OK",
                        len(_sweep_results),
                    )
            else:
                logger.debug("Final contract sweep: no verifiable contract patterns found")
        else:
            logger.debug("Final contract sweep: no acceptance criteria in plan")
    except Exception as exc:
        logger.warning("Final contract sweep failed: %s", exc)

    # Fix 40: repo-wide hard cross-ticket consistency gate.
    # Runs after all tickets complete so files written by different tickets are
    # both visible.  A finding here is binding — it downgrades definition_of_done_met
    # and is persisted on run_result so the CLI exit path can act on it.
    try:
        from saturnday.post_checks import check_cross_file_shared_default_divergence
        logger.info("--- stage: cross-ticket consistency check ---")
        _ctc_failures = check_cross_file_shared_default_divergence(Path(repo_path))
        if _ctc_failures:
            logger.warning(
                "Fix 40 cross-ticket consistency gate: %d conflicting shared default(s) found",
                len(_ctc_failures),
            )
            _log_gap("EXECUTION", "cross_ticket_conflict",
                     "conflicting shared defaults across files",
                     count=len(_ctc_failures))
            for _f in _ctc_failures:
                logger.warning("  %s", _f["message"])
            from dataclasses import replace as _replace_ctc
            run_result = _replace_ctc(
                run_result,
                cross_ticket_consistency_failures=tuple(_ctc_failures),
                definition_of_done_met=False,
            )
            write_run_summary(
                run_result, output_dir,
                ran_stages=ran_stages,
                plan_data=_plan_data_dict,
                repo_path=repo_path,
            )
        else:
            logger.info("Fix 40 cross-ticket consistency gate: PASS")
    except Exception as exc:
        logger.warning("Cross-ticket consistency check failed: %s", exc)

    # Fix 41: plan-level final executable acceptance gate.
    # Runs after all per-ticket and cross-ticket gates so it exercises the
    # assembled product, not an individual ticket's contribution.
    # Only fires when plan.acceptance_cmd is non-empty.

    # Fix 77: pick the canonical local proof source.  Declared-mode plans use
    # ``plan.local_proof_cmd`` (Fix 76 / 77); legacy_unclassified plans keep
    # using ``plan.acceptance_cmd`` (compatibility).  Validation in
    # plan_parser.py guarantees these are mutually exclusive — there is no
    # silent precedence resolution here.
    _is_legacy_plan = plan.operating_mode == "legacy_unclassified"
    _proof_cmd = plan.acceptance_cmd if _is_legacy_plan else plan.local_proof_cmd

    # Phase 5: post-execution proof derivation.  If plan.local_proof_cmd is
    # still a FIX73_PROOF_GAP (either because the planner gapped it at
    # Phase 4 or because we're dealing with worker/frontend which always
    # gap at plan time), invoke the coder NOW with the actual generated
    # code as context and attempt to derive a concrete proof.  Worker and
    # frontend modes get their first real attempt here because the
    # operator path can only be inferred from real code.
    _phase5_status = None
    _phase5_source = None
    if not _is_legacy_plan and _proof_cmd:
        try:
            from saturnday.run.proof_resolver import (
                proof_is_gap,
                resolve_proof_gap_post_execution,
            )
            if proof_is_gap(_proof_cmd):
                # Collect changed files from all successful ticket results.
                _changed = []
                for _tr in ticket_results:
                    if _tr.disposition in ("PASS", "CODED_UNGOVERNED"):
                        _changed.extend(_tr.changed_files)
                _plan_mut_dict = {
                    "project_id": plan.project_id,
                    "operating_mode": plan.operating_mode,
                    "dependency_profile": plan.dependency_profile,
                    "proof_realism": plan.proof_realism,
                    "testing_strategy": plan.testing_strategy,
                    "external_dependencies": list(plan.external_dependencies),
                    "local_proof_cmd": _proof_cmd,
                    "notes": plan.notes,
                }
                _ticket_dicts = [
                    {
                        "ticket_id": t.ticket_id,
                        "goal": t.goal,
                        "acceptance_criteria": list(t.acceptance_criteria),
                    }
                    for t in plan.tickets
                ]
                _phase5_status, _phase5_source = resolve_proof_gap_post_execution(
                    plan=_plan_mut_dict,
                    coder_config=coder_config,
                    repo_path=str(repo_path),
                    changed_files=_changed,
                    tickets=_ticket_dicts,
                )
                if _phase5_status == "resolved_coder_post_exec":
                    # Replace _proof_cmd with the derived command.
                    _proof_cmd = _plan_mut_dict["local_proof_cmd"]
                    logger.info(
                        "Phase 5: post-execution proof derivation succeeded "
                        "for %s — replacing gap marker with derived proof",
                        plan.operating_mode,
                    )
                    run_result = _update_proof_resolution(
                        run_result,
                        status="resolved_coder_post_exec",
                        source="coder_post_execution",
                        narrative=(
                            "Phase 5 derived the proof from the generated "
                            f"code for operating_mode={plan.operating_mode}."
                        ),
                    )
                else:
                    logger.info(
                        "Phase 5: post-execution proof derivation did not "
                        "replace gap for %s (status=%s)",
                        plan.operating_mode, _phase5_status,
                    )
                    run_result = _update_proof_resolution(
                        run_result,
                        status="unresolved_gap",
                        narrative=(
                            "Phase 5 could not derive a concrete proof "
                            "from the generated code — gap preserved."
                        ),
                    )
        except Exception as _phase5_exc:  # noqa: BLE001
            logger.warning(
                "Phase 5: post-execution proof resolver raised "
                "(non-fatal): %s", _phase5_exc,
            )

        # δ — targeted-question registry fallback.
        # After Phase 4 (plan-time coder derivation) and Phase 5
        # (post-execution coder derivation) have both declined to close
        # the gap, try the small deterministic registry.  CLI
        # pre-answers supplied via ``--proof-answer kind=value`` win
        # without touching stdin; on TTY the operator is asked one
        # targeted question and the answer regenerates
        # ``local_proof_cmd`` via the spec's ``apply_fn``.  In
        # non-interactive mode with no usable pre-answer, the gap is
        # left in place and an explicit refusal narrative is set —
        # downstream DoD enforcement still downgrades via
        # ``unresolved_gap``, but the operator now has an actionable
        # ``--proof-answer`` list.
        try:
            from saturnday.run.proof_resolver import proof_is_gap as _proof_is_gap_dq
            _dq_is_gap = _proof_is_gap_dq(_proof_cmd)
        except Exception:
            _dq_is_gap = False
        if _dq_is_gap:
            try:
                from saturnday.run.proof_questions import (
                    format_refusal_message,
                    resolve_via_questions,
                )
                import sys as _sys_dq
                _plan_mut_dq = {
                    "project_id": plan.project_id,
                    "operating_mode": plan.operating_mode,
                    "dependency_profile": plan.dependency_profile,
                    "proof_realism": plan.proof_realism,
                    "testing_strategy": plan.testing_strategy,
                    "external_dependencies": list(plan.external_dependencies),
                    "local_proof_cmd": _proof_cmd,
                    "notes": plan.notes,
                }
                _interactive_dq = _sys_dq.stdin.isatty()
                _dq_status, _dq_source, _dq_narrative = resolve_via_questions(
                    _plan_mut_dq,
                    cli_answers=dict(proof_answers or {}),
                    interactive=_interactive_dq,
                )
                if _dq_status == "resolved_operator":
                    _proof_cmd = _plan_mut_dq["local_proof_cmd"]
                    logger.info(
                        "δ: targeted-question resolver closed the proof "
                        "gap for operating_mode=%s via source=%s",
                        plan.operating_mode, _dq_source,
                    )
                    run_result = _update_proof_resolution(
                        run_result,
                        status="resolved_operator",
                        source="operator_targeted_answer",
                        narrative=_dq_narrative,
                    )
                else:
                    # Still unresolved — surface the refusal narrative
                    # visibly so the operator sees actionable next steps
                    # instead of a silent NOT-MET downgrade.
                    run_result = _update_proof_resolution(
                        run_result,
                        status="unresolved_gap",
                        narrative=_dq_narrative,
                    )
                    if not _interactive_dq:
                        refusal = format_refusal_message(
                            plan.operating_mode,
                            cli_answers=dict(proof_answers or {}),
                        )
                        # Stderr is the right surface — visible on CI
                        # and in terminal tails, independent of log
                        # capture settings.
                        print(
                            "\n" + ("=" * 72) + "\n" + refusal + "\n"
                            + ("=" * 72),
                            file=_sys_dq.stderr,
                            flush=True,
                        )
                        logger.error(
                            "δ refusal: %s",
                            _dq_narrative.replace("\n", " "),
                        )
            except Exception as _dq_exc:  # noqa: BLE001
                logger.warning(
                    "δ: targeted-question resolver raised (non-fatal): %s",
                    _dq_exc,
                )

    # Fix 56: gap signal for runnable product with no proof command.
    if not _proof_cmd and plan.is_runnable_product:
        _log_gap("PLANNER", "no_acceptance_cmd",
                 "runnable product but no acceptance_cmd / local_proof_cmd",
                 project_id=plan.project_id)

    # Fix 76: surface the operator disclaimer in the run log so seeded_demo /
    # storage_only / external_dependencies framing is visible.
    if plan.operator_disclaimer:
        logger.info("OPERATOR DISCLAIMER: %s", plan.operator_disclaimer)

    # Fix 52.b: if the plan declares acceptance_setup (heavy setup steps),
    # gate the acceptance phase behind explicit user approval.
    #
    # α (non-interactive acceptance-setup approval): extend the original
    # Fix 52.b gate with two non-TTY approval surfaces —
    #   (1) --approve-acceptance-setup CLI flag (plumbed via run_plan's
    #       ``approve_acceptance_setup`` parameter), and
    #   (2) SATURNDAY_APPROVE_SETUP=1 env var.
    # Either signal lets CI / scripted runs honestly approve the same
    # setup that TTY operators approve.  Without a signal, non-TTY still
    # refuses — no silent approval.  The approval source is recorded on
    # RunResult.acceptance_setup_approval_source for evidence.
    _acceptance_approved = True  # default: no gate needed
    _acceptance_approval_source = ""
    if _proof_cmd and plan.acceptance_setup:
        logger.info("--- stage: acceptance setup approval ---")
        import os as _os_accept
        import sys as _sys_accept
        _env_approved = _os_accept.environ.get("SATURNDAY_APPROVE_SETUP") == "1"
        if _sys_accept.stdin.isatty():
            print("\n  Realistic acceptance requires setup:")
            for _step in plan.acceptance_setup:
                print(f"    - {_step}")
            print()
            try:
                _choice = input("  Proceed with acceptance? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _choice = "n"
            _acceptance_approved = _choice == "y"
            if _acceptance_approved:
                _acceptance_approval_source = "interactive"
            else:
                _acceptance_approval_source = "declined"
                logger.info("Fix 52.b: user declined acceptance setup — skipping acceptance")
                _log_gap("ACCEPTANCE", "setup_declined",
                         "user declined acceptance setup — realistic testing skipped")
                print("  Acceptance skipped by user.")
        else:
            # Non-interactive: approve only if explicit signal is present.
            if approve_acceptance_setup:
                _acceptance_approved = True
                _acceptance_approval_source = "cli_flag"
                logger.info(
                    "α: acceptance setup approved via --approve-acceptance-setup "
                    "(non-interactive run)",
                )
            elif _env_approved:
                _acceptance_approved = True
                _acceptance_approval_source = "env_var"
                logger.info(
                    "α: acceptance setup approved via SATURNDAY_APPROVE_SETUP=1 "
                    "(non-interactive run)",
                )
            else:
                _acceptance_approved = False
                _acceptance_approval_source = "blocked_noninteractive"
                logger.warning(
                    "Fix 52.b: plan requires acceptance setup but running "
                    "non-interactively with no approval signal — skipping "
                    "acceptance. Pass --approve-acceptance-setup or set "
                    "SATURNDAY_APPROVE_SETUP=1 to approve in CI.",
                )
                _log_gap("ACCEPTANCE", "setup_blocked_noninteractive",
                         "non-interactive mode with no approval signal "
                         "(use --approve-acceptance-setup or "
                         "SATURNDAY_APPROVE_SETUP=1 to approve)",
                )
        # Record the approval source on RunResult so evidence is honest.
        from dataclasses import replace as _replace_approval  # noqa: F811
        run_result = _replace_approval(
            run_result,
            acceptance_setup_approval_source=_acceptance_approval_source,
        )
        # α: persist the updated approval source to evidence immediately.
        # Without this, run_plan's downstream writers only rewrite the
        # summary on a FAIL-of-proof path — a successful non-TTY approval
        # would leave the summary at its pre-approval state and the source
        # would be lost from evidence even though it lives on the returned
        # RunResult.  This write keeps the two in sync regardless of what
        # the proof execution path does later.
        write_run_summary(
            run_result, output_dir,
            ran_stages=ran_stages,
            plan_data=_plan_data_dict,
            repo_path=repo_path,
        )

    if _proof_cmd and _acceptance_approved:
        # Fix 52.c: execute approved acceptance_setup steps before the proof.
        # Each step is run as a shell command with a 300s timeout.
        # Failure in any step aborts the entire acceptance phase.
        _setup_failed = False
        if plan.acceptance_setup:
            logger.info("--- stage: acceptance setup execution ---")
            for _step_idx, _setup_step in enumerate(plan.acceptance_setup, 1):
                logger.info(
                    "Acceptance setup step %d/%d: %s",
                    _step_idx, len(plan.acceptance_setup), _setup_step,
                )
                _setup_result = _run_acceptance_setup_step(_setup_step, Path(repo_path))
                if _setup_result:
                    logger.warning(
                        "Acceptance setup step %d failed: %s", _step_idx, _setup_result[:300],
                    )
                    _log_gap("ACCEPTANCE", "setup_step_failed",
                             "acceptance setup step failed",
                             step=_step_idx, error=_setup_result[:200])
                    run_result = _record_proof_failure(
                        run_result,
                        is_legacy=_is_legacy_plan,
                        attempted=False,
                        failure=f"Setup step {_step_idx} failed: {_setup_result[:400]}",
                    )
                    write_run_summary(
                        run_result, output_dir,
                        ran_stages=ran_stages,
                        plan_data=_plan_data_dict,
                        repo_path=repo_path,
                    )
                    logger.info("Fix 52.c: acceptance setup failed at step %d — DoD downgraded", _step_idx)
                    _setup_failed = True
                    break
                logger.info("Acceptance setup step %d: OK", _step_idx)
            if not _setup_failed:
                logger.info("All %d acceptance setup step(s) completed", len(plan.acceptance_setup))

        if _setup_failed:
            pass  # skip the proof — setup failure already recorded above
        else:
            logger.info("--- stage: final acceptance gate ---")
            _proof_field_name = "acceptance_cmd" if _is_legacy_plan else "local_proof_cmd"
            logger.info("Running %s: %s", _proof_field_name, _proof_cmd)
            _accept_failure = _run_verify_cmd(_proof_cmd, Path(repo_path))

            # Phase 6: run-time proof negotiation.  If the proof fails and
            # we are interactive AND the plan is declared-mode, offer the
            # operator a bounded retry menu before marking it as failed.
            if _accept_failure and not _is_legacy_plan:
                _pre_neg_cmd = _proof_cmd
                _proof_cmd, _accept_failure = _negotiate_proof_retry(
                    original_cmd=_proof_cmd,
                    original_failure=_accept_failure,
                    plan=plan,
                    repo_path=Path(repo_path),
                    coder_config=coder_config,
                )
                # Phase 7: if the operator produced a new proof (passing
                # or failing), record the operator-intervention provenance
                # so the summary tells the true story.
                if _proof_cmd != _pre_neg_cmd:
                    _neg_source = _NEGOTIATION_SOURCE or "operator_edited"
                    _neg_status = (
                        "resolved_operator"
                        if _neg_source == "operator_edited"
                        else "resolved_coder_post_exec"
                    )
                    run_result = _update_proof_resolution(
                        run_result,
                        status=_neg_status,
                        source=_neg_source,
                        narrative=(
                            "Operator negotiated a new proof at run time "
                            f"via {_neg_source} — see Phase 6 menu."
                        ),
                    )

            # Phase 9: auto-repair of a still-unresolved proof gap.  When
            # the acceptance gate fails AND ``auto_repair`` is on AND the
            # plan is declared-mode AND the current proof is still a gap
            # marker, invoke the post-execution resolver with the failure
            # as context and run the derived proof once.  This is the
            # non-interactive sibling of Phase 6's [2] branch — lets CI
            # runs recover from a gap without operator input.
            if (
                _accept_failure
                and not _is_legacy_plan
                and auto_repair
            ):
                from saturnday.run.proof_resolver import proof_is_gap
                if proof_is_gap(_proof_cmd):
                    logger.info(
                        "Phase 9: auto-repair invoked for unresolved "
                        "proof gap — retrying post-execution derivation",
                    )
                    _repaired_cmd, _repaired_failure = _auto_repair_proof_gap(
                        current_cmd=_proof_cmd,
                        current_failure=_accept_failure,
                        plan=plan,
                        repo_path=Path(repo_path),
                        coder_config=coder_config,
                        ticket_results=ticket_results,
                    )
                    if _repaired_cmd != _proof_cmd:
                        _proof_cmd = _repaired_cmd
                        _accept_failure = _repaired_failure
                        if not _accept_failure:
                            run_result = _update_proof_resolution(
                                run_result,
                                status="resolved_coder_post_exec",
                                source="coder_post_execution",
                                narrative=(
                                    "Phase 9 auto-repair derived a new "
                                    "proof after the gap marker failed."
                                ),
                            )

            if _accept_failure:
                logger.warning(
                    "Fix 41/77 final acceptance gate FAILED: %s", _accept_failure[:300]
                )
                run_result = _record_proof_failure(
                    run_result,
                    is_legacy=_is_legacy_plan,
                    attempted=True,
                    failure=_accept_failure[:500],
                )
                write_run_summary(
                    run_result, output_dir,
                    ran_stages=ran_stages,
                    plan_data=_plan_data_dict,
                    repo_path=repo_path,
                )
                logger.info("Fix 41/77 final acceptance gate: FAIL — DoD downgraded")
                _log_gap("ACCEPTANCE", "acceptance_cmd_failed",
                         "final acceptance command failed",
                         error=_accept_failure[:200])
            else:
                run_result = _record_proof_pass(run_result, is_legacy=_is_legacy_plan)
                logger.info("Fix 41/77 final acceptance gate: PASS")

            # Fix 77: optionally run live_proof_cmd as a supplementary,
            # NON-BLOCKING proof against real external services.  Only fires
            # for declared-mode plans with external_dependencies AND when the
            # operator sets LIVE_PROOF=1 in the environment.  Failure is
            # recorded but does NOT trigger the DoD mechanical guard.
            import os as _os_lp
            if (
                not _is_legacy_plan
                and plan.dependency_profile == "external_dependencies"
                and plan.live_proof_cmd
                and _os_lp.environ.get("LIVE_PROOF") == "1"
            ):
                logger.info("--- stage: live proof gate (LIVE_PROOF=1) ---")
                logger.info("Running live_proof_cmd: %s", plan.live_proof_cmd)
                _live_failure = _run_verify_cmd(
                    plan.live_proof_cmd, Path(repo_path)
                )
                from dataclasses import replace as _replace_live
                if _live_failure:
                    logger.warning(
                        "Live proof FAILED (non-blocking): %s", _live_failure[:300]
                    )
                    run_result = _replace_live(
                        run_result,
                        live_proof_attempted=True,
                        live_proof_passed=False,
                        live_proof_failure=_live_failure[:500],
                    )
                else:
                    run_result = _replace_live(
                        run_result,
                        live_proof_attempted=True,
                        live_proof_passed=True,
                    )
                    logger.info("Live proof gate: PASS")
            elif (
                not _is_legacy_plan
                and plan.dependency_profile == "external_dependencies"
                and plan.live_proof_cmd
            ):
                logger.info(
                    "Live proof skipped — set LIVE_PROOF=1 to attempt live "
                    "verification against real external services"
                )
    elif _proof_cmd and not _acceptance_approved:
        # Fix 52.b: acceptance was gated and user declined or non-interactive
        run_result = _record_proof_failure(
            run_result,
            is_legacy=_is_legacy_plan,
            attempted=False,
            failure="Acceptance setup not approved — acceptance skipped",
        )
        logger.info("Fix 52.b: acceptance not approved — DoD downgraded")
    else:
        logger.debug("Fix 41 final acceptance gate: skipped (no proof cmd in plan)")

    # Generate run report
    try:
        from saturnday.reporting import generate_run_report
        import json as _json_report
        plan_data = _json_report.loads(Path(plan_path).read_text(encoding="utf-8"))
        report_path = generate_run_report(
            result=run_result,
            plan_data=plan_data,
            post_pack=post_pack,
            evidence_dir=Path(output_dir),
            repo_path=Path(repo_path),
        )
        logger.info("Run report: %s", report_path)
    except Exception as exc:
        logger.warning("Failed to generate run report: %s", exc)

    # Generate review report and fix prompts for ungoverned tickets
    if coded_ungoverned > 0 and output_dir:
        try:
            from saturnday.reporting import generate_review_report, generate_fix_prompts
            review_path = generate_review_report(
                ticket_results=run_result.ticket_results,
                evidence_dir=Path(output_dir),
                dod_met=run_result.definition_of_done_met,
            )
            if review_path:
                logger.info("Review report: %s", review_path)
            prompt_paths = generate_fix_prompts(
                ticket_results=run_result.ticket_results,
                evidence_dir=Path(output_dir),
            )
            if prompt_paths:
                logger.info("Fix prompts: %s", prompt_paths[0].parent)
        except Exception as exc:
            logger.warning("Failed to generate review report/prompts: %s", exc)

    # Timing summary for the entire run
    _t_run_total = time.monotonic() - _t_run_start
    _stage_timings.append(("total_run", _t_run_total))
    logger.info("TIMING === RUN SUMMARY === total %.2fs", _t_run_total)
    for _sname, _selapsed in _stage_timings:
        logger.info("TIMING   %-30s %.2fs", _sname, _selapsed)

    # Write timing data to evidence for later analysis
    try:
        _timing_path = Path(output_dir) / "stage-timings.json"
        _timing_path.write_text(
            _json_mod.dumps(
                {"stages": [{"name": n, "elapsed_s": round(e, 3)} for n, e in _stage_timings]},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    return run_result


def _run_ticket_with_retries(
    ticket: TicketSpec,
    repo_path: Path,
    coder_config: CoderConfig,
    system_prompt: str,
    state: ProjectState,
    plan_notes: str,
    output_dir: Path,
    max_retries: int = MAX_REPAIR_ATTEMPTS,
    auto_repair: bool = False,
    lessons_conn: "sqlite3.Connection | None" = None,
    memory_conn: "sqlite3.Connection | None" = None,
    project_id: str = "",
    ran_stages: set[str] | None = None,
    on_committed: "Callable[[str, str, str], None] | None" = None,
) -> TicketResult:
    """Execute a single ticket with repair retries.

    Args:
        ticket: The ticket specification to execute.
        repo_path: Path to the target repository.
        coder_config: Backend configuration for the AI coder.
        system_prompt: Pre-built system prompt for the coder.
        state: Current project state.
        plan_notes: Free-form notes injected into every coder prompt.
        output_dir: Directory for evidence output.
        max_retries: Maximum number of repair attempts (default: ``MAX_REPAIR_ATTEMPTS``).
            Pass ``plan.default_retry_limit`` to respect per-plan configuration.
        auto_repair: If ``True``, attempt one additional repair pass after all
            retries are exhausted before returning FAIL.  The last governance
            findings are used as repair context.
        lessons_conn: Open SQLite connection from :func:`init_db`, or ``None``
            to skip the lessons subsystem.  When provided, past failure lessons
            are prepended to the coder prompt before the first attempt, and a
            new lesson is recorded if the ticket exhausts all retries.
        memory_conn: Open SQLite connection from :func:`init_memory_db`, or
            ``None`` to skip Phase 1 structured memory extraction.  When
            provided, a MemoryItem is stored on every FAIL or CODED_UNGOVERNED
            disposition, and severe failures generate candidate Rules.
        project_id: Project identifier used as the lessons deduplication key.
            Ignored when ``lessons_conn`` is ``None``.
    """
    repair_context: str | None = None
    # Per-ticket observability: track gate outcomes across attempts
    _last_governance: str = ""
    _last_post_check: str = ""
    _post_checks_fired: list[str] = []
    # Per-ticket stage tracking: accumulates names of premium stages that ran
    _ran_stages: set[str] = set()

    # Load lessons across ALL projects on this machine and inject into
    # plan_notes for the first attempt.  project_id="" disables the
    # per-project scope filter so lessons recorded while building one
    # project carry forward into every subsequent project the same
    # operator builds.  Lessons are still SAVED against the current
    # project_id (so the origin stays inspectable in the DB), only the
    # READ side is broadened.
    _lessons_prefix = ""
    if lessons_conn is not None:
        try:
            _past_lessons = load_lessons(lessons_conn, project_id="", min_confidence=1)
            _lessons_prefix = format_lessons_for_prompt(_past_lessons)
            if _lessons_prefix:
                logger.debug(
                    "Injecting %d cross-project lesson(s) into coder prompt for %s",
                    len(_past_lessons), ticket.ticket_id,
                )
        except Exception as _exc:  # pragma: no cover
            logger.warning("Failed to load lessons: %s — continuing without lesson context", _exc)
            _lessons_prefix = ""

    # Prepend lessons to plan_notes so they appear in every prompt
    _plan_notes_with_lessons = (
        f"{_lessons_prefix}\n\n{plan_notes}".strip() if _lessons_prefix else plan_notes
    )

    # Carry-forward container: populated from capsule active_rules when the
    # capsule is built.  Used in the compact retry path (Fix 17) so enforced
    # constraints remain visible on retry attempts even after the full bundle
    # is stripped.
    _enforced_rules_compact = ""

    # Build context capsule (Phase 2 memory system) and prepend to plan_notes.
    # Falls back silently — never breaks the pipeline.
    if memory_conn is not None and capability_registry.is_available("memory_provider"):
        try:
            from saturnday.run.context_capsule import (
                build_context_capsule,
                capsule_to_prompt,
                generate_knowledge_check,
                format_knowledge_check_for_prompt,
            )
            _capsule = build_context_capsule(ticket, memory_conn, repo_path)
            _capsule_text = capsule_to_prompt(_capsule)
            _knowledge_check = generate_knowledge_check(_capsule, repo_path)
            _kc_text = format_knowledge_check_for_prompt(_knowledge_check)
            if _capsule_text or _kc_text:
                _combined = "\n\n".join(
                    p for p in (_kc_text, _capsule_text) if p
                )
                _plan_notes_with_lessons = (
                    f"{_combined}\n\n{_plan_notes_with_lessons}".strip()
                )
                logger.debug(
                    "Context capsule injected for ticket %s (%d chars)",
                    ticket.ticket_id, len(_combined),
                )
            # Fix 17: extract active enforced rules for the compact retry path.
            # These carry forward on retries so enforced constraints remain visible
            # even after the full capsule/lessons bundle is stripped.
            if _capsule.active_rules:
                _rules_lines = "\n".join(
                    f"  - {r}" for r in _capsule.active_rules[:3]
                )
                _enforced_rules_compact = (
                    f"ENFORCED RULES (carry forward):\n{_rules_lines}"
                )
            _ran_stages.add("memory_provider")
        except Exception as _capsule_exc:
            logger.warning(
                "Context capsule generation failed for %s: %s — continuing",
                ticket.ticket_id, _capsule_exc,
            )
    elif memory_conn is not None:
        logger.debug("Stage skipped: premium not registered (memory_provider/context_capsule)")

    # Phase 3: Pre-run impact analysis — informs coder of files likely affected.
    # Only runs when ticket scope touches more than one file.  Advisory only.
    _scope_files = list(ticket.scope.allowed_globs)
    if len(_scope_files) > 1 and capability_registry.is_available("impact_analysis"):
        try:
            from saturnday.run.impact_analysis import compute_impact, format_impact_for_prompt
            _pre_impact = compute_impact(_scope_files, repo_path, state)
            _pre_impact_text = format_impact_for_prompt(_pre_impact)
            if _pre_impact_text:
                _plan_notes_with_lessons = (
                    f"{_pre_impact_text}\n\n{_plan_notes_with_lessons}".strip()
                )
                logger.debug(
                    "Pre-run impact context injected for ticket %s (%d chars)",
                    ticket.ticket_id, len(_pre_impact_text),
                )
            _ran_stages.add("impact_analysis")
        except Exception as _impact_exc:
            logger.debug("Pre-run impact analysis failed for %s: %s", ticket.ticket_id, _impact_exc)
    elif len(_scope_files) > 1:
        logger.debug("Stage skipped: premium not registered (impact_analysis/pre-coder)")

    # Fix 17: build a compact notes string for retry attempts.
    # First attempt uses the full _plan_notes_with_lessons bundle (capsule +
    # lessons + impact + plan notes).  Retry attempts strip that bulk and keep
    # only the enforced rules (if any) and the core plan notes — or, for CLI
    # backends, a short file reference so the agent can read .saturnday/plan-notes.md
    # instead of receiving the full text inline again.
    _cli_retry = is_cli_backend(coder_config) and bool(plan_notes)
    _plan_notes_base_for_retry = (
        "PROJECT CONSTRAINTS: See .saturnday/plan-notes.md (read this file before coding)."
        if _cli_retry
        else plan_notes
    )
    _plan_notes_compact_for_retry = (
        f"{_enforced_rules_compact}\n\n{_plan_notes_base_for_retry}".strip()
        if _enforced_rules_compact
        else _plan_notes_base_for_retry
    )

    _log_progress(f"▶ {ticket.ticket_id}: {ticket.goal[:120] if ticket.goal else 'no goal'}")

    # Snapshot project-level checks BEFORE the coder runs.
    # Used for regression check: only block if this ticket made things worse.
    _t_snap = time.monotonic()
    _pre_ticket_state = _snapshot_project_checks(repo_path)
    logger.info("TIMING %-30s %.2fs [%s]", "snapshot_project_checks", time.monotonic() - _t_snap, ticket.ticket_id)

    # Fix 71: snapshot per-(check, kind, file) fingerprints for findings in
    # tracked source files OUTSIDE the ticket's allowed scope.  Used after
    # each attempt to surface NEW out-of-scope findings (regressions caused
    # by the ticket's in-scope changes propagating outward) while leaving
    # pre-existing OOS findings unaffected.  No-op when allowed_globs is "**".
    _t_oos = time.monotonic()
    _pre_oos_fingerprints = _snapshot_oos_finding_fingerprints(
        repo_path,
        ticket.scope.allowed_globs,
        ticket.scope.forbidden_globs,
    )
    logger.info("TIMING %-30s %.2fs [%s]", "snapshot_oos_fingerprints", time.monotonic() - _t_oos, ticket.ticket_id)

    # Initialise mutable loop state so the post-loop FAIL path always has values.
    changed_files: list[str] = []
    relevant_findings: list[dict] = []

    # Fix 18: oversize pressure trackers — accumulated across all retry attempts.
    # Used by the last-resort split gate after retry exhaustion.
    _budget_warn_count: int = 0   # attempts where prompt_budget_warning was True
    _max_prompt_chars: int = 0    # largest prompt (chars) seen across all attempts
    _already_last_resort_split: bool = False  # prevents gate from firing twice

    for attempt in range(1, max_retries + 2):
        logger.info(
            "Ticket %s attempt %d/%d",
            ticket.ticket_id, attempt, max_retries + 1,
        )

        # Assemble prompt and measure budget BEFORE execution so budget
        # data survives even if the coder call throws.
        # Fix 17: first attempt gets the full rich bundle; retries get the
        # compact version (stripped of capsule/lessons/impact bulk).
        _notes_for_attempt = (
            _plan_notes_with_lessons if attempt == 1 else _plan_notes_compact_for_retry
        )
        _messages, _prompt_budget = _assemble_ticket_prompt(
            ticket=ticket,
            repo_path=repo_path,
            coder_config=coder_config,
            system_prompt=system_prompt,
            state=state,
            plan_notes=_notes_for_attempt,
            repair_context=repair_context,
        )

        # Fix 18: accumulate oversize pressure signals for the post-exhaustion gate.
        _prompt_chars_this = _prompt_budget.get("prompt_chars", 0)
        if _prompt_chars_this > _max_prompt_chars:
            _max_prompt_chars = _prompt_chars_this
        if _prompt_budget.get("prompt_budget_warning"):
            _budget_warn_count += 1

        _t_coder = time.monotonic()
        try:
            response, changed_files = _execute_ticket(
                ticket=ticket,
                repo_path=repo_path,
                coder_config=coder_config,
                messages=_messages,
            )
        except CloudCoreError as exc:
            logger.info("TIMING %-30s %.2fs [%s] (error)", "execute_ticket", time.monotonic() - _t_coder, ticket.ticket_id)
            logger.error("Ticket %s attempt %d failed: %s", ticket.ticket_id, attempt, exc)
            write_ticket_evidence(
                TicketEvidence(
                    ticket_id=ticket.ticket_id,
                    attempt=attempt,
                    coder_response=getattr(exc, "response", ""),
                    governance_evidence_path="",
                    error=str(exc),
                    prompt_chars=_prompt_budget.get("prompt_chars", 0),
                    prompt_budget_warning=_prompt_budget.get("prompt_budget_warning", False),
                    prompt_budget_soft_threshold=_prompt_budget.get("prompt_budget_soft_threshold", 0),
                    prompt_budget_hard_threshold=_prompt_budget.get("prompt_budget_hard_threshold", 0),
                ),
                output_dir,
            )
            if attempt > max_retries:
                _log_ticket_summary(ticket.ticket_id, attempt, "ERROR", "", [], "FAIL")
                if auto_repair:
                    repair_result = _attempt_auto_repair(
                        ticket=ticket,
                        repo_path=repo_path,
                        coder_config=coder_config,
                        system_prompt=system_prompt,
                        state=state,
                        plan_notes=_plan_notes_compact_for_retry,
                        output_dir=output_dir,
                        repair_context=f"Previous attempt error: {exc}",
                        attempt_number=attempt + 1,
                        on_committed=on_committed,
                    )
                    if repair_result is not None:
                        return repair_result
                _extract_lesson_from_outcome(
                    memory_conn,
                    ticket=ticket,
                    outcome="FAIL",
                    attempt=attempt,
                    findings=[],
                    changed_files=[],
                    error_message=str(exc)[:300],
                )
                if ran_stages is not None:
                    ran_stages.update(_ran_stages)
                return TicketResult(
                    ticket_id=ticket.ticket_id,
                    disposition="FAIL",
                    attempts=attempt,
                    error=str(exc),
                    verify_cmd_specified=_vc_specified(ticket),
                )
            repair_context = f"Error: {exc}"
            continue

        logger.info("TIMING %-30s %.2fs [%s]", "execute_ticket", time.monotonic() - _t_coder, ticket.ticket_id)

        # Stage ONLY the current ticket's changed files
        _git_add(repo_path, changed_files)

        # Auto-install deps if coder modified dependency files
        _auto_install_deps(repo_path, changed_files)

        # Run governance
        _t_gov = time.monotonic()
        try:
            disposition, findings, gov_evidence_path, disposition_reasons = _run_governance(
                repo_path, run_mode=True, pre_ticket_state=_pre_ticket_state,
            )
            # Security triage: LLM-based false positive filtering
            _triage_handler = capability_registry.get("security_triage")
            if _triage_handler is not None:
                try:
                    findings = _triage_handler.filter_findings(findings, repo_path, coder_config)
                    _ran_stages.add("security_triage")
                except Exception as _triage_exc:
                    logger.debug("Security triage skipped: %s", _triage_exc)
            else:
                logger.debug("Stage skipped: premium not registered (security_triage)")
        except GovernanceError as exc:
            logger.info("TIMING %-30s %.2fs [%s] (error)", "governance", time.monotonic() - _t_gov, ticket.ticket_id)
            logger.error("Governance error for %s: %s", ticket.ticket_id, exc)
            _git_reset_changes(repo_path)
            write_ticket_evidence(
                TicketEvidence(
                    ticket_id=ticket.ticket_id,
                    attempt=attempt,
                    coder_response=response,
                    changed_files=changed_files,
                    governance_evidence_path="",
                    error=f"Governance error: {exc}",
                    prompt_chars=_prompt_budget.get("prompt_chars", 0),
                    prompt_budget_warning=_prompt_budget.get("prompt_budget_warning", False),
                    prompt_budget_soft_threshold=_prompt_budget.get("prompt_budget_soft_threshold", 0),
                    prompt_budget_hard_threshold=_prompt_budget.get("prompt_budget_hard_threshold", 0),
                ),
                output_dir,
            )
            if attempt > max_retries:
                _log_ticket_summary(ticket.ticket_id, attempt, "ERROR", "", [], "FAIL")
                _extract_lesson_from_outcome(
                    memory_conn,
                    ticket=ticket,
                    outcome="FAIL",
                    attempt=attempt,
                    findings=[],
                    changed_files=changed_files,
                    error_message=f"Governance error: {exc}"[:300],
                )
                if ran_stages is not None:
                    ran_stages.update(_ran_stages)
                return TicketResult(
                    ticket_id=ticket.ticket_id,
                    disposition="FAIL",
                    attempts=attempt,
                    error=f"Governance error: {exc}",
                    verify_cmd_specified=_vc_specified(ticket),
                )
            repair_context = f"Governance error: {exc}"
            continue

        logger.info("TIMING %-30s %.2fs [%s]", "governance", time.monotonic() - _t_gov, ticket.ticket_id)

        # Filter findings to only current ticket's files.
        # Pre-existing lint issues in previously committed files should not
        # block the current ticket.
        relevant_findings = _filter_findings_to_files(findings, changed_files)

        # Fix 71: surface NEW out-of-scope code-level regressions.
        # _filter_findings_to_files drops findings outside changed_files, but
        # findings in files OUTSIDE the ticket's allowed scope should block
        # IF they were not present pre-ticket (regression caused by this
        # ticket's in-scope changes propagating outward).  We keep pre-existing
        # OOS findings filtered out.  Project-level regression logic (Fix 56)
        # is unchanged.
        _oos_regressions = _detect_oos_regressions(
            findings,
            ticket.scope.allowed_globs,
            ticket.scope.forbidden_globs,
            _pre_oos_fingerprints,
        )
        if _oos_regressions:
            logger.warning(
                "Ticket %s: %d new out-of-scope regression(s) detected — blocking",
                ticket.ticket_id, len(_oos_regressions),
            )
            relevant_findings = list(relevant_findings) + _oos_regressions
            # OOS regressions are "real" findings — make sure the disposition
            # reflects them so the runner gate does not silently treat
            # "no in-scope findings + OOS regressions" as PASS.
            if disposition == "PASS":
                disposition = "FAIL"
        effective_disposition = disposition
        _last_governance = effective_disposition
        if disposition == "FAIL" and not relevant_findings:
            logger.info(
                "Ticket %s: governance FAIL but no findings in current ticket's files — treating as PASS",
                ticket.ticket_id,
            )
            effective_disposition = "PASS"

        # Fix 78: narrow runner success gate.  ``effective_disposition`` is
        # preserved (WARN stays WARN in evidence); the runner uses the helper
        # below to decide whether to take the success branch.  Only WARN with
        # the explicit policy-cleared marker counts as success — generic and
        # soft-check WARN remain non-success.
        _runner_success = (
            effective_disposition == "PASS"
            or _is_policy_cleared_warn(effective_disposition, disposition_reasons)
        )

        # Record evidence (including prompt-budget instrumentation)
        write_ticket_evidence(
            TicketEvidence(
                ticket_id=ticket.ticket_id,
                attempt=attempt,
                coder_response=response,
                changed_files=changed_files,
                governance_disposition=effective_disposition,
                governance_findings=relevant_findings,
                governance_evidence_path=gov_evidence_path,
                prompt_chars=_prompt_budget.get("prompt_chars", 0),
                prompt_budget_warning=_prompt_budget.get("prompt_budget_warning", False),
                prompt_budget_soft_threshold=_prompt_budget.get("prompt_budget_soft_threshold", 0),
                prompt_budget_hard_threshold=_prompt_budget.get("prompt_budget_hard_threshold", 0),
                context_compaction_applied=False,
                prompt_split_exempt=ticket.atomic,
                prompt_split_reason="atomic_ticket" if ticket.atomic else None,
            ),
            output_dir,
        )

        if _runner_success:
            # Run post-governance senior-judgment checks
            post_findings = run_post_checks(repo_path, changed_files)
            _post_checks_fired = sorted({f.get("message", "").split(":")[0] for f in post_findings if f.get("message")})
            if post_findings:
                _last_post_check = "FAIL"
                logger.warning(
                    "Ticket %s: post-checks found %d issue(s)",
                    ticket.ticket_id, len(post_findings),
                )
                # Record evidence with post-check findings
                write_ticket_evidence(
                    TicketEvidence(
                        ticket_id=ticket.ticket_id,
                        attempt=attempt,
                        coder_response=response,
                        changed_files=changed_files,
                        governance_disposition="FAIL",
                        governance_findings=post_findings,
                        governance_evidence_path=gov_evidence_path,
                    ),
                    output_dir,
                )
                # Treat as FAIL — reset and retry (unless last attempt)
                if attempt > max_retries:
                    findings_text = _format_findings(post_findings)
                    _record_failure_lesson(
                        lessons_conn,
                        ticket_id=ticket.ticket_id,
                        project_id=project_id,
                        failure_type="post_check_fail",
                        rule=post_findings[0].get("message", "post_check_fail").split(":")[0] if post_findings else "post_check_fail",
                        description=findings_text[:500],
                    )
                    _extract_lesson_from_outcome(
                        memory_conn,
                        ticket=ticket,
                        outcome="CODED_UNGOVERNED",
                        attempt=attempt,
                        findings=list(post_findings),
                        changed_files=changed_files,
                        error_message=findings_text[:300],
                    )
                    # Commit as ungoverned — code is still staged
                    _git_commit(
                        repo_path, ticket.ticket_id, changed_files,
                        suffix="[GOVERNANCE: review required]",
                    )
                    if on_committed:
                        on_committed(ticket.ticket_id, "CODED_UNGOVERNED", "")
                    _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "CODED_UNGOVERNED", finding_kinds=_extract_finding_kinds(post_findings))
                    if ran_stages is not None:
                        ran_stages.update(_ran_stages)
                    return TicketResult(
                        ticket_id=ticket.ticket_id,
                        disposition="CODED_UNGOVERNED",
                        attempts=attempt,
                        changed_files=tuple(changed_files),
                        governance_disposition="FAIL",
                        governance_evidence_path=gov_evidence_path,
                        error=f"Post-checks failed after {attempt} attempts — committed for review.",
                        governance_findings=tuple(post_findings),
                        verify_cmd_specified=_vc_specified(ticket),
                    )
                _git_reset_changes(repo_path)
                repair_context = _format_findings(post_findings)
                continue

            _last_post_check = "PASS"

            # Phase 5: Memory-enforced rule check — runs after post-checks pass,
            # before code_reviewer.  Error severity findings reset and retry;
            # warning severity findings are logged but do not block.
            _mem_provider = capability_registry.get("memory_provider")
            if memory_conn is not None and _mem_provider is not None:
                try:
                    _enforcement_findings = _mem_provider.enforce(memory_conn, changed_files, repo_path)
                    _ran_stages.add("memory_provider")
                    if _enforcement_findings:
                        _error_enf = [f for f in _enforcement_findings if f.get("severity", "medium") == "error"]
                        _warn_enf = [f for f in _enforcement_findings if f.get("severity", "medium") != "error"]
                        if _warn_enf:
                            logger.warning(
                                "Ticket %s: memory enforcement warnings: %s",
                                ticket.ticket_id,
                                "; ".join(f.get("detail", f.get("kind", "")) for f in _warn_enf[:3]),
                            )
                        if _error_enf:
                            logger.warning(
                                "Ticket %s: memory enforcement errors (%d) — resetting",
                                ticket.ticket_id,
                                len(_error_enf),
                            )
                            if attempt > max_retries:
                                _git_commit(
                                    repo_path, ticket.ticket_id, changed_files,
                                    suffix="[GOVERNANCE: review required — enforced rule violation]",
                                )
                                if on_committed:
                                    on_committed(ticket.ticket_id, "CODED_UNGOVERNED", "")
                                _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "CODED_UNGOVERNED", finding_kinds=_extract_finding_kinds(_error_enf, fallback_kind="memory_enforcement"))
                                _extract_lesson_from_outcome(
                                    memory_conn,
                                    ticket=ticket,
                                    outcome="CODED_UNGOVERNED",
                                    attempt=attempt,
                                    findings=list(_error_enf),
                                    changed_files=changed_files,
                                    error_message=f"Enforced rule violation after {attempt} attempts"[:300],
                                )
                                if ran_stages is not None:
                                    ran_stages.update(_ran_stages)
                                return TicketResult(
                                    ticket_id=ticket.ticket_id,
                                    disposition="CODED_UNGOVERNED",
                                    attempts=attempt,
                                    changed_files=tuple(changed_files),
                                    governance_disposition=effective_disposition,
                                    governance_evidence_path=gov_evidence_path,
                                    error=f"Enforced rule violation after {attempt} attempts — committed for review.",
                                    governance_findings=tuple(_error_enf),
                                    verify_cmd_specified=_vc_specified(ticket),
                                )
                            _git_reset_changes(repo_path)
                            repair_context = "; ".join(
                                f.get("detail", f.get("kind", "enforced rule violated"))
                                for f in _error_enf[:3]
                            )
                            continue
                except Exception as _enf_exc:  # noqa: BLE001
                    logger.debug("Memory enforcement check failed (non-fatal): %s", _enf_exc)

            # Phase 3: Post-run impact analysis on ACTUAL changed files.
            # Runs after governance passes, before contract check.  Advisory only.
            _post_impact = None  # initialise so select_verification_layers can read it
            if capability_registry.is_available("impact_analysis"):
                try:
                    from saturnday.run.impact_analysis import compute_impact, format_impact_for_prompt, explain_impact_with_llm
                    _post_impact = compute_impact(changed_files, repo_path, state)
                    _log_progress(
                        f"Impact: {_post_impact.total_blast_radius} file(s) potentially affected by {ticket.ticket_id}"
                    )
                    if _post_impact.total_blast_radius > 5:  # noqa: PLR2004
                        _llm_explanation = explain_impact_with_llm(
                            _post_impact, ticket, coder_config, repo_path,
                        )
                        if _llm_explanation:
                            logger.warning(
                                "Ticket %s impact analysis: %s",
                                ticket.ticket_id, _llm_explanation[:300],
                            )
                    _ran_stages.add("impact_analysis")
                except Exception as _post_impact_exc:
                    logger.debug("Post-run impact analysis failed: %s", _post_impact_exc)

            # T024: Execution selector — decide which verification layers to run.
            # Falls back to all-layers-enabled when impact is unavailable.
            _vlayers: dict = {
                "spec_assertions": True,
                "property_tests": True,
                "dataflow_check": True,
                "llm_spec_inference": True,
                "llm_review": True,
            }
            if capability_registry.is_available("impact_analysis"):
                try:
                    if _post_impact is not None:
                        from saturnday.run.impact_analysis import select_verification_layers  # noqa: PLC0415
                        _vlayers = select_verification_layers(_post_impact, ticket)
                        logger.debug(
                            "Ticket %s: verification layers selected: %s",
                            ticket.ticket_id, _vlayers,
                        )
                except Exception as _sel_exc:  # noqa: BLE001
                    logger.debug("Verification layer selector failed (all layers enabled): %s", _sel_exc)

            # Phase 5: Cross-function data flow check — detects type/value
            # mismatches between changed functions and their callers.
            # WARNING only — never blocks pipeline or triggers retry.
            _dataflow_findings: list[dict] = []
            if _vlayers.get("dataflow_check", True) and capability_registry.is_available("spec_verifier"):
                try:
                    from saturnday.run.dataflow_checker import (  # noqa: PLC0415
                        check_cross_function_flow,
                        format_dataflow_findings,
                    )
                    _py_changed = [f for f in changed_files if f.endswith(".py")]
                    if _py_changed:
                        _dataflow_findings = check_cross_function_flow(
                            changed_files, repo_path, state
                        )
                        if _dataflow_findings:
                            _df_text = format_dataflow_findings(_dataflow_findings)
                            logger.warning(
                                "Ticket %s: %d dataflow finding(s) (advisory):\n%s",
                                ticket.ticket_id,
                                len(_dataflow_findings),
                                _df_text[:500],
                            )
                            _log_progress(
                                f"Dataflow: {len(_dataflow_findings)} cross-function "
                                f"mismatch(es) found for {ticket.ticket_id}"
                            )
                        else:
                            logger.debug(
                                "Ticket %s: dataflow check — no findings",
                                ticket.ticket_id,
                            )
                    _ran_stages.add("spec_verifier")
                except Exception as _df_exc:  # noqa: BLE001
                    logger.debug("Dataflow check skipped: %s", _df_exc)
            else:
                logger.debug(
                    "Ticket %s: dataflow check skipped (trivial change, blast_radius=%s)",
                    ticket.ticket_id,
                    _post_impact.total_blast_radius if _post_impact else "unknown",
                )

            # Spec verification: run executable assertions derived from
            # acceptance criteria.  WARNING only — never blocks pipeline.
            _spec_verification: list[dict] = []
            if not _vlayers.get("spec_assertions", True) or not capability_registry.is_available("spec_verifier"):
                logger.debug(
                    "Ticket %s: spec verification skipped (config-only change)",
                    ticket.ticket_id,
                )
            else:
                try:
                    from saturnday.run.spec_verifier import (  # noqa: PLC0415
                        generate_spec_assertions,
                        infer_spec_assertions,
                        run_spec_assertions,
                    )
                    _spec_assertions = generate_spec_assertions(ticket, changed_files, repo_path)
                    if not _spec_assertions:
                        # Phase 4 LLM fallback: only when deterministic generation
                        # yields nothing and coder_config is available.
                        if _vlayers.get("llm_spec_inference", True):
                            try:
                                _inferred = infer_spec_assertions(
                                    ticket,
                                    list(changed_files),
                                    repo_path,
                                    coder_config,
                                )
                                if _inferred:
                                    logger.debug(
                                        "Ticket %s: inferred %d spec assertion(s) (LLM fallback)",
                                        ticket.ticket_id,
                                        len(_inferred),
                                    )
                                    _inferred_results = run_spec_assertions(_inferred, repo_path)
                                    _failed_inferred = [
                                        r for r in _inferred_results if not r.get("passed")
                                    ]
                                    if _failed_inferred:
                                        logger.warning(
                                            "Ticket %s: %d/%d inferred spec assertion(s) failed "
                                            "(LLM fallback, advisory): %s",
                                            ticket.ticket_id,
                                            len(_failed_inferred),
                                            len(_inferred_results),
                                            "; ".join(
                                                r.get("criterion", "?")[:80]
                                                for r in _failed_inferred[:3]
                                            ),
                                        )
                                    _log_progress(
                                        f"Inferred {len(_inferred)} spec assertion(s) (LLM fallback) "
                                        f"for {ticket.ticket_id}: "
                                        f"{len(_inferred_results) - len(_failed_inferred)}/"
                                        f"{len(_inferred_results)} passed"
                                    )
                                    _spec_verification = _inferred_results
                            except Exception as _infer_exc:  # noqa: BLE001
                                logger.debug("LLM spec inference skipped: %s", _infer_exc)
                    if _spec_assertions:
                        _spec_verification = run_spec_assertions(_spec_assertions, repo_path)
                        _failed_spec = [r for r in _spec_verification if not r.get("passed")]
                        if _failed_spec:
                            logger.warning(
                                "Ticket %s: %d/%d spec assertion(s) failed (advisory): %s",
                                ticket.ticket_id,
                                len(_failed_spec),
                                len(_spec_verification),
                                "; ".join(r.get("criterion", "?")[:80] for r in _failed_spec[:3]),
                            )
                        else:
                            logger.debug(
                                "Ticket %s: %d spec assertion(s) passed",
                                ticket.ticket_id,
                                len(_spec_verification),
                            )
                        _log_progress(
                            f"Spec verification: {len(_spec_verification) - len(_failed_spec)}/"
                            f"{len(_spec_verification)} assertion(s) passed for {ticket.ticket_id}"
                        )
                    _ran_stages.add("spec_verifier")
                except Exception as _spec_exc:  # noqa: BLE001
                    logger.debug("Spec verification skipped: %s", _spec_exc)

            # Property-based testing: identify eligible pure-ish functions in
            # changed files and run Hypothesis property tests.  WARNING only —
            # never blocks pipeline or triggers retry.
            _prop_results: list[dict] = []
            if not _vlayers.get("property_tests", True) or not capability_registry.is_available("spec_verifier"):
                logger.debug(
                    "Ticket %s: property tests skipped (trivial/config-only change)",
                    ticket.ticket_id,
                )
            else:
                try:
                    from saturnday.run.property_tests import (  # noqa: PLC0415
                        identify_eligible_targets,
                        generate_property_tests,
                        run_property_tests,
                    )
                    _prop_targets = identify_eligible_targets(changed_files, repo_path)
                    if _prop_targets:
                        _prop_test_content = generate_property_tests(_prop_targets, repo_path)
                        if _prop_test_content:
                            _prop_results = run_property_tests(_prop_test_content, repo_path)
                            _prop_passed = sum(1 for r in _prop_results if r.get("passed"))
                            _prop_failed = sum(1 for r in _prop_results if not r.get("passed"))
                            _prop_skipped = any(
                                r.get("test") == "SKIPPED" for r in _prop_results
                            )
                            if not _prop_skipped:
                                _log_progress(
                                    f"Property tests: {len(_prop_targets)} target(s), "
                                    f"{_prop_passed} passed, {_prop_failed} failed "
                                    f"for {ticket.ticket_id}"
                                )
                                if _prop_failed:
                                    logger.warning(
                                        "Ticket %s: %d property test(s) failed (advisory): %s",
                                        ticket.ticket_id,
                                        _prop_failed,
                                        "; ".join(
                                            r.get("detail", "")[:80]
                                            for r in _prop_results
                                            if not r.get("passed")
                                        )[:300],
                                    )
                            else:
                                logger.debug(
                                    "Ticket %s: property tests skipped (hypothesis not available)",
                                    ticket.ticket_id,
                                )
                    _ran_stages.add("spec_verifier")
                except Exception as _prop_exc:  # noqa: BLE001
                    logger.debug("Property tests skipped: %s", _prop_exc)

            # Semantic review: LLM checks code against ticket goal (WARNING only).
            # Runs AFTER all deterministic verification layers so the reviewer
            # receives full evidence.  Never blocks, never resets git state,
            # never triggers retry.
            if not _vlayers.get("llm_review", True):
                logger.debug(
                    "Ticket %s: semantic review skipped (trivial change, blast_radius=%s)",
                    ticket.ticket_id,
                    _post_impact.total_blast_radius if _post_impact else "unknown",
                )
            else:
                _reviewer = capability_registry.get("code_reviewer")
                if _reviewer is not None:
                    try:
                        _review_parts = [
                            f"## Ticket Goal\n{ticket.goal}\n",
                            "## Acceptance Criteria\n"
                            + "\n".join(f"- {c}" for c in ticket.acceptance_criteria),
                        ]

                        # --- Collect deterministic evidence (bounded to 3000 chars) ---
                        _evidence_parts: list[str] = []
                        _evidence_budget = 3000

                        # Spec verification results
                        if _spec_verification:
                            _spec_lines = []
                            for _sv in _spec_verification[:10]:
                                _status = "PASS" if _sv.get("passed") else "FAIL"
                                _crit = _sv.get("criterion", "")[:100]
                                _spec_lines.append(f"  [{_status}] {_crit}")
                            _spec_text = "## Spec Assertion Results\n" + "\n".join(_spec_lines)
                            _evidence_parts.append(_spec_text[:600])

                        # Property test results
                        if _prop_results:
                            _prop_lines = []
                            for _pr in _prop_results[:10]:
                                _ps = "PASS" if _pr.get("passed") else "FAIL"
                                _pn = _pr.get("test_name", _pr.get("target_function", ""))[:80]
                                _prop_lines.append(f"  [{_ps}] {_pn}")
                            _evidence_parts.append(
                                "## Property Test Results\n" + "\n".join(_prop_lines)
                            )

                        # Dataflow findings
                        if _dataflow_findings:
                            if capability_registry.is_available("spec_verifier"):
                                try:
                                    from saturnday.run.dataflow_checker import format_dataflow_findings as _fmt_df  # noqa: PLC0415
                                    _df_evidence = _fmt_df(_dataflow_findings)[:600]
                                except Exception:  # noqa: BLE001
                                    _df_evidence = "\n".join(
                                        f"  {f.get('kind', '')}: {f.get('detail', '')}"
                                        for f in _dataflow_findings[:5]
                                    )[:600]
                            else:
                                _df_evidence = "\n".join(
                                    f"  {f.get('kind', '')}: {f.get('detail', '')}"
                                    for f in _dataflow_findings[:5]
                                )[:600]
                            _evidence_parts.append(f"## Data Flow Findings\n{_df_evidence}")

                        if _evidence_parts:
                            _combined_evidence = "\n\n".join(_evidence_parts)[:_evidence_budget]
                            _review_parts.append(
                                f"\n## Verification Evidence\n{_combined_evidence}"
                            )

                        _review_parts.append("\n## Changed Files")
                        for _cf in changed_files:
                            _cf_path = repo_path / _cf
                            if _cf_path.is_file():
                                _file_content = _cf_path.read_text(
                                    encoding="utf-8", errors="replace"
                                )[:3000]
                                _review_parts.append(
                                    f"\n### {_cf}\n```\n{_file_content}\n```"
                                )
                        _review_task = "\n".join(_review_parts)
                        _review_result_dict = _reviewer.review(_review_task, coder_config, repo_path)
                        _ran_stages.add("code_reviewer")
                        if _review_result_dict.get("has_concerns"):
                            logger.warning(
                                "Ticket %s: semantic review found concerns: %s",
                                ticket.ticket_id,
                                _review_result_dict.get("output", "")[:500],
                            )
                            # WARNING only — do NOT reset, retry, or alter disposition.
                            # Future: promote to blocking once false-positive rate is known.
                    except Exception as _review_exc:  # noqa: BLE001
                        logger.debug("Semantic review skipped: %s", _review_exc)
                else:
                    logger.debug("Semantic review skipped: premium not registered")

            # Execute verify_cmd if specified — the mechanical proof the ticket works
            if ticket.verify_cmd:
                verify_failure = _run_verify_cmd(ticket.verify_cmd, repo_path)
                if verify_failure:
                    logger.warning(
                        "Ticket %s: verify_cmd failed: %s",
                        ticket.ticket_id, verify_failure[:200],
                    )
                    if attempt > max_retries:
                        # Commit as ungoverned — code was written and governance
                        # passed, only verify_cmd failed.  Don't lose the work.
                        _git_commit(
                            repo_path, ticket.ticket_id, changed_files,
                            suffix="[GOVERNANCE: review required — verify_cmd failed]",
                        )
                        if on_committed:
                            on_committed(ticket.ticket_id, "CODED_UNGOVERNED", "")
                        _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "CODED_UNGOVERNED", finding_kinds=["verify_cmd_failed"])
                        _extract_lesson_from_outcome(
                            memory_conn,
                            ticket=ticket,
                            outcome="CODED_UNGOVERNED",
                            attempt=attempt,
                            findings=[],
                            changed_files=changed_files,
                            error_message=f"verify_cmd failed: {verify_failure[:250]}"[:300],
                        )
                        if ran_stages is not None:
                            ran_stages.update(_ran_stages)
                        return TicketResult(
                            ticket_id=ticket.ticket_id,
                            disposition="CODED_UNGOVERNED",
                            attempts=attempt,
                            changed_files=tuple(changed_files),
                            governance_disposition=effective_disposition,
                            governance_evidence_path=gov_evidence_path,
                            error=f"verify_cmd failed after {attempt} attempts — committed for review: {verify_failure[:500]}",
                            verify_cmd_specified=_vc_specified(ticket),
                            verify_cmd_passed=False,
                            verify_cmd_failure=verify_failure[:500],
                        )
                    _git_reset_changes(repo_path)
                    repair_context = f"verify_cmd '{ticket.verify_cmd}' failed:\n{verify_failure}"
                    continue

            _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "PASS")
            _git_commit(repo_path, ticket.ticket_id, changed_files)
            if on_committed:
                on_committed(ticket.ticket_id, "PASS", "")
            if ran_stages is not None:
                ran_stages.update(_ran_stages)
            return TicketResult(
                ticket_id=ticket.ticket_id,
                disposition="PASS",
                attempts=attempt,
                changed_files=tuple(changed_files),
                governance_disposition=effective_disposition,
                governance_evidence_path=gov_evidence_path,
                verify_cmd_specified=_vc_specified(ticket),
                # True when verify_cmd ran and passed; None when no verify_cmd.
                verify_cmd_passed=True if ticket.verify_cmd else None,
            )

        # FAIL — check if this is the last attempt
        logger.warning(
            "Ticket %s governance FAIL (attempt %d)", ticket.ticket_id, attempt,
        )

        # Progress message: governance caught something
        _finding_kinds = sorted({f.get("kind", "") for f in relevant_findings if f.get("kind")})
        _why_parts = []
        try:
            from saturnday.remediation_guidance import get_guidance
            for _fk in _finding_kinds[:3]:
                _g = get_guidance(_fk)
                if _g:
                    _why_parts.append(f"{_fk}: {_g.why_it_matters[:80]}")
        except Exception:
            pass
        _retry_msg = _generate_progress_message(
            coder_config, Path(repo_path),
            f"Ticket {ticket.ticket_id} attempt {attempt}: governance caught {len(relevant_findings)} issue(s): "
            f"{', '.join(_finding_kinds[:5])}. "
            f"Goal: {ticket.goal[:100] if ticket.goal else 'N/A'}. "
            f"{'Why it matters: ' + '; '.join(_why_parts) + '. ' if _why_parts else ''}"
            f"Write one sentence: what real-world consequence would this cause if an AI coder shipped this without governance?",
        )
        if _retry_msg:
            _log_progress(_retry_msg)

        if attempt > max_retries:
            findings_text = _format_findings(relevant_findings)
            _record_failure_lesson(
                lessons_conn,
                ticket_id=ticket.ticket_id,
                project_id=project_id,
                failure_type="governance_fail",
                rule=relevant_findings[0].get("message", "governance_fail").split(":")[0] if relevant_findings else "governance_fail",
                description=findings_text[:500],
            )
            # Phase 1: structured memory extraction
            _extract_lesson_from_outcome(
                memory_conn,
                ticket=ticket,
                outcome="CODED_UNGOVERNED",
                attempt=attempt,
                findings=list(relevant_findings),
                changed_files=changed_files,
                error_message=findings_text[:300],
            )
            if auto_repair:
                # Revert before auto-repair so it starts clean
                _git_reset_changes(repo_path)
                repair_result = _attempt_auto_repair(
                    ticket=ticket,
                    repo_path=repo_path,
                    coder_config=coder_config,
                    system_prompt=system_prompt,
                    state=state,
                    plan_notes=_plan_notes_compact_for_retry,
                    output_dir=output_dir,
                    repair_context=findings_text,
                    attempt_number=attempt + 1,
                    on_committed=on_committed,
                )
                if repair_result is not None:
                    return repair_result
                # Auto-repair also failed — re-execute one last time for
                # the ungoverned commit (repair reverted changes).
                try:
                    _msgs_final, _ = _assemble_ticket_prompt(
                        ticket=ticket,
                        repo_path=repo_path,
                        coder_config=coder_config,
                        system_prompt=system_prompt,
                        state=state,
                        plan_notes=_plan_notes_compact_for_retry,
                        repair_context=findings_text,
                    )
                    _response_final, changed_files_final = _execute_ticket(
                        ticket=ticket,
                        repo_path=repo_path,
                        coder_config=coder_config,
                        messages=_msgs_final,
                    )
                    if changed_files_final:
                        _git_add(repo_path, changed_files_final)
                        _git_commit(
                            repo_path, ticket.ticket_id, changed_files_final,
                            suffix="[GOVERNANCE: review required]",
                        )
                        if on_committed:
                            on_committed(ticket.ticket_id, "CODED_UNGOVERNED", "")
                        _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "CODED_UNGOVERNED", finding_kinds=_extract_finding_kinds(relevant_findings))
                        if ran_stages is not None:
                            ran_stages.update(_ran_stages)
                        return TicketResult(
                            ticket_id=ticket.ticket_id,
                            disposition="CODED_UNGOVERNED",
                            attempts=attempt,
                            changed_files=tuple(changed_files_final),
                            governance_disposition=disposition,
                            governance_evidence_path=gov_evidence_path,
                            error=f"Governance failed after {attempt} attempts — committed for review.",
                            governance_findings=tuple(relevant_findings),
                            verify_cmd_specified=_vc_specified(ticket),
                        )
                except Exception as exc_final:
                    logger.warning("Final coder call for ungoverned commit failed: %s", exc_final)
            else:
                # Fix 18: last-resort split gate — fires before committing ungoverned
                # when repeated oversize pressure was detected across all retries.
                # Fix 19: atomic tickets must never be split; gate suppressed.
                if (
                    not _already_last_resort_split
                    and ticket.allow_last_resort_split
                    and not ticket.atomic
                    and not getattr(coder_config, "large_context", False)
                    and _budget_warn_count >= 2
                    and _max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
                ):
                    from saturnday.ticket_splitter import analyze_and_split_last_resort  # noqa: PLC0415
                    import dataclasses as _dc  # noqa: PLC0415
                    logger.warning(
                        "Ticket %s: last-resort split triggered "
                        "(budget_warn_count=%d, max_prompt_chars=%d >= OVERSIZE=%d)",
                        ticket.ticket_id, _budget_warn_count, _max_prompt_chars,
                        OVERSIZE_SPLIT_THRESHOLD,
                    )
                    _already_last_resort_split = True
                    _lrs_tickets = analyze_and_split_last_resort(
                        ticket, coder_config, repo_path, _plan_notes_compact_for_retry,
                    )
                    if len(_lrs_tickets) > 1:
                        # Snapshot HEAD before any child executes — required to
                        # hard-reset if a later child fails.  Fail closed: if
                        # the snapshot cannot be secured, do not execute children
                        # at all; preserving the original failure path is safer
                        # than risking uncommitted child residue on the branch.
                        _lrs_pre_split_sha = _git_revparse_head(repo_path)
                        if not _lrs_pre_split_sha:
                            logger.warning(
                                "Ticket %s: last-resort split aborted — branch HEAD "
                                "could not be snapshotted; preserving original failure path",
                                ticket.ticket_id,
                            )
                            write_ticket_evidence(
                                TicketEvidence(
                                    ticket_id=ticket.ticket_id,
                                    attempt=attempt + 1,
                                    prompt_chars=_max_prompt_chars,
                                    prompt_split_exempt=True,
                                    prompt_split_reason=(
                                        "last_resort_split_sha_capture_failed"
                                    ),
                                ),
                                output_dir,
                            )
                            # Fall through to original CODED_UNGOVERNED commit.
                        else:
                            # SHA secured — safe to execute children.
                            _git_reset_changes(repo_path)
                            write_ticket_evidence(
                                TicketEvidence(
                                    ticket_id=ticket.ticket_id,
                                    attempt=attempt + 1,
                                    prompt_chars=_max_prompt_chars,
                                    prompt_split_exempt=False,
                                    prompt_split_reason=(
                                        f"last_resort_split:{len(_lrs_tickets)}_sub_tickets"
                                    ),
                                ),
                                output_dir,
                            )
                            _lrs_results: list[TicketResult] = []
                            _lrs_all_passed = True
                            for _child in _lrs_tickets:
                                _child_spec = _dc.replace(
                                    _child, allow_last_resort_split=False,
                                )
                                _child_result = _run_ticket_with_retries(
                                    ticket=_child_spec,
                                    repo_path=repo_path,
                                    coder_config=coder_config,
                                    system_prompt=system_prompt,
                                    state=state,
                                    plan_notes=_plan_notes_compact_for_retry,
                                    output_dir=output_dir,
                                    max_retries=max_retries,
                                    auto_repair=auto_repair,
                                    lessons_conn=lessons_conn,
                                    memory_conn=memory_conn,
                                    project_id=project_id,
                                    ran_stages=ran_stages,
                                )
                                _lrs_results.append(_child_result)
                                if _child_result.disposition != "PASS":
                                    _lrs_all_passed = False
                                    # Hard-reset to pre-split SHA — undoes all
                                    # commits created by earlier passing children.
                                    _git_reset_hard_to(repo_path, _lrs_pre_split_sha)
                                    break
                            if _lrs_all_passed:
                                _all_lrs_files = tuple(
                                    cf for r in _lrs_results for cf in r.changed_files
                                )
                                _log_ticket_summary(
                                    ticket.ticket_id, attempt,
                                    _last_governance, _last_post_check,
                                    _post_checks_fired, "PASS",
                                )
                                if ran_stages is not None:
                                    ran_stages.update(_ran_stages)
                                return TicketResult(
                                    ticket_id=ticket.ticket_id,
                                    disposition="PASS",
                                    attempts=attempt,
                                    changed_files=_all_lrs_files,
                                    verify_cmd_specified=_vc_specified(ticket),
                                )
                            # Children failed — do not fake partial success.
                            _log_ticket_summary(
                                ticket.ticket_id, attempt,
                                _last_governance, _last_post_check,
                                _post_checks_fired, "FAIL",
                            )
                            if ran_stages is not None:
                                ran_stages.update(_ran_stages)
                            return TicketResult(
                                ticket_id=ticket.ticket_id,
                                disposition="FAIL",
                                attempts=attempt,
                                error="Last-resort split: at least one child ticket failed",
                                failure_category="oversize_defect",
                                verify_cmd_specified=_vc_specified(ticket),
                            )
                    else:
                        # No split produced — record evidence and fall through.
                        write_ticket_evidence(
                            TicketEvidence(
                                ticket_id=ticket.ticket_id,
                                attempt=attempt + 1,
                                prompt_chars=_max_prompt_chars,
                                prompt_split_exempt=True,
                                prompt_split_reason="last_resort_split_no_split",
                            ),
                            output_dir,
                        )

                # No auto-repair — commit the current code as ungoverned.
                # Changes are still staged from the last attempt.
                _git_commit(
                    repo_path, ticket.ticket_id, changed_files,
                    suffix="[GOVERNANCE: review required]",
                )
                if on_committed:
                    on_committed(ticket.ticket_id, "CODED_UNGOVERNED", "")
                _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "CODED_UNGOVERNED", finding_kinds=_extract_finding_kinds(relevant_findings))
                if ran_stages is not None:
                    ran_stages.update(_ran_stages)
                return TicketResult(
                    ticket_id=ticket.ticket_id,
                    disposition="CODED_UNGOVERNED",
                    attempts=attempt,
                    changed_files=tuple(changed_files),
                    governance_disposition=disposition,
                    governance_evidence_path=gov_evidence_path,
                    error=f"Governance failed after {attempt} attempts — committed for review.",
                    governance_findings=tuple(relevant_findings),
                    verify_cmd_specified=_vc_specified(ticket),
                )

            # If we get here, everything failed — true FAIL
            _log_ticket_summary(ticket.ticket_id, attempt, _last_governance, _last_post_check, _post_checks_fired, "FAIL")
            _extract_lesson_from_outcome(
                memory_conn,
                ticket=ticket,
                outcome="FAIL",
                attempt=attempt,
                findings=list(relevant_findings),
                changed_files=changed_files,
                error_message="Governance and ungoverned commit both failed.",
            )
            if ran_stages is not None:
                ran_stages.update(_ran_stages)
            return TicketResult(
                ticket_id=ticket.ticket_id,
                disposition="FAIL",
                attempts=attempt,
                governance_disposition=disposition,
                error=f"Governance and ungoverned commit both failed.",
                verify_cmd_specified=_vc_specified(ticket),
            )

        # Not the last attempt — revert and retry
        _git_reset_changes(repo_path)

        repair_context = _format_findings(relevant_findings)

    _log_ticket_summary(ticket.ticket_id, max_retries + 1, _last_governance, _last_post_check, _post_checks_fired, "FAIL")
    _extract_lesson_from_outcome(
        memory_conn,
        ticket=ticket,
        outcome="FAIL",
        attempt=max_retries + 1,
        findings=list(relevant_findings),
        changed_files=list(changed_files),
        error_message="Exhausted all attempts",
    )

    # Fix 18: last-resort split gate — safety-net path after full retry exhaustion.
    # Conditions: allow_last_resort_split, not already attempted, ≥2 budget warnings,
    # at least one attempt exceeded OVERSIZE_SPLIT_THRESHOLD.
    # Fix 19: atomic tickets must never be split; gate suppressed.
    if (
        not _already_last_resort_split
        and ticket.allow_last_resort_split
        and not ticket.atomic
        and _budget_warn_count >= 2
        and _max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    ):
        from saturnday.ticket_splitter import analyze_and_split_last_resort  # noqa: PLC0415
        import dataclasses as _dc  # noqa: PLC0415
        logger.warning(
            "Ticket %s: last-resort split triggered (post-loop) "
            "(budget_warn_count=%d, max_prompt_chars=%d >= OVERSIZE=%d)",
            ticket.ticket_id, _budget_warn_count, _max_prompt_chars, OVERSIZE_SPLIT_THRESHOLD,
        )
        _already_last_resort_split = True
        _lrs_tickets = analyze_and_split_last_resort(
            ticket, coder_config, repo_path, _plan_notes_compact_for_retry,
        )
        if len(_lrs_tickets) > 1:
            # Fail closed: if branch HEAD cannot be snapshotted, do not execute
            # children — the hard-reset safety net would be unavailable and
            # committed child residue could escape a failed split.
            _lrs_pre_split_sha = _git_revparse_head(repo_path)
            if not _lrs_pre_split_sha:
                logger.warning(
                    "Ticket %s: last-resort split aborted (post-loop) — branch HEAD "
                    "could not be snapshotted; preserving original failure path",
                    ticket.ticket_id,
                )
                write_ticket_evidence(
                    TicketEvidence(
                        ticket_id=ticket.ticket_id,
                        attempt=max_retries + 2,
                        prompt_chars=_max_prompt_chars,
                        prompt_split_exempt=True,
                        prompt_split_reason="last_resort_split_sha_capture_failed",
                    ),
                    output_dir,
                )
                # Fall through to FAIL return below.
            else:
                # SHA secured — safe to execute children.
                _git_reset_changes(repo_path)
                write_ticket_evidence(
                    TicketEvidence(
                        ticket_id=ticket.ticket_id,
                        attempt=max_retries + 2,
                        prompt_chars=_max_prompt_chars,
                        prompt_split_exempt=False,
                        prompt_split_reason=(
                            f"last_resort_split:{len(_lrs_tickets)}_sub_tickets"
                        ),
                    ),
                    output_dir,
                )
                _lrs_results: list[TicketResult] = []
                _lrs_all_passed = True
                for _child in _lrs_tickets:
                    _child_spec = _dc.replace(_child, allow_last_resort_split=False)
                    _child_result = _run_ticket_with_retries(
                        ticket=_child_spec,
                        repo_path=repo_path,
                        coder_config=coder_config,
                        system_prompt=system_prompt,
                        state=state,
                        plan_notes=_plan_notes_compact_for_retry,
                        output_dir=output_dir,
                        max_retries=max_retries,
                        auto_repair=auto_repair,
                        lessons_conn=lessons_conn,
                        memory_conn=memory_conn,
                        project_id=project_id,
                        ran_stages=ran_stages,
                    )
                    _lrs_results.append(_child_result)
                    if _child_result.disposition != "PASS":
                        _lrs_all_passed = False
                        # Hard-reset to pre-split SHA — undoes all commits from
                        # earlier passing children.
                        _git_reset_hard_to(repo_path, _lrs_pre_split_sha)
                        break
                if _lrs_all_passed:
                    _all_lrs_files = tuple(
                        cf for r in _lrs_results for cf in r.changed_files
                    )
                    if ran_stages is not None:
                        ran_stages.update(_ran_stages)
                    return TicketResult(
                        ticket_id=ticket.ticket_id,
                        disposition="PASS",
                        attempts=max_retries + 1,
                        changed_files=_all_lrs_files,
                        verify_cmd_specified=_vc_specified(ticket),
                    )
                if ran_stages is not None:
                    ran_stages.update(_ran_stages)
                return TicketResult(
                    ticket_id=ticket.ticket_id,
                    disposition="FAIL",
                    attempts=max_retries + 1,
                    error="Last-resort split: at least one child ticket failed",
                    failure_category="oversize_defect",
                    verify_cmd_specified=_vc_specified(ticket),
                )
        else:
            # No split produced — record evidence and fall through to FAIL return.
            write_ticket_evidence(
                TicketEvidence(
                    ticket_id=ticket.ticket_id,
                    attempt=max_retries + 2,
                    prompt_chars=_max_prompt_chars,
                    prompt_split_exempt=True,
                    prompt_split_reason="last_resort_split_no_split",
                ),
                output_dir,
            )

    if ran_stages is not None:
        ran_stages.update(_ran_stages)
    return TicketResult(
        ticket_id=ticket.ticket_id,
        disposition="FAIL",
        attempts=max_retries + 1,
        error="Exhausted all attempts",
        verify_cmd_specified=_vc_specified(ticket),
    )


def _assemble_ticket_prompt(
    ticket: TicketSpec,
    repo_path: Path,
    coder_config: CoderConfig,
    system_prompt: str,
    state: ProjectState,
    plan_notes: str,
    repair_context: str | None = None,
) -> tuple[list[dict[str, str]], dict]:
    """Assemble the final prompt messages and measure prompt budget.

    This is the single source of truth for prompt assembly.  Both
    measurement and execution use this function.

    Returns:
        Tuple of (messages, prompt_budget_dict).
    """
    cli_mode = is_cli_backend(coder_config)
    if cli_mode:
        from saturnday.project_state import write_context_file
        _ctx_path = repo_path / ".saturnday" / "context.md"
        _ctx_path.parent.mkdir(parents=True, exist_ok=True)
        write_context_file(state, _ctx_path, relevant_globs=ticket.scope.allowed_globs)
        state_summary = (
            "CRITICAL — DO NOT SKIP: Read .saturnday/context.md for the current project state "
            "(modules, functions, tests, dependencies) BEFORE writing any code. "
            "If you do NOT read this file, you will duplicate existing code, break existing "
            "interfaces, or contradict the project structure — causing governance failure and "
            "wasted retries. Do not contradict or remove anything described there unless "
            "this ticket explicitly says to."
        )
    else:
        # compact_prompts: cap context at 2000 chars to stay within local model limits
        _ctx_max = 2000 if coder_config.compact_prompts else 8000
        state_summary = generate_context_summary(
            state,
            relevant_globs=ticket.scope.allowed_globs,
            max_chars=_ctx_max,
        )
    user_prompt = build_ticket_prompt(
        ticket, state_summary, plan_notes, cli_mode=cli_mode,
    )
    messages = assemble_messages(system_prompt, user_prompt, repair_context)

    # Prompt-budget measurement (single source of truth)
    # large_context backends (e.g. local-120b with 131K window) skip the warning
    # and the last-resort split gate — their context window is not a constraint.
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    warning = (not getattr(coder_config, "large_context", False)) and (prompt_chars > PROMPT_SOFT_THRESHOLD)
    if warning:
        logger.warning(
            "PROMPT_BUDGET: %s prompt=%d chars (soft=%d hard=%d)",
            ticket.ticket_id, prompt_chars, PROMPT_SOFT_THRESHOLD, PROMPT_HARD_THRESHOLD,
        )
    else:
        logger.info("PROMPT_BUDGET: %s prompt=%d chars", ticket.ticket_id, prompt_chars)

    budget = {
        "prompt_chars": prompt_chars,
        "prompt_budget_warning": warning,
        "prompt_budget_soft_threshold": PROMPT_SOFT_THRESHOLD,
        "prompt_budget_hard_threshold": PROMPT_HARD_THRESHOLD,
        "context_compaction_applied": False,
        "prompt_split_exempt": False,
        "prompt_split_reason": None,
    }
    return messages, budget


def _execute_ticket(
    ticket: TicketSpec,
    repo_path: Path,
    coder_config: CoderConfig,
    messages: list[dict[str, str]],
) -> tuple[str, list[str]]:
    """Single attempt: call coder with pre-assembled messages, get changed files.

    For CLI backends (claude-cli, codex-cli): the agent writes files directly
    to the repo, then we check ``git status`` for what changed.

    For API backends (openai, anthropic): parse FILE blocks from the response
    and write them to the repo.

    Args:
        ticket: The ticket being executed.
        repo_path: Path to the target repository.
        coder_config: Backend configuration.
        messages: Pre-assembled OpenAI-format messages from
            ``_assemble_ticket_prompt``.

    Returns:
        Tuple of (raw_response, changed_files).
    """
    cli_mode = is_cli_backend(coder_config)

    # Record HEAD before coder runs — CLI backends may commit directly
    head_before = _git_head_sha(repo_path) if cli_mode else ""
    # Snapshot untracked files BEFORE the coder attempt so that
    # _git_changed_files can subtract them out of its post-attempt
    # output.  Without this, any pre-existing scratch file (prompts,
    # research notes, operator CLAUDE.md, etc.) reads as a coder-
    # created out-of-scope change and trips the scope gate.  API-backend
    # runs keep an empty frozenset because they don't go through the
    # CLI scope check below.
    pre_untracked: frozenset[str] = (
        _git_untracked_snapshot(repo_path) if cli_mode else frozenset()
    )

    # codex-cli and cursor-cli need agent_mode: they write files to disk
    # and their non-zero exit codes / empty stdout would incorrectly kill
    # the ticket without it.  claude-cli works correctly without it
    # (uses --print for non-interactive captured execution).
    _agent = coder_config.backend in ("codex-cli", "cursor-cli")
    response = call_coder(coder_config, messages, repo_path, agent_mode=_agent)

    if cli_mode:
        try:
            changed_files = _git_changed_files(
                repo_path,
                head_before=head_before,
                pre_attempt_untracked=pre_untracked,
            )
        except GitStateError as exc:
            # Re-raise carrying the response so error-path evidence can record it.
            raise GitStateError(str(exc), response=response) from None

        # Fix 70: scope enforcement on CLI backends.  API backends already
        # enforce scope at apply-time via _validate_path inside apply_file_blocks;
        # CLI backends write (and may commit) directly, so we validate post-hoc
        # against git status / new commits.  On violation we hard-reset to
        # head_before to undo any direct commits the agent made before raising.
        _scope_violations = _detect_scope_violations(
            changed_files,
            ticket.scope.allowed_globs,
            ticket.scope.forbidden_globs,
        )
        if _scope_violations:
            if head_before:
                _git_reset_hard_to(repo_path, head_before)
            else:
                _git_reset_changes(repo_path)
            _summary_paths = ", ".join(_scope_violations[:5])
            if len(_scope_violations) > 5:
                _summary_paths += f" (+{len(_scope_violations) - 5} more)"
            raise ScopeViolationError(
                (
                    f"Ticket {ticket.ticket_id}: CLI coder modified file(s) "
                    f"outside ticket scope (allowed_globs="
                    f"{list(ticket.scope.allowed_globs)}, forbidden_globs="
                    f"{list(ticket.scope.forbidden_globs)}): {_summary_paths}"
                ),
                violating_paths=tuple(_scope_violations),
                response=response,
            )
    else:
        changes = extract_changes(response)
        if "__unified_diff__" in changes:
            changed_files = apply_unified_diff(repo_path, changes["__unified_diff__"])
        else:
            changed_files = apply_file_blocks(
                repo_path,
                changes,
                ticket.scope.allowed_globs,
                ticket.scope.forbidden_globs,
            )

    if not changed_files:
        raise PatchExtractionError(
            f"Coder produced response but no files were changed for {ticket.ticket_id}",
            response=response,
        )

    return response, changed_files


def _git_head_sha(repo_path: Path) -> str:
    """Return the current HEAD sha, or empty string if no commits."""
    result = safe_subprocess_run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_untracked_snapshot(repo_path: Path) -> frozenset[str]:
    """Return the set of currently-untracked files in the repo.

    Used to snapshot untracked state BEFORE a coder attempt so the
    caller can subtract pre-existing untracked files from the
    post-attempt ``git status`` output.  Without this, every scratch
    note, research doc, or local prompt sitting untracked in the repo
    reads as a coder-created out-of-scope file and trips the scope gate.

    Returns an empty frozenset if git reports an error — callers use
    this snapshot only as a filter, so "best effort" is the right
    failure mode.  ``_git_changed_files`` itself still raises
    ``GitStateError`` on broken git state, so genuine repo problems
    are surfaced at the right layer.
    """
    result = safe_subprocess_run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return frozenset()
    untracked: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        if line[:2] != "??":
            continue
        rel_path = line[3:].strip()
        if rel_path:
            untracked.add(rel_path)
    return frozenset(untracked)


def _git_changed_files(
    repo_path: Path,
    head_before: str = "",
    pre_attempt_untracked: frozenset[str] = frozenset(),
) -> list[str]:
    """Detect new and modified files via ``git status`` and new commits.

    Checks both unstaged/staged changes (git status) AND files changed
    in commits made since ``head_before``.  This handles CLI backends
    like Claude Code that commit directly during execution.

    Args:
        repo_path: The target repository.
        head_before: HEAD SHA captured before the coder attempt; used
            to collect files in any commits the coder made directly.
        pre_attempt_untracked: Set of untracked files that already
            existed BEFORE this attempt (see
            :func:`_git_untracked_snapshot`).  Entries whose porcelain
            status is ``??`` AND whose path is in this set are treated
            as pre-existing and excluded from the returned list — they
            are not coder changes.  Defaults to an empty frozenset,
            which preserves pre-batch behaviour for callers that have
            not yet opted into pre/post delta accounting.

    Excludes saturnday state/evidence files from the changed list.
    """
    # Paths to ignore (our own state files, not coder output)
    _IGNORE_PREFIXES = (
        ".saturnday/",
        "saturnday-state.json",
        ".pytest_cache/",
        ".ruff_cache/",
        "__pycache__/",
    )

    changed: set[str] = set()

    # 1. Check unstaged/staged changes (git status).  ``--untracked-files=all``
    # forces git to list every individual untracked file instead of
    # collapsing whole new directories to ``?? src/`` — without that flag the
    # scope gate would flag the collapsed directory entry as an out-of-scope
    # change even when the real file ``src/wordcount/__init__.py`` IS in
    # scope.
    result = safe_subprocess_run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:200] or "unknown error"
        raise GitStateError(
            f"Saturnday relies on git to detect and govern ticket changes, but git "
            f"reported an error for '{repo_path}': {detail}. "
            f"This is a {GIT_STATE_ERROR_MARKER}, not evidence that the coder "
            f"failed to write files."
        )
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        rel_path = line[3:].strip()
        if not rel_path:
            continue
        if any(rel_path.startswith(p) for p in _IGNORE_PREFIXES):
            continue
        # Pre-existing untracked files are not coder changes.  An
        # untracked entry (``??``) whose path was already untracked
        # before the attempt is the exact scope-isolation false
        # positive this filter closes.  Tracked modifications
        # (``M`` / ``A`` / ``D`` / etc.) are unaffected.
        if line[:2] == "??" and rel_path in pre_attempt_untracked:
            continue
        changed.add(rel_path)

    # 2. Check files in new commits since head_before
    if head_before:
        head_now = _git_head_sha(repo_path)
        if head_now and head_now != head_before:
            diff_result = safe_subprocess_run(
                ["git", "diff", "--name-only", f"{head_before}..{head_now}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
            )
            for line in diff_result.stdout.splitlines():
                rel_path = line.strip()
                if not rel_path:
                    continue
                if any(rel_path.startswith(p) for p in _IGNORE_PREFIXES):
                    continue
                changed.add(rel_path)

    return sorted(changed)


def _snapshot_project_checks(repo_path: Path) -> dict[str, str]:
    """Snapshot project-level check statuses BEFORE the coder runs.

    Returns a dict like ``{"license": "PASS", "tests_pass": "FAIL", ...}``.
    Used by the regression check: only block if the coder made it worse.
    """
    from saturnday.review import (
        _check_license, _check_readme, _check_tests_pass,
        _check_project_runnable,
    )
    snapshot: dict[str, str] = {}
    # Use empty changed_files — we just want project-level status
    dummy_files = [f.name for f in repo_path.rglob("*.py") if ".venv" not in f.parts][:5]
    if not dummy_files:
        dummy_files = [f.name for f in repo_path.rglob("*.ts") if "node_modules" not in f.parts][:5]
    for check_fn, name in [
        (_check_license, "license"),
        (_check_readme, "readme"),
        (_check_tests_pass, "tests_pass"),
        (_check_project_runnable, "project_runnable"),
    ]:
        try:
            result = check_fn(repo_path, dummy_files)
            snapshot[name] = result.get("status", "UNKNOWN")
        except Exception:
            snapshot[name] = "UNKNOWN"
    logger.debug("Project check snapshot: %s", snapshot)
    return snapshot


def _run_governance(repo_path: Path, *, run_mode: bool = False, pre_ticket_state: dict[str, str] | None = None) -> tuple[str, list[dict], str, list]:
    """Run saturnday governance on staged changes.

    Args:
        repo_path: Path to the repository root.
        run_mode: If True, suppress ``excessive_blast_radius`` findings
            and apply regression filtering for project-level checks.
        pre_ticket_state: Snapshot from ``_snapshot_project_checks`` taken
            BEFORE the coder ran.  When provided, project-level findings
            that were already failing before this ticket are removed —
            only regressions (PASS→FAIL) block the ticket.

    Returns:
        Tuple of (disposition, findings_list, evidence_path, disposition_reasons).

        ``evidence_path`` is the filesystem path to the raw governance
        evidence JSON produced by ``run_governance_check``.

        ``disposition_reasons`` is the post-policy reasons surface from the
        evidence pack.  Carries the policy-cleared marker
        ``["all_findings_expected"]`` when the Fix 65 follow-up downgraded
        FAIL → WARN because every error finding is in ``expected_findings``.
        Used by Fix 78 to discriminate policy-cleared WARN from generic WARN
        without reading the evidence pack back from disk.

    Raises:
        GovernanceError: If governance itself fails to run.
    """
    try:
        from saturnday.governance import run_governance_check
    except ImportError as exc:
        raise GovernanceError(
            "saturnday package not installed. "
            "Install it with: pip install saturnday"
        ) from exc

    # Auto-detect .saturnday-policy.yaml so exemptions apply during ticket governance
    _policy_file = repo_path / ".saturnday-policy.yaml"
    _policy_arg = _policy_file if _policy_file.is_file() else None

    try:
        pack, evidence_path = run_governance_check(
            repo_path=repo_path,
            diff_range="HEAD",
            staged=True,
            policy_path=_policy_arg,
        )
    except Exception as exc:
        raise GovernanceError(f"Governance check failed: {exc}") from exc

    disposition = pack.disposition
    # Fix 78: propagate post-policy reasons in-memory to the runner gate.
    disposition_reasons = list(pack.disposition_reasons or [])
    findings: list[dict] = []
    for check in pack.check_results:
        if check.findings:
            findings.extend(check.findings)

    # In run mode, suppress blast_radius — building a project from scratch
    # is expected to touch all files.
    # Also suppress dead_code — mid-build tickets legitimately define helpers
    # before the tickets that wire callers have executed.  The post-completion
    # run_full_repo_review (not run_mode) will catch genuine dead code.
    if run_mode:
        findings = [
            f for f in findings
            if f.get("kind") not in ("excessive_blast_radius", "dead_code")
        ]

        # Regression check: if we have a pre-ticket snapshot, remove
        # project-level findings that were ALREADY failing before this ticket.
        # Only regressions (PASS→FAIL) should block.
        _PROJECT_LEVEL_CHECKS = frozenset({
            "license", "readme", "tests_pass", "project_runnable",
        })
        _PROJECT_LEVEL_KINDS = frozenset({
            "missing_license", "missing_readme", "readme_missing_section",
            "tests_failing", "tests_timeout",
            "project_no_entrypoint", "missing_project_config",
        })
        if pre_ticket_state:
            for check in pack.check_results:
                if check.name not in _PROJECT_LEVEL_CHECKS:
                    continue
                was_before = pre_ticket_state.get(check.name, "UNKNOWN")
                if was_before in ("FAIL", "UNKNOWN") and check.status == "FAIL":
                    # Pre-existing failure — not a regression. Clear findings.
                    logger.debug(
                        "Regression check: %s was %s before, still FAIL — not blocking",
                        check.name, was_before,
                    )
                    check.findings = []
                    check.status = "PASS"
            # Remove project-level finding kinds that are pre-existing
            findings = [
                f for f in findings
                if f.get("kind") not in _PROJECT_LEVEL_KINDS
                or pre_ticket_state.get(
                    {  # map kind → check name for lookup
                        "missing_license": "license",
                        "missing_readme": "readme",
                        "readme_missing_section": "readme",
                        "tests_failing": "tests_pass",
                        "tests_timeout": "tests_pass",
                        "project_no_entrypoint": "project_runnable",
                        "missing_project_config": "project_runnable",
                    }.get(f.get("kind", ""), ""),
                    "PASS",
                ) == "PASS"  # only keep if it was PASS before (regression)
            ]

        # Recalculate disposition after all filtering
        non_issue_checks = [
            c for c in pack.check_results
            if c.status in ("FAIL", "WARN") and c.name != "blast_radius"
        ]
        if disposition in ("FAIL", "WARN") and not non_issue_checks:
            disposition = "PASS"
            # Fix 78: when run-mode suppression upgrades us to PASS, the
            # post-policy reason marker no longer applies — clear it so the
            # runner gate cannot conflate suppressed-blast-radius PASS with a
            # policy-cleared WARN.
            disposition_reasons = []

    return disposition, findings, str(evidence_path), disposition_reasons


# Fix 78: explicit, narrow marker emitted by ``_apply_policy_filtering`` (Fix 65
# follow-up) when ``expected_findings`` downgrades a FAIL to WARN because every
# error finding was declared expected.  This is the ONLY WARN signal the runner
# treats as a success gate; generic WARN remains non-success.
_POLICY_CLEARED_WARN_MARKER = "all_findings_expected"


def _detect_scope_violations(
    changed_files: list[str],
    allowed_globs: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> list[str]:
    """Fix 70: Return paths that violate the ticket's scope constraints.

    Reuses :func:`saturnday.patch_extractor._validate_path` so the CLI and
    API enforcement stay in lock-step (same forbidden/allowed semantics, same
    ALWAYS_ALLOWED file set for project boilerplate).  Empty result means
    every path is in scope.
    """
    from saturnday.patch_extractor import _validate_path
    violations: list[str] = []
    for path in changed_files:
        try:
            _validate_path(path, allowed_globs, forbidden_globs)
        except PatchApplicationError:
            violations.append(path)
    return violations


def _path_is_in_scope(
    rel_path: str,
    allowed_globs: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> bool:
    """Inverse of _detect_scope_violations for a single path.  Used by Fix 71
    to decide whether a finding is in or out of the ticket's allowed scope.
    """
    from saturnday.patch_extractor import _validate_path
    try:
        _validate_path(rel_path, allowed_globs, forbidden_globs)
        return True
    except PatchApplicationError:
        return False


def _finding_fingerprint(check_name: str, finding: dict) -> tuple[str, str, str]:
    """Stable fingerprint for finding-vs-snapshot comparison (Fix 71).

    Includes (check_name, kind, file).  Line numbers are deliberately
    excluded — they shift when neighbouring code changes, which would
    cause spurious "regressions" on edits that did not actually break
    anything.  Per-(check, kind, file) granularity is the right unit:
    a regression is a NEW kind appearing in a file that did not have it.
    """
    return (
        check_name,
        str(finding.get("kind") or ""),
        str(finding.get("file") or finding.get("path") or ""),
    )


def _snapshot_oos_finding_fingerprints(
    repo_path: Path,
    allowed_globs: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> set[tuple[str, str, str]]:
    """Fix 71: snapshot the per-(check, kind, file) fingerprints of findings
    in tracked files OUTSIDE the ticket's allowed scope.

    Bounded scope: only out-of-scope tracked source files are scanned (we
    do not re-run governance on the entire repo).  When ``allowed_globs``
    is ``("**",)`` (no scope restriction) this returns an empty set
    immediately — no need to snapshot anything because every file is in
    scope and the regression filter would not apply.

    Used by :func:`_detect_oos_regressions` to distinguish NEW out-of-scope
    findings (regressions caused by the ticket's in-scope changes propagating
    outward) from pre-existing ones.
    """
    if allowed_globs == ("**",) and not forbidden_globs:
        return set()

    try:
        ls = safe_subprocess_run(
            ["git", "ls-files"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        tracked = ls.stdout.splitlines() if ls.returncode == 0 else []
    except Exception:  # noqa: BLE001
        return set()

    oos_files = [
        f for f in tracked
        if (f.endswith(".py") or f.endswith(".ts") or f.endswith(".tsx"))
        and not _path_is_in_scope(f, allowed_globs, forbidden_globs)
    ]
    if not oos_files:
        return set()

    try:
        from saturnday.review import run_review
        from saturnday.shell_policy import run_shell
        with tempfile.TemporaryDirectory() as _tmp:
            review_result = run_review(
                repo_path,
                oos_files,
                Path(_tmp),
                run_shell_func=run_shell,
                timeout_s=30,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("OOS finding snapshot failed (non-fatal): %s", exc)
        return set()

    fingerprints: set[tuple[str, str, str]] = set()
    for check_name, tool_result in (review_result.get("tools") or {}).items():
        for f in tool_result.get("findings") or []:
            if isinstance(f, dict):
                fingerprints.add(_finding_fingerprint(check_name, f))
    return fingerprints


def _detect_oos_regressions(
    findings: list[dict],
    allowed_globs: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
    pre_fingerprints: set[tuple[str, str, str]],
) -> list[dict]:
    """Fix 71: return findings that represent NEW out-of-scope regressions.

    A finding qualifies as an out-of-scope regression iff:
    - it carries a file path
    - that path is OUTSIDE the ticket's allowed scope
    - the (check_name-or-kind, kind, file) fingerprint did NOT appear in the
      pre-ticket snapshot

    Pre-existing OOS findings (in the snapshot) are deliberately not
    surfaced — only regressions caused (directly or via propagation) by
    the current ticket's changes block.

    The check_name for incoming findings is not directly attached, so we
    use the finding's ``kind`` field as the discriminator; the snapshot
    side uses (check_name, kind, file) but kind is unique per check_name
    in practice (one check produces one kind), so matching on kind+file
    is sufficient and more robust.
    """
    if allowed_globs == ("**",) and not forbidden_globs:
        return []

    pre_kind_file: set[tuple[str, str]] = {
        (kind, file) for (_check, kind, file) in pre_fingerprints
    }
    regressions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for f in findings:
        if not isinstance(f, dict):
            continue
        rel_path = str(f.get("file") or f.get("path") or "")
        if not rel_path:
            continue
        if _path_is_in_scope(rel_path, allowed_globs, forbidden_globs):
            continue
        kind = str(f.get("kind") or "")
        key = (kind, rel_path)
        if key in pre_kind_file:
            continue
        if key in seen:
            continue
        seen.add(key)
        regressions.append(f)
    return regressions


# Phase 7: sentinel populated by _negotiate_proof_retry so the caller can
# attribute the final proof command to the operator path that produced it
# (edit vs. coder retry) without changing the function's 2-tuple contract.
_NEGOTIATION_SOURCE: str | None = None


def _negotiate_proof_retry(
    *,
    original_cmd: str,
    original_failure: str,
    plan: "ProjectPlan",
    repo_path: Path,
    coder_config: CoderConfig,
) -> tuple[str, str]:
    """Phase 6: interactive proof-failure negotiation.

    When a declared-mode plan's proof fails and we're on a TTY, offer the
    operator a bounded menu:
      [1] Accept failure           → return original_failure
      [2] Let the coder retry      → invoke resolve_proof_gap_post_execution
                                     with the failing proof as failure context;
                                     if a new proof is derived, run it once.
      [3] Edit the proof in $EDITOR → open the current cmd pre-filled; save
                                      to run; saving an empty file cancels.
      [4] Exit (keep failure)      → same as [1]

    Non-interactive OR EOF/Ctrl-C at the menu: no negotiation, return the
    original failure.  Never silently promote a failed proof to success.

    Returns ``(proof_cmd, failure)`` where failure is "" on success.  The
    caller uses this pair to record proof pass vs fail.  Phase 7: the
    source that produced the final command (``"operator_edited"``,
    ``"coder_post_execution"``, or unchanged source) is exposed via the
    module-level ``_NEGOTIATION_SOURCE`` sentinel read by the caller.
    """
    import os as _os_neg
    import sys as _sys_neg

    global _NEGOTIATION_SOURCE
    _NEGOTIATION_SOURCE = None  # reset every call

    if not _sys_neg.stdin.isatty():
        return original_cmd, original_failure

    # Cap retries per invocation — at most two attempts via [2] or [3].
    _max_retries = 2
    _cmd = original_cmd
    _failure = original_failure
    _attempts = 0

    while _attempts < _max_retries and _failure:
        print()
        print("  ┌─ local_proof_cmd failed ─────────────────────────────────")
        print(f"  │ {_failure.strip().splitlines()[0][:180] if _failure.strip() else '(empty failure)'}")
        print("  └──────────────────────────────────────────────────────────")
        print("  [1] Accept failure (DoD will be marked NOT MET)")
        print("  [2] Let the coder derive a different proof and retry")
        print("  [3] Edit the proof in $EDITOR and retry")
        print("  [4] Exit (keep failure)")
        try:
            choice = input("  Select [1/2/3/4]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return _cmd, _failure

        if choice in ("1", "4", ""):
            return _cmd, _failure

        if choice == "2":
            _attempts += 1
            try:
                from saturnday.run.proof_resolver import (
                    resolve_proof_gap_post_execution,
                )
                # Collect committed changes since the run started.
                _changed = _git_changed_files(repo_path, head_before="")
                _plan_mut_dict = {
                    "project_id": plan.project_id,
                    "operating_mode": plan.operating_mode,
                    "dependency_profile": plan.dependency_profile,
                    "proof_realism": plan.proof_realism,
                    "testing_strategy": plan.testing_strategy,
                    "external_dependencies": list(plan.external_dependencies),
                    # Give the resolver the CURRENT failing command so it
                    # treats it as a gap needing re-derivation.  We force
                    # a gap marker so the eligibility gate fires.
                    "local_proof_cmd": (
                        "python -c \"import sys; sys.exit('FIX73_PROOF_GAP: "
                        "run-time retry requested by operator after previous "
                        f"proof failed: {_failure[:200]}')\""
                    ),
                    "notes": plan.notes,
                }
                _ticket_dicts = [
                    {
                        "ticket_id": t.ticket_id,
                        "goal": t.goal,
                        "acceptance_criteria": list(t.acceptance_criteria),
                    }
                    for t in plan.tickets
                ]
                status, _source = resolve_proof_gap_post_execution(
                    plan=_plan_mut_dict,
                    coder_config=coder_config,
                    repo_path=str(repo_path),
                    changed_files=_changed,
                    tickets=_ticket_dicts,
                )
                if status == "resolved_coder_post_exec":
                    _cmd = _plan_mut_dict["local_proof_cmd"]
                    print(f"\n  Coder derived a new proof — running it now.")
                    print(f"  Proof:\n{_cmd[:400]}\n")
                    _failure = _run_verify_cmd(_cmd, repo_path)
                    if not _failure:
                        _NEGOTIATION_SOURCE = "coder_post_execution"
                        return _cmd, ""
                    _NEGOTIATION_SOURCE = "coder_post_execution"
                    print("  Derived proof still failed.")
                else:
                    print("  Coder could not derive a replacement proof.")
            except Exception as exc:  # noqa: BLE001
                print(f"  Coder retry raised: {exc}")
            continue

        if choice == "3":
            _attempts += 1
            try:
                from saturnday.interactive import _edit_outcomes_in_editor
                # Reuse the editor helper by wrapping the cmd as a single
                # "outcome" line — but since _edit_outcomes_in_editor
                # strips '#' comments and blank lines, we can fit the
                # whole multi-line proof inside.  Easier: use a small
                # custom editor invocation.
                edited = _edit_proof_in_editor(_cmd)
            except Exception as exc:  # noqa: BLE001
                print(f"  Editor raised: {exc}")
                continue
            if not edited or edited == _cmd:
                print("  No changes to proof — returning original failure.")
                continue
            _cmd = edited
            _NEGOTIATION_SOURCE = "operator_edited"
            print(f"\n  Running edited proof:\n{_cmd[:400]}\n")
            _failure = _run_verify_cmd(_cmd, repo_path)
            if not _failure:
                return _cmd, ""
            print("  Edited proof still failed.")
            continue

        # Unknown choice — re-prompt.
        print(f"  Unrecognised choice {choice!r} — pick 1/2/3/4.")

    return _cmd, _failure


def _auto_repair_proof_gap(
    *,
    current_cmd: str,
    current_failure: str,
    plan: "ProjectPlan",
    repo_path: Path,
    coder_config: CoderConfig,
    ticket_results: list,
) -> tuple[str, str]:
    """Phase 9: non-interactive proof-gap auto-repair.

    Mirrors Phase 6's ``[2] Let the coder derive a different proof``
    branch but runs without operator input.  Called from the acceptance
    gate path when ``auto_repair=True`` AND the proof still carries a
    ``FIX73_PROOF_GAP`` marker — this is the bridge for CI / scripted
    runs where Phase 6 (TTY-gated) cannot fire.

    Returns ``(cmd, failure)`` like Phase 6: failure is "" on success.
    Leaves ``current_cmd`` unchanged when the resolver cannot derive a
    replacement.  Bounded to one retry — a second gap here is honestly
    unresolved.
    """
    try:
        from saturnday.run.proof_resolver import (
            resolve_proof_gap_post_execution,
        )
        _changed: list[str] = []
        for _tr in ticket_results:
            if _tr.disposition in ("PASS", "CODED_UNGOVERNED"):
                _changed.extend(_tr.changed_files)
        _plan_mut_dict = {
            "project_id": plan.project_id,
            "operating_mode": plan.operating_mode,
            "dependency_profile": plan.dependency_profile,
            "proof_realism": plan.proof_realism,
            "testing_strategy": plan.testing_strategy,
            "external_dependencies": list(plan.external_dependencies),
            # Re-prime with a failure-annotated gap so the resolver's
            # eligibility gate fires AND the prompt gets the failure
            # context as a hint about why the first derivation missed.
            "local_proof_cmd": (
                "python -c \"import sys; sys.exit('FIX73_PROOF_GAP: "
                "auto_repair requested after previous proof failed: "
                f"{current_failure[:200]}')\""
            ),
            "notes": plan.notes,
        }
        _ticket_dicts = [
            {
                "ticket_id": t.ticket_id,
                "goal": t.goal,
                "acceptance_criteria": list(t.acceptance_criteria),
            }
            for t in plan.tickets
        ]
        status, _source = resolve_proof_gap_post_execution(
            plan=_plan_mut_dict,
            coder_config=coder_config,
            repo_path=str(repo_path),
            changed_files=_changed,
            tickets=_ticket_dicts,
        )
        if status != "resolved_coder_post_exec":
            logger.info(
                "Phase 9 auto-repair: resolver did not produce a new "
                "proof (status=%s) — leaving gap intact",
                status,
            )
            return current_cmd, current_failure

        _new_cmd = _plan_mut_dict["local_proof_cmd"]
        logger.info("Phase 9 auto-repair derived new proof — executing once.")
        _new_failure = _run_verify_cmd(_new_cmd, repo_path)
        if not _new_failure:
            logger.info("Phase 9 auto-repair: derived proof PASSED.")
            return _new_cmd, ""
        logger.warning(
            "Phase 9 auto-repair: derived proof still failed: %s",
            _new_failure[:300],
        )
        return _new_cmd, _new_failure
    except Exception as exc:  # noqa: BLE001
        logger.warning("Phase 9 auto-repair raised: %s", exc)
        return current_cmd, current_failure


def _edit_proof_in_editor(cmd: str) -> str:
    """Phase 6: open ``$EDITOR`` with the current proof command pre-filled.

    Parallel to interactive._edit_outcomes_in_editor but preserves the
    multi-line shell command verbatim (no comment stripping).  Returns the
    saved content trimmed of leading/trailing whitespace.
    """
    import os as _os_ed
    import shutil as _sh_ed
    import subprocess as _sp_ed
    import tempfile as _tf_ed

    editor = _os_ed.environ.get("EDITOR") or _os_ed.environ.get("VISUAL") or ""
    if editor and not _sh_ed.which(editor.split()[0]):
        editor = ""
    if not editor:
        for fallback in ("nano", "vim", "vi"):
            if _sh_ed.which(fallback):
                editor = fallback
                break
    if not editor:
        return cmd

    with _tf_ed.NamedTemporaryFile(
        mode="w", suffix=".sh", delete=False, encoding="utf-8",
    ) as f:
        f.write(cmd or "")
        tmp_path = f.name

    try:
        _sp_ed.run([editor, tmp_path], check=False)
        new_text = Path(tmp_path).read_text(encoding="utf-8")
        return new_text.strip() or cmd
    finally:
        try:
            _os_ed.unlink(tmp_path)
        except Exception:
            pass


def _update_proof_resolution(
    run_result: "RunResult",
    *,
    status: str | None = None,
    source: str | None = None,
    narrative: str | None = None,
) -> "RunResult":
    """Phase 7: update the proof-resolution provenance on a RunResult.

    Only the fields explicitly passed are overwritten — callers at each
    transition point (plan load, Phase 5, Phase 6, acceptance pass/fail)
    update the minimum needed so the final triple
    (status, source, narrative) honestly reflects the full lifecycle.
    """
    from dataclasses import replace as _replace
    kwargs: dict = {}
    if status is not None:
        kwargs["proof_resolution_status"] = status
    if source is not None:
        kwargs["proof_resolution_source"] = source
    if narrative is not None:
        kwargs["proof_resolution_narrative"] = narrative
    if not kwargs:
        return run_result
    return _replace(run_result, **kwargs)


def _initial_proof_resolution(
    plan: "ProjectPlan",
) -> tuple[str, str, str]:
    """Phase 7: derive (status, source, narrative) from the loaded plan
    before the acceptance gate runs.

    Takes the persisted ``proof_resolution_source`` (set at plan time by
    Phase 4) and combines it with the actual shape of
    ``local_proof_cmd`` / ``acceptance_cmd`` to produce a truthful
    starting point for the run-time narrative.
    """
    from saturnday.run.proof_resolver import proof_is_gap

    is_legacy = plan.operating_mode == "legacy_unclassified"
    proof_cmd = plan.acceptance_cmd if is_legacy else plan.local_proof_cmd
    source = plan.proof_resolution_source or "none"

    if not proof_cmd:
        return (
            "not_attempted",
            "none",
            "No proof command defined — run produced no blocking proof.",
        )

    if proof_is_gap(proof_cmd):
        return (
            "unresolved_gap",
            "planner_gap" if source in ("none", "planner_gap") else source,
            "Plan shipped with a FIX73_PROOF_GAP marker — proof was not "
            "derived at plan time.  Phase 5 (post-execution derivation) "
            "and Phase 6 (operator negotiation) may still resolve it.",
        )

    if source == "coder_plan_time":
        return (
            "resolved_coder_plan_time",
            "coder_plan_time",
            "Proof was derived by the coder at plan time against the "
            "acceptance criteria.",
        )
    if source == "planner_heuristic":
        return (
            "resolved_from_planning",
            "planner_heuristic",
            "Proof was produced by a planner template for this operating "
            "mode (e.g. pipeline, storage_only).",
        )
    if source == "operator_supplied":
        return (
            "resolved_from_planning",
            "operator_supplied",
            "Proof was supplied by the operator directly in the plan.",
        )
    if source == "operator_edited":
        return (
            "resolved_operator",
            "operator_edited",
            "Proof was hand-edited by the operator before this run.",
        )
    # Catch-all — concrete proof exists but we don't know how it got there.
    return (
        "resolved_from_planning",
        source if source != "none" else "planner_heuristic",
        "Proof command was present in the plan at run time.",
    )


def _record_proof_pass(run_result: "RunResult", *, is_legacy: bool) -> "RunResult":
    """Fix 77: record a passing plan-level proof, in the right field set.

    Legacy plans (operating_mode=legacy_unclassified) populate the
    ``acceptance_cmd_passed`` field; declared-mode plans populate the
    ``local_proof_*`` field set.  Mutually exclusive — never both.

    Phase 7: also stamps ``proof_resolution_status = "passed"`` so the
    run-summary narrative terminates on the true outcome.  The source
    field is left intact — whoever resolved the proof still owns
    provenance even after the proof runs.
    """
    from dataclasses import replace as _replace
    if is_legacy:
        run_result = _replace(run_result, acceptance_cmd_passed=True)
    else:
        run_result = _replace(
            run_result,
            local_proof_attempted=True,
            local_proof_passed=True,
        )
    _prev = run_result.proof_resolution_narrative
    _passed_note = "Proof ran and passed at the final acceptance gate."
    _narr = f"{_prev}  {_passed_note}".strip() if _prev else _passed_note
    return _update_proof_resolution(
        run_result, status="passed", narrative=_narr,
    )


def _record_proof_failure(
    run_result: "RunResult",
    *,
    is_legacy: bool,
    attempted: bool,
    failure: str,
) -> "RunResult":
    """Fix 77: record a failing plan-level proof + downgrade DoD.

    ``attempted=False`` covers the setup-failed / not-approved cases where
    the proof itself never ran (still a blocking failure: the plan declared
    a proof and we could not execute it).  ``attempted=True`` is the
    proof-ran-and-failed case.

    For legacy plans, mirrors the pre-Fix-77 behaviour of populating
    acceptance_cmd_passed=False + acceptance_cmd_failure.
    For declared-mode plans, populates the local_proof_* triple.

    Phase 7: also stamps ``proof_resolution_status = "failed"`` so a
    proof that was resolved at plan time and then failed at runtime is
    not silently rolled back to ``"unresolved_gap"``.
    """
    from dataclasses import replace as _replace
    if is_legacy:
        run_result = _replace(
            run_result,
            acceptance_cmd_passed=False,
            acceptance_cmd_failure=failure,
            definition_of_done_met=False,
        )
    else:
        run_result = _replace(
            run_result,
            local_proof_attempted=attempted,
            local_proof_passed=False,
            local_proof_failure=failure,
            definition_of_done_met=False,
        )
    _prev = run_result.proof_resolution_narrative
    _failed_note = (
        "Proof ran and failed at the final acceptance gate."
        if attempted else
        "Proof could not be executed (setup or approval blocked it)."
    )
    _narr = f"{_prev}  {_failed_note}".strip() if _prev else _failed_note
    return _update_proof_resolution(
        run_result, status="failed", narrative=_narr,
    )


def _vc_specified(ticket: "TicketSpec") -> bool:
    """Fix 72b: True when the ticket plan declared a non-empty verify_cmd.

    Used at every TicketResult construction site inside the runner so the
    DoD mechanical guard can distinguish ``specified but skipped`` from
    ``never specified`` without inspecting the plan separately.
    """
    return bool(getattr(ticket, "verify_cmd", "") or "")


def _apply_dod_mechanical_guard(
    dod_met: bool,
    dod_classification: str,
    run_result: "RunResult",
) -> tuple[bool, str, list[str]]:
    """Fix 39 + Fix 74 + Fix 72b + Fix 77 mechanical DoD guard.

    Failed or skipped executable proofs must override DOD_MET regardless of
    LLM verdict:
    - any per-ticket ``verify_cmd_passed is False``  (Fix 39)
    - plan-level ``acceptance_cmd_passed is False``  (Fix 74 — legacy path)
    - plan-level ``local_proof_passed is False``  (Fix 77 — declared-mode
      path; mirrors Fix 74 across the two mutually-exclusive proof field sets)
    - any per-ticket ``verify_cmd_specified=True`` AND
      ``verify_cmd_passed is None``  (Fix 72b — specified but skipped:
      the mechanical proof was lost; treating that as success would let
      a non-PASS ticket carry an unverified outcome to DOD_MET).

    Fix 77 explicitly does NOT consult ``live_proof_passed`` — live proofs
    are supplementary by design and a failing live proof must NEVER block
    plan completion or trigger repair.

    Returns ``(new_dod_met, new_classification, reasons)`` where ``reasons`` is
    empty when no downgrade applies.  When a downgrade applies, the caller is
    responsible for replacing the run_result and re-emitting the summary.

    Pure function: no I/O, no logging.  Lets the test harness assert the
    decision directly without mocking write_run_summary or the LLM DoD pass.
    """
    failed_vc = [
        tr for tr in run_result.ticket_results if tr.verify_cmd_passed is False
    ]
    skipped_vc = [
        tr for tr in run_result.ticket_results
        if tr.verify_cmd_specified and tr.verify_cmd_passed is None
    ]
    acceptance_failed = run_result.acceptance_cmd_passed is False
    local_proof_failed = run_result.local_proof_passed is False
    # Phase 7: a declared-mode plan whose proof was never resolved (no
    # planner heuristic, no Phase 4/5/6 fix-up) is not mechanically proved
    # — treat that as a DOD_MET downgrade.  "unresolved_gap" is the
    # authoritative end-state marker for this case.
    unresolved_gap = (
        run_result.proof_resolution_status == "unresolved_gap"
    )

    if not (
        failed_vc or skipped_vc or acceptance_failed
        or local_proof_failed or unresolved_gap
    ):
        return dod_met, dod_classification, []
    if not (dod_met or dod_classification == "DOD_MET"):
        return dod_met, dod_classification, []

    reasons: list[str] = []
    if failed_vc:
        reasons.append(f"{len(failed_vc)} ticket(s) failed verify_cmd")
    if skipped_vc:
        reasons.append(
            f"{len(skipped_vc)} ticket(s) had verify_cmd specified but skipped"
        )
    if acceptance_failed:
        reasons.append("plan-level acceptance_cmd failed")
    if local_proof_failed:
        reasons.append("plan-level local_proof_cmd failed")
    if unresolved_gap:
        reasons.append(
            "local_proof_cmd remained an unresolved FIX73_PROOF_GAP"
        )
    return False, "DOD_NOT_MET", reasons


def _is_policy_cleared_warn(disposition: str, reasons: list) -> bool:
    """Return True iff the disposition is a policy-cleared WARN.

    Discriminator is the post-policy reason marker set in
    ``_apply_policy_filtering``.  Any other shape of ``reasons`` (e.g. the
    list-of-dicts produced by ``compute_disposition`` for soft-check WARN
    without a policy) leaves WARN as non-success.

    Used by the runner success gate (Fix 78) and by the verify_cmd execution
    gate (Fix 72a) so both surfaces share one explicit discriminator.
    """
    return (
        disposition == "WARN"
        and isinstance(reasons, list)
        and reasons == [_POLICY_CLEARED_WARN_MARKER]
    )


def _filter_findings_to_files(
    findings: list[dict],
    changed_files: list[str],
) -> list[dict]:
    """Filter governance findings to only those in the current ticket's files.

    Findings without a ``path`` field are kept (conservative — assume relevant).
    Findings with a ``path`` that doesn't match any changed file are dropped.
    This prevents pre-existing lint issues from blocking unrelated tickets.
    """
    if not findings or not changed_files:
        return findings

    changed_set = set(changed_files)
    relevant: list[dict] = []

    for f in findings:
        finding_path = f.get("path", "")
        if not finding_path:
            # No path info — keep it (conservative)
            relevant.append(f)
            continue
        # Check if the finding's file is one the current ticket changed
        if finding_path in changed_set:
            relevant.append(f)
        else:
            logger.debug(
                "Filtering out pre-existing finding in %s: %s",
                finding_path, f.get("message", ""),
            )

    return relevant


_DEP_FILES = frozenset({
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "package.json",
})


def _ensure_project_venv(repo_path: Path) -> None:
    """Create a .venv in the project directory if one doesn't exist."""
    venv_path = repo_path / ".venv"
    if venv_path.is_dir():
        return
    logger.info("Creating project venv at %s", venv_path)
    try:
        import sys
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_path)],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        logger.warning("Failed to create project venv: %s", exc)


def _venv_python(repo_path: Path) -> str:
    """Return the path to the project venv python, or 'python' as fallback."""
    venv_py = repo_path / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return str(venv_py)
    return "python"


_PYTHON_DEP_FILES = {
    "pyproject.toml", "setup.py", "setup.cfg",
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
}
_NODE_DEP_FILES = {"package.json"}


def _auto_install_deps(repo_path: Path, changed_files: list[str]) -> None:
    """Install dependencies if the coder modified dependency files.

    F: now resolves the correct workspace root for each changed dependency
    file rather than assuming ``repo_path``.  For a monorepo where the
    coder edited ``frontend/package.json``, ``npm install`` runs in
    ``frontend/`` — not the repo root.  Single-root repos keep identical
    behaviour because ``workspace_for_file`` returns the repo itself.

    Failures are non-fatal — governance will catch missing imports and
    the ticket can retry.
    """
    from saturnday.workspaces import workspace_for_file

    # Group changed dep files by the workspace they belong to.
    py_files_by_ws: dict[Path, list[str]] = {}
    node_files_by_ws: dict[Path, list[str]] = {}
    for f in changed_files:
        name = Path(f).name
        if name in _PYTHON_DEP_FILES:
            ws = workspace_for_file(repo_path, f)
            # Fall back to the repo itself if no detected workspace owns
            # the file (keeps pre-F behaviour for unusual layouts).
            key = ws.path if ws is not None else Path(repo_path)
            py_files_by_ws.setdefault(key, []).append(f)
        if name in _NODE_DEP_FILES:
            ws = workspace_for_file(repo_path, f)
            key = ws.path if ws is not None else Path(repo_path)
            node_files_by_ws.setdefault(key, []).append(f)

    for ws_path, _files in py_files_by_ws.items():
        _pip_install_in_workspace(ws_path)

    for ws_path, _files in node_files_by_ws.items():
        # Only run npm install when the workspace actually has a package.json
        # — the workspace resolver would not have returned it otherwise,
        # but re-check defensively because the operator could have deleted it.
        if (ws_path / "package.json").is_file():
            logger.info("package.json changed in %s — running npm install", ws_path)
            try:
                subprocess.run(
                    ["npm", "install", "--silent"],
                    cwd=str(ws_path),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except Exception as exc:
                logger.debug("Auto npm install failed in %s: %s", ws_path, exc)


def _pip_install_in_workspace(ws_path: Path) -> None:
    """Run ``pip install -e .`` and any requirements files in ``ws_path``.

    Uses the repo-root venv's pip if it exists (single-root case) or
    falls back to the system ``pip``.  Broken out of ``_auto_install_deps``
    so the monorepo path is simpler to reason about.
    """
    # Prefer a venv pip at the workspace itself, then at the repo root
    # (via the workspace's parent chain), then system pip.
    candidate_venvs = [ws_path / ".venv" / "bin" / "pip"]
    # Walk up a couple of levels looking for a repo-root .venv.
    for parent in list(ws_path.parents)[:3]:
        candidate_venvs.append(parent / ".venv" / "bin" / "pip")
    pip_cmd = "pip"
    for candidate in candidate_venvs:
        if candidate.is_file():
            pip_cmd = str(candidate)
            break

    logger.info("Dependency file changed in %s — running pip install -e .", ws_path)
    try:
        _pip_result = subprocess.run(
            [pip_cmd, "install", "-e", ".", "-q"],
            cwd=str(ws_path),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if _pip_result.returncode != 0:
            logger.warning(
                "pip install -e . failed in %s (exit %d): %s",
                ws_path, _pip_result.returncode,
                (_pip_result.stderr or "")[:300],
            )
        # Also install dev/test requirements if present at the workspace root.
        for req_file in ("requirements-dev.txt", "requirements-test.txt", "requirements.txt"):
            req_path = ws_path / req_file
            if req_path.is_file():
                subprocess.run(
                    [pip_cmd, "install", "-r", str(req_path), "-q"],
                    cwd=str(ws_path),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
    except Exception as exc:
        logger.debug("Auto pip install failed in %s: %s", ws_path, exc)


def _git_add(repo_path: Path, changed_files: list[str]) -> None:
    """Stage specific files with ``git add``."""
    if not changed_files:
        return
    cmd = ["git", "add"] + changed_files
    try:
        result = safe_subprocess_run(
            cmd, cwd=str(repo_path), capture_output=True, text=True, check=False,
        )
    except ShellPolicyViolation as exc:
        logger.error("git add blocked by shell policy: %s", exc)
        return
    if result.returncode != 0:
        logger.warning("git add failed: %s", result.stderr.strip())


def _git_commit(
    repo_path: Path, ticket_id: str, changed_files: list[str],
    *, suffix: str = "",
) -> None:
    """Commit staged changes with a ticket-tagged message.

    Fix B: sets ``SATURNDAY_INTERNAL_COMMIT=1`` on the commit subprocess
    so Saturnday's own pre-commit hook can skip governance for commits
    initiated by governed execution.  Operator commits from a shell do
    not set this variable and remain governed normally.
    """
    label = f"[{ticket_id}] Apply ticket changes"
    if suffix:
        label = f"{label} {suffix}"
    msg = f"{label}\n\nFiles: {', '.join(changed_files)}"
    _env = {**os.environ, "SATURNDAY_INTERNAL_COMMIT": "1"}
    try:
        result = safe_subprocess_run(
            ["git", "commit", "-m", msg],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            env=_env,
        )
    except ShellPolicyViolation as exc:
        logger.error("git commit blocked by shell policy: %s", exc)
        return
    if result.returncode != 0:
        logger.warning("git commit failed: %s", result.stderr.strip())
    else:
        logger.info("Committed %s: %d files", ticket_id, len(changed_files))


def _git_reset_changes(repo_path: Path) -> None:
    """Reset staged and working-tree changes on failure.

    Order matters: unstage first, then revert tracked files, then clean untracked.
    """
    # 1. Unstage everything
    try:
        safe_subprocess_run(
            ["git", "reset", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except ShellPolicyViolation as exc:
        logger.error("git reset blocked by shell policy: %s", exc)
    # 2. Revert modifications to tracked files
    try:
        safe_subprocess_run(
            ["git", "checkout", "--", "."],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except ShellPolicyViolation as exc:
        logger.error("git checkout blocked by shell policy: %s", exc)
    # 3. Remove untracked files and directories (preserve Saturnday state)
    try:
        safe_subprocess_run(
            [
                "git", "clean", "-fd",
                "-e", ".saturnday",
                "-e", ".saturnday-policy.yaml",
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except ShellPolicyViolation as exc:
        logger.error("git clean blocked by shell policy: %s", exc)


def _git_revparse_head(repo_path: Path) -> str:
    """Return the current HEAD commit SHA, or empty string on error.

    Used by the last-resort split path to snapshot the branch state before
    executing child tickets so it can be fully restored on failure.
    """
    try:
        result = safe_subprocess_run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        logger.warning(
            "git rev-parse HEAD failed (rc=%d): %s",
            result.returncode, result.stderr.strip(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("git rev-parse HEAD raised: %s", exc)
    return ""


def _git_reset_hard_to(repo_path: Path, sha: str) -> None:
    """Hard-reset the branch to *sha*, undoing all commits since that point.

    Used by the last-resort split path to undo committed child work when a
    later child ticket fails.  *sha* must be captured via
    :func:`_git_revparse_head` before any child execution begins.

    Falls back to :func:`_git_reset_changes` (working-tree only) if *sha*
    is empty, which is safer than doing nothing.
    """
    if not sha:
        logger.warning(
            "_git_reset_hard_to: no SHA — falling back to working-tree reset only"
        )
        _git_reset_changes(repo_path)
        return
    try:
        result = safe_subprocess_run(
            ["git", "reset", "--hard", sha],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error(
                "git reset --hard %s failed (rc=%d): %s",
                sha, result.returncode, result.stderr.strip(),
            )
        else:
            logger.info(
                "Hard-reset to %s — undid committed child work on branch", sha,
            )
    except ShellPolicyViolation as exc:
        logger.error("git reset --hard blocked by shell policy: %s", exc)


_GIT_EXCLUDE_ENTRIES = """\
# Saturnday operational artefacts — auto-added by saturnday
# These are generated evidence and transient state; safe to exclude from commits.
# To share these entries with your team, run: saturnday init-gitignore
.saturnday/
"""


def _ensure_git_exclude(repo_path: Path) -> None:
    """Add Saturnday entries to ``.git/info/exclude`` (repo-local, untracked).

    This avoids modifying the tracked ``.gitignore`` file which can create
    unwanted diffs and commit noise.  The ``.git/info/exclude`` file is local
    to the clone and is never committed.

    Policy decisions:
    - ``.saturnday/`` — generated operational artefacts → exclude by default
    - ``.saturnday-policy.yaml`` — project policy → NOT excluded (may be committed)
    - ``CLAUDE.md`` — team instructions → NOT excluded (may be committed)
    """
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        logger.debug("Not a git repo — skipping git exclude setup")
        return

    info_dir = git_dir / "info"
    exclude_path = info_dir / "exclude"

    try:
        info_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create .git/info: %s", exc)
        return

    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if ".saturnday/" not in existing:
            with exclude_path.open("a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(_GIT_EXCLUDE_ENTRIES)
            logger.info("Added Saturnday entries to .git/info/exclude")
    except OSError as exc:
        logger.warning("Failed to update .git/info/exclude: %s", exc)


# Legacy alias kept for backward-compatible references from test code
_ensure_gitignore = _ensure_git_exclude


def _extract_finding_kinds(findings: list[dict], fallback_kind: str = "") -> list[str]:
    """Extract sorted unique finding kinds from a list of finding dicts.

    Pure helper for Fix 64: ensures each CODED_UNGOVERNED path can
    extract the right cause from the right findings list.
    """
    kinds = set()
    for f in findings:
        k = f.get("kind", "") or f.get("message", "").split(":")[0] if f.get("kind") or f.get("message") else fallback_kind
        if k:
            kinds.add(k)
    return sorted(kinds)


def _log_ticket_summary(
    ticket_id: str,
    attempts: int,
    governance: str,
    post_check: str,
    post_checks_fired: list[str],
    final_disposition: str,
    finding_kinds: list[str] | None = None,
) -> None:
    """Emit a structured per-ticket summary at INFO level and progress log."""
    fired = ", ".join(post_checks_fired) if post_checks_fired else "none"
    logger.info(
        "TICKET SUMMARY | %s | governance=%s | post_check=%s | fired=[%s] | retries=%d | disposition=%s",
        ticket_id,
        governance or "N/A",
        post_check or "N/A",
        fired,
        attempts - 1,
        final_disposition,
    )
    # Also write to progress log file so tail -f users see outcomes
    icon = {"PASS": "✓", "FAIL": "✗", "CODED_UNGOVERNED": "⚠"}.get(final_disposition, "?")
    summary = f"{icon} {ticket_id} → {final_disposition}"
    if attempts > 1:
        summary += f" ({attempts - 1} retries)"
    if post_checks_fired:
        summary += f" [findings: {fired}]"
    _log_progress(summary)
    # Fix 54/64: gap signals for genuine Saturnday limitations
    if final_disposition == "CODED_UNGOVERNED":
        _kinds = finding_kinds or []
        _kinds_str = ", ".join(_kinds) if _kinds else "unknown"
        _log_gap("EXECUTION", "coded_ungoverned",
                 f"governance failed: {_kinds_str}",
                 ticket_id=ticket_id, retries=attempts - 1,
                 finding_kinds=_kinds)


def _record_failure_lesson(
    lessons_conn: "sqlite3.Connection | None",
    ticket_id: str,
    project_id: str,
    failure_type: str,
    rule: str,
    description: str,
) -> None:
    """Record a lesson from a final ticket failure, swallowing all errors.

    Lessons are advisory; a storage failure must never block execution.

    Args:
        lessons_conn: Open lessons DB connection, or ``None`` to skip.
        ticket_id: The failing ticket.
        project_id: The project identifier.
        failure_type: One of governance_fail, post_check_fail, coder_error,
            contract_fail.
        rule: Stable rule identifier (check name or pattern label).
        description: Human-readable description of the failure.
    """
    if lessons_conn is None:
        return
    try:
        store_lesson(
            lessons_conn,
            Lesson(
                project_id=project_id,
                ticket_id=ticket_id,
                failure_type=failure_type,
                rule=rule,
                description=description,
            ),
        )
        logger.debug(
            "Lesson recorded: ticket=%s failure_type=%s rule=%s",
            ticket_id, failure_type, rule,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to record lesson for %s: %s", ticket_id, exc)


def _extract_lesson_from_outcome(
    memory_conn: "sqlite3.Connection | None",
    ticket: "TicketSpec",
    outcome: str,
    attempt: int,
    findings: list[dict],
    changed_files: list[str],
    error_message: str,
) -> None:
    """Extract and persist a structured MemoryItem from a FAIL/CODED_UNGOVERNED outcome.

    Creates a MemoryItem from all available ticket execution evidence and
    stores it in the ``memory_items`` table. For severe failures (security
    findings, deleted tests, shell execution) also generates and stores a
    candidate Rule via :func:`generate_candidate_rule`.

    This function swallows all errors — storage failures must never block
    the execution pipeline.

    Args:
        memory_conn: Open connection from :func:`init_memory_db`, or ``None``
            to skip (no-op).
        ticket: The failing ticket specification.
        outcome: Disposition string — ``'FAIL'`` or ``'CODED_UNGOVERNED'``.
        attempt: Attempt number at time of failure.
        findings: Governance / post-check findings (list of dicts with at
            least ``'message'`` and optionally ``'severity'``, ``'path'``).
        changed_files: Files modified by the coder during this ticket.
        error_message: Human-readable failure summary (may be empty).
    """
    if memory_conn is None:
        return
    if not capability_registry.is_available("memory_provider"):
        logger.debug("Memory extraction skipped: premium not registered")
        return
    try:
        from saturnday.run.lessons import (
            MemoryItem,
            Rule,
            generate_candidate_rule,
            store_memory_item,
            store_rule,
        )
        import uuid as _uuid
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        now = _dt.now(_tz.utc).isoformat()

        # Collect finding IDs and raw messages
        finding_ids: list[str] = [
            f.get("id", f.get("kind", f.get("message", "unknown")))[:80]
            for f in findings
        ]

        # Determine severity from findings; default medium
        severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        max_sev = "medium"
        for f in findings:
            f_sev = (f.get("severity") or "medium").lower()
            if severity_order.get(f_sev, 0) > severity_order.get(max_sev, 0):
                max_sev = f_sev

        # Auto-detect risk tags from findings and file paths
        risk_tags: list[str] = []
        for f in findings:
            msg = (f.get("message") or "").lower()
            if "shell" in msg or "subprocess" in msg:
                risk_tags.append("shell_exec")
            if "security" in msg or "secret" in msg or "credential" in msg:
                risk_tags.append("security")
            if "delete" in msg:
                risk_tags.append("deletion")
        for path in changed_files:
            p_lower = path.lower()
            if ("test_" in p_lower or "_test.py" in p_lower):
                risk_tags.append("test_modification")
            if any(p_lower.endswith(ext) for ext in (".yaml", ".yml", ".toml")) \
                    or "dockerfile" in p_lower:
                risk_tags.append("config_change")
            if p_lower.endswith(".lock"):
                risk_tags.append("dependency_change")
        # Deduplicate while preserving first occurrence
        seen: set[str] = set()
        unique_tags: list[str] = []
        for tag in risk_tags:
            if tag not in seen:
                seen.add(tag)
                unique_tags.append(tag)
        risk_tags = unique_tags

        # Build corrective rule text from finding messages
        finding_messages = [f.get("message", "") for f in findings if f.get("message")]
        corrective_text = (
            "; ".join(finding_messages[:3])[:400] if finding_messages else error_message[:400]
        )

        item = MemoryItem(
            item_id=f"mem-{ticket.ticket_id}-{attempt}-{_uuid.uuid4().hex[:8]}",
            memory_type="lesson",
            created_at=now,
            ticket_id=ticket.ticket_id,
            ticket_goal=(ticket.goal or "")[:500],
            outcome=outcome,
            failure_mode=outcome,
            severity=max_sev,
            finding_ids=finding_ids[:20],
            files_touched=list(changed_files)[:20],
            risk_tags=risk_tags,
            root_cause=error_message[:300] if error_message else None,
            corrective_rule_text=corrective_text or None,
            stale_after=(_dt.now(_tz.utc) + _td(days=30)).isoformat(),
            status="active",
        )
        store_memory_item(memory_conn, item)
        logger.debug(
            "MemoryItem stored: ticket=%s outcome=%s severity=%s tags=%s",
            ticket.ticket_id, outcome, max_sev, risk_tags,
        )

        # Extension 3: populate contrastive fields from governance findings
        if findings:
            top_finding = findings[0]
            wrong = (top_finding.get("message") or "")[:400].strip()
            remediation = (
                top_finding.get("remediation")
                or top_finding.get("fix")
                or top_finding.get("hint")
                or ""
            )[:400].strip()
            if wrong:
                try:
                    from saturnday.run.lessons import store_contrastive_lesson
                    store_contrastive_lesson(
                        memory_conn,
                        item,
                        wrong=wrong,
                        correct=remediation or "See governance finding for fix guidance.",
                        why_wrong=f"Governance finding in {top_finding.get('path', 'unknown')}",
                        why_correct="Satisfies governance policy constraints.",
                    )
                except Exception as _ce:
                    logger.debug("Contrastive lesson storage skipped: %s", _ce)

        # Generate candidate rule for severe failures
        candidate = generate_candidate_rule(item)
        if candidate is not None:
            try:
                store_rule(memory_conn, candidate)
                logger.debug(
                    "Candidate rule generated: rule_id=%s from ticket=%s",
                    candidate.rule_id, ticket.ticket_id,
                )
            except Exception as rule_exc:
                logger.debug("Candidate rule storage skipped: %s", rule_exc)

    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Failed to extract memory item for %s: %s", ticket.ticket_id, exc
        )


def _format_findings(findings: list[dict]) -> str:
    """Format governance findings into text for repair context."""
    if not findings:
        return "No specific findings."
    lines: list[str] = []
    for f in findings:
        path = f.get("path", "")
        msg = f.get("message", str(f))
        if path:
            lines.append(f"- {path}: {msg}")
        else:
            lines.append(f"- {msg}")
    return "\n".join(lines)


def _run_verify_cmd(verify_cmd: str, repo_path: Path) -> str:
    """Execute a ticket's verify_cmd and return failure details or empty string.

    Uses regular ``subprocess.run`` (not safe_subprocess_run) because
    verify_cmd is planner-generated, not untrusted input.  The shell policy
    allowlist would block legitimate commands like ``python -c "from X import Y"``.

    Activates the project venv if it exists so ``python`` and ``pytest``
    resolve to the venv, not the system install.
    """
    logger.info("Running verify_cmd: %s", verify_cmd)
    # Prepend venv activation so python/pytest use the project venv
    venv_activate = repo_path / ".venv" / "bin" / "activate"
    if venv_activate.is_file():
        verify_cmd = f"source {venv_activate} && {verify_cmd}"
    try:
        result = subprocess.run(
            ["bash", "-c", verify_cmd],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            return ""
        output = (result.stdout + result.stderr)[-500:]
        return f"Exit code {result.returncode}: {output}"
    except subprocess.TimeoutExpired:
        return "verify_cmd timed out after 120 seconds"
    except Exception as exc:
        return f"verify_cmd error: {exc}"


def _run_acceptance_setup_step(step_cmd: str, repo_path: Path) -> str:
    """Execute a single acceptance setup step.

    Same style as ``_run_verify_cmd`` but with a longer timeout (300s)
    suitable for heavier setup operations like downloads or installs.

    Args:
        step_cmd: Shell command to execute.
        repo_path: Repository root (working directory).

    Returns:
        Empty string on success, failure description on error.
    """
    logger.info("Running acceptance setup step: %s", step_cmd)
    venv_activate = repo_path / ".venv" / "bin" / "activate"
    if venv_activate.is_file():
        step_cmd = f"source {venv_activate} && {step_cmd}"
    try:
        result = subprocess.run(
            ["bash", "-c", step_cmd],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if result.returncode == 0:
            return ""
        output = (result.stdout + result.stderr)[-500:]
        return f"Exit code {result.returncode}: {output}"
    except subprocess.TimeoutExpired:
        return "Setup step timed out after 300 seconds"
    except Exception as exc:
        return f"Setup step error: {exc}"


def _check_contracts(ticket: TicketSpec, repo_path: Path, *, run_mode: bool = False) -> str:
    """Verify ticket acceptance criteria contracts against the repo.

    Parses acceptance criteria for verifiable patterns (function/class/file/test
    existence) and checks each against the current repo state.

    Args:
        ticket: The ticket whose ``acceptance_criteria`` to verify.
        repo_path: Absolute path to the repository root.
        run_mode: When ``True``, blocking contract failures are NOT returned as
            repair context — they are logged at INFO level for operator visibility
            and the function returns ``""`` so the retry loop is not triggered.
            Use this during multi-ticket governed runs to prevent forward-reference
            false failures (artifacts created by later tickets) from incorrectly
            blocking the current ticket.  A separate end-of-run contract sweep
            (in ``run_plan``) enforces contracts against the completed repo state.

    Returns:
        An empty string if all contracts pass (or there are none), or if
        ``run_mode=True`` regardless of outcome.
        A non-empty human-readable failure summary if any contract fails
        and ``run_mode=False``; suitable for use as ``repair_context``.
    """
    if not ticket.acceptance_criteria:
        return ""

    from saturnday.run.contract_checker import (
        extract_contracts,
        format_contract_results,
        verify_contracts,
    )

    contracts = extract_contracts(ticket.acceptance_criteria)
    if not contracts:
        # No verifiable patterns found — nothing to check
        return ""

    if run_mode:
        # During a multi-ticket governed run, contract failures are deferred to
        # the end-of-run sweep.  A failure here may be a forward reference —
        # an artifact that a later ticket will create.  Skip the scan entirely
        # to avoid wasting AST work on criteria that will be re-verified after
        # all tickets complete.
        logger.info(
            "Contract verification: %d contract(s) deferred for %s"
            " (run_mode — will re-verify in end-of-run contract sweep)",
            len(contracts), ticket.ticket_id,
        )
        return ""

    results = verify_contracts(contracts, repo_path)
    failed_blocking = [r for r in results if not r.verified and r.severity == "error"]
    failed_advisory = [r for r in results if not r.verified and r.severity == "warning"]

    if failed_advisory:
        logger.info(
            "Contract verification: %d advisory warning(s) for %s",
            len(failed_advisory), ticket.ticket_id,
        )
        advisory_lines = format_contract_results(failed_advisory)
        logger.info("Advisory contract warnings:\n%s", advisory_lines)

    if not failed_blocking:
        logger.debug(
            "Contract verification: all blocking contract(s) passed for %s",
            ticket.ticket_id,
        )
        return ""

    logger.warning(
        "Contract verification: %d/%d blocking contract(s) FAILED for %s",
        len(failed_blocking), len(results), ticket.ticket_id,
    )
    failure_lines = format_contract_results(failed_blocking)
    return (
        f"Acceptance criteria not satisfied ({len(failed_blocking)} contract(s) failed):\n"
        + failure_lines
    )


def _attempt_auto_repair(
    ticket: TicketSpec,
    repo_path: Path,
    coder_config: CoderConfig,
    system_prompt: str,
    state: ProjectState,
    plan_notes: str,
    output_dir: Path,
    repair_context: str,
    attempt_number: int,
    on_committed: "Callable[[str, str, str], None] | None" = None,
) -> TicketResult | None:
    """Attempt a single auto-repair pass after retry exhaustion.

    This is a one-shot extra attempt that uses a repair-framed context derived
    from the final governance findings.  It is not a loop — if governance still
    fails after this attempt, the ticket stays FAIL.

    Args:
        ticket: The ticket being repaired.
        repo_path: Path to the target repository.
        coder_config: Backend configuration for the AI coder.
        system_prompt: Pre-built system prompt for the coder.
        state: Current project state.
        plan_notes: Free-form notes injected into every coder prompt.
        output_dir: Directory for evidence output.
        repair_context: Formatted findings text from the last failed attempt.
        attempt_number: The attempt number for logging and evidence.

    Returns:
        A passing ``TicketResult`` if the repair attempt succeeds, or
        ``None`` if the repair attempt also fails (caller should return FAIL).
    """
    logger.info(
        "Ticket %s: auto-repair attempt (attempt %d) starting",
        ticket.ticket_id, attempt_number,
    )

    # Wrap repair_context with explicit repair framing
    repair_prompt = (
        "AUTO-REPAIR PASS: The previous attempts failed governance. "
        "Carefully fix ONLY the issues listed below:\n"
        + repair_context
    )

    _repair_messages, _repair_budget = _assemble_ticket_prompt(
        ticket=ticket,
        repo_path=repo_path,
        coder_config=coder_config,
        system_prompt=system_prompt,
        state=state,
        plan_notes=plan_notes,
        repair_context=repair_prompt,
    )

    try:
        response, changed_files = _execute_ticket(
            ticket=ticket,
            repo_path=repo_path,
            coder_config=coder_config,
            messages=_repair_messages,
        )
    except CloudCoreError as exc:
        logger.warning(
            "Ticket %s auto-repair attempt failed during execution: %s",
            ticket.ticket_id, exc,
        )
        write_ticket_evidence(
            TicketEvidence(
                ticket_id=ticket.ticket_id,
                attempt=attempt_number,
                coder_response=getattr(exc, "response", ""),
                governance_evidence_path="",
                error=f"Auto-repair execution error: {exc}",
                prompt_chars=_repair_budget.get("prompt_chars", 0),
                prompt_budget_warning=_repair_budget.get("prompt_budget_warning", False),
                prompt_budget_soft_threshold=_repair_budget.get("prompt_budget_soft_threshold", 0),
                prompt_budget_hard_threshold=_repair_budget.get("prompt_budget_hard_threshold", 0),
            ),
            output_dir,
        )
        return None

    _git_add(repo_path, changed_files)

    try:
        disposition, findings, gov_evidence_path, disposition_reasons = _run_governance(repo_path, run_mode=True)
    except GovernanceError as exc:
        logger.warning(
            "Ticket %s auto-repair governance check failed: %s",
            ticket.ticket_id, exc,
        )
        _git_reset_changes(repo_path)
        write_ticket_evidence(
            TicketEvidence(
                ticket_id=ticket.ticket_id,
                attempt=attempt_number,
                coder_response=response,
                changed_files=changed_files,
                governance_evidence_path="",
                error=f"Auto-repair governance error: {exc}",
                prompt_chars=_repair_budget.get("prompt_chars", 0),
                prompt_budget_warning=_repair_budget.get("prompt_budget_warning", False),
                prompt_budget_soft_threshold=_repair_budget.get("prompt_budget_soft_threshold", 0),
                prompt_budget_hard_threshold=_repair_budget.get("prompt_budget_hard_threshold", 0),
            ),
            output_dir,
        )
        return None

    relevant_findings = _filter_findings_to_files(findings, changed_files)
    effective_disposition = disposition
    if disposition == "FAIL" and not relevant_findings:
        effective_disposition = "PASS"

    # Fix 78: same narrow runner success gate as the main loop.
    _runner_success = (
        effective_disposition == "PASS"
        or _is_policy_cleared_warn(effective_disposition, disposition_reasons)
    )

    write_ticket_evidence(
        TicketEvidence(
            ticket_id=ticket.ticket_id,
            attempt=attempt_number,
            coder_response=response,
            changed_files=changed_files,
            governance_disposition=effective_disposition,
            governance_findings=relevant_findings,
            governance_evidence_path=gov_evidence_path,
            prompt_chars=_repair_budget.get("prompt_chars", 0),
            prompt_budget_warning=_repair_budget.get("prompt_budget_warning", False),
            prompt_budget_soft_threshold=_repair_budget.get("prompt_budget_soft_threshold", 0),
            prompt_budget_hard_threshold=_repair_budget.get("prompt_budget_hard_threshold", 0),
        ),
        output_dir,
    )

    if _runner_success:
        # Run post-checks on the repair result
        post_findings = run_post_checks(repo_path, changed_files)
        if post_findings:
            logger.warning(
                "Ticket %s auto-repair passed governance but failed post-checks (%d issue(s))",
                ticket.ticket_id, len(post_findings),
            )
            _git_reset_changes(repo_path)
            return None

        _git_commit(repo_path, ticket.ticket_id, changed_files)
        if on_committed:
            on_committed(ticket.ticket_id, "PASS", "")
        logger.info(
            "Ticket %s auto-repair PASSED (attempt %d)",
            ticket.ticket_id, attempt_number,
        )
        return TicketResult(
            ticket_id=ticket.ticket_id,
            disposition="PASS",
            attempts=attempt_number,
            changed_files=tuple(changed_files),
            governance_disposition=effective_disposition,
            governance_evidence_path=gov_evidence_path,
            verify_cmd_specified=_vc_specified(ticket),
        )

    # Repair also failed governance
    _git_reset_changes(repo_path)
    logger.warning(
        "Ticket %s auto-repair attempt also failed governance",
        ticket.ticket_id,
    )
    _log_gap("EXECUTION", "auto_repair_failed",
             "auto-repair also failed governance",
             ticket_id=ticket.ticket_id)
    return None
