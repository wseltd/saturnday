"""Tests for automatic role-pass wiring in Run and Repair."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _minimal_plan(project_id: str = "test") -> dict:
    """Return the smallest plan dict that passes validation."""
    return {
        "version": 1,
        "project_id": project_id,
        "tickets": [
            {"ticket_id": "T-01", "goal": "stub ticket for testing",
             "acceptance_criteria": ["stub acceptance"]},
        ],
    }


class TestRunRolePasses:
    """Test role passes in the Run flow."""

    def test_default_role_passes_true(self):
        """run_plan signature defaults role_passes to True."""
        import inspect
        from saturnday.ticket_runner import run_plan
        sig = inspect.signature(run_plan)
        assert sig.parameters["role_passes"].default is True

    @patch("saturnday.role_modes.invoke_role")
    @patch("saturnday.ticket_runner._run_ticket_with_retries")
    def test_repo_analyst_pre_run(self, mock_run_ticket, mock_invoke, tmp_path):
        """Pre-run repo_analyst pass writes JSON to output_dir."""
        from saturnday.role_modes import RoleResult
        from saturnday._types import TicketResult

        # Analyst call: success; DoD + evidence-gate also stubbed
        mock_invoke.return_value = RoleResult(
            role="repo_analyst", task="t", output="analysis", success=True,
        )
        # Stub ticket execution so no real coder is called
        mock_run_ticket.return_value = TicketResult(
            ticket_id="T-01", disposition="PASS", attempts=1,
        )

        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(_minimal_plan()))
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "out"

        from saturnday.ticket_runner import run_plan
        from saturnday._types import CoderConfig
        config = CoderConfig(backend="openai", api_key="test")
        run_plan(
            plan_path=str(plan_path), repo_path=str(repo), coder_config=config,
            standards_dir=".", output_dir=str(out), role_passes=True,
        )

        analyst_file = out / "role-pass-repo-analyst.json"
        assert analyst_file.is_file()
        data = json.loads(analyst_file.read_text())
        assert data["role"] == "repo_analyst"
        assert data["success"] is True

    @patch("saturnday.role_modes.invoke_role")
    @patch("saturnday.ticket_runner._run_ticket_with_retries")
    def test_no_role_passes_skips_all(self, mock_run_ticket, mock_invoke, tmp_path):
        """role_passes=False produces no role-pass files."""
        from saturnday._types import TicketResult

        mock_run_ticket.return_value = TicketResult(
            ticket_id="T-01", disposition="PASS", attempts=1,
        )

        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(_minimal_plan()))
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "out"

        from saturnday.ticket_runner import run_plan
        from saturnday._types import CoderConfig
        config = CoderConfig(backend="openai", api_key="test")
        run_plan(
            plan_path=str(plan_path), repo_path=str(repo), coder_config=config,
            standards_dir=".", output_dir=str(out), role_passes=False,
        )

        assert not (out / "role-pass-repo-analyst.json").exists()
        assert not (out / "role-pass-dod.json").exists()
        mock_invoke.assert_not_called()

    @patch("saturnday.role_modes.invoke_role", side_effect=Exception("backend down"))
    @patch("saturnday.ticket_runner._run_ticket_with_retries")
    def test_repo_analyst_failure_does_not_block(self, mock_run_ticket, mock_invoke, tmp_path):
        """Analyst failure is caught, run continues."""
        from saturnday._types import TicketResult

        mock_run_ticket.return_value = TicketResult(
            ticket_id="T-01", disposition="PASS", attempts=1,
        )

        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(_minimal_plan()))
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "out"

        from saturnday.ticket_runner import run_plan
        from saturnday._types import CoderConfig
        config = CoderConfig(backend="openai", api_key="test")
        # Should not raise despite invoke_role raising
        result = run_plan(
            plan_path=str(plan_path), repo_path=str(repo), coder_config=config,
            standards_dir=".", output_dir=str(out), role_passes=True,
        )
        assert result is not None


class TestRepairRolePasses:
    """Test role passes in the Repair flow."""

    @patch("saturnday.coder_adapter.call_coder", return_value="mock output")
    def test_repair_role_passes_write_files(self, mock_coder, tmp_path):
        """Post-repair role passes write governance + evidence JSON."""
        from saturnday.repair.repair_runner import RepairRunResult, run_repair_role_passes
        from saturnday._types import CoderConfig

        run_result = RepairRunResult(total_tickets=2, fixed=1, partial=0, failed=1)
        config = CoderConfig(backend="openai", api_key="test")
        out = tmp_path / "repair-evidence"

        run_repair_role_passes(run_result, out, tmp_path, coder_config=config)

        assert (out / "role-pass-governance-judge.json").is_file()
        assert (out / "role-pass-evidence-gate.json").is_file()

    @patch("saturnday.coder_adapter.call_coder", side_effect=Exception("fail"))
    def test_repair_role_passes_failure_caught(self, mock_coder, tmp_path):
        """Role pass failure does not raise."""
        from saturnday.repair.repair_runner import RepairRunResult, run_repair_role_passes
        from saturnday._types import CoderConfig

        run_result = RepairRunResult(total_tickets=1, fixed=0, partial=0, failed=1)
        config = CoderConfig(backend="openai", api_key="test")
        out = tmp_path / "repair-evidence"

        # Should not raise
        run_repair_role_passes(run_result, out, tmp_path, coder_config=config)


class TestCLIFlags:
    """Test CLI flag wiring."""

    def test_run_no_role_passes_flag(self):
        from saturnday.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["run", "--plan", "p.json", "--repo", ".", "--backend", "codex-cli", "--no-role-passes"])
        assert args.no_role_passes is True

    def test_run_default_has_role_passes(self):
        from saturnday.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["run", "--plan", "p.json", "--repo", ".", "--backend", "codex-cli"])
        assert args.no_role_passes is False

    def test_repair_no_role_passes_flag(self):
        from saturnday.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["repair", "--skill", ".", "--no-role-passes"])
        assert args.no_role_passes is True


class TestRepairCoderFnPrompt:
    """Test that repair coder_fn uses the role prompt, not a hardcoded string."""

    def test_repair_coder_fn_uses_role_prompt(self):
        """Repair coder_fn should load repair.md role prompt, not a hardcoded string."""
        # The repair prompt loaded by load_role_prompt("repair") should contain
        # content from repair.md, not just "You are a code repair agent"
        from saturnday.role_modes import load_role_prompt
        prompt = load_role_prompt("repair")
        assert "repair" in prompt.lower()
        assert len(prompt) > 200  # Real prompt, not a one-liner


class TestStageOutput:
    """Test stage output formatting."""

    def test_print_role_stage(self, capsys):
        from saturnday.interactive import print_role_stage
        print_role_stage("analysing repo")
        captured = capsys.readouterr()
        assert "--- analysing repo ---" in captured.out
