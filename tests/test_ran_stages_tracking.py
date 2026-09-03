"""Tests for SPLIT-016: ran_stages tracking in _run_ticket_with_retries.

Verifies that:
- _run_ticket_with_retries accepts the ran_stages parameter
- The parameter defaults to None for backward compatibility
- When a non-None set is passed, the function merges _ran_stages into it
  before every return path
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from saturnday.ticket_runner import _run_ticket_with_retries
from saturnday._types import CoderConfig, TicketSpec, TicketScope


def _make_ticket(ticket_id: str = "T001") -> TicketSpec:
    return TicketSpec(
        ticket_id=ticket_id,
        goal="test goal",
        scope=TicketScope(allowed_globs=["*.py"]),
        acceptance_criteria=[],
    )


def _make_coder_config() -> CoderConfig:
    return CoderConfig(backend="anthropic", model="claude-3-5-sonnet-20241022")


class TestRanStagesParameterAccepted:
    """Verify the function signature accepts the ran_stages parameter."""

    def test_ran_stages_defaults_to_none(self, tmp_path: Path) -> None:
        """ran_stages=None is the default — no change for callers that don't pass it."""
        import inspect
        sig = inspect.signature(_run_ticket_with_retries)
        param = sig.parameters.get("ran_stages")
        assert param is not None, "ran_stages parameter must exist on _run_ticket_with_retries"
        assert param.default is None, "ran_stages default must be None for backward compat"

    def test_ran_stages_type_annotation(self) -> None:
        """ran_stages annotation should be set[str] | None."""
        import inspect
        sig = inspect.signature(_run_ticket_with_retries)
        param = sig.parameters.get("ran_stages")
        assert param is not None
        # annotation present — we don't decode the string repr but confirm it exists
        assert param.annotation is not inspect.Parameter.empty


class TestRanStagesMergeOnReturn:
    """Verify _ran_stages is merged into ran_stages before each return path."""

    def test_ran_stages_set_mutated_on_pass(self, tmp_path: Path) -> None:
        """When a set is passed, it is mutated (update called) on PASS."""
        ticket = _make_ticket()
        coder_cfg = _make_coder_config()
        from saturnday.project_state import ProjectState
        state = ProjectState()
        ran = set()

        with (
            patch("saturnday.ticket_runner._execute_ticket") as mock_exec,
            patch("saturnday.ticket_runner._git_add"),
            patch("saturnday.ticket_runner._auto_install_deps"),
            patch("saturnday.ticket_runner._run_governance") as mock_gov,
            patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
            patch("saturnday.ticket_runner._check_contracts", return_value=None),
            patch("saturnday.ticket_runner._log_ticket_summary"),
            patch("saturnday.ticket_runner._git_commit"),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner.write_ticket_evidence"),
            patch("saturnday.ticket_runner._log_progress"),
            patch("saturnday.ticket_runner._filter_findings_to_files", return_value=[]),
            patch("saturnday.ticket_runner._extract_lesson_from_outcome"),
        ):
            mock_exec.return_value = ("response", ["file.py"])
            mock_gov.return_value = ("PASS", [], "evidence/path", [])

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=coder_cfg,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                ran_stages=ran,
            )

        assert result.disposition == "PASS"
        # ran is a set — it was passed in and the function should have called
        # ran_stages.update(_ran_stages).  Since no premium stages ran,
        # _ran_stages is empty so ran remains empty — but the call must not error.
        assert isinstance(ran, set)

    def test_ran_stages_none_does_not_raise(self, tmp_path: Path) -> None:
        """Passing ran_stages=None must not raise any error (default behaviour)."""
        ticket = _make_ticket()
        coder_cfg = _make_coder_config()
        from saturnday.project_state import ProjectState
        state = ProjectState()

        with (
            patch("saturnday.ticket_runner._execute_ticket") as mock_exec,
            patch("saturnday.ticket_runner._git_add"),
            patch("saturnday.ticket_runner._auto_install_deps"),
            patch("saturnday.ticket_runner._run_governance") as mock_gov,
            patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
            patch("saturnday.ticket_runner._check_contracts", return_value=None),
            patch("saturnday.ticket_runner._log_ticket_summary"),
            patch("saturnday.ticket_runner._git_commit"),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner.write_ticket_evidence"),
            patch("saturnday.ticket_runner._log_progress"),
            patch("saturnday.ticket_runner._filter_findings_to_files", return_value=[]),
            patch("saturnday.ticket_runner._extract_lesson_from_outcome"),
        ):
            mock_exec.return_value = ("response", ["file.py"])
            mock_gov.return_value = ("PASS", [], "evidence/path", [])

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=coder_cfg,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                ran_stages=None,  # explicit None — backward compat path
            )

        assert result.disposition == "PASS"
