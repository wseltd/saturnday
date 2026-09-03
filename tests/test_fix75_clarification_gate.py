"""Fix 75 — clarification gate before planning.

Three responsibilities under test:

1. ``detect_ambiguity_triggers`` fires the right trigger classes for the
   right brief shapes (mode_ambiguous, data_source_unspecified,
   external_dependency_unspecified, etc.).

2. ``collect_clarifications`` resolves answers via CLI flags (highest
   priority), via interactive prompt, or refuses with the right
   ``unanswered_blocking_kinds`` list when neither path supplies an answer.

3. CLI flag parsing (``parse_clarify_flags``) and the refusal-message
   formatter behave as documented.

Out of scope: planner emission consequences (covered by Fix 73 / Fix 76);
runner gating (Fix 77).  This file is the gate's own contract.
"""

from __future__ import annotations

from saturnday._types import ClarificationEntry
from saturnday.run.clarification import (
    UNANSWERED_NONINTERACTIVE,
    ClarificationTrigger,
    collect_clarifications,
    detect_ambiguity_triggers,
    format_refusal_message,
    parse_clarify_flags,
)


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------


def _kinds(triggers: list[ClarificationTrigger]) -> set[str]:
    return {t.kind for t in triggers}


def test_empty_brief_fires_blocking_mode_ambiguous() -> None:
    triggers = detect_ambiguity_triggers("")
    assert len(triggers) == 1
    assert triggers[0].kind == "mode_ambiguous"
    assert triggers[0].blocking is True


def test_brief_with_no_mode_keywords_fires_mode_ambiguous() -> None:
    triggers = detect_ambiguity_triggers("xyzzy plugh")
    assert "mode_ambiguous" in _kinds(triggers)


def test_brief_with_multiple_mode_keywords_fires_mode_ambiguous() -> None:
    """e.g. 'CLI tool that also exposes a REST API' — both cli_tool and
    web_service hits → operator must pick one."""
    triggers = detect_ambiguity_triggers(
        "build a CLI tool that also exposes a REST API"
    )
    mode = next(t for t in triggers if t.kind == "mode_ambiguous")
    assert mode.blocking is True
    assert "cli_tool" in mode.answers
    assert "web_service" in mode.answers


def test_brief_with_one_clear_mode_does_not_fire_mode_ambiguous() -> None:
    """Briefs with exactly one mode keyword set should NOT fire mode_ambiguous."""
    triggers = detect_ambiguity_triggers("build a Python library for parsing X")
    assert "mode_ambiguous" not in _kinds(triggers)


def test_external_dependency_fires_for_slack_brief_without_clarity() -> None:
    triggers = detect_ambiguity_triggers("build a notifier that posts to Slack")
    assert "external_dependency_unspecified" in _kinds(triggers)
    ed = next(t for t in triggers if t.kind == "external_dependency_unspecified")
    assert ed.blocking is True
    assert "live_integration" in ed.answers
    assert "local_fake" in ed.answers


def test_external_dependency_does_NOT_fire_when_brief_clarifies_stub() -> None:
    triggers = detect_ambiguity_triggers(
        "build a notifier that posts to Slack — use a local fake for tests"
    )
    assert "external_dependency_unspecified" not in _kinds(triggers)


def test_data_source_unspecified_fires_for_ingest_brief() -> None:
    triggers = detect_ambiguity_triggers(
        "build a tool that will ingest data and emit summaries"
    )
    assert "data_source_unspecified" in _kinds(triggers)


def test_data_source_does_NOT_fire_when_csv_named() -> None:
    triggers = detect_ambiguity_triggers(
        "build a tool that ingests a CSV file and emits summaries"
    )
    assert "data_source_unspecified" not in _kinds(triggers)


def test_acceptance_target_unclear_fires_when_no_acceptance_language() -> None:
    triggers = detect_ambiguity_triggers("build a Python library for parsing X")
    # No 'tests pass' / 'deploy' / etc. → acceptance unclear (non-blocking).
    at = next((t for t in triggers if t.kind == "acceptance_target_unclear"), None)
    assert at is not None
    assert at.blocking is False  # advisory


def test_ui_surface_unspecified_fires_for_dashboard_brief() -> None:
    triggers = detect_ambiguity_triggers(
        "build a dashboard for revenue metrics"
    )
    assert "ui_surface_unspecified" in _kinds(triggers)


def test_ui_surface_does_NOT_fire_when_web_clarified() -> None:
    triggers = detect_ambiguity_triggers(
        "build a web page dashboard for revenue metrics"
    )
    assert "ui_surface_unspecified" not in _kinds(triggers)


def test_persistence_unspecified_fires_for_track_users_brief() -> None:
    triggers = detect_ambiguity_triggers(
        "build a tool to track user activity over time"
    )
    assert "persistence_unspecified" in _kinds(triggers)


def test_demo_vs_production_fires_for_simulate_brief() -> None:
    triggers = detect_ambiguity_triggers(
        "build a tool that simulates user behaviour"
    )
    assert "demo_vs_production" in _kinds(triggers)


def test_setup_assumptions_fires_for_model_weights_brief() -> None:
    triggers = detect_ambiguity_triggers(
        "build a service that classifies images using pretrained model weights"
    )
    assert "setup_assumptions_unstated" in _kinds(triggers)


def test_integration_scope_fires_for_build_with_brief() -> None:
    triggers = detect_ambiguity_triggers(
        "build a recommender with TensorFlow"
    )
    assert "integration_scope_unclear" in _kinds(triggers)


def test_operator_identity_fires_when_undefined_user() -> None:
    triggers = detect_ambiguity_triggers(
        "build something that helps the user manage their tasks"
    )
    assert "operator_identity_unclear" in _kinds(triggers)


# ---------------------------------------------------------------------------
# parse_clarify_flags
# ---------------------------------------------------------------------------


def test_parse_clarify_flags_basic() -> None:
    out = parse_clarify_flags(["mode_ambiguous=cli_tool"])
    assert out == {"mode_ambiguous": "cli_tool"}


def test_parse_clarify_flags_multiple() -> None:
    out = parse_clarify_flags([
        "mode_ambiguous=web_service",
        "external_dependency_unspecified=local_fake",
    ])
    assert out == {
        "mode_ambiguous": "web_service",
        "external_dependency_unspecified": "local_fake",
    }


def test_parse_clarify_flags_empty_or_none() -> None:
    assert parse_clarify_flags(None) == {}
    assert parse_clarify_flags([]) == {}


def test_parse_clarify_flags_drops_malformed_entries() -> None:
    out = parse_clarify_flags(["bad_no_equals", "=no_kind", "valid=yes"])
    assert out == {"valid": "yes"}


def test_parse_clarify_flags_handles_equals_in_answer() -> None:
    """Answers may contain `=` (e.g. URL paths).  Only the first `=` splits."""
    out = parse_clarify_flags(["url=https://example.com/path?q=1"])
    assert out == {"url": "https://example.com/path?q=1"}


# ---------------------------------------------------------------------------
# collect_clarifications — CLI answers
# ---------------------------------------------------------------------------


def test_cli_answer_takes_precedence_over_prompt() -> None:
    """A CLI answer for a trigger means the prompt_fn is never called."""
    triggers = [
        ClarificationTrigger(
            kind="mode_ambiguous", question="Q",
            answers=("library", "cli_tool"), blocking=True,
        ),
    ]
    prompt_calls: list = []

    def prompt(t: ClarificationTrigger) -> str:
        prompt_calls.append(t.kind)
        return "PROMPTED"

    entries, unanswered = collect_clarifications(
        triggers,
        cli_answers={"mode_ambiguous": "library"},
        interactive=True,
        prompt_fn=prompt,
    )
    assert prompt_calls == [], "CLI answer must skip the prompt"
    assert len(entries) == 1
    assert entries[0].answer == "library"
    assert unanswered == []


# ---------------------------------------------------------------------------
# collect_clarifications — interactive
# ---------------------------------------------------------------------------


def test_interactive_prompt_is_called_for_each_trigger_without_cli_answer() -> None:
    triggers = [
        ClarificationTrigger(kind="a", question="Qa", answers=("x", "y"), blocking=True),
        ClarificationTrigger(kind="b", question="Qb", answers=("p", "q"), blocking=False),
    ]
    answers_iter = iter(["xx", "qq"])

    def prompt(t: ClarificationTrigger) -> str:
        return next(answers_iter)

    entries, unanswered = collect_clarifications(
        triggers, interactive=True, prompt_fn=prompt,
    )
    assert [e.answer for e in entries] == ["xx", "qq"]
    assert unanswered == []


# ---------------------------------------------------------------------------
# collect_clarifications — non-interactive
# ---------------------------------------------------------------------------


def test_noninteractive_blocking_unanswered_is_refused() -> None:
    triggers = [
        ClarificationTrigger(kind="block_a", question="Qa", answers=("x",), blocking=True),
        ClarificationTrigger(kind="block_b", question="Qb", answers=("y",), blocking=True),
    ]
    entries, unanswered = collect_clarifications(triggers, interactive=False)
    assert set(unanswered) == {"block_a", "block_b"}
    # Entries are still recorded with the sentinel — evidence that the
    # operator was asked but did not answer.
    assert len(entries) == 2
    for e in entries:
        assert e.answer == UNANSWERED_NONINTERACTIVE


def test_noninteractive_nonblocking_unanswered_recorded_but_allowed() -> None:
    triggers = [
        ClarificationTrigger(kind="advise", question="Q", answers=("x",), blocking=False),
    ]
    entries, unanswered = collect_clarifications(triggers, interactive=False)
    assert unanswered == [], "non-blocking triggers must NOT block planning"
    assert entries[0].answer == UNANSWERED_NONINTERACTIVE
    assert entries[0].blocking is False


def test_noninteractive_with_cli_answers_fully_resolves_blocking() -> None:
    triggers = [
        ClarificationTrigger(kind="block_a", question="Qa", answers=("x",), blocking=True),
    ]
    entries, unanswered = collect_clarifications(
        triggers,
        cli_answers={"block_a": "x"},
        interactive=False,
    )
    assert unanswered == []
    assert entries[0].answer == "x"


def test_noninteractive_with_partial_cli_answers_still_refused() -> None:
    """If only some blocking triggers are answered via CLI, the others still block."""
    triggers = [
        ClarificationTrigger(kind="answered", question="Q", answers=("x",), blocking=True),
        ClarificationTrigger(kind="missing", question="Q", answers=("y",), blocking=True),
    ]
    entries, unanswered = collect_clarifications(
        triggers,
        cli_answers={"answered": "x"},
        interactive=False,
    )
    assert unanswered == ["missing"]


# ---------------------------------------------------------------------------
# format_refusal_message
# ---------------------------------------------------------------------------


def test_refusal_message_lists_each_unanswered_kind() -> None:
    msg = format_refusal_message(["mode_ambiguous", "data_source_unspecified"])
    # Phrased as a neutral "Clarifications needed" message so CLI coder
    # subprocesses that read this don't render it as a scary error.
    assert "Clarifications needed" in msg
    assert "mode_ambiguous" in msg
    assert "data_source_unspecified" in msg
    assert "--clarify" in msg


def test_refusal_message_empty_when_no_unanswered() -> None:
    assert format_refusal_message([]) == ""


# ---------------------------------------------------------------------------
# Persistence — clarification_record round-trips into the plan
# ---------------------------------------------------------------------------


def test_clarification_entries_persist_into_plan(tmp_path) -> None:
    """End-to-end: collected entries land in the emitted plan.json."""
    import json
    from unittest.mock import patch
    from saturnday._types import CoderConfig
    from saturnday.run.planner import generate_plan

    # Stub call_coder so we don't touch any backend.
    def fake_call_coder(*a, **k):
        return json.dumps({"tickets": [
            {"ticket_id": "T001", "goal": "G",
             "acceptance_criteria": ["function f exists in src/m.py"]}
        ]})

    entries = (
        ClarificationEntry(
            trigger_kind="mode_ambiguous", question="Q",
            answer="cli_tool", blocking=True,
        ),
    )
    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        plan_path = generate_plan(
            brief="build a CLI tool for parsing logs",
            repo_path=str(tmp_path),
            coder_config=CoderConfig(backend="openai", api_key="t", model="gpt-4"),
            clarification_record=entries,
        )
    plan = json.loads(plan_path.read_text())
    assert plan["clarification_record"] == [
        {
            "trigger_kind": "mode_ambiguous",
            "question": "Q",
            "answer": "cli_tool",
            "blocking": True,
        }
    ]
    # Fix 75 mode override: clarification answer of 'cli_tool' overrides
    # whatever the heuristic chose.
    assert plan["operating_mode"] == "cli_tool"


def test_clarification_mode_override_regenerates_local_proof_cmd(tmp_path) -> None:
    """When mode_ambiguous answer changes the operating_mode, local_proof_cmd
    must be regenerated to fit the chosen mode (Fix 73 meaningfulness)."""
    import json
    from unittest.mock import patch
    from saturnday._types import CoderConfig
    from saturnday.plan_parser import validate_proof_meaningfulness
    from saturnday.run.planner import generate_plan

    def fake_call_coder(*a, **k):
        return json.dumps({"tickets": [
            {"ticket_id": "T001", "goal": "G",
             "acceptance_criteria": ["function f exists in src/m.py"]}
        ]})

    entries = (
        ClarificationEntry(
            trigger_kind="mode_ambiguous", question="Q",
            answer="web_service", blocking=True,
        ),
    )
    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        plan_path = generate_plan(
            brief="build something for users",
            repo_path=str(tmp_path),
            coder_config=CoderConfig(backend="openai", api_key="t", model="gpt-4"),
            clarification_record=entries,
        )
    plan = json.loads(plan_path.read_text())
    assert plan["operating_mode"] == "web_service"
    # The regenerated local_proof_cmd must pass Fix 73 for web_service.
    errs = validate_proof_meaningfulness(
        operating_mode="web_service",
        proof_realism="production_intent",
        dependency_profile="self_contained",
        local_proof_cmd=plan["local_proof_cmd"],
    )
    assert errs == [], errs


def test_no_clarification_entries_yields_empty_record(tmp_path) -> None:
    """Backwards compatibility — generate_plan called without entries."""
    import json
    from unittest.mock import patch
    from saturnday._types import CoderConfig
    from saturnday.run.planner import generate_plan

    def fake_call_coder(*a, **k):
        return json.dumps({"tickets": [
            {"ticket_id": "T001", "goal": "G",
             "acceptance_criteria": ["function f exists in src/m.py"]}
        ]})

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        plan_path = generate_plan(
            brief="build a Python library",
            repo_path=str(tmp_path),
            coder_config=CoderConfig(backend="openai", api_key="t", model="gpt-4"),
        )
    plan = json.loads(plan_path.read_text())
    assert plan["clarification_record"] == []
