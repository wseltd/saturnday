"""Tests for saturnday.plan_parser."""

import json
import logging
import tempfile
from pathlib import Path

import pytest

from saturnday._exceptions import PlanValidationError
from saturnday._types import ProjectPlan
from saturnday.plan_parser import load_plan, resolve_ticket_order, validate_plan


def _write_plan(plan_data: dict, dir_path: Path) -> Path:
    """Helper: write a plan dict to a temp JSON file."""
    path = dir_path / "plan.json"
    path.write_text(json.dumps(plan_data), encoding="utf-8")
    return path


def _minimal_plan() -> dict:
    return {
        "version": 1,
        "project_id": "test-project",
        "tickets": [
            {"ticket_id": "T001", "goal": "Create scaffold",
             "acceptance_criteria": ["scaffold exists"]},
            {"ticket_id": "T002", "goal": "Add config",
             "dependencies": ["T001"],
             "acceptance_criteria": ["config exists"]},
        ],
    }


class TestLoadPlan:
    def test_loads_valid_plan(self, tmp_path: Path) -> None:
        path = _write_plan(_minimal_plan(), tmp_path)
        plan = load_plan(path)
        assert isinstance(plan, ProjectPlan)
        assert plan.project_id == "test-project"
        assert len(plan.tickets) == 2

    def test_dependency_order(self, tmp_path: Path) -> None:
        path = _write_plan(_minimal_plan(), tmp_path)
        plan = load_plan(path)
        ids = [t.ticket_id for t in plan.tickets]
        assert ids.index("T001") < ids.index("T002")

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_plan("/nonexistent/plan.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json!", encoding="utf-8")
        with pytest.raises(PlanValidationError, match="Invalid JSON"):
            load_plan(path)

    def test_missing_tickets_raises(self, tmp_path: Path) -> None:
        path = _write_plan({"version": 1}, tmp_path)
        with pytest.raises(PlanValidationError, match="tickets"):
            load_plan(path)


class TestValidatePlan:
    def test_valid_plan_no_errors(self) -> None:
        errors = validate_plan(_minimal_plan())
        assert errors == []

    def test_empty_tickets_error(self) -> None:
        errors = validate_plan({"tickets": []})
        assert any("no tickets" in e.lower() for e in errors)

    def test_duplicate_ids_error(self) -> None:
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "A"},
                {"ticket_id": "T001", "goal": "B"},
            ],
        }
        errors = validate_plan(plan)
        assert any("Duplicate" in e for e in errors)

    def test_missing_goal_error(self) -> None:
        plan = {"tickets": [{"ticket_id": "T001"}]}
        errors = validate_plan(plan)
        assert any("goal" in e.lower() for e in errors)

    def test_negative_budget_error(self) -> None:
        plan = {
            "tickets": [{
                "ticket_id": "T001",
                "goal": "test",
                "scope": {"budgets": {"max_files_changed": -1}},
            }],
        }
        errors = validate_plan(plan)
        assert any("max_files_changed" in e for e in errors)


class TestResolveTicketOrder:
    def test_linear_chain(self) -> None:
        from saturnday._types import TicketSpec
        tickets = [
            TicketSpec(ticket_id="T002", goal="B", dependencies=("T001",)),
            TicketSpec(ticket_id="T001", goal="A"),
        ]
        ordered = resolve_ticket_order(tickets)
        ids = [t.ticket_id for t in ordered]
        assert ids == ["T001", "T002"]

    def test_cycle_raises(self) -> None:
        from saturnday._types import TicketSpec
        tickets = [
            TicketSpec(ticket_id="T001", goal="A", dependencies=("T002",)),
            TicketSpec(ticket_id="T002", goal="B", dependencies=("T001",)),
        ]
        with pytest.raises(PlanValidationError, match="cycle"):
            resolve_ticket_order(tickets)

    def test_no_dependencies(self) -> None:
        from saturnday._types import TicketSpec
        tickets = [
            TicketSpec(ticket_id="T003", goal="C"),
            TicketSpec(ticket_id="T001", goal="A"),
            TicketSpec(ticket_id="T002", goal="B"),
        ]
        ordered = resolve_ticket_order(tickets)
        assert len(ordered) == 3


class TestV3PlanFields:
    """Plans with v3 runtime fields parse correctly; plans without default."""

    def test_plan_with_v3_fields(self, tmp_path: Path) -> None:
        plan_data = {
            "version": 1,
            "project_id": "test-v3",
            "definition_of_done": ["all_tickets_passed"],
            "stop_conditions": ["policy_denied"],
            "max_project_tickets": 5,
            "mode": "remediation",
            "phases": [
                {"phase_id": "p1", "name": "Build", "ticket_ids": ["T001"]},
            ],
            "tickets": [
                {"ticket_id": "T001", "goal": "Create scaffold"},
            ],
        }
        path = _write_plan(plan_data, tmp_path)
        plan = load_plan(path)
        assert plan.definition_of_done == ("all_tickets_passed",)
        assert plan.stop_conditions == ("policy_denied",)
        assert plan.max_project_tickets == 5
        assert plan.mode == "remediation"
        assert len(plan.phases) == 1
        assert plan.phases[0].phase_id == "p1"
        assert plan.phases[0].ticket_ids == ("T001",)

    def test_plan_without_v3_fields_uses_defaults(self, tmp_path: Path) -> None:
        path = _write_plan(_minimal_plan(), tmp_path)
        plan = load_plan(path)
        assert plan.definition_of_done == ("all_tickets_passed",)
        assert plan.stop_conditions == ()
        assert plan.max_project_tickets is None
        assert plan.phases == ()
        assert plan.mode == "generation"

    def test_invalid_dod_marker_produces_error(self) -> None:
        plan_data = {
            "tickets": [{"ticket_id": "T001", "goal": "test"}],
            "definition_of_done": ["unknown_marker"],
        }
        errors = validate_plan(plan_data)
        assert any("Unknown definition_of_done marker" in e for e in errors)

    def test_invalid_max_project_tickets_produces_error(self) -> None:
        plan_data = {
            "tickets": [{"ticket_id": "T001", "goal": "test"}],
            "max_project_tickets": -1,
        }
        errors = validate_plan(plan_data)
        assert any("max_project_tickets" in e for e in errors)

    def test_phase_references_unknown_ticket(self) -> None:
        plan_data = {
            "tickets": [{"ticket_id": "T001", "goal": "test"}],
            "phases": [{"phase_id": "p1", "ticket_ids": ["T999"]}],
        }
        errors = validate_plan(plan_data)
        assert any("unknown ticket" in e.lower() for e in errors)


class TestTicketRubricFields:
    """New rubric fields parse correctly and emit warnings when absent."""

    def _full_rubric_plan(self) -> dict:
        return {
            "version": 1,
            "project_id": "rubric-test",
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Add a logging helper",
                    "verify_cmd": "pytest tests/test_logging.py -q",
                    "acceptance_criteria": ["Helper exists", "Returns logger instance"],
                    "out_of_scope": ["Do not modify existing tests", "Do not change the API surface"],
                    "evidence_required": ["New test file passes", "Function exists in module"],
                    "failure_mode": "coder_error",
                    "if_blocked": "Check dependency T000 output, verify function signature",
                    "dependencies": [],
                }
            ],
        }

    def test_new_rubric_fields_parse_correctly(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(self._full_rubric_plan()), encoding="utf-8")
        plan = load_plan(path)
        ticket = plan.tickets[0]

        assert ticket.out_of_scope == (
            "Do not modify existing tests",
            "Do not change the API surface",
        )
        assert ticket.evidence_required == (
            "New test file passes",
            "Function exists in module",
        )
        assert ticket.failure_mode == "coder_error"
        assert ticket.if_blocked == "Check dependency T000 output, verify function signature"

    def test_backward_compat_plan_without_rubric_fields(self, tmp_path: Path) -> None:
        """Plans that omit the new fields must still load with empty defaults."""
        plan_data = {
            "version": 1,
            "project_id": "legacy",
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Create scaffold",
                    "acceptance_criteria": ["File exists"],
                }
            ],
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        plan = load_plan(path)
        ticket = plan.tickets[0]

        assert ticket.out_of_scope == ()
        assert ticket.evidence_required == ()
        assert ticket.failure_mode == ""
        assert ticket.if_blocked == ""

    def test_rubric_warning_for_missing_acceptance_criteria(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plan_data = {
            "version": 1,
            "project_id": "warn-test",
            "mode": "remediation",  # allows missing acceptance_criteria — warn only
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Do something",
                    "out_of_scope": ["No API changes"],
                    "evidence_required": ["File exists"],
                }
            ],
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            load_plan(path)
        assert any("acceptance_criteria" in r.message for r in caplog.records)

    def test_rubric_warning_for_missing_out_of_scope(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plan_data = {
            "version": 1,
            "project_id": "warn-test",
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Do something",
                    "acceptance_criteria": ["It works"],
                    "evidence_required": ["File exists"],
                }
            ],
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            load_plan(path)
        assert any("out_of_scope" in r.message for r in caplog.records)

    def test_rubric_warning_for_missing_evidence_required(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plan_data = {
            "version": 1,
            "project_id": "warn-test",
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Do something",
                    "acceptance_criteria": ["It works"],
                    "out_of_scope": ["No API changes"],
                }
            ],
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        with caplog.at_level(logging.DEBUG, logger="saturnday.plan_parser"):
            load_plan(path)
        assert any("evidence_required" in r.message for r in caplog.records)

    def test_rubric_warnings_do_not_reject_plan(self, tmp_path: Path) -> None:
        """A plan missing all rubric fields must still load — warnings only."""
        plan_data = {
            "version": 1,
            "project_id": "bare-plan",
            "mode": "remediation",  # remediation tolerates missing rubric fields
            "tickets": [{"ticket_id": "T001", "goal": "Minimal"}],
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan_data), encoding="utf-8")
        plan = load_plan(path)  # must not raise
        assert len(plan.tickets) == 1

    def test_no_warnings_when_rubric_is_complete(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(self._full_rubric_plan()), encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="saturnday.plan_parser"):
            load_plan(path)
        rubric_warnings = [
            r for r in caplog.records
            if any(f in r.message for f in ("acceptance_criteria", "out_of_scope", "evidence_required"))
        ]
        assert rubric_warnings == []
