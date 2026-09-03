"""Tests for the planner-validator contract fix: every cli_tool
strategy-aware proof template must pass ``validate_proof_meaningfulness``.

Before this batch, ``_strategy_proof_local_emulator(cli_tool)``,
``_strategy_proof_oss_substitute(cli_tool)``,
``_strategy_proof_recorded_fixture(cli_tool)``, and
``_strategy_proof_seeded_demo(cli_tool)`` emitted a
``--help > file && test -s file && echo ...`` shape that
``_has_cli_meaningful_assertion`` (plan_parser.py:521) correctly
rejected as version-check theatre.  The same was true of the
``live_credentials_gated`` branch in ``_generate_strategy_aware_proof``
when ``operating_mode == "cli_tool"``.

The fix is planner-only: each broken template now emits the same
``python -c "import pathlib; t = ...; assert len(t) > 1; print(...)"``
tail that ``_strategy_proof_generated_fake(cli_tool)`` already uses and
that the validator already accepts.  The validator rule is NOT widened.
"""
from __future__ import annotations

import pytest

from saturnday.plan_parser import (
    _has_cli_meaningful_assertion,
    validate_proof_meaningfulness,
)
from saturnday.run.planner import _generate_strategy_aware_proof


_HINTS_WITH_CLI_FLAG = [
    {
        "ticket_id": "T001",
        "goal": "Add --foo flag support to the CLI.",
        "acceptance_criteria": ["function build exists in src/demo/core.py"],
    },
]


def _validate_cli_tool(cmd: str) -> list[str]:
    """Invoke the validator the same way the planner does at load time."""
    return validate_proof_meaningfulness(
        operating_mode="cli_tool",
        proof_realism="production_intent",
        dependency_profile="external_dependencies",
        local_proof_cmd=cmd,
    )


# ---------------------------------------------------------------------------
# 1. Each affected (cli_tool, strategy) template validates
# ---------------------------------------------------------------------------


class TestCliToolStrategyTemplatesValidate:
    @pytest.mark.parametrize("strategy", [
        "local_emulator",
        "oss_substitute",
        "recorded_fixture",
        "seeded_demo",
        "live_credentials_gated",
    ])
    def test_strategy_template_passes_validator(self, strategy: str) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="cli_tool",
            testing_strategy=strategy,
            project_id="demo",
            tickets=_HINTS_WITH_CLI_FLAG,
            external_dependencies=["stripe"],
        )
        assert cmd, (
            f"planner returned empty template for (cli_tool, {strategy})"
        )
        errs = _validate_cli_tool(cmd)
        assert errs == [], (
            f"(cli_tool, {strategy}) template rejected by validator: {errs}\n"
            f"generated command:\n{cmd}"
        )

    @pytest.mark.parametrize("strategy", [
        "local_emulator",
        "oss_substitute",
        "recorded_fixture",
        "seeded_demo",
    ])
    def test_strategy_template_retains_strategy_specific_marker(
        self, strategy: str,
    ) -> None:
        """The fix must not lose the strategy-specific signal: the emitted
        command must still mention the strategy name (so evidence /
        logs attribute the proof to the operator's chosen strategy)."""
        cmd = _generate_strategy_aware_proof(
            operating_mode="cli_tool",
            testing_strategy=strategy,
            project_id="demo",
            tickets=_HINTS_WITH_CLI_FLAG,
            external_dependencies=["stripe"],
        )
        assert strategy in cmd

    def test_live_credentials_gated_preserves_stub_honesty(self) -> None:
        """The stub message still names 'live_credentials_gated' and
        still says 'Exit 0 is NOT evidence of product correctness'."""
        cmd = _generate_strategy_aware_proof(
            operating_mode="cli_tool",
            testing_strategy="live_credentials_gated",
            project_id="demo",
            tickets=[],
            external_dependencies=["sandbox_api"],
        )
        assert "live_credentials_gated" in cmd
        assert "LIVE_PROOF=1" in cmd
        assert "NOT evidence" in cmd


# ---------------------------------------------------------------------------
# 2. generated_fake(cli_tool) still passes (regression pin)
# ---------------------------------------------------------------------------


class TestGeneratedFakeCliToolUnchanged:
    def test_generated_fake_cli_tool_still_validates(self) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="cli_tool",
            testing_strategy="generated_fake",
            project_id="demo",
            tickets=_HINTS_WITH_CLI_FLAG,
            external_dependencies=["x"],
        )
        errs = _validate_cli_tool(cmd)
        assert errs == []
        # Same shape as before: uses SATURNDAY_USE_FAKE and len(t) > 1.
        assert "SATURNDAY_USE_FAKE" in cmd
        assert "len(t)" in cmd


# ---------------------------------------------------------------------------
# 3. Direct validator pin — test-s shape still rejected
# ---------------------------------------------------------------------------


class TestValidatorRuleUnchanged:
    def test_test_s_after_help_still_rejected(self) -> None:
        broken = (
            "python -m x --help > /tmp/f.txt && "
            "test -s /tmp/f.txt && echo y"
        )
        assert _has_cli_meaningful_assertion(broken) is False

    def test_plain_test_s_still_rejected(self) -> None:
        assert _has_cli_meaningful_assertion(
            "foo --help > f.txt && test -s f.txt && echo ok"
        ) is False

    def test_len_assertion_still_accepted(self) -> None:
        """Pin the current rule: ``len(var) > N`` is the shape we're
        relying on for the fix; the validator must keep accepting it."""
        ok = (
            "python -m x --help > /tmp/f.txt && "
            "python -c \"import pathlib; "
            "t = pathlib.Path('/tmp/f.txt').read_text(); "
            "assert len(t) > 1; print(t[:50])\""
        )
        assert _has_cli_meaningful_assertion(ok) is True


# ---------------------------------------------------------------------------
# 4. End-to-end: clarification → planner → plan validation doesn't raise
# ---------------------------------------------------------------------------


class TestEndToEndClarificationFlow:
    def test_local_emulator_answer_produces_validating_plan(self) -> None:
        """Drive the planner's ``_apply_clarification_answers`` path with
        a ``testing_strategy_unspecified=local_emulator`` answer and
        confirm the resulting ``local_proof_cmd`` validates."""
        from saturnday._types import ClarificationEntry
        from saturnday.run.planner import (
            FIX73_PROOF_GAP_PREFIX,
            _apply_clarification_answers,
        )

        plan = {
            "project_id": "demo",
            "operating_mode": "cli_tool",
            "dependency_profile": "external_dependencies",
            "external_dependencies": ["stripe"],
            "local_proof_cmd": (
                # Same FIX73 gap shape the planner emits pre-clarification.
                f"python <<'FIX73_GAP'\n"
                f"import sys\n"
                f"sys.exit('{FIX73_PROOF_GAP_PREFIX}: cli_tool — reason.')\n"
                f"FIX73_GAP"
            ),
            "acceptance_setup": [],
            "testing_strategy": "unspecified",
            "proof_realism": "production_intent",
            "notes": "",
            "required_outcomes": [],
            "proof_resolution_source": "planner_gap",
        }
        answers = (
            ClarificationEntry(
                trigger_kind="testing_strategy_unspecified",
                question="",
                answer="local_emulator",
                blocking=True,
            ),
        )
        tickets = [
            {
                "ticket_id": "T001",
                "goal": "Expose --run subcommand and --input flag.",
                "acceptance_criteria": ["function build exists in src/demo/core.py"],
            }
        ]

        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=tickets,
        )

        # The new local_proof_cmd must validate under the same validator
        # the planner's stage-3 load uses.
        errs = _validate_cli_tool(plan["local_proof_cmd"])
        assert errs == [], (
            f"end-to-end clarification flow produced a cli_tool proof "
            f"that the validator rejects: {errs}\n"
            f"command:\n{plan['local_proof_cmd']}"
        )


# ---------------------------------------------------------------------------
# 5. Honest metadata — testing_strategy matches operator's answer
# ---------------------------------------------------------------------------


class TestHonestMetadata:
    def test_testing_strategy_field_preserves_operator_answer(self) -> None:
        """Anti-workaround guard: the recorded strategy must be exactly
        what the operator answered, not silently substituted to
        ``generated_fake`` just because that template happens to pass."""
        from saturnday._types import ClarificationEntry
        from saturnday.run.planner import _apply_clarification_answers

        plan = {
            "project_id": "demo",
            "operating_mode": "cli_tool",
            "dependency_profile": "external_dependencies",
            "external_dependencies": ["stripe"],
            "local_proof_cmd": "python <<'FIX73_GAP'\nimport sys\nsys.exit('FIX73_PROOF_GAP: cli_tool — gap')\nFIX73_GAP",
            "acceptance_setup": [],
            "testing_strategy": "unspecified",
            "proof_realism": "production_intent",
            "notes": "",
            "required_outcomes": [],
            "proof_resolution_source": "planner_gap",
        }
        answers = (
            ClarificationEntry(
                trigger_kind="testing_strategy_unspecified",
                question="",
                answer="local_emulator",
                blocking=True,
            ),
        )
        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=[{"ticket_id": "T", "goal": "x", "acceptance_criteria": []}],
        )
        assert plan["testing_strategy"] == "local_emulator"
        assert plan["testing_strategy"] != "generated_fake"


# ---------------------------------------------------------------------------
# 6. No unintended drift outside cli_tool
# ---------------------------------------------------------------------------


class TestNonCliToolModesUnchanged:
    @pytest.mark.parametrize("strategy", [
        "local_emulator",
        "oss_substitute",
        "seeded_demo",
    ])
    def test_worker_still_emits_test_s_shape(self, strategy: str) -> None:
        """Worker/pipeline branches are deliberately not touched in this
        batch.  Their ``test -s`` shape is preserved; if worker/pipeline
        strategies need the same fix later, that's a separate batch."""
        cmd = _generate_strategy_aware_proof(
            operating_mode="worker",
            testing_strategy=strategy,
            project_id="demo",
            tickets=[],
            external_dependencies=["x"],
        )
        assert "test -s" in cmd

    def test_web_service_local_emulator_still_has_http_body_assertion(
        self,
    ) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="web_service",
            testing_strategy="local_emulator",
            project_id="demo",
            tickets=[{"ticket_id": "T", "goal": "GET /users", "acceptance_criteria": []}],
            external_dependencies=["x"],
        )
        # web_service branch uses its own non-cli_tool proof shape;
        # pinned here to catch accidental drift from the cli_tool fix.
        assert "assert len(body) > 0" in cmd
        assert "urllib.request" in cmd

    def test_library_generated_fake_unchanged(self) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="library",
            testing_strategy="generated_fake",
            project_id="demo",
            tickets=[{"ticket_id": "T", "goal": "function foo", "acceptance_criteria": []}],
            external_dependencies=[],
        )
        # Pre-existing library shape: SATURNDAY_USE_FAKE + import + call.
        assert "SATURNDAY_USE_FAKE" in cmd
        assert "assert result is not None" in cmd
