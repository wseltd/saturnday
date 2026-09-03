"""Tests for the ``--max-failures`` CLI flag on run / resume / rerun-failed /
rerun-remaining.

Before this batch, ``consecutive_failure_limit`` was only reachable via
``saturnday repair --max-failures``.  ``saturnday run`` and its resume /
rerun variants had no knob, so operators hitting the default limit of 3
had to choose between (a) repeatedly re-invoking resume (each resume
starts a fresh ledger with counter=0) or (b) editing
``run_ledger.py:26`` in source.

The fix threads a single new flag through:

    CLI parser → _cmd_* handler → run_plan / resume_plan / rerun_failed /
    rerun_remaining → _run_with_filter → create_ledger_from_plan →
    RunLedger(consecutive_failure_limit=...)

``--max-failures`` is typed ``int`` and defaults to ``None`` at the CLI
and helper layer.  ``None`` means "use the library default"
(``DEFAULT_CONSECUTIVE_FAILURE_LIMIT = 3``) so existing callers are
byte-for-byte preserved.
"""
from __future__ import annotations

import pytest

from saturnday._types import PhaseDef
from saturnday.cli import build_parser
from saturnday.run.run_ledger import (
    DEFAULT_CONSECUTIVE_FAILURE_LIMIT,
    create_ledger_from_plan,
)


# ---------------------------------------------------------------------------
# 1. create_ledger_from_plan accepts an optional override
# ---------------------------------------------------------------------------


class TestCreateLedgerFromPlanLimitOverride:
    def _phases(self) -> tuple[PhaseDef, ...]:
        return (PhaseDef(phase_id="p1", name="Phase 1", ticket_ids=("T1",)),)

    def test_default_none_preserves_library_default(self) -> None:
        """Passing ``consecutive_failure_limit=None`` (or omitting it)
        must leave the ledger on ``DEFAULT_CONSECUTIVE_FAILURE_LIMIT``."""
        ledger = create_ledger_from_plan(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=(),
            max_project_tickets=None,
            phases=self._phases(),
            ticket_ids=("T1",),
        )
        assert ledger.consecutive_failure_limit == DEFAULT_CONSECUTIVE_FAILURE_LIMIT

    def test_explicit_none_also_preserves_default(self) -> None:
        ledger = create_ledger_from_plan(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=(),
            max_project_tickets=None,
            phases=self._phases(),
            ticket_ids=("T1",),
            consecutive_failure_limit=None,
        )
        assert ledger.consecutive_failure_limit == DEFAULT_CONSECUTIVE_FAILURE_LIMIT

    def test_override_value_is_threaded_through(self) -> None:
        ledger = create_ledger_from_plan(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=(),
            max_project_tickets=None,
            phases=self._phases(),
            ticket_ids=("T1",),
            consecutive_failure_limit=20,
        )
        assert ledger.consecutive_failure_limit == 20

    def test_override_of_one_also_works(self) -> None:
        """Lower-than-default override: tight API-budget runs want
        early abort."""
        ledger = create_ledger_from_plan(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=(),
            max_project_tickets=None,
            phases=self._phases(),
            ticket_ids=("T1",),
            consecutive_failure_limit=1,
        )
        assert ledger.consecutive_failure_limit == 1


# ---------------------------------------------------------------------------
# 2. CLI parser exposes --max-failures on all four relevant subcommands
# ---------------------------------------------------------------------------


class TestCliParserExposesMaxFailures:
    @pytest.mark.parametrize("subcommand", [
        "run", "resume", "rerun-failed", "rerun-remaining",
    ])
    def test_each_subcommand_accepts_max_failures(
        self, subcommand: str, tmp_path,
    ) -> None:
        """The four run-style subcommands must parse ``--max-failures 7``
        without error and surface the value on ``args.max_failures``."""
        plan_path = tmp_path / "plan.json"
        plan_path.write_text("{}")

        parser = build_parser()

        base = [
            subcommand,
            "--plan", str(plan_path),
            "--repo", str(tmp_path),
            "--backend", "claude-cli",
        ]
        if subcommand != "run":
            # resume / rerun-failed / rerun-remaining require --output-dir.
            base += ["--output-dir", str(tmp_path / "evidence")]

        args = parser.parse_args(base + ["--max-failures", "7"])
        assert args.max_failures == 7

    @pytest.mark.parametrize("subcommand", [
        "run", "resume", "rerun-failed", "rerun-remaining",
    ])
    def test_default_is_none_when_flag_omitted(
        self, subcommand: str, tmp_path,
    ) -> None:
        """Without the flag, ``args.max_failures`` must be ``None`` so
        the downstream helper preserves the library default."""
        plan_path = tmp_path / "plan.json"
        plan_path.write_text("{}")

        parser = build_parser()
        base = [
            subcommand,
            "--plan", str(plan_path),
            "--repo", str(tmp_path),
            "--backend", "claude-cli",
        ]
        if subcommand != "run":
            base += ["--output-dir", str(tmp_path / "evidence")]

        args = parser.parse_args(base)
        assert getattr(args, "max_failures", "MISSING") is None

    def test_non_integer_is_rejected(self, tmp_path) -> None:
        """Argparse should reject a non-integer value with a clean error."""
        plan_path = tmp_path / "plan.json"
        plan_path.write_text("{}")

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "run",
                "--plan", str(plan_path),
                "--repo", str(tmp_path),
                "--backend", "claude-cli",
                "--max-failures", "not-an-integer",
            ])


# ---------------------------------------------------------------------------
# 3. Regression: run_plan / resume_plan / rerun_* accept the new kwarg
# ---------------------------------------------------------------------------


class TestHelperSignatures:
    """Pin the new kwarg on each helper's public signature so future
    refactors can't silently drop it."""

    def test_run_plan_accepts_max_consecutive_failures(self) -> None:
        import inspect
        from saturnday.ticket_runner import run_plan
        sig = inspect.signature(run_plan)
        assert "max_consecutive_failures" in sig.parameters
        assert sig.parameters["max_consecutive_failures"].default is None

    def test_resume_plan_accepts_max_consecutive_failures(self) -> None:
        import inspect
        from saturnday.run.resume import resume_plan
        sig = inspect.signature(resume_plan)
        assert "max_consecutive_failures" in sig.parameters
        assert sig.parameters["max_consecutive_failures"].default is None

    def test_rerun_failed_accepts_max_consecutive_failures(self) -> None:
        import inspect
        from saturnday.run.resume import rerun_failed
        sig = inspect.signature(rerun_failed)
        assert "max_consecutive_failures" in sig.parameters
        assert sig.parameters["max_consecutive_failures"].default is None

    def test_rerun_remaining_accepts_max_consecutive_failures(self) -> None:
        import inspect
        from saturnday.run.resume import rerun_remaining
        sig = inspect.signature(rerun_remaining)
        assert "max_consecutive_failures" in sig.parameters
        assert sig.parameters["max_consecutive_failures"].default is None
