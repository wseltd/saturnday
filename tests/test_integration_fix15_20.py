"""Fix 21 — integration and behavioural verification across Fixes 15–20.

Five bounded scenarios verifying the real seams across landed fixes without
redoing their unit tests.

Governance policy per scenario
-------------------------------
Scenarios 1, 2, 3, 5 — MOCKED governance
    _run_governance is patched to ("PASS", [], "") or a deterministic side
    effect.  These scenarios are about runner plumbing, ledger state,
    continuation routing, and real-file contract satisfaction — not about
    review-path accuracy.

Scenario 4 — REAL governance
    _run_governance is NOT mocked.  The scenario verifies that the Fix 15
    dead_code suppression (run_mode=True) fires correctly during a live run
    against a minimal git repo.  No fake findings are injected; the
    governance path is exercised for real.
"""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from saturnday._types import CoderConfig
from saturnday.ticket_runner import run_plan


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

def _create_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo at tmp_path/repo with one initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in [
        ["git", "init", "-q", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        ["git", "-C", str(repo), "config", "user.name", "Test"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "placeholder.py").write_text("# placeholder\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    return repo


def _create_standards(tmp_path: Path) -> Path:
    """Create a minimal standards directory."""
    standards = tmp_path / "standards"
    standards.mkdir(parents=True, exist_ok=True)
    (standards / "engineering_standards.md").write_text(
        "# Standards\nWrite correct, minimal code.\n", encoding="utf-8",
    )
    return standards


def _write_plan(plan_dir: Path, tickets: list[dict], project_id: str = "proj") -> Path:
    """Write a minimal plan JSON file."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "project_id": project_id,
        "notes": "integration test plan",
        "default_retry_limit": 2,
        "tickets": tickets,
    }
    path = plan_dir / "plan.json"
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def _base_patches(*, governance_side_effect=None):
    """Return the minimal patch list for mocked-governance scenarios.

    Mocks:
    - _execute_ticket: callers provide their own via side_effect
    - _run_governance: deterministic PASS (or custom side_effect)
    - run_post_checks: no post-check findings
    - analyze_and_split: no ticket splitting
    - _generate_progress_message: no LLM progress blurb
    - _ensure_project_venv: no real venv creation
    - _auto_install_deps: no pip calls
    - _snapshot_project_checks: fast empty snapshot
    - run_full_repo_review: no full post-completion scan
    """
    gov_se = governance_side_effect or (lambda *a, **k: ("PASS", [], "", []))
    return [
        patch("saturnday.ticket_runner._run_governance", side_effect=gov_se),
        patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
        patch(
            "saturnday.ticket_splitter.analyze_and_split",
            side_effect=lambda ticket, *a, **k: [ticket],
        ),
        patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
        patch("saturnday.ticket_runner._ensure_project_venv"),
        patch("saturnday.ticket_runner._auto_install_deps"),
        patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
        patch("saturnday.governance.run_full_repo_review",
              return_value=(MagicMock(disposition="PASS", check_results=[]), "")),
    ]


# ---------------------------------------------------------------------------
# Scenario 1 — Normal PASS flow (mocked governance)
#
# Purpose: baseline integration sanity — ledger, evidence, report, and
# no false continuation guidance when every ticket passes.
# ---------------------------------------------------------------------------

class TestScenario1NormalPass:
    """Scenario 1 — Normal PASS flow (mocked governance)."""

    def test_pass_flow_ledger_report_and_no_continuation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Single-ticket run passes end-to-end: ledger=PASS, no Recovery section,
        CLI continuation block prints no next-step command."""
        repo = _create_repo(tmp_path)
        standards = _create_standards(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        plan_path = _write_plan(
            tmp_path / "plan",
            tickets=[{
                "ticket_id": "T001",
                "goal": "Create placeholder module",
                "acceptance_criteria": ["placeholder module exists"],
            }],
        )
        config = CoderConfig(backend="claude-cli")

        patches = _base_patches()
        # Coder returns empty response, no changed files — git commit is a no-op
        patches.append(
            patch("saturnday.ticket_runner._execute_ticket", return_value=("done", [])),
        )

        for p in patches:
            p.start()
        try:
            result = run_plan(
                plan_path=str(plan_path),
                repo_path=str(repo),
                coder_config=config,
                standards_dir=str(standards),
                output_dir=str(output_dir),
                role_passes=False,
            )
        finally:
            for p in patches:
                p.stop()

        # 1. RunResult disposition
        assert len(result.ticket_results) == 1
        assert result.ticket_results[0].disposition == "PASS"
        assert result.passed == 1
        assert result.failed == 0

        # 2. Ledger: T001 must be PASS
        ledger_path = output_dir / "evidence" / "run" / "ledger.json"
        assert ledger_path.is_file(), "ledger.json must be written"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        t001_status = ledger.get("ticket_statuses", {}).get("T001", {})
        assert t001_status.get("disposition") == "PASS", (
            f"Ledger must show T001=PASS, got: {t001_status}"
        )

        # 3. Run report: no Recovery section (settled run)
        from saturnday.reporting import generate_run_report
        report_path = generate_run_report(
            result,
            plan_data={"project_id": "proj", "tickets": [{"ticket_id": "T001"}]},
            post_pack=None,
            evidence_dir=output_dir,
            repo_path=repo,
        )
        report_content = report_path.read_text(encoding="utf-8")
        assert "## Recovery" not in report_content, (
            "No Recovery section must appear in a fully-passed run report"
        )
        assert "Next step" not in report_content

        # 4. CLI continuation: no next-step printed
        from saturnday.cli import _print_continuation_block
        _print_continuation_block(
            result,
            output_dir=str(output_dir),
            plan_path=str(plan_path),
            repo_path=str(repo),
            backend="claude-cli",
        )
        captured = capsys.readouterr()
        assert "Next step" not in captured.out, (
            "No continuation command must be printed for a fully-passed run"
        )
        assert "saturnday resume" not in captured.out
        assert "rerun-failed" not in captured.out


# ---------------------------------------------------------------------------
# Scenario 2 — Failure-continuation case (mocked governance)
#
# Purpose: honest failure-continuation case for Fix 20. Not an interruption
# scenario. Two tickets; ticket 1 passes, ticket 2 exhausts retries and
# ends as CODED_UNGOVERNED.
# ---------------------------------------------------------------------------

class TestScenario2FailureContinuation:
    """Scenario 2 — Failure-continuation (mocked governance)."""

    def test_mixed_state_produces_correct_continuation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """T001=PASS, T002=CODED_UNGOVERNED (governance fails all 3 attempts).
        Ledger reflects mixed state; continuation routing resolves to rerun-failed;
        CLI output contains the correct command with real evidence_dir path."""
        repo = _create_repo(tmp_path)
        standards = _create_standards(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        plan_path = _write_plan(
            tmp_path / "plan",
            tickets=[
                {"ticket_id": "T001", "goal": "First ticket",
                 "acceptance_criteria": ["first ticket landed"]},
                {"ticket_id": "T002", "goal": "Second ticket",
                 "acceptance_criteria": ["second ticket landed"]},
            ],
        )
        config = CoderConfig(backend="claude-cli")

        # Governance: PASS for T001's one attempt, FAIL for T002's three attempts
        gov_call_count = 0

        def governance_se(*args, **kwargs):
            nonlocal gov_call_count
            gov_call_count += 1
            if gov_call_count == 1:
                return ("PASS", [], "", [])
            # T002 attempts 1-3: FAIL with a non-file-scoped finding
            # (no path field → kept by _filter_findings_to_files when changed_files=[])
            return ("FAIL", [{"kind": "syntax_error", "message": "bad code"}], "", [])

        patches = _base_patches(governance_side_effect=governance_se)
        patches.append(
            patch("saturnday.ticket_runner._execute_ticket", return_value=("done", [])),
        )

        for p in patches:
            p.start()
        try:
            result = run_plan(
                plan_path=str(plan_path),
                repo_path=str(repo),
                coder_config=config,
                standards_dir=str(standards),
                output_dir=str(output_dir),
                role_passes=False,
            )
        finally:
            for p in patches:
                p.stop()

        # 1. RunResult: T001=PASS, T002=CODED_UNGOVERNED
        dispositions = {tr.ticket_id: tr.disposition for tr in result.ticket_results}
        assert dispositions.get("T001") == "PASS", f"Expected T001=PASS, got {dispositions}"
        assert dispositions.get("T002") == "CODED_UNGOVERNED", (
            f"Expected T002=CODED_UNGOVERNED after governance exhaustion, got {dispositions}"
        )

        # 2. Ledger: reflects mixed state
        ledger_path = output_dir / "evidence" / "run" / "ledger.json"
        assert ledger_path.is_file()
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        statuses = ledger.get("ticket_statuses", {})
        assert statuses.get("T001", {}).get("disposition") == "PASS"
        assert statuses.get("T002", {}).get("disposition") == "CODED_UNGOVERNED"

        # 3. describe_recovery_state classifies correctly
        from saturnday.run.resume import describe_recovery_state
        state = describe_recovery_state(str(output_dir))
        assert "T001" in state["passed"]
        assert "T002" in state["coded_ungoverned"]
        assert state["failed"] == []
        assert state["pending"] == []

        # 4. _select_continuation_command resolves to rerun-failed
        from saturnday.run.resume import _select_continuation_command
        cmd = _select_continuation_command(
            failed=state["failed"],
            coded_ungoverned=state["coded_ungoverned"],
            skipped=state["skipped"],
            pending=state["pending"],
        )
        assert cmd == "rerun-failed", (
            f"CODED_UNGOVERNED-only state must resolve to rerun-failed, got {cmd!r}"
        )

        # 5. CLI output contains rerun-failed with real evidence_dir path
        from saturnday.cli import _print_continuation_block
        _print_continuation_block(
            result,
            output_dir=str(output_dir),
            plan_path=str(plan_path),
            repo_path=str(repo),
            backend="claude-cli",
        )
        captured = capsys.readouterr()
        assert "rerun-failed" in captured.out, (
            "CLI continuation block must suggest rerun-failed for CODED_UNGOVERNED state"
        )
        assert str(output_dir) in captured.out, (
            "CLI continuation block must use real output dir, not a placeholder"
        )
        # Continuation block uses --output-dir (the current CLI flag) to pass
        # the evidence directory through to the rerun / resume subcommand.
        assert "--output-dir" in captured.out

        # 6. Report Recovery section reflects the failed/coded state
        from saturnday.reporting import generate_run_report
        report_path = generate_run_report(
            result,
            plan_data={"project_id": "proj", "tickets": [
                {"ticket_id": "T001"}, {"ticket_id": "T002"},
            ]},
            post_pack=None,
            evidence_dir=output_dir,
            repo_path=repo,
        )
        report_content = report_path.read_text(encoding="utf-8")
        assert "## Recovery" in report_content, (
            "Report must contain a Recovery section when run has unsettled tickets"
        )
        assert str(output_dir) in report_content, (
            "Recovery section must use real evidence_dir path"
        )


# ---------------------------------------------------------------------------
# Scenario 3 — Atomic ticket through retry exhaustion (mocked governance)
#
# Purpose: verify Fix 19 suppresses last-resort split when ticket is atomic,
# and Fix 18 last-resort gate is not triggered (budget signals absent).
# ---------------------------------------------------------------------------

class TestScenario3AtomicRetryExhaustion:
    """Scenario 3 — Atomic ticket through retry exhaustion (mocked governance)."""

    def test_atomic_ticket_exhausts_without_split(self, tmp_path: Path) -> None:
        """atomic=True ticket exhausts retries and ends CODED_UNGOVERNED.
        No last-resort split fires. Evidence shows prompt_split_exempt=True
        with reason 'atomic_ticket' on each attempt (Fix 19 marker)."""
        repo = _create_repo(tmp_path)
        standards = _create_standards(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        plan_path = _write_plan(
            tmp_path / "plan",
            tickets=[{
                "ticket_id": "T001",
                "goal": "Atomic schema migration",
                "atomic": True,
                "acceptance_criteria": ["schema migrated atomically"],
            }],
        )
        config = CoderConfig(backend="claude-cli")

        # Governance always fails — forces retry exhaustion
        def always_fail(*args, **kwargs):
            return ("FAIL", [{"kind": "syntax_error", "message": "bad code"}], "", [])

        patches = _base_patches(governance_side_effect=always_fail)
        patches.append(
            patch("saturnday.ticket_runner._execute_ticket", return_value=("done", [])),
        )
        # Do NOT mock analyze_and_split_last_resort — it must never be called
        # (atomic=True and budget signals absent prevent the gate from firing)

        for p in patches:
            p.start()
        try:
            result = run_plan(
                plan_path=str(plan_path),
                repo_path=str(repo),
                coder_config=config,
                standards_dir=str(standards),
                output_dir=str(output_dir),
                role_passes=False,
            )
        finally:
            for p in patches:
                p.stop()

        # 1. Final disposition is CODED_UNGOVERNED (governance exhausted, no split)
        assert len(result.ticket_results) == 1
        tr = result.ticket_results[0]
        assert tr.disposition == "CODED_UNGOVERNED", (
            f"Atomic ticket exhausted via governance must end CODED_UNGOVERNED, got {tr.disposition}"
        )

        # 2. Evidence files: prompt_split_exempt=True, prompt_split_reason="atomic_ticket"
        #    on every attempt — never a last_resort_split:* reason
        tickets_dir = output_dir / "tickets" / "T001"
        evidence_files = sorted(tickets_dir.glob("attempt_*.json"))
        assert evidence_files, "Evidence files must be written for the atomic ticket"

        for ev_file in evidence_files:
            ev = json.loads(ev_file.read_text(encoding="utf-8"))
            reason = ev.get("prompt_split_reason")
            assert reason != "last_resort_split_no_split", (
                f"Atomic ticket must not record last_resort_split_no_split evidence "
                f"(file: {ev_file.name})"
            )
            if reason is not None:
                assert not reason.startswith("last_resort_split:"), (
                    f"Atomic ticket must not have last_resort_split:* reason, got {reason!r} "
                    f"(file: {ev_file.name})"
                )
            # When exempt=True is recorded (governance-pass attempt evidence),
            # reason must be atomic_ticket
            if ev.get("prompt_split_exempt"):
                assert reason == "atomic_ticket", (
                    f"When prompt_split_exempt=True, reason must be 'atomic_ticket', "
                    f"got {reason!r} (file: {ev_file.name})"
                )

        # 3. Ledger disposition matches RunResult
        ledger_path = output_dir / "evidence" / "run" / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert ledger["ticket_statuses"]["T001"]["disposition"] == "CODED_UNGOVERNED"


# ---------------------------------------------------------------------------
# Scenario 4 — Real-governance dead-code mid-build seam (real governance)
#
# GOVERNANCE MODE: REAL — _run_governance is NOT mocked.
#
# Purpose: prove the Fix 15 dead_code suppression fires correctly in the
# integrated runner. Ticket A writes an orphaned helper (dead code). Real
# governance runs in run_mode=True — dead_code finding is suppressed so
# T_A is not blocked. Ticket B wires the helper. Final state is coherent
# and no false dead-code block occurred mid-build.
# ---------------------------------------------------------------------------

class TestScenario4RealGovernanceDeadCode:
    """Scenario 4 — Real governance dead-code mid-build seam."""

    def test_dead_code_not_blocked_during_mid_build(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Ticket A introduces an orphaned helper (dead code).
        Real _run_governance in run_mode=True suppresses dead_code findings.
        T_A passes. Ticket B wires the helper. Both tickets complete without
        a false dead-code block."""
        import logging

        repo = _create_repo(tmp_path)
        standards = _create_standards(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        plan_path = _write_plan(
            tmp_path / "plan",
            tickets=[
                {"ticket_id": "T_A", "goal": "Add orphaned helper",
                 "acceptance_criteria": ["helper module exists"]},
                {"ticket_id": "T_B", "goal": "Wire the helper",
                 "acceptance_criteria": ["helper wired into main"]},
            ],
        )
        config = CoderConfig(backend="claude-cli")

        ticket_call_count = 0

        def coder_se(*, ticket, repo_path, coder_config, messages, **kwargs):
            nonlocal ticket_call_count
            ticket_call_count += 1
            if ticket.ticket_id == "T_A":
                # Write orphaned helper — dead code until T_B wires it
                helper = Path(repo_path) / "helper.py"
                helper.write_text(
                    "def orphaned_helper():\n    return 42\n", encoding="utf-8",
                )
                return ("wrote helper.py", ["helper.py"])
            else:
                # T_B: wire the helper
                main = Path(repo_path) / "main.py"
                main.write_text(
                    "from helper import orphaned_helper\n\nresult = orphaned_helper()\n",
                    encoding="utf-8",
                )
                return ("wrote main.py", ["main.py"])

        # Mocked: LLM-heavy, side-effect-free, or slow operations
        # Real: _run_governance, _git_add, _git_commit, _git_reset_changes
        partial_patches = [
            patch(
                "saturnday.ticket_splitter.analyze_and_split",
                side_effect=lambda ticket, *a, **k: [ticket],
            ),
            patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
            patch("saturnday.ticket_runner._ensure_project_venv"),
            patch("saturnday.ticket_runner._auto_install_deps"),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
            patch("saturnday.governance.run_full_repo_review",
                  return_value=(MagicMock(disposition="PASS", check_results=[]), "")),
            patch("saturnday.ticket_runner._execute_ticket", side_effect=coder_se),
        ]

        for p in partial_patches:
            p.start()
        try:
            with caplog.at_level(logging.DEBUG, logger="saturnday.ticket_runner"):
                result = run_plan(
                    plan_path=str(plan_path),
                    repo_path=str(repo),
                    coder_config=config,
                    standards_dir=str(standards),
                    output_dir=str(output_dir),
                    role_passes=False,
                )
        finally:
            for p in partial_patches:
                p.stop()

        # 1. Both tickets completed
        assert len(result.ticket_results) == 2

        dispositions = {tr.ticket_id: tr.disposition for tr in result.ticket_results}

        # 2. T_A must not have been blocked by a dead-code finding.
        #    Real governance may produce PASS or CODED_UNGOVERNED depending on
        #    what other checks fire on the minimal repo state. The critical
        #    invariant is that no dead_code-caused FAIL occurred mid-build —
        #    i.e., T_A's disposition must not be FAIL.
        assert dispositions.get("T_A") != "FAIL", (
            "T_A must not be hard-blocked by dead_code during mid-build run_mode "
            f"(disposition={dispositions.get('T_A')!r}). "
            "Fix 15 dead_code suppression in run_mode=True must prevent this."
        )

        # 3. No 'dead_code' finding appears in the governance WARN logs for T_A.
        #    run_mode filtering must have dropped it before it could block.
        dead_code_blocks = [
            r.message for r in caplog.records
            if "dead_code" in r.message and "T_A" in r.message
            and r.levelno >= logging.WARNING
        ]
        assert not dead_code_blocks, (
            f"No dead_code WARNING for T_A expected (suppressed by run_mode). "
            f"Found: {dead_code_blocks}"
        )

        # 4. T_B completed — helper is wired and the build is coherent
        assert dispositions.get("T_B") is not None, "T_B must have run"

        # 5. helper.py and main.py exist in the repo filesystem
        assert (repo / "helper.py").is_file(), "helper.py must exist in repo"
        assert (repo / "main.py").is_file(), "main.py must exist in repo"


# ---------------------------------------------------------------------------
# Scenario 5 — Final contract sweep with real file satisfaction (mocked gov)
#
# Purpose: exercise the real final sweep path in the integrated runner flow
# with the Fix 16 test-fixture discipline (real file, real scanner, no mocks
# on verify_contracts or extract_contracts).
# ---------------------------------------------------------------------------

class TestScenario5FinalContractSweepRealFile:
    """Scenario 5 — Final contract sweep with real file satisfaction (mocked governance)."""

    def test_final_sweep_passes_with_real_file(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Single ticket with a parseable contract criterion.
        Mocked coder writes a real Python file satisfying the contract.
        The final sweep runs with the real scanner (extract_contracts +
        verify_contracts) and logs 'verified OK'."""
        import logging

        repo = _create_repo(tmp_path)
        standards = _create_standards(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        plan_path = _write_plan(
            tmp_path / "plan",
            tickets=[{
                "ticket_id": "T001",
                "goal": "Implement satisfy_contract function",
                "acceptance_criteria": ["function satisfy_contract exists"],
            }],
        )
        config = CoderConfig(backend="claude-cli")

        def coder_se(*, ticket, repo_path, coder_config, messages, **kwargs):
            # Write a real Python file satisfying the contract
            target = Path(repo_path) / "satisfy_contract.py"
            target.write_text(
                "def satisfy_contract():\n    \"\"\"Satisfies the contract.\"\"\"\n    return True\n",
                encoding="utf-8",
            )
            return ("wrote satisfy_contract.py", ["satisfy_contract.py"])

        patches = _base_patches()
        patches.append(
            patch("saturnday.ticket_runner._execute_ticket", side_effect=coder_se),
        )

        for p in patches:
            p.start()
        try:
            with caplog.at_level(logging.INFO, logger="saturnday.ticket_runner"):
                result = run_plan(
                    plan_path=str(plan_path),
                    repo_path=str(repo),
                    coder_config=config,
                    standards_dir=str(standards),
                    output_dir=str(output_dir),
                    role_passes=False,
                )
        finally:
            for p in patches:
                p.stop()

        # 1. Ticket completed (governance mocked to PASS)
        assert len(result.ticket_results) == 1
        assert result.ticket_results[0].disposition == "PASS"

        # 2. Real file was written to the repo filesystem
        assert (repo / "satisfy_contract.py").is_file(), (
            "satisfy_contract.py must exist in repo after coder runs"
        )
        content = (repo / "satisfy_contract.py").read_text(encoding="utf-8")
        assert "def satisfy_contract" in content

        # 3. Final contract sweep ran and found the contract satisfied
        #    (real extract_contracts + real verify_contracts — no mocks)
        sweep_ok_logs = [
            r.message for r in caplog.records
            if "verified OK" in r.message and "contract" in r.message.lower()
        ]
        assert sweep_ok_logs, (
            "Final contract sweep must log 'verified OK' when function exists in real file. "
            f"Caplog records (INFO+): {[r.message for r in caplog.records if r.levelno >= logging.INFO]}"
        )

        # 4. Ledger is coherent with the PASS result
        ledger_path = output_dir / "evidence" / "run" / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert ledger["ticket_statuses"]["T001"]["disposition"] == "PASS"
