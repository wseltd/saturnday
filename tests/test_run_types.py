"""Tests for saturnday._types."""

import pytest

from saturnday._types import (
    CoderConfig,
    ProjectPlan,
    RunResult,
    TicketResult,
    TicketScope,
    TicketSpec,
)


def test_coder_config_defaults() -> None:
    cfg = CoderConfig(backend="openai")
    assert cfg.backend == "openai"
    assert cfg.temperature == 0.0
    assert cfg.max_tokens == 16384
    assert cfg.timeout_s == 1200


def test_coder_config_is_frozen() -> None:
    cfg = CoderConfig(backend="codex-cli")
    with pytest.raises(AttributeError):
        cfg.backend = "claude-cli"  # type: ignore[misc]


def test_ticket_scope_defaults() -> None:
    scope = TicketScope()
    assert scope.allowed_globs == ("**",)
    assert scope.forbidden_globs == ()
    assert scope.max_files_changed == 20


def test_ticket_spec_minimal() -> None:
    spec = TicketSpec(ticket_id="T001", goal="Create scaffold")
    assert spec.ticket_id == "T001"
    assert spec.dependencies == ()
    assert spec.verify_cmd == ""


def test_project_plan_immutable() -> None:
    plan = ProjectPlan(version=1, project_id="test")
    with pytest.raises(AttributeError):
        plan.version = 2  # type: ignore[misc]


def test_ticket_result_fields() -> None:
    result = TicketResult(
        ticket_id="T001",
        disposition="PASS",
        attempts=1,
        changed_files=("src/foo.py",),
        governance_disposition="PASS",
    )
    assert result.disposition == "PASS"
    assert result.changed_files == ("src/foo.py",)


def test_run_result_totals() -> None:
    result = RunResult(
        project_id="test",
        total_tickets=3,
        passed=2,
        failed=1,
        skipped=0,
    )
    assert result.passed + result.failed + result.skipped == result.total_tickets
