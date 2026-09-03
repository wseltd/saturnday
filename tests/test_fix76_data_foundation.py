"""Fix 76 — persisted mode / dependency / proof-realism data foundation.

These tests pin the data-model and validator behaviour:

- Legacy plans (no operating_mode field) parse as ``legacy_unclassified`` and
  retain access to the ``acceptance_cmd`` field.  The compatibility path is
  explicit, NOT a silent default to ``library``.

- Declared-mode plans must use ``local_proof_cmd``.  Setting ``acceptance_cmd``
  on a declared-mode plan is rejected.  Setting both is rejected.

- ``external_dependencies`` is required iff ``dependency_profile ==
  external_dependencies``; ``live_proof_cmd`` is empty unless that profile is
  set; ``demo_seed_source`` and ``operator_disclaimer`` are required when
  ``proof_realism == seeded_demo``; ``operator_disclaimer`` is also required
  for ``storage_only`` and ``external_dependencies``.

- ``ClarificationEntry`` is parsed faithfully from the clarification_record list.

No behaviour change in the runner is exercised here — Fix 77 covers that.
"""

from __future__ import annotations

from saturnday._types import (
    DECLARED_OPERATING_MODES,
    DEPENDENCY_PROFILES,
    OPERATING_MODES,
    PROOF_REALISMS,
    ClarificationEntry,
    ProjectPlan,
)
from saturnday.plan_parser import (
    _parse_clarification_record,
    _parse_operating_mode,
    _validate_mode_and_proof_fields,
    validate_plan,
)


# ---------------------------------------------------------------------------
# Enum surfaces
# ---------------------------------------------------------------------------


def test_operating_modes_includes_legacy_and_seven_declared() -> None:
    assert "legacy_unclassified" in OPERATING_MODES
    assert len(OPERATING_MODES) == 8
    assert len(DECLARED_OPERATING_MODES) == 7
    assert "legacy_unclassified" not in DECLARED_OPERATING_MODES
    for m in (
        "library", "cli_tool", "web_service", "worker",
        "pipeline", "frontend", "storage_only",
    ):
        assert m in DECLARED_OPERATING_MODES


def test_dependency_profiles_three_values() -> None:
    assert tuple(DEPENDENCY_PROFILES) == (
        "self_contained", "local_dependencies", "external_dependencies",
    )


def test_proof_realisms_two_values() -> None:
    assert tuple(PROOF_REALISMS) == ("production_intent", "seeded_demo")


def test_demo_is_not_an_operating_mode() -> None:
    """Correction 2: 'demo' must NOT live in operating_mode (it is a
    proof_realism axis, orthogonal to the runtime shape)."""
    assert "demo" not in OPERATING_MODES
    assert "demo" not in DECLARED_OPERATING_MODES


# ---------------------------------------------------------------------------
# Compatibility — legacy plans
# ---------------------------------------------------------------------------


def _legacy_plan() -> dict:
    """Pre-Fix-76 plan shape.  Carries acceptance_cmd, no operating_mode."""
    return {
        "version": 1,
        "project_id": "legacy",
        "mode": "generation",
        "acceptance_cmd": "pytest tests/ -q",
        "tickets": [
            {
                "ticket_id": "T001",
                "goal": "do something",
                "acceptance_criteria": ["function f exists in src/m.py"],
            }
        ],
    }


def test_legacy_plan_parses_as_legacy_unclassified() -> None:
    """No operating_mode field → legacy_unclassified compatibility state.
    Must NOT silently default to ``library`` (would weaken acceptance)."""
    raw = _legacy_plan()
    assert _parse_operating_mode(raw) == "legacy_unclassified"


def test_legacy_plan_validates_with_no_errors() -> None:
    """Legacy plans pass validation (compatibility path)."""
    errs = validate_plan(_legacy_plan())
    assert errs == [], errs


def test_legacy_plan_with_local_proof_cmd_is_rejected() -> None:
    """Legacy compatibility path forbids local_proof_cmd (no dual-read)."""
    raw = _legacy_plan()
    raw["local_proof_cmd"] = "pytest -q"
    errs = validate_plan(raw)
    assert any("legacy_unclassified must NOT set local_proof_cmd" in e for e in errs), errs


def test_explicit_legacy_unclassified_is_accepted() -> None:
    """A plan that explicitly sets operating_mode=legacy_unclassified
    parses through the compatibility path (validator does not reject)."""
    raw = _legacy_plan()
    raw["operating_mode"] = "legacy_unclassified"
    errs = validate_plan(raw)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# Declared-mode plans — proof-field precedence
# ---------------------------------------------------------------------------


def _declared_plan(**overrides) -> dict:
    """Minimum valid declared-mode plan."""
    base = {
        "version": 1,
        "project_id": "declared",
        "mode": "generation",
        "operating_mode": "library",
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "local_proof_cmd": "python -c \"import x; assert x.compute(1) == 1\"",
        "tickets": [
            {
                "ticket_id": "T001",
                "goal": "do something",
                "acceptance_criteria": ["function f exists in src/m.py"],
            }
        ],
    }
    base.update(overrides)
    return base


def test_declared_mode_requires_local_proof_cmd() -> None:
    raw = _declared_plan()
    raw.pop("local_proof_cmd")
    errs = validate_plan(raw)
    assert any("requires non-empty local_proof_cmd" in e for e in errs), errs


def test_declared_mode_rejects_acceptance_cmd_alone() -> None:
    """Setting acceptance_cmd on a declared-mode plan WITHOUT local_proof_cmd
    must be rejected — the validator must not silently prefer one over the other."""
    raw = _declared_plan()
    raw.pop("local_proof_cmd")
    raw["acceptance_cmd"] = "pytest tests/ -q"
    errs = validate_plan(raw)
    # Two errors expected: missing local_proof_cmd AND must-not-set acceptance_cmd
    assert any("requires non-empty local_proof_cmd" in e for e in errs)
    assert any("must NOT set acceptance_cmd" in e for e in errs)


def test_declared_mode_rejects_both_proof_fields() -> None:
    """No precedence ambiguity — both fields set on a declared-mode plan is
    explicitly rejected, not silently resolved."""
    raw = _declared_plan(acceptance_cmd="pytest -q")
    errs = validate_plan(raw)
    assert any("must NOT set acceptance_cmd" in e for e in errs), errs


def test_declared_mode_with_only_local_proof_cmd_validates() -> None:
    raw = _declared_plan()
    errs = validate_plan(raw)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# external_dependencies invariants
# ---------------------------------------------------------------------------


def test_external_dependencies_profile_requires_non_empty_list() -> None:
    raw = _declared_plan(
        dependency_profile="external_dependencies",
        operator_disclaimer="External: openai_api.  Local proof uses fakes.",
    )
    # external_dependencies missing — must error.
    errs = validate_plan(raw)
    assert any(
        "non-empty external_dependencies" in e for e in errs
    ), errs


def test_external_dependencies_profile_with_list_validates() -> None:
    raw = _declared_plan(
        dependency_profile="external_dependencies",
        external_dependencies=["openai_api"],
        operator_disclaimer="External: openai_api.  Local proof uses fakes.",
    )
    errs = validate_plan(raw)
    assert errs == [], errs


def test_external_dependencies_must_be_empty_for_other_profiles() -> None:
    raw = _declared_plan(
        dependency_profile="self_contained",
        external_dependencies=["openai_api"],
    )
    errs = validate_plan(raw)
    assert any(
        "external_dependencies must be empty" in e for e in errs
    ), errs


def test_live_proof_cmd_must_be_empty_unless_external_dependencies() -> None:
    raw = _declared_plan(
        dependency_profile="self_contained",
        live_proof_cmd="python -m mytool --live",
    )
    errs = validate_plan(raw)
    assert any("live_proof_cmd must be empty" in e for e in errs), errs


def test_live_proof_cmd_allowed_for_external_dependencies() -> None:
    raw = _declared_plan(
        dependency_profile="external_dependencies",
        external_dependencies=["openai_api"],
        live_proof_cmd="python -m mytool --live",
        operator_disclaimer="External: openai_api.",
    )
    errs = validate_plan(raw)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# seeded_demo invariants
# ---------------------------------------------------------------------------


def test_seeded_demo_requires_demo_seed_source() -> None:
    raw = _declared_plan(
        proof_realism="seeded_demo",
        operator_disclaimer="demo",
    )
    errs = validate_plan(raw)
    assert any("seeded_demo requires non-empty demo_seed_source" in e for e in errs), errs


def test_seeded_demo_requires_operator_disclaimer() -> None:
    raw = _declared_plan(
        proof_realism="seeded_demo",
        demo_seed_source="seeds/demo.json",
    )
    errs = validate_plan(raw)
    assert any("operator_disclaimer must be non-empty" in e for e in errs), errs


def test_seeded_demo_with_both_fields_validates() -> None:
    raw = _declared_plan(
        proof_realism="seeded_demo",
        demo_seed_source="seeds/demo.json",
        operator_disclaimer="Demo mode — uses seeds/demo.json.",
    )
    errs = validate_plan(raw)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# operator_disclaimer requirements
# ---------------------------------------------------------------------------


def test_storage_only_requires_operator_disclaimer() -> None:
    raw = _declared_plan(operating_mode="storage_only")
    errs = validate_plan(raw)
    assert any("operator_disclaimer must be non-empty" in e for e in errs), errs


def test_external_deps_requires_operator_disclaimer() -> None:
    raw = _declared_plan(
        dependency_profile="external_dependencies",
        external_dependencies=["openai_api"],
    )
    errs = validate_plan(raw)
    assert any("operator_disclaimer must be non-empty" in e for e in errs), errs


# ---------------------------------------------------------------------------
# Unknown enum values
# ---------------------------------------------------------------------------


def test_unknown_operating_mode_is_rejected_by_validator() -> None:
    raw = _declared_plan(operating_mode="banana")
    errs = validate_plan(raw)
    assert any("operating_mode" in e and "banana" in e for e in errs), errs


def test_unknown_dependency_profile_is_rejected() -> None:
    raw = _declared_plan(dependency_profile="cloud_only")
    errs = validate_plan(raw)
    assert any("dependency_profile" in e and "cloud_only" in e for e in errs), errs


def test_unknown_proof_realism_is_rejected() -> None:
    raw = _declared_plan(proof_realism="aspirational")
    errs = validate_plan(raw)
    assert any("proof_realism" in e and "aspirational" in e for e in errs), errs


# ---------------------------------------------------------------------------
# clarification_record parsing
# ---------------------------------------------------------------------------


def test_clarification_record_parses_dict_entries() -> None:
    raw = [
        {"trigger_kind": "mode_ambiguous", "question": "Mode?",
         "answer": "cli_tool", "blocking": True},
        {"trigger_kind": "data_source_unspecified", "question": "Source?",
         "answer": "fixture", "blocking": False},
    ]
    out = _parse_clarification_record(raw)
    assert len(out) == 2
    assert out[0] == ClarificationEntry(
        trigger_kind="mode_ambiguous", question="Mode?",
        answer="cli_tool", blocking=True,
    )
    assert out[1].trigger_kind == "data_source_unspecified"
    assert out[1].blocking is False


def test_clarification_record_skips_malformed_entries() -> None:
    raw = [
        "not a dict",
        {"question": "missing trigger_kind"},  # no trigger_kind → skipped
        {"trigger_kind": "valid_kind", "question": "Q", "answer": "A"},
    ]
    out = _parse_clarification_record(raw)
    assert len(out) == 1
    assert out[0].trigger_kind == "valid_kind"


def test_clarification_record_empty_when_missing() -> None:
    assert _parse_clarification_record(None) == ()
    assert _parse_clarification_record([]) == ()


# ---------------------------------------------------------------------------
# ProjectPlan dataclass exposes new fields with safe defaults
# ---------------------------------------------------------------------------


def test_project_plan_defaults_are_legacy_safe() -> None:
    """A ProjectPlan constructed with no Fix 76 fields defaults to the
    explicit legacy compatibility state, NOT to library."""
    p = ProjectPlan(version=1, project_id="x")
    assert p.operating_mode == "legacy_unclassified"
    assert p.dependency_profile == "self_contained"
    assert p.proof_realism == "production_intent"
    assert p.external_dependencies == ()
    assert p.local_proof_cmd == ""
    assert p.live_proof_cmd == ""
    assert p.demo_seed_source == ""
    assert p.operator_disclaimer == ""
    assert p.clarification_record == ()


# ---------------------------------------------------------------------------
# End-to-end via load_plan
# ---------------------------------------------------------------------------


def test_load_plan_round_trips_declared_mode_fields(tmp_path) -> None:
    import json
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_declared_plan(
        dependency_profile="external_dependencies",
        external_dependencies=["openai_api", "stripe"],
        operator_disclaimer="External: openai_api, stripe.",
        live_proof_cmd="python -m mytool --live",
    )))
    from saturnday.plan_parser import load_plan
    plan = load_plan(plan_path)
    assert plan.operating_mode == "library"
    assert plan.dependency_profile == "external_dependencies"
    assert plan.proof_realism == "production_intent"
    assert plan.external_dependencies == ("openai_api", "stripe")
    assert "import x" in plan.local_proof_cmd
    assert plan.live_proof_cmd == "python -m mytool --live"
    assert "External" in plan.operator_disclaimer


def test_load_plan_handles_legacy_format(tmp_path) -> None:
    import json
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_legacy_plan()))
    from saturnday.plan_parser import load_plan
    plan = load_plan(plan_path)
    assert plan.operating_mode == "legacy_unclassified"
    assert plan.acceptance_cmd == "pytest tests/ -q"
    # Defaults for new fields stay safe.
    assert plan.dependency_profile == "self_contained"
    assert plan.proof_realism == "production_intent"
    assert plan.local_proof_cmd == ""


# ---------------------------------------------------------------------------
# Planner emission: must NOT emit legacy_unclassified
# ---------------------------------------------------------------------------


def test_planner_emits_declared_mode_for_library_brief() -> None:
    from saturnday.run.planner import _detect_operating_mode
    assert _detect_operating_mode(
        "build a Python library for parsing X", []
    ) == "library"


def test_planner_emits_web_service_for_api_brief() -> None:
    from saturnday.run.planner import _detect_operating_mode
    assert _detect_operating_mode(
        "build a REST API server with FastAPI", []
    ) == "web_service"


def test_planner_emits_cli_tool_for_cli_brief() -> None:
    from saturnday.run.planner import _detect_operating_mode
    assert _detect_operating_mode(
        "build a CLI tool to convert X to Y", []
    ) == "cli_tool"


def test_planner_emits_frontend_for_frontend_brief() -> None:
    from saturnday.run.planner import _detect_operating_mode
    assert _detect_operating_mode(
        "build a React single-page app", []
    ) == "frontend"


def test_planner_never_emits_legacy_unclassified() -> None:
    """Newly generated plans must declare a real mode.  Even an
    unrecognisable brief must fall back to a declared mode (library is the
    safest), NEVER to legacy_unclassified."""
    from saturnday.run.planner import _detect_operating_mode
    out = _detect_operating_mode("xyzzy plugh nonsense", [])
    assert out in DECLARED_OPERATING_MODES
    assert out != "legacy_unclassified"


def test_planner_detects_external_dependencies_for_openai_brief() -> None:
    from saturnday.run.planner import _detect_dependency_profile_and_externals
    profile, services = _detect_dependency_profile_and_externals(
        "build a chatbot using the OpenAI API"
    )
    assert profile == "external_dependencies"
    assert "openai_api" in services


def test_planner_detects_local_dependencies_for_postgres_brief() -> None:
    from saturnday.run.planner import _detect_dependency_profile_and_externals
    profile, services = _detect_dependency_profile_and_externals(
        "build a service backed by Postgres"
    )
    assert profile == "local_dependencies"
    assert services == ()


def test_planner_detects_seeded_demo() -> None:
    from saturnday.run.planner import _detect_proof_realism
    assert _detect_proof_realism("build a demo of X") == "seeded_demo"
    assert _detect_proof_realism("build a production tool") == "production_intent"
