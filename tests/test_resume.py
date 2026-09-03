"""Tests for saturnday.run.resume and run_plan skip_tickets integration.

Covers:
- resume_plan passes PASS tickets as skip_tickets to run_plan
- rerun_failed passes non-FAIL tickets as skip_tickets to run_plan
- Tickets in skip_tickets are added to the completed set so that downstream
  dependency resolution works correctly
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import (
    CoderConfig,
    RunResult,
    TicketResult,
    TicketSpec,
)
from saturnday.run.resume import (
    _detect_committed_tickets,
    _reconcile_stale_ledger,
    describe_recovery_state,
    rerun_failed,
    rerun_remaining,
    resume_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_plan(plan_data: dict, dir_path: Path) -> Path:
    """Write a plan dict to a temp JSON file."""
    path = dir_path / "plan.json"
    path.write_text(json.dumps(plan_data), encoding="utf-8")
    return path


def _write_ledger(ledger_data: dict, evidence_dir: Path) -> Path:
    """Write a ledger dict to the canonical ledger path."""
    ledger_dir = evidence_dir / "evidence" / "run"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / "ledger.json"
    ledger_path.write_text(json.dumps(ledger_data), encoding="utf-8")
    return ledger_path


def _make_coder_config() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="test-key", model="gpt-4")


def _fake_run_result() -> RunResult:
    return RunResult(project_id="test-project", passed=1)


# ---------------------------------------------------------------------------
# Test: resume_plan passes PASS tickets as skip_tickets
# ---------------------------------------------------------------------------

class TestResumePlanSkipsPassed:
    def test_resume_skips_passed_tickets(self, tmp_path: Path) -> None:
        """resume_plan must pass the set of PASS tickets as skip_tickets to run_plan."""
        plan_data = {
            "version": 1,
            "project_id": "test-project",
            "tickets": [
                {"ticket_id": "T001", "goal": "First"},
                {"ticket_id": "T002", "goal": "Second"},
                {"ticket_id": "T003", "goal": "Third"},
            ],
        }
        plan_path = _write_plan(plan_data, tmp_path)

        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL"},
                "T003": {"disposition": "PENDING"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        captured: dict = {}

        def fake_run_plan(**kwargs: object) -> RunResult:
            captured["skip_tickets"] = kwargs.get("skip_tickets")
            return _fake_run_result()

        with patch(
            "saturnday.ticket_runner.run_plan",
            side_effect=fake_run_plan,
        ):
            resume_plan(
                evidence_dir=evidence_dir,
                plan_path=plan_path,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                standards_dir=tmp_path,
            )

        skip = captured.get("skip_tickets")
        assert skip is not None, "skip_tickets was not passed to run_plan"
        assert "T001" in skip, "T001 (PASS) must be in skip_tickets"
        assert "T002" not in skip, "T002 (FAIL) must NOT be in skip_tickets"
        assert "T003" not in skip, "T003 (PENDING) must NOT be in skip_tickets"


# ---------------------------------------------------------------------------
# Test: rerun_failed passes non-FAIL tickets as skip_tickets
# ---------------------------------------------------------------------------

class TestRerunFailedSkipsNonFailed:
    def test_rerun_only_executes_failed(self, tmp_path: Path) -> None:
        """rerun_failed must skip PASS and PENDING tickets; only FAIL re-executes."""
        plan_data = {
            "version": 1,
            "project_id": "test-project",
            "tickets": [
                {"ticket_id": "T001", "goal": "First"},
                {"ticket_id": "T002", "goal": "Second"},
                {"ticket_id": "T003", "goal": "Third"},
            ],
        }
        plan_path = _write_plan(plan_data, tmp_path)

        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL"},
                "T003": {"disposition": "PASS"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        captured: dict = {}

        def fake_run_plan(**kwargs: object) -> RunResult:
            captured["skip_tickets"] = kwargs.get("skip_tickets")
            return _fake_run_result()

        with patch(
            "saturnday.ticket_runner.run_plan",
            side_effect=fake_run_plan,
        ):
            rerun_failed(
                evidence_dir=evidence_dir,
                plan_path=plan_path,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                standards_dir=tmp_path,
            )

        skip = captured.get("skip_tickets")
        assert skip is not None, "skip_tickets was not passed to run_plan"
        assert "T001" in skip, "T001 (PASS) must be in skip_tickets"
        assert "T003" in skip, "T003 (PASS) must be in skip_tickets"
        assert "T002" not in skip, "T002 (FAIL) must NOT be in skip_tickets — it should re-execute"


# ---------------------------------------------------------------------------
# Test: skipped tickets satisfy downstream dependencies
# ---------------------------------------------------------------------------

class TestSkippedTicketsSatisfyDependencies:
    def test_skipped_tickets_satisfy_dependencies(self, tmp_path: Path) -> None:
        """A ticket in skip_tickets must be added to completed so dependent tickets run.

        Plan: T002 depends on T001. T001 is in skip_tickets.
        Expected: T002 is NOT skipped due to an unmet dependency — it should
        be attempted (or at least reach _run_ticket_with_retries).
        """
        plan_data = {
            "version": 1,
            "project_id": "dep-test",
            "tickets": [
                {"ticket_id": "T001", "goal": "Scaffold",
                 "acceptance_criteria": ["scaffold in place"]},
                {"ticket_id": "T002", "goal": "Build on scaffold",
                 "dependencies": ["T001"],
                 "acceptance_criteria": ["built on scaffold"]},
            ],
        }
        plan_path = _write_plan(plan_data, tmp_path)

        # Track which tickets reached _run_ticket_with_retries
        attempted: list[str] = []

        def fake_run_ticket(ticket: TicketSpec, **kwargs: object) -> TicketResult:
            attempted.append(ticket.ticket_id)
            return TicketResult(
                ticket_id=ticket.ticket_id,
                disposition="PASS",
                changed_files=(),
            )

        # Also mock analyze_and_split to return the ticket unchanged (no splitting)
        def fake_analyze_and_split(
            ticket: TicketSpec, *args: object, **kwargs: object
        ) -> list[TicketSpec]:
            return [ticket]

        # Mock write_ledger_snapshot and write_run_summary to avoid FS writes.
        # analyze_and_split is imported locally inside the ticket loop, so it must
        # be patched on its source module (ticket_splitter), not on ticket_runner.
        with (
            patch(
                "saturnday.ticket_runner._run_ticket_with_retries",
                side_effect=fake_run_ticket,
            ),
            patch(
                "saturnday.ticket_splitter.analyze_and_split",
                side_effect=fake_analyze_and_split,
            ),
            patch("saturnday.ticket_runner.write_ledger_snapshot"),
            patch("saturnday.ticket_runner.write_run_summary"),
            patch("saturnday.ticket_runner.load_state", return_value=None),
            patch("saturnday.ticket_runner.save_state"),
            patch("saturnday.ticket_runner.update_state", return_value=MagicMock()),
            patch("saturnday.ticket_runner._ensure_gitignore"),
            patch("saturnday.ticket_runner.build_system_prompt", return_value="sys"),
        ):
            from saturnday.ticket_runner import run_plan

            result = run_plan(
                plan_path=plan_path,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                standards_dir=tmp_path,
                output_dir=tmp_path / "out",
                skip_tickets=frozenset({"T001"}),
            )

        # T001 was pre-completed — it must NOT have been attempted via the coder
        assert "T001" not in attempted, (
            "T001 is in skip_tickets; it must not reach _run_ticket_with_retries"
        )

        # T002 depends on T001; since T001 is pre-completed, T002 must be attempted
        assert "T002" in attempted, (
            "T002 depends on T001 which was pre-completed; T002 must be attempted"
        )


# ---------------------------------------------------------------------------
# Test: rerun_remaining only executes SKIP and PENDING tickets
# ---------------------------------------------------------------------------

class TestRerunRemainingSkipsCompletedAndFailed:
    def test_rerun_remaining_targets_skip_and_pending(self, tmp_path: Path) -> None:
        """rerun_remaining must skip PASS, FAIL, CODED_UNGOVERNED; re-execute SKIP and PENDING."""
        plan_data = {
            "version": 1,
            "project_id": "test-project",
            "tickets": [
                {"ticket_id": "T001", "goal": "Done"},
                {"ticket_id": "T002", "goal": "Failed"},
                {"ticket_id": "T003", "goal": "Ungoverned"},
                {"ticket_id": "T004", "goal": "Skipped"},
                {"ticket_id": "T005", "goal": "Pending"},
            ],
        }
        plan_path = _write_plan(plan_data, tmp_path)

        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL"},
                "T003": {"disposition": "CODED_UNGOVERNED"},
                "T004": {"disposition": "SKIP"},
                "T005": {"disposition": "PENDING"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        captured: dict = {}

        def fake_run_plan(**kwargs: object) -> RunResult:
            captured["skip_tickets"] = kwargs.get("skip_tickets")
            return _fake_run_result()

        with patch(
            "saturnday.ticket_runner.run_plan",
            side_effect=fake_run_plan,
        ):
            rerun_remaining(
                evidence_dir=evidence_dir,
                plan_path=plan_path,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                standards_dir=tmp_path,
            )

        skip = captured.get("skip_tickets")
        assert skip is not None
        assert "T001" in skip, "T001 (PASS) must be skipped"
        assert "T002" in skip, "T002 (FAIL) must be skipped — use rerun-failed for that"
        assert "T003" in skip, "T003 (CODED_UNGOVERNED) must be skipped — use rerun-failed for that"
        assert "T004" not in skip, "T004 (SKIP) must re-execute"
        assert "T005" not in skip, "T005 (PENDING) must re-execute"

    def test_rerun_remaining_returns_early_when_none(self, tmp_path: Path) -> None:
        """If all tickets are PASS/FAIL/CODED_UNGOVERNED, return empty result."""
        plan_data = {
            "version": 1,
            "project_id": "test-project",
            "tickets": [{"ticket_id": "T001", "goal": "Done"}],
        }
        _write_plan(plan_data, tmp_path)

        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        result = rerun_remaining(
            evidence_dir=evidence_dir,
            plan_path=tmp_path / "plan.json",
            repo_path=tmp_path,
            coder_config=_make_coder_config(),
            standards_dir=tmp_path,
        )
        assert result.project_id == "unknown"


# ---------------------------------------------------------------------------
# Test: describe_recovery_state classifies tickets correctly
# ---------------------------------------------------------------------------

class TestDescribeRecoveryState:
    def test_mixed_state_ledger(self, tmp_path: Path) -> None:
        """describe_recovery_state must classify each disposition correctly."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL"},
                "T003": {"disposition": "CODED_UNGOVERNED"},
                "T004": {"disposition": "SKIP"},
                "T005": {"disposition": "PENDING"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        state = describe_recovery_state(evidence_dir)
        assert state["passed"] == ["T001"]
        assert state["failed"] == ["T002"]
        assert state["coded_ungoverned"] == ["T003"]
        assert state["skipped"] == ["T004"]
        assert state["pending"] == ["T005"]
        assert state["total"] == 5
        assert "resume" in state["recovery_paths"]
        assert "rerun-failed" in state["recovery_paths"]
        assert "rerun-remaining" in state["recovery_paths"]


# ---------------------------------------------------------------------------
# Test: no semantic relabelling
# ---------------------------------------------------------------------------

class TestNoSemanticRelabelling:
    def test_coded_ungoverned_not_treated_as_pass(self, tmp_path: Path) -> None:
        """CODED_UNGOVERNED must NOT be in the passed list."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "CODED_UNGOVERNED"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        state = describe_recovery_state(evidence_dir)
        assert "T001" not in state["passed"]
        assert "T001" in state["coded_ungoverned"]

    def test_skip_not_treated_as_fail(self, tmp_path: Path) -> None:
        """SKIP must NOT be in the failed list."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "SKIP"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        state = describe_recovery_state(evidence_dir)
        assert "T001" not in state["failed"]
        assert "T001" in state["skipped"]


# ---------------------------------------------------------------------------
# Fix 44.a: git-backed ledger reconciliation tests
# ---------------------------------------------------------------------------

class TestDetectCommittedTickets:
    """Test _detect_committed_tickets extracts ticket IDs from git log."""

    def test_detects_ticket_ids_from_git_log(self, tmp_path: Path) -> None:
        """Committed ticket IDs are correctly detected from git log subjects."""
        # Set up a real git repo with ticket-style commits
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "[T001] Apply ticket changes"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "[T002] Apply ticket changes [GOVERNANCE: review required]"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "[T003.a] Apply ticket changes"], cwd=str(tmp_path), capture_output=True, check=True)

        result = _detect_committed_tickets(tmp_path)
        assert "T001" in result
        assert "T002" in result
        assert "T003.a" in result

    def test_returns_empty_for_no_git_repo(self, tmp_path: Path) -> None:
        """Returns empty frozenset when path is not a git repo."""
        result = _detect_committed_tickets(tmp_path)
        assert result == frozenset()

    def test_returns_empty_for_no_ticket_commits(self, tmp_path: Path) -> None:
        """Returns empty frozenset when no commits match the pattern."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(tmp_path), capture_output=True, check=True)

        result = _detect_committed_tickets(tmp_path)
        assert result == frozenset()


class TestReconcileStaleLedger:
    """Test _reconcile_stale_ledger promotes stale PENDING to CODED_UNGOVERNED."""

    def test_reconciles_pending_with_commit(self, tmp_path: Path) -> None:
        """Stale PENDING ticket with a git commit is promoted to CODED_UNGOVERNED."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "PENDING"},
                "T003": {"disposition": "FAIL"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        committed = frozenset({"T002", "T003"})
        reconciled = _reconcile_stale_ledger(ledger_data, committed, tmp_path, evidence_dir)

        assert reconciled == frozenset({"T002"})
        assert ledger_data["ticket_statuses"]["T002"]["disposition"] == "CODED_UNGOVERNED"
        # PASS and FAIL unchanged
        assert ledger_data["ticket_statuses"]["T001"]["disposition"] == "PASS"
        assert ledger_data["ticket_statuses"]["T003"]["disposition"] == "FAIL"

    def test_persists_reconciled_ledger(self, tmp_path: Path) -> None:
        """Reconciliation is persisted to ledger.json on disk."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PENDING"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        committed = frozenset({"T001"})
        _reconcile_stale_ledger(ledger_data, committed, tmp_path, evidence_dir)

        # Re-read from disk
        ledger_path = evidence_dir / "evidence" / "run" / "ledger.json"
        persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert persisted["ticket_statuses"]["T001"]["disposition"] == "CODED_UNGOVERNED"

    def test_no_reconciliation_when_no_stale_pending(self, tmp_path: Path) -> None:
        """No changes when no PENDING tickets have commits."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        committed = frozenset({"T001"})  # T001 is PASS, not PENDING
        reconciled = _reconcile_stale_ledger(ledger_data, committed, tmp_path, evidence_dir)

        assert reconciled == frozenset()

    def test_uncommitted_pending_unchanged(self, tmp_path: Path) -> None:
        """Genuine PENDING tickets with no commit stay PENDING."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PENDING"},
                "T002": {"disposition": "PENDING"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        committed = frozenset({"T001"})  # Only T001 has a commit
        _reconcile_stale_ledger(ledger_data, committed, tmp_path, evidence_dir)

        assert ledger_data["ticket_statuses"]["T001"]["disposition"] == "CODED_UNGOVERNED"
        assert ledger_data["ticket_statuses"]["T002"]["disposition"] == "PENDING"

    def test_fail_tickets_unchanged_by_reconciliation(self, tmp_path: Path) -> None:
        """True FAIL tickets are not affected by reconciliation."""
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "FAIL"},
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        committed = frozenset({"T001"})
        reconciled = _reconcile_stale_ledger(ledger_data, committed, tmp_path, evidence_dir)

        assert reconciled == frozenset()
        assert ledger_data["ticket_statuses"]["T001"]["disposition"] == "FAIL"


class TestResumePlanReconciliation:
    """Test that resume_plan reconciles stale ledger before skip decisions."""

    def test_resume_skips_stale_pending_committed_ticket(self, tmp_path: Path) -> None:
        """A PENDING ticket with a git commit must be reconciled and skipped."""
        plan_data = {
            "version": 1,
            "project_id": "test-project",
            "tickets": [
                {"ticket_id": "T001", "goal": "First"},
                {"ticket_id": "T002", "goal": "Second"},
            ],
        }
        plan_path = _write_plan(plan_data, tmp_path)

        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "PENDING"},  # stale — commit exists
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        captured: dict = {}

        def fake_run_plan(**kwargs: object) -> RunResult:
            captured["skip_tickets"] = kwargs.get("skip_tickets")
            return _fake_run_result()

        with (
            patch("saturnday.ticket_runner.run_plan", side_effect=fake_run_plan),
            patch(
                "saturnday.run.resume._detect_committed_tickets",
                return_value=frozenset({"T001", "T002"}),
            ),
        ):
            resume_plan(
                evidence_dir=evidence_dir,
                plan_path=plan_path,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                standards_dir=tmp_path,
            )

        skip = captured.get("skip_tickets")
        assert skip is not None
        assert "T001" in skip, "T001 (PASS) must be in skip_tickets"
        assert "T002" in skip, "T002 (stale PENDING, reconciled to CODED_UNGOVERNED) must be in skip_tickets"


# ---------------------------------------------------------------------------
# Fix 44.b: immediate post-commit ledger persistence tests
# ---------------------------------------------------------------------------

class TestOnCommittedCallback:
    """Test that _run_ticket_with_retries calls on_committed after git commit."""

    def test_pass_path_calls_on_committed(self, tmp_path: Path) -> None:
        """PASS disposition triggers on_committed before returning."""
        committed_calls: list[tuple[str, str, str]] = []

        def fake_on_committed(tid: str, disposition: str, fc: str) -> None:
            committed_calls.append((tid, disposition, fc))

        # Build a minimal ticket
        ticket = TicketSpec(ticket_id="T001", goal="test")

        # Mock the internals so _run_ticket_with_retries reaches PASS
        with (
            patch("saturnday.ticket_runner._assemble_ticket_prompt", return_value=(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}],
                {"prompt_chars": 100, "prompt_budget_warning": False,
                 "prompt_budget_soft_threshold": 7500, "prompt_budget_hard_threshold": 9000,
                 "context_compaction_applied": False, "prompt_split_exempt": False,
                 "prompt_split_reason": None},
            )),
            patch("saturnday.ticket_runner._execute_ticket", return_value=("response", ["file.py"])),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner._git_add"),
            patch("saturnday.ticket_runner._git_commit"),
            patch("saturnday.ticket_runner._run_governance", return_value=("PASS", [], "", [])),
            patch("saturnday.ticket_runner._filter_findings_to_files", return_value=[]),
            patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
            patch("saturnday.ticket_runner._check_contracts", return_value=""),
            patch("saturnday.ticket_runner.write_ticket_evidence"),
            patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
            patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
        ):
            from saturnday.ticket_runner import _run_ticket_with_retries
            from saturnday.project_state import ProjectState

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                system_prompt="sys",
                state=ProjectState(project_id="test"),
                plan_notes="",
                output_dir=tmp_path / "out",
                max_retries=2,
                on_committed=fake_on_committed,
            )

        assert result.disposition == "PASS"
        assert len(committed_calls) == 1
        assert committed_calls[0] == ("T001", "PASS", "")

    def test_coded_ungoverned_calls_on_committed(self, tmp_path: Path) -> None:
        """CODED_UNGOVERNED disposition triggers on_committed before returning."""
        committed_calls: list[tuple[str, str, str]] = []

        def fake_on_committed(tid: str, disposition: str, fc: str) -> None:
            committed_calls.append((tid, disposition, fc))

        ticket = TicketSpec(ticket_id="T002", goal="test")

        # Return governance findings so it fails governance and hits CODED_UNGOVERNED
        fake_findings = [{"kind": "test_fail", "file": "file.py", "message": "bad"}]

        with (
            patch("saturnday.ticket_runner._assemble_ticket_prompt", return_value=(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}],
                {"prompt_chars": 100, "prompt_budget_warning": False,
                 "prompt_budget_soft_threshold": 7500, "prompt_budget_hard_threshold": 9000,
                 "context_compaction_applied": False, "prompt_split_exempt": False,
                 "prompt_split_reason": None},
            )),
            patch("saturnday.ticket_runner._execute_ticket", return_value=("response", ["file.py"])),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner._git_add"),
            patch("saturnday.ticket_runner._git_commit"),
            patch("saturnday.ticket_runner._git_reset_changes"),
            patch("saturnday.ticket_runner._run_governance", return_value=("FAIL", fake_findings, "", [])),
            patch("saturnday.ticket_runner._filter_findings_to_files", return_value=fake_findings),
            patch("saturnday.ticket_runner.write_ticket_evidence"),
            patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
        ):
            from saturnday.ticket_runner import _run_ticket_with_retries
            from saturnday.project_state import ProjectState

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                system_prompt="sys",
                state=ProjectState(project_id="test"),
                plan_notes="",
                output_dir=tmp_path / "out",
                max_retries=0,  # no retries — goes straight to CODED_UNGOVERNED
                on_committed=fake_on_committed,
            )

        assert result.disposition == "CODED_UNGOVERNED"
        assert len(committed_calls) == 1
        assert committed_calls[0] == ("T002", "CODED_UNGOVERNED", "")

    def test_no_callback_still_works(self, tmp_path: Path) -> None:
        """on_committed=None (default) does not break existing flow."""
        ticket = TicketSpec(ticket_id="T003", goal="test")

        with (
            patch("saturnday.ticket_runner._assemble_ticket_prompt", return_value=(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}],
                {"prompt_chars": 100, "prompt_budget_warning": False,
                 "prompt_budget_soft_threshold": 7500, "prompt_budget_hard_threshold": 9000,
                 "context_compaction_applied": False, "prompt_split_exempt": False,
                 "prompt_split_reason": None},
            )),
            patch("saturnday.ticket_runner._execute_ticket", return_value=("response", ["file.py"])),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner._git_add"),
            patch("saturnday.ticket_runner._git_commit"),
            patch("saturnday.ticket_runner._run_governance", return_value=("PASS", [], "", [])),
            patch("saturnday.ticket_runner._filter_findings_to_files", return_value=[]),
            patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
            patch("saturnday.ticket_runner._check_contracts", return_value=""),
            patch("saturnday.ticket_runner.write_ticket_evidence"),
            patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
            patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
        ):
            from saturnday.ticket_runner import _run_ticket_with_retries
            from saturnday.project_state import ProjectState

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                system_prompt="sys",
                state=ProjectState(project_id="test"),
                plan_notes="",
                output_dir=tmp_path / "out",
                max_retries=2,
                # on_committed not passed — defaults to None
            )

        assert result.disposition == "PASS"


class TestRerunRemainingReconciliation:
    """Test that rerun_remaining reconciles stale ledger."""

    def test_rerun_remaining_does_not_rerun_committed_pending(self, tmp_path: Path) -> None:
        """A stale PENDING ticket with a git commit must NOT be re-run by rerun_remaining."""
        plan_data = {
            "version": 1,
            "project_id": "test-project",
            "tickets": [
                {"ticket_id": "T001", "goal": "First",
                 "acceptance_criteria": ["T001 done"]},
                {"ticket_id": "T002", "goal": "Second",
                 "acceptance_criteria": ["T002 done"]},
                {"ticket_id": "T003", "goal": "Third",
                 "acceptance_criteria": ["T003 done"]},
            ],
        }
        plan_path = _write_plan(plan_data, tmp_path)

        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "PENDING"},  # stale — commit exists
                "T003": {"disposition": "PENDING"},  # genuine — no commit
            }
        }
        evidence_dir = tmp_path / "evidence-out"
        _write_ledger(ledger_data, evidence_dir)

        captured: dict = {}

        def fake_run_plan(**kwargs: object) -> RunResult:
            captured["skip_tickets"] = kwargs.get("skip_tickets")
            return _fake_run_result()

        with (
            patch("saturnday.ticket_runner.run_plan", side_effect=fake_run_plan),
            patch(
                "saturnday.run.resume._detect_committed_tickets",
                return_value=frozenset({"T001", "T002"}),
            ),
        ):
            rerun_remaining(
                evidence_dir=evidence_dir,
                plan_path=plan_path,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                standards_dir=tmp_path,
            )

        skip = captured.get("skip_tickets")
        assert skip is not None
        assert "T001" in skip, "T001 (PASS) must be skipped"
        assert "T002" in skip, "T002 (reconciled to CODED_UNGOVERNED) must be skipped"
        assert "T003" not in skip, "T003 (genuine PENDING, no commit) must re-execute"
