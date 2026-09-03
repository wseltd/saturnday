"""Phase 8 — plan preview surfacing.

``show_plan_preview`` surfaces the full product framing at the approval
gate so the operator can see WHAT will be built and HOW it will be proved
before the run starts:

- operating_mode / dependency_profile / proof_realism / testing_strategy
- operator_disclaimer (verbatim, with line wrapping)
- external_dependencies
- local_proof_cmd and live_proof_cmd — with a visible warning for gaps
- clarification record
- persisted proof_resolution_source
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from saturnday.interactive import show_plan_preview


def _render(plan_data: dict) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        show_plan_preview(plan_data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Legacy plan — no framing surfaced
# ---------------------------------------------------------------------------


def test_legacy_plan_omits_product_framing_section() -> None:
    """A plan with operating_mode=legacy_unclassified should not print the
    product-framing block — that block is specifically for declared-mode
    plans so legacy plans don't sprout an empty section header."""
    plan = {
        "project_id": "legacy",
        "tickets": [],
        "phases": [],
        "operating_mode": "legacy_unclassified",
    }
    out = _render(plan)
    assert "Product framing" not in out
    # Project header is always printed.
    assert "legacy" in out


# ---------------------------------------------------------------------------
# Declared-mode plan — framing surfaces
# ---------------------------------------------------------------------------


def test_declared_mode_framing_is_visible() -> None:
    plan = {
        "project_id": "webapp",
        "tickets": [],
        "operating_mode": "web_service",
        "dependency_profile": "external_dependencies",
        "proof_realism": "production_intent",
        "testing_strategy": "live_credentials_gated",
        "external_dependencies": ["stripe", "sendgrid"],
    }
    out = _render(plan)
    assert "Product framing" in out
    assert "web_service" in out
    assert "external_dependencies" in out
    assert "production_intent" in out
    assert "live_credentials_gated" in out
    assert "stripe" in out
    assert "sendgrid" in out


def test_unspecified_testing_strategy_is_hidden() -> None:
    """Hide testing_strategy when it's the default 'unspecified' — avoid
    noise on plans that weren't negotiated."""
    plan = {
        "project_id": "lib",
        "tickets": [],
        "operating_mode": "library",
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "testing_strategy": "unspecified",
    }
    out = _render(plan)
    assert "Product framing" in out
    assert "unspecified" not in out


# ---------------------------------------------------------------------------
# Operator disclaimer
# ---------------------------------------------------------------------------


def test_operator_disclaimer_is_surfaced() -> None:
    plan = {
        "project_id": "demo",
        "tickets": [],
        "operating_mode": "storage_only",
        "operator_disclaimer": (
            "Product writes to /var/app/store and has no runnable user path — "
            "evaluate by inspecting the stored artefacts."
        ),
    }
    out = _render(plan)
    assert "Operator disclaimer" in out
    assert "stored artefacts" in out


def test_empty_disclaimer_is_not_printed() -> None:
    plan = {
        "project_id": "p",
        "tickets": [],
        "operating_mode": "library",
        "operator_disclaimer": "",
    }
    out = _render(plan)
    assert "Operator disclaimer" not in out


# ---------------------------------------------------------------------------
# Proof surface — GAP warning is visible
# ---------------------------------------------------------------------------


def test_fix73_proof_gap_marker_is_flagged() -> None:
    plan = {
        "project_id": "wkr",
        "tickets": [],
        "operating_mode": "worker",
        "local_proof_cmd": (
            "python -c \"import sys; sys.exit('FIX73_PROOF_GAP: worker "
            "side-effect path unknown')\""
        ),
    }
    out = _render(plan)
    assert "local_proof (GAP)" in out
    assert "Phase 5" in out or "Phase 6" in out
    assert "not derived at plan time" in out


def test_concrete_proof_shows_as_local_proof() -> None:
    plan = {
        "project_id": "lib",
        "tickets": [],
        "operating_mode": "library",
        "local_proof_cmd": 'python -c "from src.m import f; assert f(1) == 2"',
    }
    out = _render(plan)
    assert "local_proof " in out
    assert "GAP" not in out


def test_live_proof_surfaces_with_gate_note() -> None:
    plan = {
        "project_id": "ws",
        "tickets": [],
        "operating_mode": "web_service",
        "dependency_profile": "external_dependencies",
        "external_dependencies": ["stripe"],
        "local_proof_cmd": "python -m smoke",
        "live_proof_cmd": 'curl -fs https://api.stripe.com/v1/charges',
    }
    out = _render(plan)
    assert "live_proof" in out
    assert "LIVE_PROOF=1" in out


def test_proof_resolution_source_surfaced() -> None:
    plan = {
        "project_id": "lib",
        "tickets": [],
        "operating_mode": "library",
        "local_proof_cmd": 'python -c "assert 1"',
        "proof_resolution_source": "coder_plan_time",
    }
    out = _render(plan)
    assert "resolution_source" in out
    assert "coder_plan_time" in out


def test_proof_source_none_is_hidden() -> None:
    plan = {
        "project_id": "lib",
        "tickets": [],
        "operating_mode": "library",
        "local_proof_cmd": 'python -c "assert 1"',
        "proof_resolution_source": "none",
    }
    out = _render(plan)
    assert "resolution_source" not in out


# ---------------------------------------------------------------------------
# Clarification record
# ---------------------------------------------------------------------------


def test_clarification_record_surfaces_with_answers() -> None:
    plan = {
        "project_id": "ws",
        "tickets": [],
        "operating_mode": "web_service",
        "clarification_record": [
            {
                "trigger_kind": "mode_ambiguous",
                "question": "Which operating mode?",
                "answer": "web_service",
                "blocking": True,
            },
            {
                "trigger_kind": "testing_strategy_unspecified",
                "question": "How should we test?",
                "answer": "local_emulator",
                "blocking": True,
            },
        ],
    }
    out = _render(plan)
    assert "Clarification record" in out
    assert "mode_ambiguous" in out
    assert "web_service" in out
    assert "testing_strategy_unspecified" in out
    assert "local_emulator" in out


def test_empty_clarification_record_not_shown() -> None:
    plan = {
        "project_id": "p",
        "tickets": [],
        "operating_mode": "library",
        "clarification_record": [],
    }
    out = _render(plan)
    assert "Clarification record" not in out


# ---------------------------------------------------------------------------
# DoD contract + ticket list still render
# ---------------------------------------------------------------------------


def test_dod_and_tickets_still_surface_for_declared_plan() -> None:
    plan = {
        "project_id": "app",
        "tickets": [
            {"ticket_id": "T001", "goal": "build auth"},
            {"ticket_id": "T002", "goal": "add billing"},
        ],
        "operating_mode": "web_service",
        "required_outcomes": ["login works end-to-end"],
        "proof_expectations": ["HTTP 200 on /login with valid creds"],
    }
    out = _render(plan)
    # Product framing visible
    assert "Product framing" in out
    # DoD still visible
    assert "Required outcomes" in out
    assert "login works end-to-end" in out
    # Ticket list still visible
    assert "T001" in out
    assert "build auth" in out
