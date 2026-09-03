"""Proof-automation batch — γ + δ + non-interactive refusal tests.

γ: operator-answered clarifications must actually drive
   ``local_proof_cmd`` regeneration, not merely decorate notes.
δ: after Phase 4 / Phase 5 proof derivation fails, a small deterministic
   question registry closes the gap (CLI pre-answer or TTY prompt).
Non-interactive refusal: unresolved proof + no usable answer must
   surface an explicit operator-visible refusal narrative, not a silent
   DoD NOT-MET downgrade.
"""
from __future__ import annotations

import json

from saturnday._types import ClarificationEntry, PROOF_RESOLUTION_SOURCES
from saturnday.run.planner import (
    FIX73_PROOF_GAP_PREFIX,
    _apply_clarification_answers,
    _generate_strategy_aware_proof,
)
from saturnday.run.proof_questions import (
    REGISTRY,
    ProofQuestionSpec,
    format_refusal_message,
    parse_proof_answer_flags,
    resolve_via_questions,
    specs_for_mode,
)


# ---------------------------------------------------------------------------
# γ — clarification-driven proof generation
# ---------------------------------------------------------------------------


def _gap_cmd() -> str:
    """Minimal FIX73 gap marker as the planner would emit."""
    return (
        f"python <<'FIX73_GAP'\n"
        f"import sys\n"
        f"sys.exit('{FIX73_PROOF_GAP_PREFIX}: web_service — reason.')\n"
        f"FIX73_GAP"
    )


def _plan_with_gap(
    operating_mode: str = "web_service",
    dependency_profile: str = "external_dependencies",
    external_dependencies: tuple[str, ...] = ("stripe",),
) -> dict:
    return {
        "project_id": "demo",
        "operating_mode": operating_mode,
        "dependency_profile": dependency_profile,
        "external_dependencies": list(external_dependencies),
        "local_proof_cmd": _gap_cmd(),
        "acceptance_setup": [],
        "testing_strategy": "unspecified",
        "proof_realism": "production_intent",
        "notes": "",
        "required_outcomes": [],
        "proof_resolution_source": "planner_gap",
    }


def _entry(kind: str, answer: str) -> ClarificationEntry:
    return ClarificationEntry(
        trigger_kind=kind,
        question="",
        answer=answer,
        blocking=True,
    )


def _enriched_ticket_with_endpoint() -> list[dict]:
    return [
        {
            "ticket_id": "T001",
            "goal": "Expose POST /users and GET /users endpoint returning JSON.",
            "acceptance_criteria": ["function build exists in src/demo/app.py"],
        }
    ]


class TestGammaGeneratesProofFromStrategyAnswer:
    """γ direct tests on ``_generate_strategy_aware_proof``."""

    def test_generated_fake_webservice_produces_proof_with_fake_env(self) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="web_service",
            testing_strategy="generated_fake",
            project_id="demo",
            tickets=_enriched_ticket_with_endpoint(),
            external_dependencies=["stripe"],
        )
        assert cmd
        assert "SATURNDAY_USE_FAKE" in cmd
        # Must use the hint-extracted endpoint, not just "/"
        assert "/users" in cmd

    def test_local_emulator_webservice_probes_emulator_url(self) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="web_service",
            testing_strategy="local_emulator",
            project_id="demo",
            tickets=_enriched_ticket_with_endpoint(),
            external_dependencies=["s3"],
        )
        assert cmd
        assert "SATURNDAY_EMULATOR_URL" in cmd
        assert "curl" in cmd  # probes the emulator

    def test_recorded_fixture_webservice_checks_fixtures_dir(self) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="web_service",
            testing_strategy="recorded_fixture",
            project_id="demo",
            tickets=_enriched_ticket_with_endpoint(),
            external_dependencies=["anthropic_api"],
        )
        assert cmd
        assert "SATURNDAY_FIXTURES_DIR" in cmd
        assert "SATURNDAY_REPLAY_FIXTURES" in cmd

    def test_live_credentials_gated_returns_honest_stub(self) -> None:
        cmd = _generate_strategy_aware_proof(
            operating_mode="cli_tool",
            testing_strategy="live_credentials_gated",
            project_id="demo",
            tickets=[],
            external_dependencies=["anthropic_api"],
        )
        assert cmd
        assert "LIVE_PROOF=1" in cmd
        assert "NOT evidence" in cmd  # explicit honesty text

    def test_every_generated_command_differs_per_strategy(self) -> None:
        seen = {}
        for strat in (
            "generated_fake", "local_emulator", "oss_substitute",
            "recorded_fixture", "live_credentials_gated",
        ):
            cmd = _generate_strategy_aware_proof(
                operating_mode="web_service",
                testing_strategy=strat,
                project_id="demo",
                tickets=_enriched_ticket_with_endpoint(),
                external_dependencies=["x"],
            )
            assert cmd, f"strategy {strat} must emit a non-empty command"
            seen[strat] = cmd
        # No two strategies should produce identical commands.
        assert len(set(seen.values())) == len(seen)

    def test_unknown_combo_returns_empty_preserving_gap(self) -> None:
        # library + local_emulator has no deterministic template.
        cmd = _generate_strategy_aware_proof(
            operating_mode="library",
            testing_strategy="local_emulator",
            project_id="demo",
            tickets=[],
            external_dependencies=[],
        )
        assert cmd == ""


class TestGammaFlowFromClarificationToProof:
    """γ integration: _apply_clarification_answers must rewrite
    local_proof_cmd when the operator answered testing_strategy."""

    def test_local_emulator_answer_rewrites_proof_and_records_source(self) -> None:
        plan = _plan_with_gap(operating_mode="web_service")
        answers = (_entry("testing_strategy_unspecified", "local_emulator"),)
        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=_enriched_ticket_with_endpoint(),
        )
        # Proof was regenerated — not left as FIX73 gap.
        assert FIX73_PROOF_GAP_PREFIX not in plan["local_proof_cmd"]
        assert "SATURNDAY_EMULATOR_URL" in plan["local_proof_cmd"]
        # Origin honestly recorded as clarification-driven.
        assert plan["proof_resolution_source"] == "clarification_strategy"
        # testing_strategy persisted alongside.
        assert plan["testing_strategy"] == "local_emulator"

    def test_generated_fake_answer_rewrites_proof(self) -> None:
        plan = _plan_with_gap(operating_mode="web_service")
        answers = (_entry("testing_strategy_unspecified", "generated_fake"),)
        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=_enriched_ticket_with_endpoint(),
        )
        assert "SATURNDAY_USE_FAKE" in plan["local_proof_cmd"]
        assert plan["proof_resolution_source"] == "clarification_strategy"

    def test_external_dependency_local_fake_bridges_to_generated_fake_proof(
        self,
    ) -> None:
        plan = _plan_with_gap(operating_mode="web_service")
        # Operator answered integration-intent (trigger 3) but NOT
        # testing_strategy (trigger 11).  The γ bridge translates
        # "local_fake" into generated_fake so proof still rewrites.
        answers = (_entry("external_dependency_unspecified", "local_fake"),)
        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=_enriched_ticket_with_endpoint(),
        )
        assert "SATURNDAY_USE_FAKE" in plan["local_proof_cmd"]
        assert plan["testing_strategy"] == "generated_fake"
        assert plan["proof_resolution_source"] == "clarification_strategy"

    def test_notes_only_drift_is_eliminated_for_implemented_strategies(
        self,
    ) -> None:
        """Before the γ batch, answering testing_strategy_unspecified only
        appended an ``echo 'MANUAL: ...'`` hint to acceptance_setup; the
        proof command was untouched.  This test proves that is no longer
        the case for the implemented answer classes."""
        for strat in ("generated_fake", "local_emulator", "recorded_fixture",
                      "oss_substitute", "live_credentials_gated"):
            plan = _plan_with_gap(operating_mode="web_service")
            pre_proof = plan["local_proof_cmd"]
            answers = (_entry("testing_strategy_unspecified", strat),)
            _apply_clarification_answers(
                plan=plan,
                clarification_record=answers,
                project_id="demo",
                enriched_tickets=_enriched_ticket_with_endpoint(),
            )
            assert plan["local_proof_cmd"] != pre_proof, (
                f"strategy {strat} did not change local_proof_cmd — γ bug"
            )

    def test_seeded_demo_still_flips_proof_realism_and_seed_source(self) -> None:
        plan = _plan_with_gap(operating_mode="web_service")
        answers = (_entry("testing_strategy_unspecified", "seeded_demo"),)
        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=_enriched_ticket_with_endpoint(),
        )
        assert plan["proof_realism"] == "seeded_demo"
        assert plan["demo_seed_source"]
        assert plan["operator_disclaimer"]

    def test_unknown_strategy_answer_is_ignored(self) -> None:
        plan = _plan_with_gap()
        pre_proof = plan["local_proof_cmd"]
        answers = (_entry("testing_strategy_unspecified", "invent_a_strategy"),)
        _apply_clarification_answers(
            plan=plan,
            clarification_record=answers,
            project_id="demo",
            enriched_tickets=_enriched_ticket_with_endpoint(),
        )
        # Unrecognised answer must not mutate the proof — honesty rule.
        assert plan["local_proof_cmd"] == pre_proof


# ---------------------------------------------------------------------------
# δ — targeted-question registry
# ---------------------------------------------------------------------------


class TestDeltaRegistryShape:
    def test_registry_is_mode_keyed(self) -> None:
        kinds = {s.kind for s in REGISTRY}
        assert {
            "cli_entrypoint", "ws_endpoint", "library_call",
            "worker_cycle", "frontend_path", "custom_proof",
        } <= kinds

    def test_specs_for_mode_puts_mode_specific_first(self) -> None:
        out = specs_for_mode("cli_tool")
        assert out[0].kind == "cli_entrypoint"
        # Mode-agnostic escape hatch must appear but last.
        assert out[-1].kind == "custom_proof"

    def test_specs_for_mode_unknown_mode_only_gets_any(self) -> None:
        out = specs_for_mode("pipeline")  # no pipeline-specific spec
        kinds = [s.kind for s in out]
        assert kinds == ["custom_proof"]


class TestDeltaParseFlags:
    def test_valid_kv_pairs_parsed(self) -> None:
        out = parse_proof_answer_flags([
            "cli_entrypoint=python -m foo run",
            "library_call=assert mod.x() == 1",
        ])
        assert out == {
            "cli_entrypoint": "python -m foo run",
            "library_call": "assert mod.x() == 1",
        }

    def test_malformed_entries_dropped(self) -> None:
        out = parse_proof_answer_flags([
            "missing_equals",
            "=no_kind",
            "kind=",      # empty answer also dropped
            "good=ok",
        ])
        assert out == {"good": "ok"}

    def test_none_input_returns_empty(self) -> None:
        assert parse_proof_answer_flags(None) == {}


class TestDeltaResolveViaRegistry:
    def test_cli_preanswer_resolves_noninteractive(self) -> None:
        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, narrative = resolve_via_questions(
            plan,
            cli_answers={"cli_entrypoint": "python -m demo run --input /tmp/a.json"},
            interactive=False,
        )
        assert status == "resolved_operator"
        assert source == "operator_targeted_answer"
        assert FIX73_PROOF_GAP_PREFIX not in plan["local_proof_cmd"]
        assert "python -m demo run" in plan["local_proof_cmd"]
        assert plan["proof_resolution_source"] == "operator_targeted_answer"

    def test_cli_preanswer_resolves_ws_endpoint(self) -> None:
        plan = {
            "operating_mode": "web_service",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, narrative = resolve_via_questions(
            plan,
            cli_answers={"ws_endpoint": "GET /users"},
            interactive=False,
        )
        assert status == "resolved_operator"
        assert "http://127.0.0.1:8000/users" in plan["local_proof_cmd"]

    def test_cli_preanswer_resolves_library_call(self) -> None:
        plan = {
            "operating_mode": "library",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, narrative = resolve_via_questions(
            plan,
            cli_answers={
                "library_call": "from mylib import sq; assert sq(3) == 9",
            },
            interactive=False,
        )
        assert status == "resolved_operator"
        assert "sq(3) == 9" in plan["local_proof_cmd"]

    def test_custom_proof_escape_hatch_works_for_any_mode(self) -> None:
        plan = {
            "operating_mode": "pipeline",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, _ = resolve_via_questions(
            plan,
            cli_answers={"custom_proof": "bash scripts/smoke.sh"},
            interactive=False,
        )
        assert status == "resolved_operator"
        assert plan["local_proof_cmd"] == "bash scripts/smoke.sh"

    def test_empty_cli_answer_is_rejected_and_gap_remains(self) -> None:
        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, _ = resolve_via_questions(
            plan,
            # Empty value is stripped by parse_proof_answer_flags normally,
            # but resolve_via_questions must also defend directly.
            cli_answers={"cli_entrypoint": ""},
            interactive=False,
        )
        assert status == "unresolved_gap"
        assert FIX73_PROOF_GAP_PREFIX in plan["local_proof_cmd"]
        assert plan["proof_resolution_source"] == "planner_gap"

    def test_tautological_cli_entrypoint_answer_rejected(self) -> None:
        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        # "--help" alone is NOT acceptable proof.
        status, _, _ = resolve_via_questions(
            plan,
            cli_answers={"cli_entrypoint": "--help"},
            interactive=False,
        )
        assert status == "unresolved_gap"

    def test_malformed_library_call_without_assert_or_paren_rejected(self) -> None:
        plan = {
            "operating_mode": "library",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, _, _ = resolve_via_questions(
            plan,
            cli_answers={"library_call": "just prose without invocation"},
            interactive=False,
        )
        assert status == "unresolved_gap"


class TestDeltaInteractive:
    def test_tty_prompt_asks_mode_specific_question_and_resolves(self) -> None:
        prompts_seen: list[ProofQuestionSpec] = []

        def fake_prompt(spec: ProofQuestionSpec) -> str:
            prompts_seen.append(spec)
            return "python -m demo run --fixture /tmp/x.json"

        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, _ = resolve_via_questions(
            plan,
            cli_answers={},
            interactive=True,
            prompt_fn=fake_prompt,
        )
        assert status == "resolved_operator"
        assert prompts_seen and prompts_seen[0].kind == "cli_entrypoint"
        assert "python -m demo run" in plan["local_proof_cmd"]

    def test_tty_empty_answer_leaves_gap_cleanly(self) -> None:
        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, _, narrative = resolve_via_questions(
            plan,
            cli_answers={},
            interactive=True,
            prompt_fn=lambda s: "",
        )
        assert status == "unresolved_gap"
        assert FIX73_PROOF_GAP_PREFIX in plan["local_proof_cmd"]
        # Gap narrative is operator-actionable.
        assert narrative

    def test_tty_only_asks_one_question_not_the_whole_chain(self) -> None:
        call_count = {"n": 0}

        def counting_prompt(spec: ProofQuestionSpec) -> str:
            call_count["n"] += 1
            return ""  # unusable answer

        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, _, _ = resolve_via_questions(
            plan,
            cli_answers={},
            interactive=True,
            prompt_fn=counting_prompt,
        )
        assert status == "unresolved_gap"
        # Empty answer → stop, do not chain into custom_proof escape
        # hatch silently.  Operator must re-run.
        assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Non-interactive honesty refusal
# ---------------------------------------------------------------------------


class TestNonInteractiveRefusal:
    def test_no_answer_and_noninteractive_returns_unresolved_with_narrative(
        self,
    ) -> None:
        plan = {
            "operating_mode": "web_service",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, source, narrative = resolve_via_questions(
            plan, cli_answers=None, interactive=False,
        )
        assert status == "unresolved_gap"
        assert source == "planner_gap"
        # Narrative lists the expected CLI flags (actionable).
        assert "--proof-answer ws_endpoint=" in narrative
        assert "--proof-answer custom_proof=" in narrative

    def test_refusal_message_names_every_applicable_spec(self) -> None:
        msg = format_refusal_message("web_service")
        assert "ws_endpoint" in msg
        assert "custom_proof" in msg
        # Must tell the operator what to do.
        assert "rerun in an interactive terminal" in msg
        assert "proof_resolution_source" in msg  # mentions hand-auth path

    def test_refusal_message_mentions_supplied_but_rejected_answers(self) -> None:
        msg = format_refusal_message(
            "cli_tool",
            cli_answers={"cli_entrypoint": "--help"},  # was supplied but rejected
        )
        assert "--proof-answer supplied: cli_entrypoint" in msg
        assert "none were usable" in msg

    def test_usable_cli_preanswer_is_preferred_over_refusal(self) -> None:
        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": _gap_cmd(),
            "proof_resolution_source": "planner_gap",
        }
        status, _, _ = resolve_via_questions(
            plan,
            cli_answers={"cli_entrypoint": "python -m demo run --input /tmp/a"},
            interactive=False,
        )
        assert status == "resolved_operator"
        assert "python -m demo run" in plan["local_proof_cmd"]


# ---------------------------------------------------------------------------
# Schema extension — new proof_resolution_source values accepted
# ---------------------------------------------------------------------------


class TestProofResolutionSourceEnumExtension:
    def test_clarification_strategy_is_a_valid_source(self) -> None:
        assert "clarification_strategy" in PROOF_RESOLUTION_SOURCES

    def test_operator_targeted_answer_is_a_valid_source(self) -> None:
        assert "operator_targeted_answer" in PROOF_RESOLUTION_SOURCES

    def test_plan_schema_accepts_the_new_sources(self) -> None:
        from saturnday.shared.plan_schema import PLAN_SCHEMA
        enum = PLAN_SCHEMA["properties"]["proof_resolution_source"]["enum"]
        assert "clarification_strategy" in enum
        assert "operator_targeted_answer" in enum

    def test_plan_parser_round_trips_new_sources(self, tmp_path) -> None:
        """A plan file declaring ``clarification_strategy`` must load
        without warnings and preserve the source."""
        from saturnday.plan_parser import load_plan
        plan_json = {
            "version": 1,
            "project_id": "demo",
            # library mode has the loosest meaningfulness validator for
            # a round-trip — we only care that the new enum value passes
            # provenance validation.
            "operating_mode": "library",
            "dependency_profile": "self_contained",
            "proof_realism": "production_intent",
            "local_proof_cmd": (
                "python -c \"from demo.core import build; "
                "assert build() == 'ok'\""
            ),
            "proof_resolution_source": "clarification_strategy",
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Sample ticket for round-trip test.",
                    "acceptance_criteria": ["function build exists in src/demo/core.py"],
                }
            ],
        }
        p = tmp_path / "plan.json"
        p.write_text(json.dumps(plan_json), encoding="utf-8")
        loaded = load_plan(p)
        assert loaded.proof_resolution_source == "clarification_strategy"


# ---------------------------------------------------------------------------
# Existing resolved-proof paths unchanged
# ---------------------------------------------------------------------------


class TestResolvedProofPathsUnchanged:
    def test_no_answer_required_when_proof_already_concrete(self) -> None:
        # If a caller invokes resolve_via_questions with a plan whose
        # local_proof_cmd is already concrete (no gap), the registry is
        # a no-op — there's nothing to close.  We simulate that by
        # passing a non-gap command and showing the resolver does not
        # rewrite it.
        plan = {
            "operating_mode": "cli_tool",
            "project_id": "demo",
            "local_proof_cmd": "python -m demo already-concrete",
            "proof_resolution_source": "operator_supplied",
        }
        # resolve_via_questions always runs regardless of whether the
        # proof is a gap — that decision lives in the caller
        # (ticket_runner).  With no usable CLI answer and no interactive
        # prompt, it returns unresolved but does NOT mutate the plan.
        pre = plan["local_proof_cmd"]
        status, _, _ = resolve_via_questions(
            plan, cli_answers=None, interactive=False,
        )
        assert status == "unresolved_gap"
        assert plan["local_proof_cmd"] == pre
        # Caller is responsible for gating on proof_is_gap() before
        # invoking resolve_via_questions in production — the runner
        # does this (see ticket_runner.py).
