"""V3 behavioural parity tests.

These are NOT unit tests — they verify that the cloud runner's ledger
produces identical outcomes to v3's autopilot.py on the same inputs.

Golden fixtures are derived from v3's 9970+ production runs.
"""

import json

import pytest

from saturnday.run.run_ledger import (
    KNOWN_DEFINITION_MARKERS,
    RunLedger,
    _normalize_marker,
)


# ---------------------------------------------------------------------------
# Golden fixtures: known v3 marker normalizations
# ---------------------------------------------------------------------------

V3_MARKER_CASES = [
    ("Tickets Applied", "tickets_applied"),
    ("All Tickets Passed", "all_tickets_passed"),
    ("Stop on Compile Error", "stop_on_compile_error"),
    ("policy_denied", "policy_denied"),
    ("REVIEW_FAILED", "review_failed"),
    ("verification_failed", "verification_failed"),
    ("ticket_failure", "ticket_failure"),
    ("  leading spaces  ", "leading_spaces"),
    ("MiXeD---CaSe!!!Stuff", "mixed_case_stuff"),
    ("", ""),
]


class TestV3MarkerParity:
    """Verify _normalize_marker produces identical slugs to v3."""

    @pytest.mark.parametrize("raw,expected", V3_MARKER_CASES)
    def test_normalize_marker_matches_v3(self, raw: str, expected: str) -> None:
        assert _normalize_marker(raw) == expected

    def test_known_markers_match_v3(self) -> None:
        assert KNOWN_DEFINITION_MARKERS == {"tickets_applied", "all_tickets_passed"}


# ---------------------------------------------------------------------------
# Golden fixture: v3 stop behaviour on 3 consecutive failures
# ---------------------------------------------------------------------------

class TestV3StopBehaviour:
    """Verify stop conditions match v3 autopilot output."""

    def test_3_consecutive_failures_stops_run(self) -> None:
        """v3 stops after 3 consecutive failures (default limit)."""
        ledger = RunLedger(ticket_ids=("T001", "T002", "T003", "T004", "T005"))
        # Simulate: T001 PASS, T002 FAIL, T003 FAIL, T004 FAIL → stop
        ledger.record_ticket_result("T001", "PASS")
        assert not ledger.check_stop_conditions()[0]

        ledger.record_ticket_result("T002", "FAIL")
        assert not ledger.check_stop_conditions()[0]

        ledger.record_ticket_result("T003", "FAIL")
        assert not ledger.check_stop_conditions()[0]

        ledger.record_ticket_result("T004", "FAIL")
        should_stop, reason = ledger.check_stop_conditions()
        assert should_stop
        assert "consecutive_failure_limit" in reason
        assert ledger.consecutive_failures == 3

    def test_pass_resets_consecutive_window(self) -> None:
        """v3: a PASS resets the consecutive failure counter to zero."""
        ledger = RunLedger(ticket_ids=("T1", "T2", "T3", "T4", "T5", "T6"))
        ledger.record_ticket_result("T1", "FAIL")
        ledger.record_ticket_result("T2", "FAIL")
        # Reset
        ledger.record_ticket_result("T3", "PASS")
        assert ledger.consecutive_failures == 0
        # Start new window
        ledger.record_ticket_result("T4", "FAIL")
        ledger.record_ticket_result("T5", "FAIL")
        assert not ledger.check_stop_conditions()[0]
        # Only 2 consecutive, not 3

    def test_stop_condition_marker_match(self) -> None:
        """v3 line 2029: normalized ticket reasons matched against stop conditions."""
        ledger = RunLedger(
            stop_conditions=("policy_denied", "review_failed"),
            ticket_ids=("T001",),
        )
        ledger.record_ticket_result(
            "T001", "FAIL",
            reasons=["Policy Denied", "some_other_reason"],
        )
        should_stop, reason = ledger.check_stop_conditions(
            ticket_id="T001",
            reasons=["Policy Denied", "some_other_reason"],
        )
        assert should_stop
        assert "policy_denied" in reason


# ---------------------------------------------------------------------------
# Golden fixture: v3 definition of done evaluation
# ---------------------------------------------------------------------------

class TestV3DefinitionOfDone:
    """Verify DoD evaluation matches v3 autopilot.py line 2496."""

    def test_all_pass_with_known_marker(self) -> None:
        """v3: DoD met when all executed AND zero failed AND known marker."""
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            ticket_ids=("T001", "T002", "T003"),
        )
        for tid in ("T001", "T002", "T003"):
            ledger.record_ticket_result(tid, "PASS")
        assert ledger.evaluate_definition_of_done() is True

    def test_one_failure_blocks_dod(self) -> None:
        """v3: any FAIL blocks DoD."""
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            ticket_ids=("T001", "T002"),
        )
        ledger.record_ticket_result("T001", "PASS")
        ledger.record_ticket_result("T002", "FAIL")
        assert ledger.evaluate_definition_of_done() is False

    def test_incomplete_execution_blocks_dod(self) -> None:
        """v3: DoD requires all tickets executed."""
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            ticket_ids=("T001", "T002", "T003"),
        )
        ledger.record_ticket_result("T001", "PASS")
        ledger.record_ticket_result("T002", "PASS")
        # T003 not executed
        assert ledger.evaluate_definition_of_done() is False

    def test_no_dod_defaults_to_true(self) -> None:
        """v3: empty definition_of_done → definition_met defaults to True."""
        ledger = RunLedger(
            definition_of_done=(),
            ticket_ids=("T001",),
        )
        ledger.record_ticket_result("T001", "PASS")
        assert ledger.evaluate_definition_of_done() is True


# ---------------------------------------------------------------------------
# Golden fixture: v3 phase status transitions
# ---------------------------------------------------------------------------

class TestV3PhaseTransitions:
    """Verify phase transitions match v3 autopilot.py lines 2011-2027."""

    def test_phase_lifecycle(self) -> None:
        """Full lifecycle: PENDING → IN_PROGRESS → PASS."""
        ledger = RunLedger(
            phases=(("phase-1", "Build", ("T001", "T002")),),
            ticket_ids=("T001", "T002"),
        )
        assert ledger.phase_statuses[0].status == "PENDING"

        ledger.record_ticket_result("T001", "PASS")
        ledger.update_phase_status("T001")
        assert ledger.phase_statuses[0].status == "IN_PROGRESS"

        ledger.record_ticket_result("T002", "PASS")
        ledger.update_phase_status("T002")
        assert ledger.phase_statuses[0].status == "PASS"

    def test_phase_fails_if_any_ticket_fails(self) -> None:
        """v3: phase is FAIL if any ticket fails, even with some PASS."""
        ledger = RunLedger(
            phases=(("phase-1", "Build", ("T001", "T002", "T003")),),
            ticket_ids=("T001", "T002", "T003"),
        )
        ledger.record_ticket_result("T001", "PASS")
        ledger.update_phase_status("T001")
        ledger.record_ticket_result("T002", "FAIL")
        ledger.update_phase_status("T002")
        ledger.record_ticket_result("T003", "PASS")
        ledger.update_phase_status("T003")
        assert ledger.phase_statuses[0].status == "FAIL"

    def test_multi_phase_independence(self) -> None:
        """v3: phases track independently."""
        ledger = RunLedger(
            phases=(
                ("phase-1", "Build", ("T001",)),
                ("phase-2", "Test", ("T002",)),
            ),
            ticket_ids=("T001", "T002"),
        )
        ledger.record_ticket_result("T001", "PASS")
        ledger.update_phase_status("T001")
        assert ledger.phase_statuses[0].status == "PASS"
        assert ledger.phase_statuses[1].status == "PENDING"

        ledger.record_ticket_result("T002", "FAIL")
        ledger.update_phase_status("T002")
        assert ledger.phase_statuses[1].status == "FAIL"


# ---------------------------------------------------------------------------
# Golden fixture: max_project_tickets enforcement
# ---------------------------------------------------------------------------

class TestV3MaxProjectTickets:
    """Verify max_project_tickets matches v3 autopilot.py lines 1633-1695."""

    def test_silent_stop_at_limit(self) -> None:
        """v3: exceeding max_project_tickets causes a silent break."""
        ledger = RunLedger(
            max_project_tickets=2,
            ticket_ids=("T001", "T002", "T003"),
        )
        ledger.record_ticket_result("T001", "PASS")
        assert not ledger.should_skip_ticket("T002")[0]

        ledger.record_ticket_result("T002", "PASS")
        skip, reason = ledger.should_skip_ticket("T003")
        assert skip
        assert "max_project_tickets" in reason

    def test_none_means_no_limit(self) -> None:
        """v3: None or invalid max_project_tickets defaults to no limit."""
        ledger = RunLedger(
            max_project_tickets=None,
            ticket_ids=("T001", "T002", "T003"),
        )
        for tid in ("T001", "T002", "T003"):
            ledger.record_ticket_result(tid, "PASS")
            assert not ledger.should_skip_ticket(tid)[0]


# ---------------------------------------------------------------------------
# Golden fixture: ledger persistence round-trip
# ---------------------------------------------------------------------------

class TestV3LedgerPersistence:
    """Verify ledger serialization preserves all state for resume."""

    def test_full_state_round_trip(self) -> None:
        ledger = RunLedger(
            definition_of_done=("all_tickets_passed",),
            stop_conditions=("policy_denied",),
            max_project_tickets=10,
            phases=(
                ("phase-1", "Build", ("T001", "T002")),
                ("phase-2", "Test", ("T003",)),
            ),
            ticket_ids=("T001", "T002", "T003"),
        )

        ledger.record_ticket_result("T001", "PASS")
        ledger.update_phase_status("T001")
        ledger.record_ticket_result("T002", "FAIL", failure_category="timeout")
        ledger.update_phase_status("T002")

        data = ledger.to_dict()
        json_str = json.dumps(data)
        restored_data = json.loads(json_str)
        restored = RunLedger.from_dict(restored_data)

        assert restored.total_executed == ledger.total_executed
        assert restored.total_passed == ledger.total_passed
        assert restored.total_failed == ledger.total_failed
        assert restored.consecutive_failures == ledger.consecutive_failures
        assert restored.max_project_tickets == ledger.max_project_tickets
        assert len(restored.phase_statuses) == 2
        assert restored.phase_statuses[0].status == "FAIL"
        assert restored.ticket_statuses["T002"].failure_category == "timeout"
