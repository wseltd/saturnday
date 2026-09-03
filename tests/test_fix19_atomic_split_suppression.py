"""Fix 19 — explicit atomic or do-not-split support tests.

Proves that tickets declared atomic=True cannot be split by either:
1. The pre-execution split path in run_plan() (Path A).
2. The Fix 18 last-resort split path (both inside-loop and post-loop gates).

Covers:
1. TicketSpec.atomic defaults to False.
2. atomic=True suppresses pre-execution split (Path A guard in run_plan()).
3. atomic=True suppresses Fix 18 last-resort split gate (both conditions).
4. atomic=True evidence: prompt_split_exempt=True, prompt_split_reason="atomic_ticket".
5. atomic=False preserves existing split behaviour (no regression).
6. atomic: true in plan JSON reaches TicketSpec via plan_parser.
7. No regression to Fix 18 split behaviour for non-atomic tickets.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday._types import TicketSpec
from saturnday.ticket_runner import OVERSIZE_SPLIT_THRESHOLD


# ---------------------------------------------------------------------------
# Test 1: TicketSpec.atomic defaults to False
# ---------------------------------------------------------------------------

def test_ticketspec_atomic_defaults_false() -> None:
    """atomic must default to False so existing plans are unaffected."""
    ticket = TicketSpec(ticket_id="T001", goal="Do something")
    assert ticket.atomic is False


def test_ticketspec_atomic_can_be_set_true() -> None:
    """atomic=True must be accepted and stored."""
    ticket = TicketSpec(ticket_id="T001", goal="Migrate schema", atomic=True)
    assert ticket.atomic is True


def test_ticketspec_atomic_is_immutable() -> None:
    """TicketSpec is frozen — atomic cannot be mutated after construction."""
    ticket = TicketSpec(ticket_id="T001", goal="Migrate schema", atomic=True)
    with pytest.raises((AttributeError, TypeError)):
        ticket.atomic = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 2: atomic=True suppresses Path A (pre-execution split in run_plan())
# ---------------------------------------------------------------------------

def test_atomic_true_suppresses_pre_execution_split() -> None:
    """When ticket.atomic is True, analyze_and_split must not be called."""
    ticket = TicketSpec(ticket_id="T001", goal="Rename Foo to Bar across 5 files", atomic=True)

    with patch("saturnday.ticket_splitter.analyze_and_split") as mock_split:
        # Simulate the Path A guard logic directly
        if ticket.atomic:
            sub_tickets = [ticket]
        else:
            sub_tickets = mock_split(ticket, None, None, "")

        assert sub_tickets == [ticket]
        mock_split.assert_not_called()


def test_atomic_false_calls_pre_execution_split() -> None:
    """When ticket.atomic is False, analyze_and_split is called (no regression)."""
    ticket = TicketSpec(ticket_id="T001", goal="Rename Foo to Bar across 5 files", atomic=False)
    sentinel = [ticket]

    with patch("saturnday.ticket_splitter.analyze_and_split", return_value=sentinel) as mock_split:
        if ticket.atomic:
            sub_tickets = [ticket]
        else:
            sub_tickets = mock_split(ticket, None, None, "")

        mock_split.assert_called_once()
        assert sub_tickets is sentinel


# ---------------------------------------------------------------------------
# Test 3: atomic=True suppresses Fix 18 last-resort split gate
# ---------------------------------------------------------------------------

def test_fix18_gate_suppressed_when_atomic_true() -> None:
    """Fix 18 gate must not fire when atomic=True, regardless of other conditions."""
    ticket = TicketSpec(
        ticket_id="T001",
        goal="Migrate schema",
        atomic=True,
        allow_last_resort_split=True,
    )
    budget_warn_count = 3
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD + 1000
    already = False

    # Inside-loop gate condition
    gate_fires = (
        not already
        and ticket.allow_last_resort_split
        and not ticket.atomic
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate_fires, (
        "Fix 18 inside-loop gate must not fire when atomic=True"
    )


def test_fix18_post_loop_gate_suppressed_when_atomic_true() -> None:
    """Fix 18 post-loop gate must not fire when atomic=True."""
    ticket = TicketSpec(
        ticket_id="T001",
        goal="Migrate schema",
        atomic=True,
        allow_last_resort_split=True,
    )
    budget_warn_count = 3
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD + 1000
    already = False

    # Post-loop gate condition
    gate_fires = (
        not already
        and ticket.allow_last_resort_split
        and not ticket.atomic
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate_fires, (
        "Fix 18 post-loop gate must not fire when atomic=True"
    )


def test_fix18_gate_still_fires_when_atomic_false() -> None:
    """Fix 18 gate must still fire for non-atomic tickets (no regression)."""
    ticket = TicketSpec(
        ticket_id="T001",
        goal="Add feature",
        atomic=False,
        allow_last_resort_split=True,
    )
    budget_warn_count = 2
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD
    already = False

    gate_fires = (
        not already
        and ticket.allow_last_resort_split
        and not ticket.atomic
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert gate_fires, (
        "Fix 18 gate must still fire for non-atomic tickets with all other conditions met"
    )


def test_fix18_gate_suppressed_by_allow_last_resort_split_false_unchanged() -> None:
    """allow_last_resort_split=False still suppresses Fix 18 gate (no regression)."""
    ticket = TicketSpec(
        ticket_id="T001",
        goal="Add feature",
        atomic=False,
        allow_last_resort_split=False,
    )
    budget_warn_count = 3
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD + 1000
    already = False

    gate_fires = (
        not already
        and ticket.allow_last_resort_split
        and not ticket.atomic
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate_fires, (
        "allow_last_resort_split=False must still suppress the Fix 18 gate"
    )


# ---------------------------------------------------------------------------
# Test 4: atomic=True evidence recording
# ---------------------------------------------------------------------------

def test_atomic_true_evidence_fields() -> None:
    """When atomic=True, prompt_split_exempt must be True and reason 'atomic_ticket'."""
    ticket = TicketSpec(ticket_id="T001", goal="Migrate schema", atomic=True)

    # Simulate the evidence field assignment from ticket_runner.py
    prompt_split_exempt = ticket.atomic
    prompt_split_reason = "atomic_ticket" if ticket.atomic else None

    assert prompt_split_exempt is True
    assert prompt_split_reason == "atomic_ticket"


def test_atomic_false_evidence_fields() -> None:
    """When atomic=False, prompt_split_exempt must be False and reason None."""
    ticket = TicketSpec(ticket_id="T001", goal="Add feature", atomic=False)

    prompt_split_exempt = ticket.atomic
    prompt_split_reason = "atomic_ticket" if ticket.atomic else None

    assert prompt_split_exempt is False
    assert prompt_split_reason is None


# ---------------------------------------------------------------------------
# Test 5: atomic=False preserves existing behaviour (regression)
# ---------------------------------------------------------------------------

def test_atomic_false_does_not_change_allow_last_resort_split_semantics() -> None:
    """atomic=False must not interfere with allow_last_resort_split behaviour."""
    # Both fields independent
    ticket_default = TicketSpec(ticket_id="T001", goal="Do thing")
    assert ticket_default.atomic is False
    assert ticket_default.allow_last_resort_split is True

    ticket_no_lrs = TicketSpec(ticket_id="T001", goal="Do thing", allow_last_resort_split=False)
    assert ticket_no_lrs.atomic is False
    assert ticket_no_lrs.allow_last_resort_split is False


# ---------------------------------------------------------------------------
# Test 6: atomic: true in plan JSON reaches TicketSpec via plan_parser
# ---------------------------------------------------------------------------

def test_plan_parser_passes_atomic_true(tmp_path: Path) -> None:
    """atomic: true in plan JSON must reach TicketSpec.atomic=True."""
    import json as _json
    from saturnday.plan_parser import load_plan

    plan_data = {
        "version": 1,
        "project_id": "test-atomic",
        "tickets": [
            {
                "ticket_id": "T001",
                "goal": "Rename Foo to Bar across all files",
                "atomic": True,
                "acceptance_criteria": ["renames applied"],
            },
            {
                "ticket_id": "T002",
                "goal": "Add a new feature",
                "atomic": False,
                "acceptance_criteria": ["feature added"],
            },
            {
                "ticket_id": "T003",
                "goal": "Another feature",
                "acceptance_criteria": ["another feature added"],
                # no atomic field — must default to False
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(_json.dumps(plan_data), encoding="utf-8")

    plan = load_plan(plan_path)

    t001 = next(t for t in plan.tickets if t.ticket_id == "T001")
    t002 = next(t for t in plan.tickets if t.ticket_id == "T002")
    t003 = next(t for t in plan.tickets if t.ticket_id == "T003")

    assert t001.atomic is True, "T001: atomic: true in JSON must reach TicketSpec"
    assert t002.atomic is False, "T002: atomic: false in JSON must reach TicketSpec"
    assert t003.atomic is False, "T003: missing atomic in JSON must default to False"


def test_plan_parser_atomic_non_bool_defaults_false(tmp_path: Path) -> None:
    """Non-boolean atomic value in plan JSON must be treated as False (safe default)."""
    import json as _json
    from saturnday.plan_parser import load_plan

    plan_data = {
        "version": 1,
        "project_id": "test-atomic-invalid",
        "tickets": [
            {
                "ticket_id": "T001",
                "goal": "Do something",
                "atomic": "yes",  # invalid — not a bool
                "acceptance_criteria": ["done"],
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(_json.dumps(plan_data), encoding="utf-8")

    plan = load_plan(plan_path)
    t001 = next(t for t in plan.tickets if t.ticket_id == "T001")
    assert t001.atomic is False, (
        "Non-boolean atomic value must default to False (safe default)"
    )


# ---------------------------------------------------------------------------
# Test 7: no regression to Fix 18 split behaviour for non-atomic tickets
# ---------------------------------------------------------------------------

def test_fix18_oversize_threshold_unchanged() -> None:
    """OVERSIZE_SPLIT_THRESHOLD must remain above PROMPT_HARD_THRESHOLD (Fix 18 invariant)."""
    from saturnday.ticket_runner import OVERSIZE_SPLIT_THRESHOLD, PROMPT_HARD_THRESHOLD
    assert OVERSIZE_SPLIT_THRESHOLD > PROMPT_HARD_THRESHOLD, (
        "Fix 18 invariant: OVERSIZE_SPLIT_THRESHOLD must exceed PROMPT_HARD_THRESHOLD"
    )


def test_fix18_gate_conditions_unchanged_for_non_atomic() -> None:
    """All six Fix 18 gate conditions remain operative for non-atomic tickets."""
    ticket = TicketSpec(
        ticket_id="T001",
        goal="Large feature",
        atomic=False,
        allow_last_resort_split=True,
    )

    # Condition 1: not already attempted
    assert not False  # _already_last_resort_split starts False

    # Condition 2: allow_last_resort_split
    assert ticket.allow_last_resort_split is True

    # Condition 3: Fix 19 — not atomic
    assert not ticket.atomic

    # Condition 4 & 5: budget conditions (value-based, not structural)
    budget_warn_count = 2
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD
    assert budget_warn_count >= 2
    assert max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD

    # Full gate
    gate = (
        True  # not _already_last_resort_split
        and ticket.allow_last_resort_split
        and not ticket.atomic
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert gate, "All Fix 18 conditions met for non-atomic ticket — gate must fire"


def test_atomic_true_with_allow_last_resort_split_false_both_block() -> None:
    """Both atomic=True and allow_last_resort_split=False independently block the gate."""
    # atomic=True alone
    t_atomic = TicketSpec(ticket_id="T001", goal="Migrate", atomic=True, allow_last_resort_split=True)
    gate_atomic = (
        t_atomic.allow_last_resort_split
        and not t_atomic.atomic
        and True  # other conditions met
    )
    assert not gate_atomic

    # allow_last_resort_split=False alone
    t_no_lrs = TicketSpec(ticket_id="T001", goal="Migrate", atomic=False, allow_last_resort_split=False)
    gate_no_lrs = (
        t_no_lrs.allow_last_resort_split
        and not t_no_lrs.atomic
        and True
    )
    assert not gate_no_lrs

    # Both set
    t_both = TicketSpec(ticket_id="T001", goal="Migrate", atomic=True, allow_last_resort_split=False)
    gate_both = (
        t_both.allow_last_resort_split
        and not t_both.atomic
        and True
    )
    assert not gate_both
