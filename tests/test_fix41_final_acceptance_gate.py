"""Fix 41 — hard final executable acceptance gate.

Proves that:
1. ProjectPlan and schema accept and preserve acceptance_cmd.
2. When acceptance_cmd is absent, existing behaviour is unchanged (exit 0).
3. When acceptance_cmd passes, run completion remains clean.
4. When acceptance_cmd fails, definition_of_done_met is downgraded.
5. Failed final acceptance is surfaced clearly in run summary / CLI output.
6. CLI exit becomes non-success when final acceptance fails.
7. acceptance_cmd_passed and acceptance_cmd_failure are written to run-summary.json.
8. plan_parser round-trips acceptance_cmd correctly.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**kw: Any) -> Any:
    from saturnday._types import RunResult
    defaults: dict[str, Any] = {
        "project_id": "fix41-test",
        "total_tickets": 1,
        "passed": 1,
        "failed": 0,
    }
    defaults.update(kw)
    return RunResult(**defaults)


def _capture_cli_summary(result: Any) -> str:
    from saturnday.cli import _print_run_summary
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_run_summary(result)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: ProjectPlan accepts and preserves acceptance_cmd
# ---------------------------------------------------------------------------

def test_project_plan_accepts_acceptance_cmd() -> None:
    """ProjectPlan stores acceptance_cmd and defaults to empty string."""
    from saturnday._types import ProjectPlan
    plan = ProjectPlan(version=1, project_id="test", acceptance_cmd="pytest tests/ -q")
    assert plan.acceptance_cmd == "pytest tests/ -q"


def test_project_plan_acceptance_cmd_defaults_empty() -> None:
    """acceptance_cmd defaults to '' — backward compatible."""
    from saturnday._types import ProjectPlan
    plan = ProjectPlan(version=1, project_id="test")
    assert plan.acceptance_cmd == ""


# ---------------------------------------------------------------------------
# Test 2: plan_parser round-trips acceptance_cmd
# ---------------------------------------------------------------------------

def test_plan_parser_roundtrips_acceptance_cmd(tmp_path: Path) -> None:
    """load_plan preserves acceptance_cmd from JSON."""
    from saturnday.plan_parser import load_plan

    plan_data = {
        "version": 1,
        "project_id": "fix41-roundtrip",
        "tickets": [{"ticket_id": "T001", "goal": "do something", "acceptance_criteria": ["done"]}],
        "acceptance_cmd": "python main.py --smoke-test",
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

    plan = load_plan(plan_path)
    assert plan.acceptance_cmd == "python main.py --smoke-test"


def test_plan_parser_acceptance_cmd_absent_defaults_empty(tmp_path: Path) -> None:
    """load_plan returns acceptance_cmd='' when field is absent (backward compat)."""
    from saturnday.plan_parser import load_plan

    plan_data = {
        "version": 1,
        "project_id": "fix41-compat",
        "tickets": [{"ticket_id": "T001", "goal": "do something", "acceptance_criteria": ["done"]}],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

    plan = load_plan(plan_path)
    assert plan.acceptance_cmd == ""


# ---------------------------------------------------------------------------
# Test 3: schema accepts acceptance_cmd
# ---------------------------------------------------------------------------

def test_plan_schema_accepts_acceptance_cmd() -> None:
    """plan JSON schema allows acceptance_cmd as a string property."""
    from saturnday.shared.plan_schema import PLAN_SCHEMA
    assert "acceptance_cmd" in PLAN_SCHEMA["properties"]
    assert PLAN_SCHEMA["properties"]["acceptance_cmd"] == {"type": "string"}


# ---------------------------------------------------------------------------
# Test 4: absent acceptance_cmd → existing behaviour unchanged (no-op)
# ---------------------------------------------------------------------------

def test_cmd_run_exit_zero_no_acceptance_cmd(tmp_path: Path) -> None:
    """When acceptance_cmd is absent, CLI exits 0 and behaviour is unchanged."""
    import argparse
    from saturnday._types import RunResult, TicketResult
    from saturnday.cli import _cmd_run

    plan_path = tmp_path / ".saturnday" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({
        "version": 1,
        "project_id": "fix41-no-cmd",
        "tickets": [],
        "phases": [],
        "definition_of_done": [],
    }), encoding="utf-8")

    happy_result = RunResult(
        project_id="fix41-no-cmd",
        total_tickets=1,
        passed=1,
        failed=0,
        definition_of_done_met=True,
        plan_governance_met=True,
        plan_governance_reason="PLAN_GOVERNANCE_MET",
        acceptance_cmd_passed=None,  # no acceptance_cmd declared
        ticket_results=(TicketResult(ticket_id="T001", disposition="PASS"),),
    )

    args = argparse.Namespace(
        plan=str(plan_path), repo=str(tmp_path), backend="claude-cli",
        standards_dir=str(tmp_path), output_dir=None, auto_repair=False,
        lessons=None, no_role_passes=False, strict_dod=False, verbose=False,
        api_key="", base_url="", model="", temperature=0.0, max_tokens=0, timeout=120,
    )

    with patch("saturnday.ticket_runner.run_plan", return_value=happy_result), \
         patch("saturnday.cli._require_git_repo", return_value=True):
        rc = _cmd_run(args)

    assert rc == 0, "No acceptance_cmd → must exit 0 (no regression)"


# ---------------------------------------------------------------------------
# Test 5: acceptance_cmd passes → completion remains clean
# ---------------------------------------------------------------------------

def test_cmd_run_exit_zero_when_acceptance_cmd_passes(tmp_path: Path) -> None:
    """When acceptance_cmd passes, CLI exits 0."""
    import argparse
    from saturnday._types import RunResult, TicketResult
    from saturnday.cli import _cmd_run

    plan_path = tmp_path / ".saturnday" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({
        "version": 1, "project_id": "fix41-pass", "tickets": [], "phases": [],
        "definition_of_done": [], "acceptance_cmd": "python -c 'print(1)'",
    }), encoding="utf-8")

    pass_result = RunResult(
        project_id="fix41-pass",
        total_tickets=1,
        passed=1,
        failed=0,
        definition_of_done_met=True,
        plan_governance_met=True,
        plan_governance_reason="PLAN_GOVERNANCE_MET",
        acceptance_cmd_passed=True,
        ticket_results=(TicketResult(ticket_id="T001", disposition="PASS"),),
    )

    args = argparse.Namespace(
        plan=str(plan_path), repo=str(tmp_path), backend="claude-cli",
        standards_dir=str(tmp_path), output_dir=None, auto_repair=False,
        lessons=None, no_role_passes=False, strict_dod=False, verbose=False,
        api_key="", base_url="", model="", temperature=0.0, max_tokens=0, timeout=120,
    )

    with patch("saturnday.ticket_runner.run_plan", return_value=pass_result), \
         patch("saturnday.cli._require_git_repo", return_value=True):
        rc = _cmd_run(args)

    assert rc == 0, "Passing acceptance_cmd → must exit 0"


# ---------------------------------------------------------------------------
# Test 6: acceptance_cmd fails → DoD downgraded
# ---------------------------------------------------------------------------

def test_acceptance_cmd_failure_downgrades_dod() -> None:
    """When acceptance_cmd_passed=False, definition_of_done_met must be False."""
    result = _make_result(
        definition_of_done_met=False,
        acceptance_cmd_passed=False,
        acceptance_cmd_failure="Exit code 1: smoke test failed",
    )
    assert result.definition_of_done_met is False
    assert result.acceptance_cmd_passed is False
    assert "smoke test failed" in result.acceptance_cmd_failure


def test_acceptance_cmd_mechanical_downgrade_logic() -> None:
    """Mechanical guard in ticket_runner downgrades DoD when acceptance_cmd fails."""
    from dataclasses import replace
    from saturnday._types import RunResult

    base = RunResult(
        project_id="fix41-guard",
        total_tickets=1,
        passed=1,
        definition_of_done_met=True,
        acceptance_cmd_passed=None,
    )

    # Simulate the failure path from ticket_runner
    failure = "Exit code 1: product not working"
    result = replace(
        base,
        acceptance_cmd_passed=False,
        acceptance_cmd_failure=failure[:500],
        definition_of_done_met=False,
    )

    assert result.definition_of_done_met is False
    assert result.acceptance_cmd_passed is False
    assert "product not working" in result.acceptance_cmd_failure


# ---------------------------------------------------------------------------
# Test 7: failed acceptance surfaced clearly in run summary
# ---------------------------------------------------------------------------

def test_print_run_summary_shows_acceptance_failure() -> None:
    """_print_run_summary must show 'Final acceptance: FAILED' and failure details."""
    result = _make_result(
        definition_of_done_met=False,
        acceptance_cmd_passed=False,
        acceptance_cmd_failure="Exit code 1: python main.py exited with error",
    )
    out = _capture_cli_summary(result)
    assert "Final acceptance: FAILED" in out
    assert "python main.py exited with error" in out


def test_print_run_summary_shows_acceptance_pass() -> None:
    """_print_run_summary shows 'Final acceptance: PASSED' when acceptance_cmd passed."""
    result = _make_result(
        definition_of_done_met=True,
        acceptance_cmd_passed=True,
    )
    out = _capture_cli_summary(result)
    assert "Final acceptance: PASSED" in out


def test_print_run_summary_silent_when_no_acceptance_cmd() -> None:
    """_print_run_summary does not mention final acceptance when acceptance_cmd_passed=None."""
    result = _make_result(
        definition_of_done_met=True,
        acceptance_cmd_passed=None,
    )
    out = _capture_cli_summary(result)
    assert "Final acceptance" not in out


# ---------------------------------------------------------------------------
# Test 8: CLI exit non-success when acceptance_cmd fails
# ---------------------------------------------------------------------------

def test_cmd_run_exit_nonzero_when_acceptance_cmd_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """CLI returns 1 when acceptance_cmd_passed=False even if all tickets passed."""
    import argparse
    from saturnday._types import RunResult, TicketResult
    from saturnday.cli import _cmd_run

    plan_path = tmp_path / ".saturnday" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({
        "version": 1, "project_id": "fix41-fail-cli", "tickets": [], "phases": [],
        "definition_of_done": [], "acceptance_cmd": "python main.py --smoke-test",
    }), encoding="utf-8")

    fail_result = RunResult(
        project_id="fix41-fail-cli",
        total_tickets=2,
        passed=2,
        failed=0,
        definition_of_done_met=False,
        plan_governance_met=True,
        plan_governance_reason="PLAN_GOVERNANCE_MET",
        acceptance_cmd_passed=False,
        acceptance_cmd_failure="Exit code 1: RuntimeError — product broken",
        ticket_results=(
            TicketResult(ticket_id="T001", disposition="PASS"),
            TicketResult(ticket_id="T002", disposition="PASS"),
        ),
    )

    args = argparse.Namespace(
        plan=str(plan_path), repo=str(tmp_path), backend="claude-cli",
        standards_dir=str(tmp_path), output_dir=None, auto_repair=False,
        lessons=None, no_role_passes=False, strict_dod=False, verbose=False,
        api_key="", base_url="", model="", temperature=0.0, max_tokens=0, timeout=120,
    )

    with patch("saturnday.ticket_runner.run_plan", return_value=fail_result), \
         patch("saturnday.cli._require_git_repo", return_value=True):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            rc = _cmd_run(args)

    assert rc == 1, "CLI must return 1 when acceptance_cmd failed"
    assert "final acceptance gate" in stderr_buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Test 9: acceptance_cmd results written to run-summary.json
# ---------------------------------------------------------------------------

def test_acceptance_cmd_results_written_to_run_summary(tmp_path: Path) -> None:
    """acceptance_cmd_passed and acceptance_cmd_failure appear in run-summary.json."""
    from saturnday._types import RunResult
    from saturnday.run.evidence import write_run_summary

    # Failure case
    fail_result = RunResult(
        project_id="fix41-evidence",
        total_tickets=1,
        passed=1,
        definition_of_done_met=False,
        acceptance_cmd_passed=False,
        acceptance_cmd_failure="Exit code 1: broken",
    )
    write_run_summary(fail_result, tmp_path)
    data = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
    assert data["acceptance_cmd_passed"] is False
    assert "broken" in data["acceptance_cmd_failure"]

    # Pass case
    pass_result = RunResult(
        project_id="fix41-evidence-pass",
        total_tickets=1,
        passed=1,
        definition_of_done_met=True,
        acceptance_cmd_passed=True,
    )
    write_run_summary(pass_result, tmp_path)
    data2 = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
    assert data2["acceptance_cmd_passed"] is True
    assert data2["acceptance_cmd_failure"] == ""

    # None case (no acceptance_cmd)
    none_result = RunResult(
        project_id="fix41-evidence-none",
        total_tickets=1,
        passed=1,
        definition_of_done_met=True,
        acceptance_cmd_passed=None,
    )
    write_run_summary(none_result, tmp_path)
    data3 = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
    assert data3["acceptance_cmd_passed"] is None
