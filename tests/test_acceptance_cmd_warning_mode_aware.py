"""Fix 45 warning + planner prompt are mode-aware about acceptance_cmd
vs local_proof_cmd.

Before this batch:
  * ``plan_parser.validate_plan_dict`` fired a Fix 45 warning whenever
    ``acceptance_cmd`` was empty for any plan whose notes mentioned a
    runnable product.  Fix 76 had deprecated ``acceptance_cmd`` for any
    declared ``operating_mode``, replacing it with ``local_proof_cmd``,
    but Fix 45 was never updated to know about the new field.  Result:
    every correctly-shaped Fix-76 plan tripped a false-positive
    "you have no acceptance_cmd!" warning even when its
    ``local_proof_cmd`` was correctly populated.
  * The planner prompt told the LLM to set ``acceptance_cmd`` for any
    runnable plan, full stop.  Fix 76's ``_validate_mode_and_proof_fields``
    then rejected exactly that for declared modes, producing
    contradictory instructions and a planning loop that often left both
    proof fields empty.

This batch makes both spots mode-aware.
"""
from __future__ import annotations

import logging

import pytest

from saturnday.plan_parser import validate_plan
from saturnday.run.planner import _build_planner_prompt


# ---------------------------------------------------------------------------
# 1. validate_plan_dict — Fix 45 warning is mode-aware
# ---------------------------------------------------------------------------


_RUNNABLE_NOTES = "Build a small web api for users."
_BASE_PLAN = {
    "project_id": "demo",
    "phases": [],
    # validate_plan returns early if tickets is empty (line 267-269), so
    # provide a minimal-but-valid ticket so execution reaches the Fix 45
    # warning at line 352.
    "tickets": [
        {
            "ticket_id": "T001",
            "goal": "stub",
            "acceptance_criteria": ["function foo exists in src/demo/core.py"],
        }
    ],
    "definition_of_done": [],
    "stop_conditions": [],
    "notes": _RUNNABLE_NOTES,
}


class TestFix45ModeAware:
    def test_legacy_unclassified_warns_only_when_acceptance_cmd_empty(
        self, caplog,
    ) -> None:
        """legacy_unclassified plan with empty acceptance_cmd on a
        runnable description must still warn."""
        plan = {
            **_BASE_PLAN,
            "operating_mode": "legacy_unclassified",
            "acceptance_cmd": "",
            "local_proof_cmd": "",
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Fix 45" in m and "acceptance_cmd" in m for m in msgs)

    def test_legacy_unclassified_does_not_warn_when_acceptance_cmd_set(
        self, caplog,
    ) -> None:
        plan = {
            **_BASE_PLAN,
            "operating_mode": "legacy_unclassified",
            "acceptance_cmd": "python -m mypkg --healthcheck",
            "local_proof_cmd": "",
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("Fix 45" in m for m in msgs), (
            "Fix 45 must not fire when the appropriate proof field is set"
        )

    def test_declared_mode_with_local_proof_cmd_set_does_not_warn(
        self, caplog,
    ) -> None:
        """The exact regression closure: a Fix-76 plan with operating_mode
        and a populated local_proof_cmd must NOT trip Fix 45 just because
        acceptance_cmd is empty."""
        plan = {
            **_BASE_PLAN,
            "operating_mode": "web_service",
            "acceptance_cmd": "",
            "local_proof_cmd": "python -m demo serve & sleep 2 && curl -fsS http://127.0.0.1:8000/health",
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("Fix 45" in m for m in msgs), (
            "Fix-76 plan with local_proof_cmd populated must NOT trigger "
            "the legacy acceptance_cmd warning"
        )

    def test_declared_mode_with_empty_local_proof_cmd_warns(
        self, caplog,
    ) -> None:
        """Declared-mode plan with empty local_proof_cmd on a runnable
        description must warn — naming local_proof_cmd, not
        acceptance_cmd."""
        plan = {
            **_BASE_PLAN,
            "operating_mode": "cli_tool",
            "acceptance_cmd": "",
            "local_proof_cmd": "",
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        fix45 = [m for m in msgs if "Fix 45" in m]
        assert fix45, "Fix 45 must still fire when the right field is empty"
        assert any("local_proof_cmd" in m for m in fix45), (
            "warning must name the mode-appropriate field"
        )
        assert not any(
            "acceptance_cmd" in m and "local_proof_cmd" not in m
            for m in fix45
        ), "must not name acceptance_cmd for a declared-mode plan"

    @pytest.mark.parametrize("mode", [
        "web_service", "cli_tool", "library", "worker", "pipeline", "frontend",
    ])
    def test_every_declared_mode_consults_local_proof_cmd(
        self, mode: str, caplog,
    ) -> None:
        plan = {
            **_BASE_PLAN,
            "operating_mode": mode,
            "acceptance_cmd": "",
            "local_proof_cmd": (
                # Any non-empty value silences the warning.
                "python -m demo --selftest"
            ),
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("Fix 45" in m for m in msgs)

    def test_non_runnable_plan_does_not_warn(self, caplog) -> None:
        """No runnable hint in notes/goal → no Fix 45 warning regardless
        of which field is empty.  Pre-batch behaviour preserved."""
        plan = {
            **_BASE_PLAN,
            "notes": "Refactor internal helpers.  No new entry points.",
            "operating_mode": "legacy_unclassified",
            "acceptance_cmd": "",
            "local_proof_cmd": "",
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("Fix 45" in m for m in msgs)

    def test_absent_operating_mode_treated_as_legacy(self, caplog) -> None:
        """A plan with no operating_mode field is treated as legacy
        for Fix 45 purposes (consults acceptance_cmd)."""
        plan = {
            **_BASE_PLAN,
            "acceptance_cmd": "",
            "local_proof_cmd": "",
        }
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            validate_plan(plan)
        msgs = [r.getMessage() for r in caplog.records]
        fix45 = [m for m in msgs if "Fix 45" in m]
        assert fix45 and any("acceptance_cmd" in m for m in fix45)


# ---------------------------------------------------------------------------
# 2. Planner prompt — mode-aware guidance
# ---------------------------------------------------------------------------


class TestPlannerPromptModeAware:
    def test_prompt_describes_local_proof_cmd_for_declared_modes(self) -> None:
        prompt = _build_planner_prompt(
            brief="Build a CLI for X",
            repo_context="",
        )
        # The prompt must mention local_proof_cmd as the field for
        # declared modes.
        assert "local_proof_cmd" in prompt
        # And must instruct that acceptance_cmd is empty in that case.
        assert "acceptance_cmd EMPTY" in prompt or (
            "acceptance_cmd" in prompt and "empty" in prompt.lower()
        )

    def test_prompt_describes_acceptance_cmd_for_legacy(self) -> None:
        prompt = _build_planner_prompt(
            brief="Build a CLI for X",
            repo_context="",
        )
        assert "legacy_unclassified" in prompt
        assert "acceptance_cmd" in prompt

    def test_prompt_no_longer_unconditionally_demands_acceptance_cmd(
        self,
    ) -> None:
        """The pre-batch guidance was a single line that told the LLM to
        set acceptance_cmd for any runnable product, full stop.  That
        line conflicts with Fix 76's validator and must be gone."""
        prompt = _build_planner_prompt(
            brief="Build a CLI for X",
            repo_context="",
        )
        assert "include a plan-level acceptance_cmd" not in prompt, (
            "pre-batch unconditional guidance must be replaced with "
            "mode-aware guidance"
        )
