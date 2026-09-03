"""Phase 1 — proof_resolution_status / testing_strategy data model foundation.

New persisted fields:
- ProjectPlan.testing_strategy (enum: 7 values, default "unspecified")
- RunResult.proof_resolution_status (enum: 8 values, default "not_attempted")
- RunResult.proof_resolution_source (enum: 7 values, default "none")
- RunResult.proof_resolution_narrative (str)

New validator rules:
- testing_strategy in {live_credentials_gated, local_emulator, oss_substitute,
  generated_fake, recorded_fixture} requires non-self_contained profile.
- testing_strategy == seeded_demo requires proof_realism == seeded_demo.

Evidence surfacing:
- run-summary.json carries proof_resolution_status/source/narrative.
- run-summary.json carries testing_strategy when declared.
"""

from __future__ import annotations

import json
from pathlib import Path

from saturnday._types import (
    PROOF_RESOLUTION_SOURCES,
    PROOF_RESOLUTION_STATUSES,
    TESTING_STRATEGIES,
    ProjectPlan,
    RunResult,
)
from saturnday.plan_parser import _parse_testing_strategy, validate_plan


# ---------------------------------------------------------------------------
# Enum surfaces
# ---------------------------------------------------------------------------


def test_testing_strategies_enum_shape() -> None:
    assert "unspecified" in TESTING_STRATEGIES
    assert "live_credentials_gated" in TESTING_STRATEGIES
    assert "local_emulator" in TESTING_STRATEGIES
    assert "oss_substitute" in TESTING_STRATEGIES
    assert "generated_fake" in TESTING_STRATEGIES
    assert "seeded_demo" in TESTING_STRATEGIES
    assert "recorded_fixture" in TESTING_STRATEGIES
    assert len(TESTING_STRATEGIES) == 7


def test_proof_resolution_statuses_enum_shape() -> None:
    for v in ("not_attempted", "unresolved_gap", "resolved_from_planning",
              "resolved_coder_plan_time", "resolved_coder_post_exec",
              "resolved_operator", "passed", "failed"):
        assert v in PROOF_RESOLUTION_STATUSES
    assert len(PROOF_RESOLUTION_STATUSES) == 8


def test_proof_resolution_sources_enum_shape() -> None:
    for v in ("none", "planner_heuristic", "planner_gap", "coder_plan_time",
              "coder_post_execution", "operator_supplied", "operator_edited"):
        assert v in PROOF_RESOLUTION_SOURCES
    assert len(PROOF_RESOLUTION_SOURCES) == 7


# ---------------------------------------------------------------------------
# Defaults — must preserve legacy shape
# ---------------------------------------------------------------------------


def test_project_plan_testing_strategy_defaults_unspecified() -> None:
    p = ProjectPlan(version=1, project_id="x")
    assert p.testing_strategy == "unspecified"


def test_run_result_proof_resolution_defaults() -> None:
    r = RunResult(project_id="x")
    assert r.proof_resolution_status == "not_attempted"
    assert r.proof_resolution_source == "none"
    assert r.proof_resolution_narrative == ""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parse_testing_strategy_absent_default() -> None:
    assert _parse_testing_strategy({}) == "unspecified"


def test_parse_testing_strategy_valid() -> None:
    assert _parse_testing_strategy(
        {"testing_strategy": "local_emulator"}
    ) == "local_emulator"


def test_parse_testing_strategy_unknown_falls_back() -> None:
    assert _parse_testing_strategy({"testing_strategy": "banana"}) == "unspecified"


# ---------------------------------------------------------------------------
# Validator — testing_strategy coherence
# ---------------------------------------------------------------------------


def _base_plan(**overrides) -> dict:
    base = {
        "version": 1,
        "project_id": "p",
        "operating_mode": "library",
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "local_proof_cmd": 'python -c "from x import f; assert f(1) == 1"',
        "tickets": [{
            "ticket_id": "T001", "goal": "do",
            "acceptance_criteria": ["function f exists in src/m.py"],
        }],
    }
    base.update(overrides)
    return base


def test_live_strategy_requires_non_self_contained() -> None:
    for strat in ("live_credentials_gated", "local_emulator",
                  "oss_substitute", "generated_fake", "recorded_fixture"):
        raw = _base_plan(
            dependency_profile="self_contained",
            testing_strategy=strat,
        )
        errs = validate_plan(raw)
        assert any(
            f"testing_strategy={strat!r}" in e and "self_contained" in e
            for e in errs
        ), f"strategy {strat} must be rejected for self_contained: {errs}"


def test_live_strategy_accepted_for_external_dependencies() -> None:
    raw = _base_plan(
        dependency_profile="external_dependencies",
        external_dependencies=["openai_api"],
        testing_strategy="local_emulator",
        operator_disclaimer="External: openai_api, local_emulator fake.",
    )
    errs = validate_plan(raw)
    assert errs == [], errs


def test_seeded_demo_strategy_requires_seeded_demo_realism() -> None:
    raw = _base_plan(
        testing_strategy="seeded_demo",
        proof_realism="production_intent",
    )
    errs = validate_plan(raw)
    assert any(
        "testing_strategy=seeded_demo requires proof_realism=seeded_demo" in e
        for e in errs
    ), errs


def test_seeded_demo_strategy_accepted_with_seeded_demo_realism() -> None:
    raw = _base_plan(
        proof_realism="seeded_demo",
        testing_strategy="seeded_demo",
        demo_seed_source="seeds/demo.json",
        operator_disclaimer="Demo from seeds/demo.json.",
    )
    errs = validate_plan(raw)
    assert errs == [], errs


def test_unknown_testing_strategy_rejected() -> None:
    raw = _base_plan(testing_strategy="banana")
    errs = validate_plan(raw)
    assert any("testing_strategy" in e and "banana" in e for e in errs), errs


def test_unspecified_strategy_is_always_allowed() -> None:
    """Default unspecified must not trigger any coherence errors — keeps
    existing plans (and the majority of simple plans) working."""
    raw = _base_plan(testing_strategy="unspecified")
    errs = validate_plan(raw)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# Round-trip through load_plan
# ---------------------------------------------------------------------------


def test_load_plan_carries_testing_strategy(tmp_path: Path) -> None:
    from saturnday.plan_parser import load_plan
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_base_plan(
        dependency_profile="external_dependencies",
        external_dependencies=["stripe"],
        testing_strategy="generated_fake",
        operator_disclaimer="External: stripe, generated_fake.",
    )))
    plan = load_plan(plan_path)
    assert plan.testing_strategy == "generated_fake"


def test_load_plan_legacy_defaults_testing_strategy(tmp_path: Path) -> None:
    """Legacy plans without testing_strategy parse cleanly as 'unspecified'."""
    from saturnday.plan_parser import load_plan
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "version": 1, "project_id": "legacy",
        "acceptance_cmd": "pytest tests/ -q",
        "tickets": [{"ticket_id": "T001", "goal": "do",
                      "acceptance_criteria": ["function f exists in src/m.py"]}],
    }))
    plan = load_plan(plan_path)
    assert plan.testing_strategy == "unspecified"
    assert plan.operating_mode == "legacy_unclassified"


# ---------------------------------------------------------------------------
# Evidence export — run-summary carries new fields
# ---------------------------------------------------------------------------


def test_run_summary_exports_proof_resolution_fields(tmp_path: Path) -> None:
    from saturnday.run.evidence import write_run_summary
    rr = RunResult(
        project_id="x",
        proof_resolution_status="resolved_coder_post_exec",
        proof_resolution_source="coder_post_execution",
        proof_resolution_narrative="Derived from src/app.py after tickets ran.",
    )
    out = tmp_path / "out"
    summary_path = write_run_summary(rr, out)
    data = json.loads(summary_path.read_text())
    assert data["proof_resolution_status"] == "resolved_coder_post_exec"
    assert data["proof_resolution_source"] == "coder_post_execution"
    assert data["proof_resolution_narrative"].startswith("Derived from")


def test_run_summary_exports_testing_strategy(tmp_path: Path) -> None:
    from saturnday.run.evidence import write_run_summary
    rr = RunResult(project_id="x")
    out = tmp_path / "out"
    summary_path = write_run_summary(rr, out, plan_data={
        "operating_mode": "web_service",
        "dependency_profile": "external_dependencies",
        "proof_realism": "production_intent",
        "operator_disclaimer": "External: stripe.",
        "testing_strategy": "generated_fake",
        "governing_goal": "g",
        "required_outcomes": [],
        "scoped_categories": [],
        "exclusions": [],
    })
    data = json.loads(summary_path.read_text())
    assert data.get("testing_strategy") == "generated_fake"


def test_run_summary_omits_unspecified_testing_strategy(tmp_path: Path) -> None:
    """When testing_strategy is 'unspecified', don't pollute the summary."""
    from saturnday.run.evidence import write_run_summary
    rr = RunResult(project_id="x")
    out = tmp_path / "out"
    summary_path = write_run_summary(rr, out, plan_data={
        "operating_mode": "library",
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "operator_disclaimer": "",
        "testing_strategy": "unspecified",
        "governing_goal": "g",
        "required_outcomes": [],
        "scoped_categories": [],
        "exclusions": [],
    })
    data = json.loads(summary_path.read_text())
    assert "testing_strategy" not in data
