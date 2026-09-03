"""Tests for U1: default_retry_limit wired from plan into _run_ticket_with_retries.

Verifies that:
- A plan with ``default_retry_limit: 1`` causes exactly 1 retry (2 total attempts).
- A plan with the default ``default_retry_limit: 2`` causes up to 2 retries (3 total
  attempts) before giving up.
- The ``max_retries`` parameter on ``_run_ticket_with_retries`` is respected.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from saturnday._types import CoderConfig, TicketResult, TicketSpec
from saturnday.ticket_runner import MAX_REPAIR_ATTEMPTS, _run_ticket_with_retries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_coder_config() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="test-key", model="gpt-4")


def _make_ticket(ticket_id: str = "T001") -> TicketSpec:
    return TicketSpec(ticket_id=ticket_id, goal="Do something")


def _make_project_state():
    from saturnday.project_state import ProjectState
    return ProjectState(project_id="test-proj")


# ---------------------------------------------------------------------------
# Unit tests for _run_ticket_with_retries directly
# ---------------------------------------------------------------------------

class TestRunTicketWithRetriesMaxRetriesParam:
    """Test that max_retries parameter controls the number of attempts."""

    def _run_with_always_failing_coder(
        self, tmp_path: Path, max_retries: int,
    ) -> tuple[TicketResult, int]:
        """Run a ticket where the coder always raises PatchExtractionError.

        Returns the result and the number of coder call invocations.
        """
        from saturnday._exceptions import PatchExtractionError

        call_count = 0

        def _fake_execute_ticket(**kwargs):
            nonlocal call_count
            call_count += 1
            raise PatchExtractionError("Coder produced no changes (simulated failure)")

        ticket = _make_ticket()
        state = _make_project_state()

        with patch(
            "saturnday.ticket_runner._execute_ticket",
            side_effect=_fake_execute_ticket,
        ), patch(
            "saturnday.ticket_runner.write_ticket_evidence",
        ):
            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                system_prompt="test system prompt",
                state=state,
                plan_notes="",
                output_dir=tmp_path / "out",
                max_retries=max_retries,
            )

        return result, call_count

    def test_max_retries_1_makes_two_attempts(self, tmp_path: Path) -> None:
        """max_retries=1 means 1 initial attempt + 1 retry = 2 total."""
        result, call_count = self._run_with_always_failing_coder(tmp_path, max_retries=1)
        assert result.disposition == "FAIL"
        # 2 total attempts: initial + 1 retry
        assert call_count == 2

    def test_max_retries_2_makes_three_attempts(self, tmp_path: Path) -> None:
        """max_retries=2 (default) means 1 initial + 2 retries = 3 total."""
        result, call_count = self._run_with_always_failing_coder(tmp_path, max_retries=2)
        assert result.disposition == "FAIL"
        # 3 total attempts: initial + 2 retries
        assert call_count == 3

    def test_max_retries_0_makes_one_attempt(self, tmp_path: Path) -> None:
        """max_retries=0 means no retries — exactly 1 attempt."""
        result, call_count = self._run_with_always_failing_coder(tmp_path, max_retries=0)
        assert result.disposition == "FAIL"
        assert call_count == 1

    def test_default_matches_MAX_REPAIR_ATTEMPTS_constant(self, tmp_path: Path) -> None:
        """Omitting max_retries uses MAX_REPAIR_ATTEMPTS as the default."""
        from saturnday._exceptions import PatchExtractionError

        call_count = 0

        def _fake_execute(**kwargs):
            nonlocal call_count
            call_count += 1
            raise PatchExtractionError("simulated")

        ticket = _make_ticket()
        state = _make_project_state()

        with patch(
            "saturnday.ticket_runner._execute_ticket",
            side_effect=_fake_execute,
        ), patch("saturnday.ticket_runner.write_ticket_evidence"):
            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=_make_coder_config(),
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path / "out",
                # max_retries NOT passed — should default to MAX_REPAIR_ATTEMPTS
            )

        assert result.disposition == "FAIL"
        # MAX_REPAIR_ATTEMPTS + 1 total calls
        assert call_count == MAX_REPAIR_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# Integration: plan.default_retry_limit flows through run_plan
# ---------------------------------------------------------------------------

class TestRunPlanPassesRetryLimit:
    """Test that run_plan passes plan.default_retry_limit to _run_ticket_with_retries."""

    def _write_plan(self, plan_data: dict, dir_path: Path) -> Path:
        import json
        path = dir_path / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        return path

    def test_plan_retry_limit_1_propagated(self, tmp_path: Path) -> None:
        """A plan with default_retry_limit=1 should result in at most 2 total attempts."""
        plan_data = {
            "version": 1,
            "project_id": "retry-test",
            "default_retry_limit": 1,
            "tickets": [{"ticket_id": "T001", "goal": "Test ticket", "acceptance_criteria": ["done"]}],
        }
        plan_path = self._write_plan(plan_data, tmp_path)

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        standards_dir = tmp_path / "standards"
        standards_dir.mkdir()

        captured_max_retries: list[int] = []

        original_run = _run_ticket_with_retries

        def _spy_run_ticket(**kwargs):
            captured_max_retries.append(kwargs.get("max_retries", MAX_REPAIR_ATTEMPTS))
            # Return a passing result to avoid needing full git/coder setup
            return TicketResult(
                ticket_id=kwargs["ticket"].ticket_id,
                disposition="PASS",
                attempts=1,
                changed_files=("dummy.py",),
            )

        with patch(
            "saturnday.ticket_runner._run_ticket_with_retries",
            side_effect=_spy_run_ticket,
        ), patch(
            "saturnday.ticket_runner.build_system_prompt",
            return_value="sys",
        ), patch(
            "saturnday.ticket_runner.write_run_metadata",
        ), patch(
            "saturnday.ticket_runner.write_run_summary",
        ), patch(
            "saturnday.ticket_runner.write_ledger_snapshot",
        ), patch(
            "saturnday.ticket_runner.write_phase_summary",
        ), patch(
            "saturnday.ticket_runner.write_analytics",
        ), patch(
            "saturnday.ticket_runner.compute_run_analytics",
            return_value={
                "acceptance_rate": 1.0,
                "avg_retries": 0.0,
                "senior_quality_verdict": {"quality_level": "high"},
                "definition_of_done_met": True,
            },
        ), patch(
            "saturnday.ticket_runner.load_state",
            return_value=None,
        ), patch(
            "saturnday.ticket_runner.save_state",
        ), patch(
            "saturnday.ticket_runner.update_state",
            return_value=MagicMock(project_id="retry-test"),
        ), patch(
            "saturnday.ticket_runner._ensure_gitignore",
        ), patch(
            "saturnday.ticket_runner.detect_auth_mode",
            return_value="api_key",
        ), patch(
            "saturnday.ticket_runner.capability_matrix",
            return_value={},
        ), patch(
            "saturnday.ticket_splitter.analyze_and_split",
            side_effect=lambda ticket, *a, **kw: [ticket],
        ):
            from saturnday.ticket_runner import run_plan
            result = run_plan(
                plan_path=plan_path,
                repo_path=repo_path,
                coder_config=_make_coder_config(),
                standards_dir=standards_dir,
                output_dir=tmp_path / "out",
            )

        assert len(captured_max_retries) == 1
        assert captured_max_retries[0] == 1, (
            f"Expected max_retries=1 from plan.default_retry_limit, got {captured_max_retries[0]}"
        )

    def test_plan_default_retry_limit_is_2(self, tmp_path: Path) -> None:
        """A plan without default_retry_limit should propagate 2 (the default)."""
        plan_data = {
            "version": 1,
            "project_id": "retry-default",
            # No default_retry_limit — should default to 2
            "tickets": [{"ticket_id": "T001", "goal": "Test ticket", "acceptance_criteria": ["done"]}],
        }
        plan_path = self._write_plan(plan_data, tmp_path)

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        standards_dir = tmp_path / "standards"
        standards_dir.mkdir()

        captured_max_retries: list[int] = []

        def _spy_run_ticket(**kwargs):
            captured_max_retries.append(kwargs.get("max_retries", MAX_REPAIR_ATTEMPTS))
            return TicketResult(
                ticket_id=kwargs["ticket"].ticket_id,
                disposition="PASS",
                attempts=1,
                changed_files=("dummy.py",),
            )

        with patch(
            "saturnday.ticket_runner._run_ticket_with_retries",
            side_effect=_spy_run_ticket,
        ), patch(
            "saturnday.ticket_runner.build_system_prompt",
            return_value="sys",
        ), patch(
            "saturnday.ticket_runner.write_run_metadata",
        ), patch(
            "saturnday.ticket_runner.write_run_summary",
        ), patch(
            "saturnday.ticket_runner.write_ledger_snapshot",
        ), patch(
            "saturnday.ticket_runner.write_phase_summary",
        ), patch(
            "saturnday.ticket_runner.write_analytics",
        ), patch(
            "saturnday.ticket_runner.compute_run_analytics",
            return_value={
                "acceptance_rate": 1.0,
                "avg_retries": 0.0,
                "senior_quality_verdict": {"quality_level": "high"},
                "definition_of_done_met": True,
            },
        ), patch(
            "saturnday.ticket_runner.load_state",
            return_value=None,
        ), patch(
            "saturnday.ticket_runner.save_state",
        ), patch(
            "saturnday.ticket_runner.update_state",
            return_value=MagicMock(project_id="retry-default"),
        ), patch(
            "saturnday.ticket_runner._ensure_gitignore",
        ), patch(
            "saturnday.ticket_runner.detect_auth_mode",
            return_value="api_key",
        ), patch(
            "saturnday.ticket_runner.capability_matrix",
            return_value={},
        ), patch(
            "saturnday.ticket_splitter.analyze_and_split",
            side_effect=lambda ticket, *a, **kw: [ticket],
        ):
            from saturnday.ticket_runner import run_plan
            result = run_plan(
                plan_path=plan_path,
                repo_path=repo_path,
                coder_config=_make_coder_config(),
                standards_dir=standards_dir,
                output_dir=tmp_path / "out",
            )

        assert len(captured_max_retries) == 1
        assert captured_max_retries[0] == 2, (
            f"Expected max_retries=2 (default), got {captured_max_retries[0]}"
        )
