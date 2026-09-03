"""Phase 7 — status/summary honesty for proof resolution.

Verifies that the ``RunResult.proof_resolution_{status,source,narrative}``
triple is populated truthfully at every transition:

  - plan load: derives initial triple from the plan's persisted
    ``proof_resolution_source`` and the shape of ``local_proof_cmd`` /
    ``acceptance_cmd``.
  - _record_proof_pass: stamps status="passed"; source preserved.
  - _record_proof_failure: stamps status="failed"; source preserved.
  - _apply_dod_mechanical_guard: downgrades DOD_MET when the proof was
    never resolved (``unresolved_gap``).
  - plan JSON round-trip: ``proof_resolution_source`` survives
    serialise/deserialise.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from saturnday._types import ProjectPlan, RunResult, TicketResult, TicketSpec
from saturnday.ticket_runner import (
    _apply_dod_mechanical_guard,
    _initial_proof_resolution,
    _record_proof_failure,
    _record_proof_pass,
    _update_proof_resolution,
)


def _empty_result() -> RunResult:
    return RunResult(
        project_id="p",
        total_tickets=1,
        passed=1,
        failed=0,
        skipped=0,
        coded_ungoverned=0,
        ticket_results=(),
        definition_of_done_met=True,
    )


def _plan(**kw) -> ProjectPlan:
    base = dict(
        version=1,
        project_id="p",
        tickets=(TicketSpec(ticket_id="T001", goal="g"),),
        operating_mode="library",
        dependency_profile="self_contained",
        proof_realism="production_intent",
        testing_strategy="unspecified",
        local_proof_cmd='python -c "from src.m import f; assert f(1) == 2"',
        proof_resolution_source="planner_heuristic",
    )
    base.update(kw)
    return ProjectPlan(**base)


# ---------------------------------------------------------------------------
# _initial_proof_resolution
# ---------------------------------------------------------------------------


class TestInitialProofResolution:
    def test_legacy_no_acceptance_is_not_attempted(self) -> None:
        p = _plan(
            operating_mode="legacy_unclassified",
            local_proof_cmd="",
            acceptance_cmd="",
            proof_resolution_source="none",
        )
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "not_attempted"
        assert source == "none"
        assert "No proof command defined" in narrative

    def test_declared_no_local_proof_is_not_attempted(self) -> None:
        p = _plan(local_proof_cmd="", proof_resolution_source="none")
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "not_attempted"
        assert source == "none"

    def test_gap_marker_preserves_unresolved_gap(self) -> None:
        p = _plan(
            local_proof_cmd=(
                "python -c \"import sys; sys.exit('FIX73_PROOF_GAP: worker')\""
            ),
            proof_resolution_source="planner_gap",
        )
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "unresolved_gap"
        assert source == "planner_gap"
        assert "FIX73_PROOF_GAP" in narrative

    def test_planner_heuristic_maps_to_resolved_from_planning(self) -> None:
        p = _plan(
            local_proof_cmd="python -m mypipeline",
            proof_resolution_source="planner_heuristic",
        )
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "resolved_from_planning"
        assert source == "planner_heuristic"

    def test_coder_plan_time_preserved(self) -> None:
        p = _plan(
            local_proof_cmd='python -c "assert 1"',
            proof_resolution_source="coder_plan_time",
        )
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "resolved_coder_plan_time"
        assert source == "coder_plan_time"
        assert "coder at plan time" in narrative

    def test_operator_supplied_preserved(self) -> None:
        p = _plan(
            local_proof_cmd='python -c "assert 1"',
            proof_resolution_source="operator_supplied",
        )
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "resolved_from_planning"
        assert source == "operator_supplied"

    def test_operator_edited_maps_to_resolved_operator(self) -> None:
        p = _plan(
            local_proof_cmd='python -c "assert 1"',
            proof_resolution_source="operator_edited",
        )
        status, source, narrative = _initial_proof_resolution(p)
        assert status == "resolved_operator"
        assert source == "operator_edited"


# ---------------------------------------------------------------------------
# _record_proof_pass / _record_proof_failure stamp status + preserve source
# ---------------------------------------------------------------------------


class TestRecordProofHelpers:
    def test_record_pass_stamps_passed_and_preserves_source(self) -> None:
        r = _update_proof_resolution(
            _empty_result(),
            status="resolved_coder_plan_time",
            source="coder_plan_time",
            narrative="Derived at plan time.",
        )
        r2 = _record_proof_pass(r, is_legacy=False)
        assert r2.proof_resolution_status == "passed"
        assert r2.proof_resolution_source == "coder_plan_time"
        assert "Derived at plan time" in r2.proof_resolution_narrative
        assert "ran and passed" in r2.proof_resolution_narrative

    def test_record_failure_stamps_failed_and_preserves_source(self) -> None:
        r = _update_proof_resolution(
            _empty_result(),
            status="resolved_from_planning",
            source="planner_heuristic",
            narrative="Pipeline template.",
        )
        r2 = _record_proof_failure(
            r, is_legacy=False, attempted=True, failure="assert",
        )
        assert r2.proof_resolution_status == "failed"
        assert r2.proof_resolution_source == "planner_heuristic"
        assert r2.local_proof_passed is False
        assert "ran and failed" in r2.proof_resolution_narrative

    def test_record_failure_not_attempted_sets_different_narrative(self) -> None:
        r = _record_proof_failure(
            _empty_result(), is_legacy=False, attempted=False,
            failure="setup declined",
        )
        assert r.proof_resolution_status == "failed"
        assert "could not be executed" in r.proof_resolution_narrative


# ---------------------------------------------------------------------------
# _apply_dod_mechanical_guard — unresolved_gap downgrade
# ---------------------------------------------------------------------------


class TestDodGuardUnresolvedGap:
    def test_unresolved_gap_downgrades_dod_met(self) -> None:
        r = replace(
            _empty_result(),
            proof_resolution_status="unresolved_gap",
            proof_resolution_source="planner_gap",
        )
        dod_met, cls, reasons = _apply_dod_mechanical_guard(
            dod_met=True, dod_classification="DOD_MET", run_result=r,
        )
        assert dod_met is False
        assert cls == "DOD_NOT_MET"
        assert any("FIX73_PROOF_GAP" in r for r in reasons)

    def test_passed_status_does_not_downgrade(self) -> None:
        r = replace(
            _empty_result(),
            proof_resolution_status="passed",
            proof_resolution_source="coder_plan_time",
            local_proof_attempted=True,
            local_proof_passed=True,
        )
        dod_met, cls, reasons = _apply_dod_mechanical_guard(
            dod_met=True, dod_classification="DOD_MET", run_result=r,
        )
        assert dod_met is True
        assert reasons == []

    def test_guard_does_not_upgrade_when_dod_already_not_met(self) -> None:
        r = replace(
            _empty_result(),
            proof_resolution_status="unresolved_gap",
            proof_resolution_source="planner_gap",
            definition_of_done_met=False,
        )
        dod_met, cls, reasons = _apply_dod_mechanical_guard(
            dod_met=False, dod_classification="DOD_NOT_MET", run_result=r,
        )
        # Already DOD_NOT_MET — guard returns unchanged (no-op).
        assert dod_met is False
        assert cls == "DOD_NOT_MET"
        assert reasons == []


# ---------------------------------------------------------------------------
# Plan JSON round-trip — proof_resolution_source persists
# ---------------------------------------------------------------------------


class TestPlanRoundTrip:
    def test_parse_known_source(self) -> None:
        from saturnday.plan_parser import _parse_proof_resolution_source
        assert _parse_proof_resolution_source(
            {"proof_resolution_source": "coder_plan_time"}
        ) == "coder_plan_time"

    def test_parse_unknown_source_falls_back(self) -> None:
        from saturnday.plan_parser import _parse_proof_resolution_source
        assert _parse_proof_resolution_source(
            {"proof_resolution_source": "garbage"}
        ) == "none"

    def test_parse_missing_field_defaults_to_none(self) -> None:
        from saturnday.plan_parser import _parse_proof_resolution_source
        assert _parse_proof_resolution_source({}) == "none"

    def test_plan_object_default_is_none(self) -> None:
        p = ProjectPlan(version=1, project_id="p")
        assert p.proof_resolution_source == "none"


# ---------------------------------------------------------------------------
# _update_proof_resolution — pure helper
# ---------------------------------------------------------------------------


class TestUpdateProofResolution:
    def test_updates_only_provided_fields(self) -> None:
        r = replace(
            _empty_result(),
            proof_resolution_status="resolved_from_planning",
            proof_resolution_source="planner_heuristic",
            proof_resolution_narrative="start",
        )
        r2 = _update_proof_resolution(r, status="passed")
        assert r2.proof_resolution_status == "passed"
        assert r2.proof_resolution_source == "planner_heuristic"
        assert r2.proof_resolution_narrative == "start"

    def test_noop_when_all_none(self) -> None:
        r = _empty_result()
        assert _update_proof_resolution(r) is r
