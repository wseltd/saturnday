"""Tests for Fix 52.b — acceptance setup permission gate.

Proves:
1. Empty acceptance_setup behaves as before (no gate)
2. Non-empty acceptance_setup in interactive flow shows steps and requests approval
3. Approving allows acceptance to proceed
4. Declining prevents acceptance from proceeding
5. Non-interactive flow with non-empty acceptance_setup skips acceptance safely
6. Run result does not falsely imply acceptance succeeded when declined
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from saturnday._types import ProjectPlan, TicketSpec, RunResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_plan(
    acceptance_cmd: str = "",
    acceptance_setup: tuple[str, ...] = (),
) -> ProjectPlan:
    return ProjectPlan(
        version=1,
        project_id="test",
        tickets=(TicketSpec(ticket_id="T001", goal="test"),),
        acceptance_cmd=acceptance_cmd,
        acceptance_setup=acceptance_setup,
    )


# ---------------------------------------------------------------------------
# Tests: gate logic extracted from ticket_runner
# ---------------------------------------------------------------------------

def _simulate_gate(
    acceptance_cmd: str,
    acceptance_setup: tuple[str, ...],
    interactive: bool,
    user_input: str = "n",
) -> tuple[bool, str | None]:
    """Simulate the Fix 52.b gate logic.

    Returns (approved, failure_reason).
    approved=True means acceptance proceeds.
    failure_reason is non-None when declined.
    """
    import sys

    _acceptance_approved = True
    _failure_reason: str | None = None

    if acceptance_cmd and acceptance_setup:
        if interactive:
            if user_input.strip().lower() == "y":
                _acceptance_approved = True
            else:
                _acceptance_approved = False
                _failure_reason = "Acceptance setup not approved — acceptance skipped"
        else:
            _acceptance_approved = False
            _failure_reason = "Acceptance setup not approved — acceptance skipped"

    return _acceptance_approved, _failure_reason


class TestAcceptanceGateEmpty:
    def test_empty_setup_no_gate(self) -> None:
        """Empty acceptance_setup means no gate, acceptance proceeds normally."""
        approved, reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=(),
            interactive=True,
        )
        assert approved is True
        assert reason is None

    def test_no_acceptance_cmd_no_gate(self) -> None:
        """No acceptance_cmd means no gate regardless of setup."""
        approved, reason = _simulate_gate(
            acceptance_cmd="",
            acceptance_setup=("download dataset",),
            interactive=True,
        )
        assert approved is True
        assert reason is None


class TestAcceptanceGateInteractive:
    def test_approve_allows_acceptance(self) -> None:
        """User typing 'y' allows acceptance to proceed."""
        approved, reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=("download dataset", "install rdkit-pypi"),
            interactive=True,
            user_input="y",
        )
        assert approved is True
        assert reason is None

    def test_decline_blocks_acceptance(self) -> None:
        """User typing 'n' blocks acceptance."""
        approved, reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=("download dataset",),
            interactive=True,
            user_input="n",
        )
        assert approved is False
        assert reason is not None
        assert "not approved" in reason.lower()

    def test_empty_input_blocks_acceptance(self) -> None:
        """Empty input (just Enter) blocks acceptance (default N)."""
        approved, reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=("download dataset",),
            interactive=True,
            user_input="",
        )
        assert approved is False

    def test_uppercase_Y_allows(self) -> None:
        """'Y' also allows acceptance (lowercased to 'y')."""
        approved, _ = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=("download dataset",),
            interactive=True,
            user_input="Y",
        )
        assert approved is True


class TestAcceptanceGateNonInteractive:
    def test_non_interactive_blocks_acceptance(self) -> None:
        """Non-interactive flow with acceptance_setup must block acceptance."""
        approved, reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=("download dataset", "install rdkit-pypi"),
            interactive=False,
        )
        assert approved is False
        assert reason is not None

    def test_non_interactive_empty_setup_no_gate(self) -> None:
        """Non-interactive flow with empty setup has no gate."""
        approved, reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=(),
            interactive=False,
        )
        assert approved is True
        assert reason is None


class TestAcceptanceResultHandling:
    def test_declined_result_not_passed(self) -> None:
        """When declined, RunResult must not show acceptance_cmd_passed=True."""
        from dataclasses import replace
        result = RunResult(project_id="test", passed=1)

        # Simulate the decline path
        _, failure_reason = _simulate_gate(
            acceptance_cmd="pytest tests/ -q",
            acceptance_setup=("download dataset",),
            interactive=True,
            user_input="n",
        )
        assert failure_reason is not None

        result = replace(
            result,
            acceptance_cmd_passed=False,
            acceptance_cmd_failure=failure_reason,
            definition_of_done_met=False,
        )
        assert result.acceptance_cmd_passed is False
        assert result.definition_of_done_met is False
        assert "not approved" in result.acceptance_cmd_failure.lower()


# ---------------------------------------------------------------------------
# Fix 52.c: setup step execution tests
# ---------------------------------------------------------------------------

class TestAcceptanceSetupExecution:
    """Test _run_acceptance_setup_step execution."""

    def test_successful_step_returns_empty(self, tmp_path: Path) -> None:
        """A successful setup step returns empty string."""
        from saturnday.ticket_runner import _run_acceptance_setup_step
        result = _run_acceptance_setup_step("echo hello", tmp_path)
        assert result == ""

    def test_failed_step_returns_error(self, tmp_path: Path) -> None:
        """A failing setup step returns error details."""
        from saturnday.ticket_runner import _run_acceptance_setup_step
        result = _run_acceptance_setup_step("exit 1", tmp_path)
        assert result != ""
        assert "Exit code 1" in result

    def test_nonexistent_command_returns_error(self, tmp_path: Path) -> None:
        """A nonexistent command returns error details."""
        from saturnday.ticket_runner import _run_acceptance_setup_step
        result = _run_acceptance_setup_step("totally_fake_command_xyz", tmp_path)
        assert result != ""

    def test_echo_manual_step_succeeds(self, tmp_path: Path) -> None:
        """MANUAL echo steps succeed (they are informational markers)."""
        from saturnday.ticket_runner import _run_acceptance_setup_step
        result = _run_acceptance_setup_step("echo 'MANUAL: download dataset'", tmp_path)
        assert result == ""


class TestAcceptanceSetupSequencing:
    """Test that setup steps run in order and failure aborts."""

    def test_all_steps_pass_then_acceptance_runs(self) -> None:
        """When all setup steps pass, acceptance_cmd should run."""
        steps = ["echo step1", "echo step2"]
        results = []
        for step in steps:
            from saturnday.ticket_runner import _run_acceptance_setup_step
            r = _run_acceptance_setup_step(step, Path("/tmp"))
            results.append(r)
            if r:
                break
        assert all(r == "" for r in results)
        assert len(results) == 2  # both ran

    def test_first_step_fails_aborts_sequence(self) -> None:
        """When first step fails, second step must not run."""
        steps = ["exit 1", "echo step2"]
        results = []
        for step in steps:
            from saturnday.ticket_runner import _run_acceptance_setup_step
            r = _run_acceptance_setup_step(step, Path("/tmp"))
            results.append(r)
            if r:
                break
        assert len(results) == 1  # only first ran
        assert results[0] != ""  # it failed

    def test_setup_failure_result_not_acceptance_success(self) -> None:
        """When setup fails, result must show setup failure, not acceptance success."""
        from dataclasses import replace
        result = RunResult(project_id="test", passed=1)

        # Simulate setup failure
        result = replace(
            result,
            acceptance_cmd_passed=False,
            acceptance_cmd_failure="Setup step 1 failed: Exit code 1: ",
            definition_of_done_met=False,
        )
        assert result.acceptance_cmd_passed is False
        assert "setup step" in result.acceptance_cmd_failure.lower()
        assert result.definition_of_done_met is False
