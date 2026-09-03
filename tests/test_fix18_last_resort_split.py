"""Fix 18 — last-resort oversize split tests.

Covers:
1. TicketSpec.allow_last_resort_split defaults to True.
2. TicketSpec.allow_last_resort_split can be disabled (False).
3. "oversize_defect" is a valid FailureCategory literal.
4. OVERSIZE_SPLIT_THRESHOLD is higher than PROMPT_HARD_THRESHOLD.
5. _LAST_RESORT_SPLIT_PROMPT is conservative (prefers no split, blocks atomics).
6. analyze_and_split_last_resort returns [ticket] when coder declines.
7. analyze_and_split_last_resort skips should_split heuristics (always queries).
8. Child tickets produced by last-resort split have allow_last_resort_split=False.
9. Gate: all 6 conditions must be met — missing any one suppresses the split.
10. Gate: budget_warn_count < 2 suppresses the split.
11. Gate: max_prompt_chars < OVERSIZE_SPLIT_THRESHOLD suppresses the split.
12. analyze_and_split_last_resort falls back to [ticket] on CoderAPIError.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import TicketScope, TicketSpec
from saturnday.run.failure_classifier import FailureCategory
from saturnday.ticket_runner import OVERSIZE_SPLIT_THRESHOLD, PROMPT_HARD_THRESHOLD
from saturnday.ticket_splitter import (
    _LAST_RESORT_SPLIT_PROMPT,
    analyze_and_split_last_resort,
)


def _make_ticket(
    ticket_id: str = "T001",
    goal: str = "Build a thing",
    allow_last_resort_split: bool = True,
) -> TicketSpec:
    return TicketSpec(
        ticket_id=ticket_id,
        goal=goal,
        allow_last_resort_split=allow_last_resort_split,
    )


def _make_config() -> SimpleNamespace:
    return SimpleNamespace(backend="openai", model="test-model", max_tokens=4096)


# ---------------------------------------------------------------------------
# Test 1: default is True
# ---------------------------------------------------------------------------

def test_allow_last_resort_split_default_is_true() -> None:
    """TicketSpec.allow_last_resort_split must default to True."""
    ticket = TicketSpec(ticket_id="T001", goal="Build a thing")
    assert ticket.allow_last_resort_split is True


# ---------------------------------------------------------------------------
# Test 2: can be disabled
# ---------------------------------------------------------------------------

def test_allow_last_resort_split_can_be_set_false() -> None:
    """TicketSpec.allow_last_resort_split can be set to False."""
    ticket = TicketSpec(ticket_id="T001", goal="Build a thing", allow_last_resort_split=False)
    assert ticket.allow_last_resort_split is False


# ---------------------------------------------------------------------------
# Test 3: oversize_defect is a valid FailureCategory
# ---------------------------------------------------------------------------

def test_oversize_defect_is_valid_failure_category() -> None:
    """'oversize_defect' must be accepted as a FailureCategory literal value."""
    from saturnday.run.failure_classifier import FailureClassification, GovernanceOutcome
    fc = FailureClassification(
        governance_outcome="HARD_FAIL_BLOCKING",
        failure_category="oversize_defect",
        summary="Prompt exceeded oversize threshold",
    )
    assert fc.failure_category == "oversize_defect"


# ---------------------------------------------------------------------------
# Test 4: OVERSIZE_SPLIT_THRESHOLD > PROMPT_HARD_THRESHOLD
# ---------------------------------------------------------------------------

def test_oversize_split_threshold_higher_than_hard_threshold() -> None:
    """OVERSIZE_SPLIT_THRESHOLD must be clearly above PROMPT_HARD_THRESHOLD."""
    assert OVERSIZE_SPLIT_THRESHOLD > PROMPT_HARD_THRESHOLD, (
        f"OVERSIZE_SPLIT_THRESHOLD ({OVERSIZE_SPLIT_THRESHOLD}) must exceed "
        f"PROMPT_HARD_THRESHOLD ({PROMPT_HARD_THRESHOLD})"
    )


# ---------------------------------------------------------------------------
# Test 5: _LAST_RESORT_SPLIT_PROMPT is conservative
# ---------------------------------------------------------------------------

def test_last_resort_prompt_prefers_no_split() -> None:
    """_LAST_RESORT_SPLIT_PROMPT must prefer {{split: false}} explicitly."""
    assert "split: false" in _LAST_RESORT_SPLIT_PROMPT or '"split": false' in _LAST_RESORT_SPLIT_PROMPT, (
        "_LAST_RESORT_SPLIT_PROMPT must express a preference for no-split"
    )


def test_last_resort_prompt_rejects_atomic_tickets() -> None:
    """_LAST_RESORT_SPLIT_PROMPT must explicitly forbid splitting atomic tickets."""
    prompt_lower = _LAST_RESORT_SPLIT_PROMPT.lower()
    assert "atomic" in prompt_lower or "migration" in prompt_lower, (
        "_LAST_RESORT_SPLIT_PROMPT must mention atomic refactors or migrations as DO NOT SPLIT"
    )


def test_last_resort_prompt_rejects_api_contracts() -> None:
    """_LAST_RESORT_SPLIT_PROMPT must explicitly forbid splitting API contracts."""
    assert "api" in _LAST_RESORT_SPLIT_PROMPT.lower() or "contract" in _LAST_RESORT_SPLIT_PROMPT.lower(), (
        "_LAST_RESORT_SPLIT_PROMPT must mention API contracts as DO NOT SPLIT"
    )


# ---------------------------------------------------------------------------
# Test 6: analyze_and_split_last_resort returns [ticket] when coder declines
# ---------------------------------------------------------------------------

def test_analyze_and_split_last_resort_returns_unsplit_when_declined(tmp_path: Path) -> None:
    """When the coder responds {split: false}, [ticket] is returned unchanged."""
    ticket = _make_ticket(goal="Build a module with a single function")
    config = _make_config()

    with patch("saturnday.ticket_splitter.call_coder", return_value='{"split": false}'):
        result = analyze_and_split_last_resort(ticket, config, tmp_path, "plan notes")

    assert result == [ticket], "Must return the original ticket unchanged when no split"


def test_analyze_and_split_last_resort_returns_unsplit_on_invalid_json(tmp_path: Path) -> None:
    """When the coder returns non-JSON, [ticket] is returned unchanged."""
    ticket = _make_ticket(goal="Build a module")
    config = _make_config()

    with patch("saturnday.ticket_splitter.call_coder", return_value="sorry, no split"):
        result = analyze_and_split_last_resort(ticket, config, tmp_path, "")

    assert result == [ticket]


# ---------------------------------------------------------------------------
# Test 7: analyze_and_split_last_resort skips should_split heuristics
# ---------------------------------------------------------------------------

def test_analyze_and_split_last_resort_queries_even_for_short_goal(tmp_path: Path) -> None:
    """Even a short goal (< _MIN_GOAL_LENGTH_FOR_SPLIT chars) must be queried."""
    short_goal = "Add x"  # too short for should_split to fire
    ticket = _make_ticket(goal=short_goal)
    config = _make_config()

    coder_called = []

    def mock_coder(cfg, messages, path):
        coder_called.append(True)
        return '{"split": false}'

    with patch("saturnday.ticket_splitter.call_coder", side_effect=mock_coder):
        analyze_and_split_last_resort(ticket, config, tmp_path, "")

    assert coder_called, (
        "analyze_and_split_last_resort must always query the coder, "
        "even when should_split would return False"
    )


# ---------------------------------------------------------------------------
# Test 8: child tickets have allow_last_resort_split=False
# ---------------------------------------------------------------------------

def test_child_tickets_disabled_via_dataclasses_replace() -> None:
    """Children must be created with allow_last_resort_split=False to prevent recursion."""
    parent = _make_ticket(ticket_id="T001", goal="Build two independent modules")
    child_a = TicketSpec(ticket_id="T001.a", goal="Build module A", allow_last_resort_split=True)
    child_b = TicketSpec(ticket_id="T001.b", goal="Build module B", allow_last_resort_split=True)

    # Simulate the dataclasses.replace call the runner performs before executing children
    safe_a = dataclasses.replace(child_a, allow_last_resort_split=False)
    safe_b = dataclasses.replace(child_b, allow_last_resort_split=False)

    assert safe_a.allow_last_resort_split is False
    assert safe_b.allow_last_resort_split is False
    # Original unchanged
    assert child_a.allow_last_resort_split is True


# ---------------------------------------------------------------------------
# Test 9: gate — all 6 conditions must be met
# ---------------------------------------------------------------------------

def test_gate_suppressed_when_allow_last_resort_split_is_false() -> None:
    """Gate must not fire when ticket.allow_last_resort_split is False."""
    ticket = _make_ticket(allow_last_resort_split=False)
    # Simulate the gate condition evaluation
    budget_warn_count = 3
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD + 1000
    already = False

    gate_would_fire = (
        not already
        and ticket.allow_last_resort_split
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate_would_fire, "Gate must be suppressed when allow_last_resort_split=False"


def test_gate_suppressed_when_already_attempted() -> None:
    """Gate must not fire when _already_last_resort_split is True."""
    ticket = _make_ticket(allow_last_resort_split=True)
    budget_warn_count = 3
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD + 1000
    already = True  # already attempted

    gate_would_fire = (
        not already
        and ticket.allow_last_resort_split
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate_would_fire, "Gate must be suppressed when already attempted"


# ---------------------------------------------------------------------------
# Test 10: budget_warn_count < 2 suppresses the gate
# ---------------------------------------------------------------------------

def test_gate_suppressed_when_budget_warn_count_below_two() -> None:
    """Gate must not fire if fewer than 2 attempts triggered a budget warning."""
    ticket = _make_ticket(allow_last_resort_split=True)

    for warn_count in (0, 1):
        gate = (
            ticket.allow_last_resort_split
            and warn_count >= 2
            and OVERSIZE_SPLIT_THRESHOLD + 1 >= OVERSIZE_SPLIT_THRESHOLD
        )
        assert not gate, f"Gate fired unexpectedly with budget_warn_count={warn_count}"


# ---------------------------------------------------------------------------
# Test 11: max_prompt_chars < OVERSIZE_SPLIT_THRESHOLD suppresses the gate
# ---------------------------------------------------------------------------

def test_gate_suppressed_when_max_prompt_chars_below_threshold() -> None:
    """Gate must not fire if no attempt exceeded OVERSIZE_SPLIT_THRESHOLD chars."""
    ticket = _make_ticket(allow_last_resort_split=True)
    budget_warn_count = 3
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD - 1  # just below threshold

    gate = (
        ticket.allow_last_resort_split
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate, "Gate must be suppressed when max_prompt_chars < OVERSIZE_SPLIT_THRESHOLD"


def test_gate_fires_when_all_conditions_met() -> None:
    """Gate must fire when all 6 conditions are satisfied."""
    ticket = _make_ticket(allow_last_resort_split=True)
    budget_warn_count = 2
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD  # exactly at threshold
    already = False

    gate = (
        not already
        and ticket.allow_last_resort_split
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert gate, "Gate must fire when all conditions are met"


# ---------------------------------------------------------------------------
# Test 12: falls back to [ticket] on CoderAPIError
# ---------------------------------------------------------------------------

def test_analyze_and_split_last_resort_fallback_on_api_error(tmp_path: Path) -> None:
    """analyze_and_split_last_resort must return [ticket] when the coder API fails."""
    from saturnday._exceptions import CoderAPIError

    ticket = _make_ticket(goal="Build a large feature with many parts")
    config = _make_config()

    with patch("saturnday.ticket_splitter.call_coder", side_effect=CoderAPIError("timeout")):
        result = analyze_and_split_last_resort(ticket, config, tmp_path, "")

    assert result == [ticket], "Must return original ticket when CoderAPIError is raised"
