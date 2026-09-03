"""Tests for saturnday.run.run_ledger.

Covers:
- ``_normalize_marker()`` matches v3 behaviour
- Consecutive failure tracking triggers stop
- Definition of done evaluation (all_tickets_passed, tickets_applied)
- Phase status transitions (PENDING → IN_PROGRESS → PASS/FAIL)
- Max project tickets enforcement
- Serialization round-trip (to_dict / from_dict)
"""

import json
from pathlib import Path

import pytest

from saturnday.run.run_ledger import (
    DEFAULT_CONSECUTIVE_FAILURE_LIMIT,
    KNOWN_DEFINITION_MARKERS,
    PhaseStatus,
    RunLedger,
    TicketStatus,
    _normalize_marker,
    create_ledger_from_plan,
    write_ledger_snapshot,
)


class TestNormalizeMarker:
    """Must match v3 autopilot.py line 976 exactly."""

    def test_simple_spaces(self) -> None:
        assert _normalize_marker("Tickets Applied") == "tickets_applied"

    def test_all_tickets_passed(self) -> None:
        assert _normalize_marker("All Tickets Passed") == "all_tickets_passed"

    def test_stop_on_compile_error(self) -> None:
        assert _normalize_marker("Stop on Compile Error") == "stop_on_compile_error"

    def test_empty_string(self) -> None:
        assert _normalize_marker("") == ""

    def test_non_string(self) -> None:
        assert _normalize_marker(42) == ""  # type: ignore[arg-type]
        assert _normalize_marker(None) == ""  # type: ignore[arg-type]

    def test_already_normalized(self) -> None:
        assert _normalize_marker("tickets_applied") == "tickets_applied"

    def test_mixed_punctuation(self) -> None:
        assert _normalize_marker("stop---on!!!compile") == "stop_on_compile"

    def test_leading_trailing_underscores(self) -> None:
        assert _normalize_marker("__test__") == "test"

    def test_consecutive_underscores(self) -> None:
        assert _normalize_marker("foo___bar") == "foo_bar"

    def test_unicode_preserved(self) -> None:
        # Python isalnum() considers accented chars alphanumeric
        assert _normalize_marker("café_résumé") == "café_résumé"


class TestConsecutiveFailures:
    def test_three_consecutive_failures_triggers_stop(self) -> None:
        ledger = RunLedger(ticket_ids=("T1", "T2", "T3", "T4"))
        ledger.record_ticket_result("T1", "FAIL")
        assert not ledger.check_stop_conditions()[0]
        ledger.record_ticket_result("T2", "FAIL")
        assert not ledger.check_stop_conditions()[0]
        ledger.record_ticket_result("T3", "FAIL")
        should_stop, reason = ledger.check_stop_conditions()
        assert should_stop
        assert "consecutive_failure_limit" in reason

    def test_pass_resets_consecutive_failures(self) -> None:
        ledger = RunLedger(ticket_ids=("T1", "T2", "T3", "T4"))
        ledger.record_ticket_result("T1", "FAIL")
        ledger.record_ticket_result("T2", "FAIL")
        ledger.record_ticket_result("T3", "PASS")
        assert ledger.consecutive_failures == 0
        assert not ledger.check_stop_conditions()[0]

    def test_custom_failure_limit(self) -> None:
        ledger = RunLedger(
            ticket_ids=("T1", "T2"),
            consecutive_failure_limit=1,
        )
        ledger.record_ticket_result("T1", "FAIL")
        should_stop, _ = ledger.check_stop_conditions()
        assert should_stop

    def test_skip_does_not_reset_counter(self) -> None:
        ledger = RunLedger(ticket_ids=("T1", "T2", "T3", "T4"))
        ledger.record_ticket_result("T1", "FAIL")
        ledger.record_ticket_result("T2", "FAIL")
        ledger.record_ticket_result("T3", "SKIP")
        assert ledger.consecutive_failures == 2


class TestDefinitionOfDone:
    def test_all_tickets_passed_happy_path(self) -> None:
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            ticket_ids=("T1", "T2"),
        )
        ledger.record_ticket_result("T1", "PASS")
        ledger.record_ticket_result("T2", "PASS")
        assert ledger.evaluate_definition_of_done() is True

    def test_all_tickets_passed_with_failure(self) -> None:
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            ticket_ids=("T1", "T2"),
        )
        ledger.record_ticket_result("T1", "PASS")
        ledger.record_ticket_result("T2", "FAIL")
        assert ledger.evaluate_definition_of_done() is False

    def test_tickets_applied(self) -> None:
        """tickets_applied: passes when all tickets executed, regardless of FAIL."""
        ledger = RunLedger(
            definition_of_done=("tickets_applied",),
            ticket_ids=("T1", "T2"),
        )
        ledger.record_ticket_result("T1", "PASS")
        ledger.record_ticket_result("T2", "FAIL")
        # v3 behaviour: tickets_applied is a KNOWN marker but DoD also
        # requires zero failures (v3 line 2496). So this should be False.
        assert ledger.evaluate_definition_of_done() is False

    def test_empty_dod_always_true(self) -> None:
        ledger = RunLedger(
            definition_of_done=(),
            ticket_ids=("T1",),
        )
        ledger.record_ticket_result("T1", "PASS")
        assert ledger.evaluate_definition_of_done() is True

    def test_partial_execution_fails_dod(self) -> None:
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            ticket_ids=("T1", "T2"),
        )
        ledger.record_ticket_result("T1", "PASS")
        # T2 not yet executed
        assert ledger.evaluate_definition_of_done() is False

    def test_unknown_marker_fails_dod(self) -> None:
        ledger = RunLedger(
            definition_of_done=("unknown_marker",),
            ticket_ids=("T1",),
        )
        ledger.record_ticket_result("T1", "PASS")
        # No known marker present
        assert ledger.evaluate_definition_of_done() is False

    def test_no_tickets_fails_dod(self) -> None:
        ledger = RunLedger(definition_of_done=("all_tickets_passed",))
        assert ledger.evaluate_definition_of_done() is False


class TestPhaseStatusTransitions:
    def _make_ledger_with_phase(self) -> RunLedger:
        return RunLedger(
            phases=(("phase-1", "Phase One", ("T1", "T2")),),
            ticket_ids=("T1", "T2"),
        )

    def test_phase_starts_pending(self) -> None:
        ledger = self._make_ledger_with_phase()
        assert ledger.phase_statuses[0].status == "PENDING"

    def test_phase_transitions_to_in_progress(self) -> None:
        ledger = self._make_ledger_with_phase()
        ledger.record_ticket_result("T1", "PASS")
        ledger.update_phase_status("T1")
        assert ledger.phase_statuses[0].status == "IN_PROGRESS"
        assert ledger.phase_statuses[0].started_utc != ""

    def test_phase_passes_when_all_pass(self) -> None:
        ledger = self._make_ledger_with_phase()
        ledger.record_ticket_result("T1", "PASS")
        ledger.update_phase_status("T1")
        ledger.record_ticket_result("T2", "PASS")
        ledger.update_phase_status("T2")
        assert ledger.phase_statuses[0].status == "PASS"
        assert ledger.phase_statuses[0].completed_utc != ""

    def test_phase_fails_when_any_fail(self) -> None:
        ledger = self._make_ledger_with_phase()
        ledger.record_ticket_result("T1", "PASS")
        ledger.update_phase_status("T1")
        ledger.record_ticket_result("T2", "FAIL")
        ledger.update_phase_status("T2")
        assert ledger.phase_statuses[0].status == "FAIL"

    def test_phase_not_terminal_until_all_done(self) -> None:
        ledger = self._make_ledger_with_phase()
        ledger.record_ticket_result("T1", "PASS")
        ledger.update_phase_status("T1")
        # T2 still PENDING
        assert ledger.phase_statuses[0].status == "IN_PROGRESS"

    def test_ticket_not_in_phase(self) -> None:
        ledger = self._make_ledger_with_phase()
        # T99 not in any phase — should not crash
        ledger.record_ticket_result("T99", "PASS")
        ledger.update_phase_status("T99")
        assert ledger.phase_statuses[0].status == "PENDING"


class TestMaxProjectTickets:
    def test_stops_at_limit(self) -> None:
        ledger = RunLedger(
            max_project_tickets=2,
            ticket_ids=("T1", "T2", "T3"),
        )
        ledger.record_ticket_result("T1", "PASS")
        assert not ledger.check_stop_conditions()[0]
        ledger.record_ticket_result("T2", "PASS")
        should_stop, reason = ledger.check_stop_conditions()
        assert should_stop
        assert "max_project_tickets" in reason

    def test_should_skip_after_limit(self) -> None:
        ledger = RunLedger(
            max_project_tickets=1,
            ticket_ids=("T1", "T2"),
        )
        ledger.record_ticket_result("T1", "PASS")
        skip, reason = ledger.should_skip_ticket("T2")
        assert skip
        assert "max_project_tickets" in reason

    def test_no_limit(self) -> None:
        ledger = RunLedger(ticket_ids=("T1", "T2", "T3"))
        ledger.record_ticket_result("T1", "PASS")
        ledger.record_ticket_result("T2", "PASS")
        ledger.record_ticket_result("T3", "PASS")
        assert not ledger.check_stop_conditions()[0]


class TestStopConditionMarkers:
    def test_stop_on_matching_reason(self) -> None:
        ledger = RunLedger(
            stop_conditions=("policy_denied",),
            ticket_ids=("T1",),
        )
        ledger.record_ticket_result("T1", "FAIL", reasons=["policy_denied"])
        should_stop, reason = ledger.check_stop_conditions(
            ticket_id="T1", reasons=["policy_denied"],
        )
        assert should_stop
        assert "stop_condition_met" in reason

    def test_normalized_matching(self) -> None:
        ledger = RunLedger(
            stop_conditions=("Policy Denied",),
            ticket_ids=("T1",),
        )
        ledger.record_ticket_result("T1", "FAIL", reasons=["Policy Denied"])
        should_stop, _ = ledger.check_stop_conditions(
            ticket_id="T1", reasons=["Policy Denied"],
        )
        assert should_stop

    def test_no_match(self) -> None:
        ledger = RunLedger(
            stop_conditions=("policy_denied",),
            ticket_ids=("T1",),
        )
        ledger.record_ticket_result("T1", "FAIL", reasons=["timeout"])
        should_stop, _ = ledger.check_stop_conditions(
            ticket_id="T1", reasons=["timeout"],
        )
        assert not should_stop


class TestShouldSkipTicket:
    def test_skip_after_stop(self) -> None:
        ledger = RunLedger(ticket_ids=("T1", "T2"))
        ledger.stopped = True
        ledger.stop_reason = "test stop"
        skip, reason = ledger.should_skip_ticket("T2")
        assert skip
        assert "Run stopped" in reason

    def test_no_skip_by_default(self) -> None:
        ledger = RunLedger(ticket_ids=("T1",))
        skip, _ = ledger.should_skip_ticket("T1")
        assert not skip


class TestSerialization:
    def test_round_trip(self) -> None:
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=("policy_denied",),
            max_project_tickets=5,
            phases=(("p1", "Phase 1", ("T1", "T2")),),
            ticket_ids=("T1", "T2", "T3"),
        )
        ledger.record_ticket_result("T1", "PASS")
        ledger.update_phase_status("T1")
        ledger.record_ticket_result("T2", "FAIL", failure_category="timeout")

        data = ledger.to_dict()
        restored = RunLedger.from_dict(data)

        assert restored.total_executed == 2
        assert restored.total_passed == 1
        assert restored.total_failed == 1
        assert restored.consecutive_failures == 1
        assert restored.definition_of_done == ("all_tickets_passed",)
        assert restored.stop_conditions == ("policy_denied",)
        assert restored.max_project_tickets == 5
        assert len(restored.phase_statuses) == 1
        assert restored.phase_statuses[0].status == "IN_PROGRESS"
        assert restored.ticket_statuses["T2"].failure_category == "timeout"

    def test_json_serializable(self) -> None:
        ledger = RunLedger(ticket_ids=("T1",))
        ledger.record_ticket_result("T1", "PASS")
        data = ledger.to_dict()
        json_str = json.dumps(data)
        assert isinstance(json_str, str)


class TestWriteLedgerSnapshot:
    def test_writes_to_evidence_dir(self, tmp_path: Path) -> None:
        ledger = RunLedger(ticket_ids=("T1",))
        ledger.record_ticket_result("T1", "PASS")
        path = write_ledger_snapshot(ledger, tmp_path)
        assert path.exists()
        assert path == tmp_path / "evidence" / "run" / "ledger.json"
        data = json.loads(path.read_text())
        assert data["total_passed"] == 1


class TestCreateLedgerFromPlan:
    def test_creates_from_plan_fields(self) -> None:
        from saturnday._types import PhaseDef

        phases = (PhaseDef(phase_id="p1", name="Phase 1", ticket_ids=("T1",)),)
        ledger = create_ledger_from_plan(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=("compile_error",),
            max_project_tickets=10,
            phases=phases,
            ticket_ids=("T1", "T2"),
        )
        assert len(ledger.phase_statuses) == 1
        assert ledger.max_project_tickets == 10
        assert "T1" in ledger.ticket_statuses
        assert "T2" in ledger.ticket_statuses


# ---------------------------------------------------------------------------
# Split cluster consecutive failure counting
# ---------------------------------------------------------------------------

class TestSplitClusterConsecutiveFailures:
    """Sub-tickets from the same parent count as one failure signal."""

    def _make_ledger(self, limit: int = 3) -> RunLedger:
        return RunLedger(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=(),
            max_project_tickets=None,
            consecutive_failure_limit=limit,
            phases=[],
            ticket_ids=(),
        )

    def test_three_sub_failures_same_parent_count_as_one(self) -> None:
        """T044.a, T044.b, T044.c all FAIL under parent T044 → consecutive_failures = 1."""
        ledger = self._make_ledger(limit=3)
        ledger.record_ticket_result("T044.a", "FAIL", parent_ticket_id="T044")
        ledger.record_ticket_result("T044.b", "FAIL", parent_ticket_id="T044")
        ledger.record_ticket_result("T044.c", "FAIL", parent_ticket_id="T044")
        assert ledger.consecutive_failures == 1
        should_stop, _ = ledger.check_stop_conditions()
        assert not should_stop, "One parent failing should not trigger stop at limit=3"

    def test_three_different_parents_failing_triggers_stop(self) -> None:
        """T044, T045, T046 each fail independently → consecutive_failures = 3 → stop."""
        ledger = self._make_ledger(limit=3)
        ledger.record_ticket_result("T044", "FAIL", parent_ticket_id="T044")
        ledger.record_ticket_result("T045", "FAIL", parent_ticket_id="T045")
        ledger.record_ticket_result("T046", "FAIL", parent_ticket_id="T046")
        assert ledger.consecutive_failures == 3
        should_stop, _ = ledger.check_stop_conditions()
        assert should_stop, "Three independent parent failures must trigger stop"

    def test_pass_resets_counter_between_split_clusters(self) -> None:
        """A PASS between two failing parents resets the counter."""
        ledger = self._make_ledger(limit=3)
        ledger.record_ticket_result("T044.a", "FAIL", parent_ticket_id="T044")
        ledger.record_ticket_result("T044.b", "FAIL", parent_ticket_id="T044")
        assert ledger.consecutive_failures == 1
        ledger.record_ticket_result("T045", "PASS")
        assert ledger.consecutive_failures == 0
        ledger.record_ticket_result("T046.a", "FAIL", parent_ticket_id="T046")
        assert ledger.consecutive_failures == 1

    def test_coded_ungoverned_resets_counter(self) -> None:
        """CODED_UNGOVERNED still resets the counter."""
        ledger = self._make_ledger(limit=3)
        ledger.record_ticket_result("T044", "FAIL", parent_ticket_id="T044")
        assert ledger.consecutive_failures == 1
        ledger.record_ticket_result("T045", "CODED_UNGOVERNED")
        assert ledger.consecutive_failures == 0

    def test_split_cluster_then_independent_fail_counts_correctly(self) -> None:
        """Split cluster (one signal) + independent fail (second signal) = 2."""
        ledger = self._make_ledger(limit=3)
        # Split cluster: T044.a, T044.b both fail → one signal
        ledger.record_ticket_result("T044.a", "FAIL", parent_ticket_id="T044")
        ledger.record_ticket_result("T044.b", "FAIL", parent_ticket_id="T044")
        assert ledger.consecutive_failures == 1
        # Independent ticket fails → second signal
        ledger.record_ticket_result("T045", "FAIL", parent_ticket_id="T045")
        assert ledger.consecutive_failures == 2
        should_stop, _ = ledger.check_stop_conditions()
        assert not should_stop, "Only 2 parent failures, limit is 3"
        # Third independent parent fails → third signal → stop
        ledger.record_ticket_result("T046", "FAIL", parent_ticket_id="T046")
        assert ledger.consecutive_failures == 3
        should_stop, _ = ledger.check_stop_conditions()
        assert should_stop

    def test_no_parent_id_treated_as_own_parent(self) -> None:
        """Without parent_ticket_id, each ticket is its own parent (backward compat)."""
        ledger = self._make_ledger(limit=3)
        ledger.record_ticket_result("T001", "FAIL")
        ledger.record_ticket_result("T002", "FAIL")
        ledger.record_ticket_result("T003", "FAIL")
        assert ledger.consecutive_failures == 3
        should_stop, _ = ledger.check_stop_conditions()
        assert should_stop

    def test_serialization_preserves_last_failure_parent(self) -> None:
        """to_dict / from_dict round-trips _last_failure_parent."""
        ledger = self._make_ledger(limit=3)
        ledger.record_ticket_result("T044.a", "FAIL", parent_ticket_id="T044")
        data = ledger.to_dict()
        assert data["_last_failure_parent"] == "T044"
        restored = RunLedger.from_dict(data)
        assert restored._last_failure_parent == "T044"
        assert restored.consecutive_failures == 1
