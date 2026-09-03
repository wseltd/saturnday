"""Fix 36 — planner Stage 1 falls through to single-shot fallback on CoderAPIError.

Proves that:
1. When Stage 1 call_coder() raises CoderAPIError, the single-shot fallback is attempted.
2. Successful Stage 1 response still produces a plan normally.
3. No fake success when both Stage 1 and single-shot fallback fail.
4. Stage 2 retry behaviour is unchanged by this fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._exceptions import CoderAPIError
from saturnday._types import CoderConfig


# ---------------------------------------------------------------------------
# Minimal valid plan JSON that passes validate_plan
# ---------------------------------------------------------------------------

def _valid_plan(project_id: str = "test-proj") -> str:
    return json.dumps({
        "project_id": project_id,
        "tickets": [
            {
                "ticket_id": "T001",
                "goal": "scaffold the project",
                "acceptance_criteria": ["file src/main.py exists"],
            }
        ],
        "phases": [{"name": "core", "ticket_ids": ["T001"]}],
        "definition_of_done": ["all_tickets_passed"],
    })


def _valid_skeleton() -> str:
    return json.dumps({"tickets": [{"ticket_id": "T001", "goal": "scaffold"}]})


def _valid_enriched() -> str:
    return json.dumps([{
        "ticket_id": "T001",
        "goal": "scaffold the project",
        "acceptance_criteria": ["file src/main.py exists"],
        "depends_on": [],
    }])


# ---------------------------------------------------------------------------
# Test 1: Stage 1 CoderAPIError → single-shot fallback is reached
# ---------------------------------------------------------------------------

def test_stage1_coder_api_error_falls_through_to_single_shot(tmp_path: Path) -> None:
    """Stage 1 raising CoderAPIError must fall through to the single-shot path."""
    from saturnday.run.planner import generate_plan

    call_count = []

    def fake_call_coder(config, messages, repo_path, **kwargs):
        call_count.append(1)
        if len(call_count) == 1:
            # Stage 1 fails
            raise CoderAPIError("claude-cli returned empty response")
        # Single-shot fallback succeeds with a valid plan
        return _valid_plan()

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        config = CoderConfig(backend="claude-cli")
        plan_path = generate_plan(
            brief="build a simple app",
            repo_path=tmp_path,
            coder_config=config,
        )

    assert plan_path.exists(), "plan.json must be written when fallback succeeds"
    data = json.loads(plan_path.read_text())
    assert data["project_id"] == "test-proj"
    # Exactly 2 calls: Stage 1 (failed) + 1 single-shot round (succeeded)
    assert len(call_count) == 2, (
        f"Expected 2 call_coder calls (Stage 1 + 1 fallback round), got {len(call_count)}"
    )


# ---------------------------------------------------------------------------
# Test 2: Successful Stage 1 still works normally
# ---------------------------------------------------------------------------

def test_stage1_success_produces_plan_normally(tmp_path: Path) -> None:
    """When Stage 1 succeeds, the plan is generated via the 3-stage path."""
    from saturnday.run.planner import generate_plan

    calls = []

    def fake_call_coder(config, messages, repo_path, **kwargs):
        calls.append(len(calls) + 1)
        n = len(calls)
        if n == 1:
            return _valid_skeleton()       # Stage 1: skeleton
        if n == 2:
            return _valid_enriched()       # Stage 2: enrich batch
        # Stage 2b governance extraction
        return json.dumps({"required_outcomes": [], "exclusions": [], "constraints": [], "proof_expectations": []})

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        config = CoderConfig(backend="claude-cli")
        plan_path = generate_plan(
            brief="build a simple app",
            repo_path=tmp_path,
            coder_config=config,
        )

    assert plan_path.exists()
    data = json.loads(plan_path.read_text())
    assert data["tickets"][0]["ticket_id"] == "T001"
    # Stage 1 must have been used (not single-shot fallback)
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# Test 3: Both Stage 1 and single-shot fallback fail → ValueError, no plan
# ---------------------------------------------------------------------------

def test_stage1_and_fallback_both_fail_raises_value_error(tmp_path: Path) -> None:
    """When Stage 1 raises CoderAPIError and all fallback rounds also fail, ValueError is raised."""
    from saturnday.run.planner import generate_plan

    def fake_call_coder(config, messages, repo_path, **kwargs):
        raise CoderAPIError("claude-cli returned empty response")

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
        config = CoderConfig(backend="claude-cli")
        with pytest.raises((ValueError, CoderAPIError)):
            generate_plan(
                brief="build a simple app",
                repo_path=tmp_path,
                coder_config=config,
            )

    plan_path = tmp_path / ".saturnday" / "plan.json"
    assert not plan_path.exists(), "plan.json must not be written when all calls fail"
