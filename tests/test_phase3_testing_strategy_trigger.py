"""Phase 3 — testing_strategy_unspecified clarification trigger.

New (11th) trigger fires when the brief implies external services, asking
HOW the local proof should be constructed.  Six bounded answer categories:
live_credentials_gated / local_emulator / oss_substitute / generated_fake
/ seeded_demo / recorded_fixture.

Consumption in the planner:
- Sets plan.testing_strategy to the chosen value.
- Adds an acceptance_setup hint appropriate to the strategy (idempotent).
- seeded_demo strategy also sets proof_realism=seeded_demo +
  demo_seed_source + operator_disclaimer for validator coherence.
"""

from __future__ import annotations

from saturnday._types import ClarificationEntry
from saturnday.run.clarification import detect_ambiguity_triggers
from saturnday.run.planner import _apply_clarification_answers


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------


def _kinds(triggers) -> set[str]:
    return {t.kind for t in triggers}


def test_external_brief_fires_testing_strategy_trigger() -> None:
    triggers = detect_ambiguity_triggers(
        "build a notifier that posts to Slack"
    )
    assert "testing_strategy_unspecified" in _kinds(triggers)


def test_self_contained_brief_does_not_fire_testing_strategy() -> None:
    triggers = detect_ambiguity_triggers(
        "build a Python library that parses JSON files"
    )
    assert "testing_strategy_unspecified" not in _kinds(triggers)


def test_external_brief_with_strategy_language_suppresses_trigger() -> None:
    """If the brief already mentions a strategy (local emulator / fake /
    sandbox key / wiremock / etc.), don't re-ask."""
    triggers = detect_ambiguity_triggers(
        "build a Stripe integration using a generated fake for tests"
    )
    assert "testing_strategy_unspecified" not in _kinds(triggers)


def test_trigger_is_blocking() -> None:
    triggers = detect_ambiguity_triggers("build a Slack integration")
    t = next(x for x in triggers if x.kind == "testing_strategy_unspecified")
    assert t.blocking is True


def test_trigger_has_six_bounded_answers() -> None:
    triggers = detect_ambiguity_triggers("build an OpenAI chatbot")
    t = next(x for x in triggers if x.kind == "testing_strategy_unspecified")
    assert set(t.answers) == {
        "live_credentials_gated", "local_emulator", "oss_substitute",
        "generated_fake", "seeded_demo", "recorded_fixture",
    }


def test_trigger_coexists_with_external_dependency_unspecified() -> None:
    """Trigger 11 composes with trigger 3 — together they capture
    integration intent AND local proof shape."""
    triggers = detect_ambiguity_triggers("build a GitHub API integration")
    kinds = _kinds(triggers)
    assert "external_dependency_unspecified" in kinds
    assert "testing_strategy_unspecified" in kinds


# ---------------------------------------------------------------------------
# Planner consumption
# ---------------------------------------------------------------------------


def _empty_plan(**overrides) -> dict:
    base = {
        "operating_mode": "web_service",
        "dependency_profile": "external_dependencies",
        "proof_realism": "production_intent",
        "external_dependencies": ["openai_api"],
        "local_proof_cmd": "",
        "live_proof_cmd": "",
        "demo_seed_source": "",
        "operator_disclaimer": "External: openai_api.",
        "acceptance_setup": [],
        "required_outcomes": [],
        "notes": "",
        "testing_strategy": "unspecified",
    }
    base.update(overrides)
    return base


def _entry(kind: str, answer: str) -> ClarificationEntry:
    return ClarificationEntry(
        trigger_kind=kind, question="Q", answer=answer, blocking=True,
    )


def test_local_emulator_answer_sets_strategy_and_setup() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "local_emulator"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "local_emulator"
    assert any("LocalStack" in s or "MinIO" in s or "emulator" in s.lower()
               for s in plan["acceptance_setup"])


def test_generated_fake_answer() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "generated_fake"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "generated_fake"
    assert any("Fake" in s for s in plan["acceptance_setup"])


def test_live_credentials_gated_answer() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "live_credentials_gated"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "live_credentials_gated"
    assert any("LIVE_PROOF" in s or "sandbox" in s
               for s in plan["acceptance_setup"])


def test_seeded_demo_strategy_sets_realism_and_disclaimer() -> None:
    """seeded_demo strategy must drive proof_realism + demo_seed_source +
    operator_disclaimer for validator coherence."""
    plan = _empty_plan(proof_realism="production_intent",
                       demo_seed_source="",
                       operator_disclaimer="")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "seeded_demo"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "seeded_demo"
    assert plan["proof_realism"] == "seeded_demo"
    assert plan["demo_seed_source"] == "seeds/demo.json"
    assert "Seeded demo" in plan["operator_disclaimer"]


def test_oss_substitute_answer() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "oss_substitute"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "oss_substitute"
    assert any("OSS substitute" in s for s in plan["acceptance_setup"])


def test_recorded_fixture_answer() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "recorded_fixture"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "recorded_fixture"
    assert any("WireMock" in s or "VCR" in s or "traces" in s
               for s in plan["acceptance_setup"])


def test_invalid_strategy_answer_ignored() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified", "banana"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "unspecified"
    assert plan["acceptance_setup"] == []


def test_empty_strategy_answer_ignored() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified", ""),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["testing_strategy"] == "unspecified"


# ---------------------------------------------------------------------------
# Compose: trigger 3 (external) + trigger 11 (strategy)
# ---------------------------------------------------------------------------


def test_compose_live_integration_plus_local_emulator() -> None:
    plan = _empty_plan(
        dependency_profile="self_contained",
        external_dependencies=[],
        operator_disclaimer="",
    )
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(
            _entry("external_dependency_unspecified", "live_integration"),
            _entry("testing_strategy_unspecified", "local_emulator"),
        ),
        project_id="p", enriched_tickets=[],
    )
    # trigger 3 sets dependency_profile + disclaimer
    assert plan["dependency_profile"] == "external_dependencies"
    assert plan["external_dependencies"]
    assert plan["operator_disclaimer"]
    # trigger 11 sets strategy + setup hint
    assert plan["testing_strategy"] == "local_emulator"
    assert any("LocalStack" in s or "emulator" in s.lower()
               for s in plan["acceptance_setup"])


def test_compose_local_fake_plus_generated_fake() -> None:
    """Operator says 'local fake' (integration intent) + 'generated_fake'
    (how to build it) — both layer cleanly."""
    plan = _empty_plan(dependency_profile="self_contained",
                       external_dependencies=[])
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(
            _entry("external_dependency_unspecified", "local_fake"),
            _entry("testing_strategy_unspecified", "generated_fake"),
        ),
        project_id="p", enriched_tickets=[],
    )
    assert plan["dependency_profile"] == "external_dependencies"
    assert plan["testing_strategy"] == "generated_fake"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_reapplying_same_strategy_does_not_duplicate_setup() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "local_emulator"),),
        project_id="p", enriched_tickets=[],
    )
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("testing_strategy_unspecified",
                                      "local_emulator"),),
        project_id="p", enriched_tickets=[],
    )
    hints = [s for s in plan["acceptance_setup"] if "emulator" in s.lower()]
    assert len(hints) == 1
