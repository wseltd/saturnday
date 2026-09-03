"""Fix 74 — failed plan-level acceptance_cmd cannot be DoD-overridden.

Pre-Fix-74 the mechanical guard at ticket_runner.py:1218 only consulted
per-ticket ``verify_cmd_passed`` (Fix 39).  A failed plan-level
``acceptance_cmd`` paired with an LLM ``DOD_MET`` verdict could therefore
upgrade ``dod_met=True`` and remain unchallenged.

The extracted ``_apply_dod_mechanical_guard`` helper now downgrades on either
failure path.  These tests exercise the helper directly so the decision is
visible without mocking the LLM DoD pass or write_run_summary.
"""

from __future__ import annotations

from saturnday._types import RunResult, TicketResult
from saturnday.ticket_runner import _apply_dod_mechanical_guard


def _tr(
    verify_cmd_passed: bool | None = None,
    verify_cmd_specified: bool = False,
) -> TicketResult:
    return TicketResult(
        ticket_id="T",
        disposition="PASS",
        verify_cmd_specified=verify_cmd_specified,
        verify_cmd_passed=verify_cmd_passed,
    )


def _rr(
    *,
    ticket_results: tuple[TicketResult, ...] = (),
    acceptance_cmd_passed: bool | None = None,
) -> RunResult:
    return RunResult(
        project_id="p",
        ticket_results=ticket_results,
        acceptance_cmd_passed=acceptance_cmd_passed,
    )


# ---------------------------------------------------------------------------
# Downgrade fires (the new Fix 74 case)
# ---------------------------------------------------------------------------


def test_failed_acceptance_cmd_with_dod_met_is_downgraded() -> None:
    """Fix 74 closure: plan-level acceptance failure must override LLM DOD_MET."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=True),),
        acceptance_cmd_passed=False,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert reasons == ["plan-level acceptance_cmd failed"]


def test_failed_acceptance_cmd_with_classification_only_is_downgraded() -> None:
    """Even when mechanical dod_met is False but LLM said DOD_MET, downgrade fires."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=True),),
        acceptance_cmd_passed=False,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=False, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert reasons == ["plan-level acceptance_cmd failed"]


# ---------------------------------------------------------------------------
# Existing Fix 39 path remains intact
# ---------------------------------------------------------------------------


def test_failed_verify_cmd_still_downgrades_fix39() -> None:
    """Pre-existing Fix 39 path must continue to fire."""
    rr = _rr(
        ticket_results=(
            _tr(verify_cmd_passed=False),
            _tr(verify_cmd_passed=True),
        ),
        acceptance_cmd_passed=True,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert reasons == ["1 ticket(s) failed verify_cmd"]


def test_both_failures_report_both_reasons() -> None:
    """When both proofs fail, both reasons are surfaced (no silent collapse)."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=False), _tr(verify_cmd_passed=False)),
        acceptance_cmd_passed=False,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert reasons == [
        "2 ticket(s) failed verify_cmd",
        "plan-level acceptance_cmd failed",
    ]


# ---------------------------------------------------------------------------
# No-op cases — guard must not fire
# ---------------------------------------------------------------------------


def test_no_proof_failures_no_downgrade() -> None:
    """When no executable proof failed, the guard is silent."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=True),),
        acceptance_cmd_passed=True,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is True
    assert new_class == "DOD_MET"
    assert reasons == []


def test_unspecified_acceptance_cmd_does_not_trigger() -> None:
    """``acceptance_cmd_passed is None`` (no acceptance_cmd in plan) must NOT
    be conflated with a failure — only an explicit ``False`` triggers."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=True),),
        acceptance_cmd_passed=None,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is True
    assert new_class == "DOD_MET"
    assert reasons == []


def test_dod_already_not_met_no_op() -> None:
    """If DoD already says NOT_MET, the guard does not need to fire (reasons empty).
    Defensive: the caller treats empty reasons as "no log/replace needed"."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=False),),
        acceptance_cmd_passed=False,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=False, dod_classification="DOD_NOT_MET", run_result=rr,
    )
    assert reasons == []
    # Returned values reflect input (no spurious mutation).
    assert new_met is False
    assert new_class == "DOD_NOT_MET"


def test_dod_partial_with_failed_acceptance_does_not_falsely_promote() -> None:
    """DOD_PARTIAL with a failed acceptance_cmd: guard does not fire because
    the LLM verdict is not DOD_MET and dod_met is False.  The caller's
    DOD_PARTIAL handling is unchanged."""
    rr = _rr(
        ticket_results=(_tr(verify_cmd_passed=True),),
        acceptance_cmd_passed=False,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=False, dod_classification="DOD_PARTIAL", run_result=rr,
    )
    assert reasons == []
    assert new_met is False
    assert new_class == "DOD_PARTIAL"


# ---------------------------------------------------------------------------
# Fix 72b — skipped verify_cmd must be distinguishable from unspecified
# ---------------------------------------------------------------------------


def test_specified_but_skipped_verify_cmd_triggers_downgrade() -> None:
    """Fix 72b: a ticket whose verify_cmd was specified but did not run
    (e.g. ticket landed CODED_UNGOVERNED before reaching the success branch)
    must NOT be conflated with a ticket that had no verify_cmd at all.

    The guard fails closed: the mechanical proof was lost, DoD must downgrade.
    """
    rr = _rr(
        ticket_results=(
            _tr(verify_cmd_specified=True, verify_cmd_passed=None),
        ),
        acceptance_cmd_passed=True,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert reasons == ["1 ticket(s) had verify_cmd specified but skipped"]


def test_unspecified_verify_cmd_does_not_trigger_skipped_path() -> None:
    """A ticket with no verify_cmd in the plan (specified=False, passed=None)
    must NOT be flagged as skipped.  Only specified-but-skipped triggers."""
    rr = _rr(
        ticket_results=(
            _tr(verify_cmd_specified=False, verify_cmd_passed=None),
        ),
        acceptance_cmd_passed=True,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert reasons == []
    assert new_met is True
    assert new_class == "DOD_MET"


def test_specified_and_passed_does_not_trigger_skipped_path() -> None:
    """Specified AND passed (verify_cmd ran successfully) must not trigger."""
    rr = _rr(
        ticket_results=(
            _tr(verify_cmd_specified=True, verify_cmd_passed=True),
        ),
        acceptance_cmd_passed=True,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert reasons == []
    assert new_met is True
    assert new_class == "DOD_MET"


def test_skipped_and_failed_acceptance_report_both_reasons() -> None:
    """When both a skipped verify_cmd and a failed acceptance_cmd occur, the
    guard surfaces both reasons (no silent collapse)."""
    rr = _rr(
        ticket_results=(
            _tr(verify_cmd_specified=True, verify_cmd_passed=None),
        ),
        acceptance_cmd_passed=False,
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert reasons == [
        "1 ticket(s) had verify_cmd specified but skipped",
        "plan-level acceptance_cmd failed",
    ]
