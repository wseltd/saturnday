"""Fix 20 — large-plan continuation UX tests.

Proves that:
1. _select_continuation_command returns the correct subcommand for every
   disposition combination.
2. The CODED_UNGOVERNED visibility warning is printed when warranted.
3. No next-step is printed when all tickets are settled (no fabrication).
4. _print_continuation_block uses the ledger when present, not RunResult.
5. resume_plan() emits a logger.warning when CODED_UNGOVERNED tickets are
   in the skip set.
"""

from __future__ import annotations

import json
import logging
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.run.resume import _select_continuation_command


# ---------------------------------------------------------------------------
# Tests 1–7: _select_continuation_command decision matrix
# ---------------------------------------------------------------------------

def test_select_fail_and_skip_returns_resume() -> None:
    """FAIL + SKIP → resume."""
    result = _select_continuation_command(
        failed=["T003"],
        coded_ungoverned=[],
        skipped=["T004"],
        pending=[],
    )
    assert result == "resume"


def test_select_fail_and_pending_returns_resume() -> None:
    """FAIL + PENDING → resume."""
    result = _select_continuation_command(
        failed=["T003"],
        coded_ungoverned=[],
        skipped=[],
        pending=["T004"],
    )
    assert result == "resume"


def test_select_fail_only_returns_rerun_failed() -> None:
    """FAIL with no remaining → rerun-failed."""
    result = _select_continuation_command(
        failed=["T003"],
        coded_ungoverned=[],
        skipped=[],
        pending=[],
    )
    assert result == "rerun-failed"


def test_select_coded_ungoverned_only_returns_rerun_failed() -> None:
    """CODED_UNGOVERNED with no FAIL or remaining → rerun-failed."""
    result = _select_continuation_command(
        failed=[],
        coded_ungoverned=["T003"],
        skipped=[],
        pending=[],
    )
    assert result == "rerun-failed"


def test_select_skip_only_returns_rerun_remaining() -> None:
    """SKIP with no FAIL → rerun-remaining."""
    result = _select_continuation_command(
        failed=[],
        coded_ungoverned=[],
        skipped=["T003"],
        pending=[],
    )
    assert result == "rerun-remaining"


def test_select_pending_only_returns_rerun_remaining() -> None:
    """PENDING with no FAIL → rerun-remaining."""
    result = _select_continuation_command(
        failed=[],
        coded_ungoverned=[],
        skipped=[],
        pending=["T003"],
    )
    assert result == "rerun-remaining"


def test_select_all_settled_returns_none() -> None:
    """All empty (settled run) → None."""
    result = _select_continuation_command(
        failed=[],
        coded_ungoverned=[],
        skipped=[],
        pending=[],
    )
    assert result is None


# ---------------------------------------------------------------------------
# Test 8: CODED_UNGOVERNED warning is printed when warranted
# ---------------------------------------------------------------------------

def test_coded_ungoverned_warning_printed(capsys: pytest.CaptureFixture[str]) -> None:
    """_print_continuation_block prints the CODED_UNGOVERNED warning when coded_ungoverned is non-empty."""
    from saturnday.cli import _print_continuation_block
    from saturnday._types import RunResult, TicketResult

    # Build a RunResult with a CODED_UNGOVERNED ticket
    tr = TicketResult(ticket_id="T002", disposition="CODED_UNGOVERNED")
    result = RunResult(project_id="test", ticket_results=[tr])

    # No ledger on disk — fallback to RunResult
    _print_continuation_block(result, output_dir="", plan_path="", repo_path="", backend="")
    captured = capsys.readouterr()

    assert "coded but governance did not clear" in captured.out
    assert "rerun-failed" in captured.out
    assert "resume will skip" in captured.out


# ---------------------------------------------------------------------------
# Test 9: No next-step printed when all tickets are settled
# ---------------------------------------------------------------------------

def test_no_next_step_when_all_settled(capsys: pytest.CaptureFixture[str]) -> None:
    """No continuation line printed when every ticket passed — no fabrication."""
    from saturnday.cli import _print_continuation_block
    from saturnday._types import RunResult, TicketResult

    tr = TicketResult(ticket_id="T001", disposition="PASS")
    result = RunResult(project_id="test", ticket_results=[tr])

    _print_continuation_block(result, output_dir="", plan_path="", repo_path="", backend="")
    captured = capsys.readouterr()

    assert "Next step" not in captured.out
    assert "saturnday resume" not in captured.out
    assert "rerun-failed" not in captured.out


# ---------------------------------------------------------------------------
# Test 10: Ledger-first — RunResult values ignored when ledger present
# ---------------------------------------------------------------------------

def test_ledger_first_ignores_runresult_when_ledger_present(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When ledger.json exists, RunResult dispositions are not used for command selection."""
    from saturnday.cli import _print_continuation_block
    from saturnday._types import RunResult, TicketResult

    # Ledger says T001=FAIL, T002=SKIP (→ resume)
    ledger_dir = tmp_path / "evidence" / "run"
    ledger_dir.mkdir(parents=True)
    ledger_data = {
        "project_id": "test",
        "ticket_statuses": {
            "T001": {"disposition": "FAIL"},
            "T002": {"disposition": "SKIP"},
        },
    }
    (ledger_dir / "ledger.json").write_text(json.dumps(ledger_data), encoding="utf-8")

    # RunResult says everything PASS (contradicts ledger — ledger must win)
    tr1 = TicketResult(ticket_id="T001", disposition="PASS")
    tr2 = TicketResult(ticket_id="T002", disposition="PASS")
    result = RunResult(project_id="test", ticket_results=[tr1, tr2])

    _print_continuation_block(
        result,
        output_dir=str(tmp_path),
        plan_path="/path/plan.json",
        repo_path="/path/repo",
        backend="claude-cli",
    )
    captured = capsys.readouterr()

    # Ledger has FAIL+SKIP → command must be resume, not nothing
    assert "saturnday resume" in captured.out
    assert "--output-dir" in captured.out
    assert "--evidence-dir" not in captured.out


# ---------------------------------------------------------------------------
# Test 11: resume_plan() emits logger.warning for CODED_UNGOVERNED skip
# ---------------------------------------------------------------------------

def test_resume_plan_warns_coded_ungoverned(tmp_path: Path) -> None:
    """resume_plan() emits logger.warning when CODED_UNGOVERNED tickets are skipped."""
    from saturnday.run.resume import resume_plan
    from saturnday._types import CoderConfig, RunResult

    # Build a ledger with a CODED_UNGOVERNED ticket
    ledger_dir = tmp_path / "evidence" / "run"
    ledger_dir.mkdir(parents=True)
    ledger_data = {
        "project_id": "test-proj",
        "ticket_statuses": {
            "T001": {"disposition": "PASS"},
            "T002": {"disposition": "CODED_UNGOVERNED"},
            "T003": {"disposition": "FAIL"},
        },
    }
    (ledger_dir / "ledger.json").write_text(json.dumps(ledger_data), encoding="utf-8")

    # Create a minimal plan file
    plan_data = {
        "version": 1,
        "project_id": "test-proj",
        "tickets": [
            {"ticket_id": "T001", "goal": "First"},
            {"ticket_id": "T002", "goal": "Second"},
            {"ticket_id": "T003", "goal": "Third"},
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

    config = CoderConfig(backend="claude-cli")

    sentinel = RunResult(project_id="test-proj")

    with patch("saturnday.ticket_runner.run_plan", return_value=sentinel):
        with patch("saturnday.run.resume._run_with_filter", return_value=sentinel):
            with patch(
                "saturnday.run.resume.logger",
            ) as mock_logger:
                try:
                    resume_plan(
                        evidence_dir=str(tmp_path),
                        plan_path=str(plan_path),
                        repo_path=str(tmp_path),
                        coder_config=config,
                        standards_dir=str(tmp_path),
                    )
                except Exception:
                    pass

                # Verify warning was called with CODED_UNGOVERNED in message
                warning_calls = mock_logger.warning.call_args_list
                assert warning_calls, "logger.warning must be called when CODED_UNGOVERNED in skip set"
                warning_msgs = " ".join(str(c) for c in warning_calls)
                assert "T002" in warning_msgs or "coded" in warning_msgs.lower()


# ---------------------------------------------------------------------------
# Test 12: report Recovery section uses real evidence_dir and repo_path values
# ---------------------------------------------------------------------------

def test_report_recovery_uses_real_paths(tmp_path: Path) -> None:
    """generate_run_report Recovery section uses real evidence_dir and repo_path, not placeholders."""
    import json as _json
    from saturnday.reporting import generate_run_report
    from saturnday._types import RunResult, TicketResult

    # Write a ledger so _append_recovery_section can classify via describe_recovery_state
    ledger_dir = tmp_path / "evidence" / "run"
    ledger_dir.mkdir(parents=True)
    ledger_data = {
        "project_id": "proj",
        "ticket_statuses": {
            "T001": {"disposition": "FAIL"},
        },
    }
    (ledger_dir / "ledger.json").write_text(_json.dumps(ledger_data), encoding="utf-8")

    tr = TicketResult(ticket_id="T001", disposition="FAIL")
    result = RunResult(project_id="proj", ticket_results=[tr])
    repo_path = tmp_path / "myrepo"
    repo_path.mkdir()

    out = generate_run_report(result, {}, None, tmp_path, repo_path)
    content = out.read_text(encoding="utf-8")

    # Real evidence_dir path must appear
    assert str(tmp_path) in content, "evidence_dir real path must appear in Recovery command"
    # Real repo_path must appear
    assert str(repo_path) in content, "repo_path real path must appear in Recovery command"
    # Plan and backend remain genuine placeholders
    assert "<plan>" in content, "--plan must remain a placeholder (not available at call site)"
    assert "<backend>" in content, "--backend must remain a placeholder (not available at call site)"


# ---------------------------------------------------------------------------
# Test 13: no Recovery section when all tickets pass (settled-state)
# ---------------------------------------------------------------------------

def test_report_no_recovery_section_when_all_pass(tmp_path: Path) -> None:
    """generate_run_report writes no Recovery section when all tickets passed."""
    from saturnday.reporting import generate_run_report
    from saturnday._types import RunResult, TicketResult

    tr = TicketResult(ticket_id="T001", disposition="PASS")
    result = RunResult(project_id="proj", ticket_results=[tr])

    out = generate_run_report(result, {}, None, tmp_path, tmp_path)
    content = out.read_text(encoding="utf-8")

    assert "## Recovery" not in content, "No Recovery section must appear when all tickets passed"
    assert "Next step" not in content, "No Next step must appear when all tickets passed"


# ---------------------------------------------------------------------------
# Fix 28 — continuation command flag mismatch
# ---------------------------------------------------------------------------

def test_cli_continuation_uses_output_dir_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fix 28 T1: CLI continuation output uses --output-dir, not --evidence-dir."""
    from saturnday.cli import _print_continuation_block
    from saturnday._types import RunResult, TicketResult

    tr = TicketResult(ticket_id="T001", disposition="FAIL")
    result = RunResult(project_id="test", ticket_results=[tr])

    _print_continuation_block(
        result,
        output_dir=str(tmp_path),
        plan_path="/path/plan.json",
        repo_path="/path/repo",
        backend="claude-cli",
    )
    captured = capsys.readouterr()

    assert "--output-dir" in captured.out, "continuation command must use --output-dir"
    assert "--evidence-dir" not in captured.out, "continuation command must not use --evidence-dir"


def test_report_recovery_uses_output_dir_flag(tmp_path: Path) -> None:
    """Fix 28 T3: report Recovery section uses --output-dir, not --evidence-dir."""
    import json as _json
    from saturnday.reporting import generate_run_report
    from saturnday._types import RunResult, TicketResult

    ledger_dir = tmp_path / "evidence" / "run"
    ledger_dir.mkdir(parents=True)
    ledger_data = {
        "project_id": "proj",
        "ticket_statuses": {"T001": {"disposition": "FAIL"}},
    }
    (ledger_dir / "ledger.json").write_text(_json.dumps(ledger_data), encoding="utf-8")

    tr = TicketResult(ticket_id="T001", disposition="FAIL")
    result = RunResult(project_id="proj", ticket_results=[tr])
    repo_path = tmp_path / "myrepo"
    repo_path.mkdir()

    out = generate_run_report(result, {}, None, tmp_path, repo_path)
    content = out.read_text(encoding="utf-8")

    assert "--output-dir" in content, "report Recovery command must use --output-dir"
    assert "--evidence-dir" not in content, "report Recovery command must not use --evidence-dir"
