"""Tests for saturnday._exceptions."""

from saturnday._exceptions import (
    CloudCoreError,
    CoderAPIError,
    GovernanceError,
    PatchApplicationError,
    PatchExtractionError,
    PlanValidationError,
    TicketFailedError,
)


def test_all_inherit_from_base() -> None:
    """Every exception should be catchable via CloudCoreError."""
    subclasses = [
        PlanValidationError(["err"]),
        CoderAPIError("fail"),
        PatchExtractionError("fail"),
        PatchApplicationError("fail"),
        GovernanceError("fail"),
        TicketFailedError("T001", 2),
    ]
    for exc in subclasses:
        assert isinstance(exc, CloudCoreError)


def test_plan_validation_error_stores_errors() -> None:
    errors = ["missing field", "bad type"]
    exc = PlanValidationError(errors)
    assert exc.errors == errors
    assert "missing field" in str(exc)
    assert "bad type" in str(exc)


def test_coder_api_error_stores_metadata() -> None:
    exc = CoderAPIError("timeout", http_status=504, raw_body="gateway error")
    assert exc.http_status == 504
    assert exc.raw_body == "gateway error"
    assert "timeout" in str(exc)


def test_coder_api_error_defaults() -> None:
    exc = CoderAPIError("generic fail")
    assert exc.http_status is None
    assert exc.raw_body == ""


def test_ticket_failed_error_stores_context() -> None:
    exc = TicketFailedError("T042", 3)
    assert exc.ticket_id == "T042"
    assert exc.attempt == 3
    assert "T042" in str(exc)
    assert "3" in str(exc)
