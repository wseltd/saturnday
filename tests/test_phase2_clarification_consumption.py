"""Phase 2 — full clarification answer consumption.

Prior state: only ``mode_ambiguous`` answer was consumed by the planner;
the other 9 trigger answers were persisted to ``clarification_record`` and
then ignored.

After Phase 2: every trigger answer drives specific plan-field updates via
``_apply_clarification_answers``.  Unknown / empty answers leave the plan
untouched.  The mapping is documented in the helper's docstring.
"""

from __future__ import annotations

from saturnday._types import ClarificationEntry
from saturnday.run.planner import _apply_clarification_answers


def _entry(kind: str, answer: str, blocking: bool = True) -> ClarificationEntry:
    return ClarificationEntry(
        trigger_kind=kind, question="Q", answer=answer, blocking=blocking,
    )


def _empty_plan(**overrides) -> dict:
    base = {
        "operating_mode": "library",
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "external_dependencies": [],
        "local_proof_cmd": "",
        "live_proof_cmd": "",
        "demo_seed_source": "",
        "operator_disclaimer": "",
        "acceptance_setup": [],
        "required_outcomes": [],
        "notes": "",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# No record → no change
# ---------------------------------------------------------------------------


def test_no_clarifications_leaves_plan_unchanged() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(plan=plan, clarification_record=(),
                                  project_id="p", enriched_tickets=[])
    assert plan == _empty_plan()


# ---------------------------------------------------------------------------
# mode_ambiguous — still works, now routed through the shared helper
# ---------------------------------------------------------------------------


def test_mode_ambiguous_overrides_operating_mode() -> None:
    plan = _empty_plan(operating_mode="library")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("mode_ambiguous", "cli_tool"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["operating_mode"] == "cli_tool"
    assert "FIX73_PROOF_GAP" in plan["local_proof_cmd"]


def test_mode_ambiguous_invalid_answer_ignored() -> None:
    plan = _empty_plan(operating_mode="library")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("mode_ambiguous", "banana"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["operating_mode"] == "library"


# ---------------------------------------------------------------------------
# demo_vs_production
# ---------------------------------------------------------------------------


def test_demo_answer_sets_proof_realism_and_disclaimer() -> None:
    plan = _empty_plan(proof_realism="production_intent")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("demo_vs_production", "demo"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["proof_realism"] == "seeded_demo"
    assert plan["demo_seed_source"] == "seeds/demo.json"
    assert "Demo mode" in plan["operator_disclaimer"]


def test_production_intent_answer_sets_realism() -> None:
    plan = _empty_plan(proof_realism="seeded_demo", demo_seed_source="x",
                       operator_disclaimer="existing")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("demo_vs_production", "production_intent"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["proof_realism"] == "production_intent"
    # demo_seed_source and disclaimer left alone (validator separately catches
    # coherence violations — this phase only implements the mapping).
    assert plan["demo_seed_source"] == "x"


# ---------------------------------------------------------------------------
# external_dependency_unspecified
# ---------------------------------------------------------------------------


def test_live_integration_sets_external_dependencies_profile() -> None:
    plan = _empty_plan(dependency_profile="self_contained",
                       external_dependencies=[])
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("external_dependency_unspecified",
                                      "live_integration"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["dependency_profile"] == "external_dependencies"
    assert plan["external_dependencies"]  # non-empty
    assert "External dependencies" in plan["operator_disclaimer"]


def test_local_fake_sets_external_dependencies_profile() -> None:
    plan = _empty_plan(dependency_profile="self_contained")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("external_dependency_unspecified",
                                      "local_fake"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["dependency_profile"] == "external_dependencies"


def test_mock_only_test_does_not_change_profile() -> None:
    plan = _empty_plan(dependency_profile="self_contained")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("external_dependency_unspecified",
                                      "mock_only_test"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["dependency_profile"] == "self_contained"


def test_existing_external_deps_preserved() -> None:
    plan = _empty_plan(
        dependency_profile="self_contained",
        external_dependencies=["openai_api", "stripe"],
    )
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("external_dependency_unspecified",
                                      "live_integration"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["dependency_profile"] == "external_dependencies"
    assert plan["external_dependencies"] == ["openai_api", "stripe"]


# ---------------------------------------------------------------------------
# data_source_unspecified
# ---------------------------------------------------------------------------


def test_data_source_real_source_adds_setup_hint() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("data_source_unspecified", "real_source"),),
        project_id="p", enriched_tickets=[],
    )
    assert any("wire the real data source" in step
               for step in plan["acceptance_setup"])


def test_data_source_to_be_wired_later_does_not_add_hint() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("data_source_unspecified",
                                      "to_be_wired_later"),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["acceptance_setup"] == []


def test_data_source_hint_not_duplicated_on_reapply() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("data_source_unspecified", "test_fixture"),),
        project_id="p", enriched_tickets=[],
    )
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("data_source_unspecified", "test_fixture"),),
        project_id="p", enriched_tickets=[],
    )
    # Idempotent — don't append the same hint twice.
    fixture_hints = [s for s in plan["acceptance_setup"]
                     if "test fixtures" in s]
    assert len(fixture_hints) == 1


# ---------------------------------------------------------------------------
# persistence_unspecified
# ---------------------------------------------------------------------------


def test_persistence_sqlite_adds_setup_step() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("persistence_unspecified", "sqlite"),),
        project_id="p", enriched_tickets=[],
    )
    assert any("sqlite3" in step for step in plan["acceptance_setup"])


def test_persistence_postgres_adds_pg_check() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("persistence_unspecified", "postgres"),),
        project_id="p", enriched_tickets=[],
    )
    assert any("pg_isready" in step for step in plan["acceptance_setup"])


def test_persistence_in_memory_adds_note() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("persistence_unspecified", "in_memory_only"),),
        project_id="p", enriched_tickets=[],
    )
    assert any("in-memory" in step for step in plan["acceptance_setup"])


# ---------------------------------------------------------------------------
# ui_surface_unspecified / integration_scope_unclear / operator_identity_unclear
# ---------------------------------------------------------------------------


def test_ui_surface_clarification_annotates_notes() -> None:
    plan = _empty_plan(notes="base notes")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("ui_surface_unspecified", "web_page"),),
        project_id="p", enriched_tickets=[],
    )
    assert "web_page" in plan["notes"]


def test_integration_scope_annotates_notes() -> None:
    plan = _empty_plan(notes="base")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("integration_scope_unclear",
                                      "install_dependency"),),
        project_id="p", enriched_tickets=[],
    )
    assert "install dependency" in plan["notes"]


def test_operator_identity_annotates_notes() -> None:
    plan = _empty_plan(notes="")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("operator_identity_unclear", "ci"),),
        project_id="p", enriched_tickets=[],
    )
    assert "ci" in plan["notes"].lower()


# ---------------------------------------------------------------------------
# acceptance_target_unclear
# ---------------------------------------------------------------------------


def test_acceptance_target_adds_required_outcome() -> None:
    plan = _empty_plan(required_outcomes=["already here"])
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("acceptance_target_unclear",
                                      "smoke_test", blocking=False),),
        project_id="p", enriched_tickets=[],
    )
    assert "already here" in plan["required_outcomes"]
    assert any("smoke_test" in o for o in plan["required_outcomes"])


# ---------------------------------------------------------------------------
# setup_assumptions_unstated
# ---------------------------------------------------------------------------


def test_setup_manual_adds_acceptance_setup_hint() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("setup_assumptions_unstated",
                                      "manual_setup", blocking=False),),
        project_id="p", enriched_tickets=[],
    )
    assert any("MANUAL" in step for step in plan["acceptance_setup"])


def test_setup_assume_present_does_not_add_hint() -> None:
    plan = _empty_plan()
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(_entry("setup_assumptions_unstated",
                                      "assume_present", blocking=False),),
        project_id="p", enriched_tickets=[],
    )
    assert plan["acceptance_setup"] == []


# ---------------------------------------------------------------------------
# Multi-trigger composition
# ---------------------------------------------------------------------------


def test_multiple_clarifications_compose_correctly() -> None:
    """Real-world: operator answers several triggers at once; each mapping
    fires independently without overwriting others."""
    plan = _empty_plan(operating_mode="library")
    _apply_clarification_answers(
        plan=plan,
        clarification_record=(
            _entry("mode_ambiguous", "web_service"),
            _entry("external_dependency_unspecified", "live_integration"),
            _entry("persistence_unspecified", "postgres"),
            _entry("demo_vs_production", "production_intent"),
        ),
        project_id="p", enriched_tickets=[],
    )
    assert plan["operating_mode"] == "web_service"
    assert plan["dependency_profile"] == "external_dependencies"
    assert plan["proof_realism"] == "production_intent"
    assert any("pg_isready" in s for s in plan["acceptance_setup"])
    assert plan["operator_disclaimer"]  # populated


# ---------------------------------------------------------------------------
# End-to-end via generate_plan — clarification_record persists into plan.json
# ---------------------------------------------------------------------------


def test_generate_plan_consumes_all_clarifications(tmp_path) -> None:
    """Integration: generate_plan threads clarification_record through and
    persists the resulting plan with every answer applied."""
    import json
    from unittest.mock import patch
    from saturnday._types import CoderConfig
    from saturnday.run.planner import generate_plan

    def fake_call_coder(*a, **k):
        return json.dumps({"tickets": [
            {"ticket_id": "T001", "goal": "G",
             "acceptance_criteria": ["function f exists in src/m.py"]}
        ]})

    entries = (
        ClarificationEntry(trigger_kind="mode_ambiguous", question="?",
                           answer="cli_tool", blocking=True),
        ClarificationEntry(trigger_kind="demo_vs_production", question="?",
                           answer="demo", blocking=True),
        ClarificationEntry(trigger_kind="persistence_unspecified", question="?",
                           answer="sqlite", blocking=True),
    )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        plan_path = generate_plan(
            brief="build something",
            repo_path=str(tmp_path),
            coder_config=CoderConfig(backend="openai", api_key="k", model="m"),
            clarification_record=entries,
        )
    plan = json.loads(plan_path.read_text())

    assert plan["operating_mode"] == "cli_tool"
    assert plan["proof_realism"] == "seeded_demo"
    assert plan["demo_seed_source"]
    assert any("sqlite3" in s for s in plan["acceptance_setup"])
