"""Tests for Fix 59 — block incomplete ticket rubric at plan validation.

Proves:
1. Ticket missing acceptance_criteria causes validation failure
2. Multiple incomplete tickets all produce errors
3. Complete tickets still pass
4. Missing out_of_scope does NOT cause validation failure (warning only)
5. Remediation-mode plans are exempt from the requirement
"""

from __future__ import annotations

import pytest

from saturnday.plan_parser import validate_plan


class TestAcceptanceCriteriaRequired:
    def test_missing_acceptance_criteria_fails(self) -> None:
        """A generation-mode ticket with no acceptance_criteria must fail validation."""
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "Build something"},
            ],
        }
        errors = validate_plan(plan)
        assert any("acceptance_criteria" in e and "T001" in e for e in errors)

    def test_empty_acceptance_criteria_fails(self) -> None:
        """Empty acceptance_criteria list must also fail."""
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "Build something", "acceptance_criteria": []},
            ],
        }
        errors = validate_plan(plan)
        assert any("acceptance_criteria" in e for e in errors)

    def test_multiple_incomplete_tickets_all_fail(self) -> None:
        """Every ticket missing acceptance_criteria produces its own error."""
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "First"},
                {"ticket_id": "T002", "goal": "Second"},
                {"ticket_id": "T003", "goal": "Third", "acceptance_criteria": ["file exists"]},
            ],
        }
        errors = validate_plan(plan)
        ac_errors = [e for e in errors if "acceptance_criteria" in e]
        assert len(ac_errors) == 2
        assert any("T001" in e for e in ac_errors)
        assert any("T002" in e for e in ac_errors)
        assert not any("T003" in e for e in ac_errors)

    def test_complete_ticket_passes(self) -> None:
        """A ticket with acceptance_criteria does not produce that error."""
        plan = {
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Build something",
                    "acceptance_criteria": ["function main exists in app.py"],
                },
            ],
        }
        errors = validate_plan(plan)
        assert not any("acceptance_criteria" in e for e in errors)

    def test_remediation_mode_exempt(self) -> None:
        """Remediation-mode plans are exempt from acceptance_criteria requirement."""
        plan = {
            "mode": "remediation",
            "tickets": [
                {"ticket_id": "T001", "goal": "Fix the bug"},
            ],
        }
        errors = validate_plan(plan)
        assert not any("acceptance_criteria" in e for e in errors)


class TestOutOfScopeNotBlocking:
    def test_missing_out_of_scope_does_not_fail(self) -> None:
        """Missing out_of_scope must NOT cause a validation error."""
        plan = {
            "tickets": [
                {
                    "ticket_id": "T001",
                    "goal": "Build something",
                    "acceptance_criteria": ["file exists"],
                },
            ],
        }
        errors = validate_plan(plan)
        assert not any("out_of_scope" in e for e in errors)
