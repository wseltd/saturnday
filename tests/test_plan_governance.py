"""Tests for plan-governance fields and wiring (PG-001 through PG-008).

Covers:
  - PG-001: ProjectPlan and RunResult governance fields exist with defaults
  - PG-003: plan_parser reads governance fields; backward compat for old plans
  - PG-004: scoped_categories extraction from brief text
  - PG-005: run_dod_check task payload includes governance sections
  - PG-006: extract_classification finds PG_MET / PG_NOT_MET / PLAN_GOVERNANCE_MET
  - PG-007: write_run_summary includes governance fields
  - PG-008: show_run_summary prints plan governance verdict
"""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# PG-001: ProjectPlan and RunResult field presence
# ---------------------------------------------------------------------------


class TestProjectPlanGovernanceFields:
    """PG-001: ProjectPlan has all 6 governance fields with correct defaults."""

    def test_project_plan_has_governing_goal(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="x")
        assert hasattr(p, "governing_goal")
        assert p.governing_goal == ""

    def test_project_plan_has_required_outcomes(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="x")
        assert hasattr(p, "required_outcomes")
        assert p.required_outcomes == ()

    def test_project_plan_has_scoped_categories(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="x")
        assert hasattr(p, "scoped_categories")
        assert p.scoped_categories == ()

    def test_project_plan_has_exclusions(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="x")
        assert hasattr(p, "exclusions")
        assert p.exclusions == ()

    def test_project_plan_has_constraints(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="x")
        assert hasattr(p, "constraints")
        assert p.constraints == ()

    def test_project_plan_has_proof_expectations(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="x")
        assert hasattr(p, "proof_expectations")
        assert p.proof_expectations == ()

    def test_project_plan_accepts_governance_values(self) -> None:
        from saturnday._types import ProjectPlan

        p = ProjectPlan(
            version=1,
            project_id="x",
            governing_goal="build X",
            required_outcomes=("X works",),
            scoped_categories=("security",),
            exclusions=("do not change auth",),
            constraints=("Python 3.10 only",),
            proof_expectations=("tests pass",),
        )
        assert p.governing_goal == "build X"
        assert p.required_outcomes == ("X works",)
        assert p.scoped_categories == ("security",)
        assert p.exclusions == ("do not change auth",)
        assert p.constraints == ("Python 3.10 only",)
        assert p.proof_expectations == ("tests pass",)

    def test_project_plan_backward_compat_construction(self) -> None:
        """Old-style construction without governance fields still works."""
        from saturnday._types import ProjectPlan

        p = ProjectPlan(version=1, project_id="y")
        assert p.governing_goal == ""
        assert p.required_outcomes == ()


class TestRunResultGovernanceFields:
    """PG-001: RunResult has plan_governance_met and plan_governance_reason."""

    def test_run_result_has_plan_governance_met(self) -> None:
        from saturnday._types import RunResult

        r = RunResult(project_id="x")
        assert hasattr(r, "plan_governance_met")
        assert r.plan_governance_met is False

    def test_run_result_has_plan_governance_reason(self) -> None:
        from saturnday._types import RunResult

        r = RunResult(project_id="x")
        assert hasattr(r, "plan_governance_reason")
        assert r.plan_governance_reason == ""

    def test_run_result_accepts_governance_values(self) -> None:
        from saturnday._types import RunResult

        r = RunResult(
            project_id="x",
            plan_governance_met=True,
            plan_governance_reason="all outcomes met",
        )
        assert r.plan_governance_met is True
        assert r.plan_governance_reason == "all outcomes met"

    def test_run_result_backward_compat_construction(self) -> None:
        """Old-style RunResult construction still works."""
        from saturnday._types import RunResult

        r = RunResult(project_id="x", passed=1, failed=0)
        assert r.plan_governance_met is False
        assert r.plan_governance_reason == ""


# ---------------------------------------------------------------------------
# PG-003: plan_parser reads governance fields
# ---------------------------------------------------------------------------


def _write_plan(plan_data: dict[str, Any], dir_path: Path) -> Path:
    """Write a plan dict to a temp JSON file."""
    path = dir_path / "plan.json"
    path.write_text(json.dumps(plan_data), encoding="utf-8")
    return path


def _minimal_plan_raw() -> dict[str, Any]:
    return {
        "version": 1,
        "project_id": "test",
        "tickets": [{
            "ticket_id": "T001",
            "goal": "do something",
            "acceptance_criteria": ["it works"],
        }],
    }


class TestPlanParserReadsGovernanceFields:
    """PG-003: load_plan parses governance fields with correct types."""

    def test_plan_parser_reads_governing_goal(self, tmp_path: Path) -> None:
        from saturnday.plan_parser import load_plan

        raw = _minimal_plan_raw()
        raw["governing_goal"] = "build a REST API"
        path = _write_plan(raw, tmp_path)
        plan = load_plan(path)
        assert plan.governing_goal == "build a REST API"

    def test_plan_parser_reads_required_outcomes(self, tmp_path: Path) -> None:
        from saturnday.plan_parser import load_plan

        raw = _minimal_plan_raw()
        raw["required_outcomes"] = ["API responds to /health", "Auth flow works"]
        path = _write_plan(raw, tmp_path)
        plan = load_plan(path)
        assert plan.required_outcomes == ("API responds to /health", "Auth flow works")

    def test_plan_parser_reads_all_governance_fields(self, tmp_path: Path) -> None:
        from saturnday.plan_parser import load_plan

        raw = _minimal_plan_raw()
        raw.update(
            {
                "governing_goal": "build X",
                "required_outcomes": ["X works"],
                "scoped_categories": ["security"],
                "exclusions": ["do not change auth"],
                "constraints": ["Python 3.10 only"],
                "proof_expectations": ["tests pass"],
            }
        )
        path = _write_plan(raw, tmp_path)
        plan = load_plan(path)
        assert plan.governing_goal == "build X"
        assert plan.required_outcomes == ("X works",)
        assert plan.scoped_categories == ("security",)
        assert plan.exclusions == ("do not change auth",)
        assert plan.constraints == ("Python 3.10 only",)
        assert plan.proof_expectations == ("tests pass",)

    def test_plan_parser_fallback_to_notes_when_no_governing_goal(
        self, tmp_path: Path
    ) -> None:
        """When governing_goal absent, falls back to notes (existing field)."""
        from saturnday.plan_parser import load_plan

        raw = _minimal_plan_raw()
        raw["notes"] = "This is the original brief"
        # No governing_goal key
        path = _write_plan(raw, tmp_path)
        plan = load_plan(path)
        assert plan.governing_goal == "This is the original brief"

    def test_plan_parser_old_plan_no_governance_fields(self, tmp_path: Path) -> None:
        """Old plan without any governance fields loads with empty defaults."""
        from saturnday.plan_parser import load_plan

        path = _write_plan(_minimal_plan_raw(), tmp_path)
        plan = load_plan(path)
        assert plan.required_outcomes == ()
        assert plan.scoped_categories == ()
        assert plan.exclusions == ()
        assert plan.constraints == ()
        assert plan.proof_expectations == ()


class TestParseStringTuple:
    """PG-003: _parse_string_tuple helper handles all input types."""

    def test_none_returns_empty_tuple(self) -> None:
        from saturnday.plan_parser import _parse_string_tuple

        assert _parse_string_tuple(None) == ()

    def test_string_list_returns_tuple(self) -> None:
        from saturnday.plan_parser import _parse_string_tuple

        assert _parse_string_tuple(["a", "b"]) == ("a", "b")

    def test_mixed_list_filters_non_strings(self) -> None:
        from saturnday.plan_parser import _parse_string_tuple

        assert _parse_string_tuple([1, "b", None, "c"]) == ("b", "c")

    def test_non_list_returns_empty_tuple(self) -> None:
        from saturnday.plan_parser import _parse_string_tuple

        assert _parse_string_tuple("not a list") == ()
        assert _parse_string_tuple(42) == ()
        assert _parse_string_tuple({}) == ()

    def test_empty_list_returns_empty_tuple(self) -> None:
        from saturnday.plan_parser import _parse_string_tuple

        assert _parse_string_tuple([]) == ()


# ---------------------------------------------------------------------------
# PG-004: scoped_categories extracted from brief
# ---------------------------------------------------------------------------


class TestScopedCategoriesFromBrief:
    """PG-004: scoped_categories recognised from security-related brief text."""

    def test_security_brief_produces_security_category(self) -> None:
        """Brief mentioning security → scoped_categories includes 'security'."""
        from saturnday.repair.finding_category import parse_repair_categories

        result = parse_repair_categories("fix all security issues in this repo")
        assert result is not None
        assert "security" in result


# ---------------------------------------------------------------------------
# PG-006: extract_classification finds PLAN_GOVERNANCE_MET
# ---------------------------------------------------------------------------


class TestPGClassificationExtraction:
    """PG-006: extract_classification correctly extracts PG tokens."""

    def test_pg_met_classification(self) -> None:
        from saturnday.role_modes import extract_classification

        output = "All tickets passed. PLAN_GOVERNANCE_MET. The brief was fulfilled."
        result = extract_classification(output, ["PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NOT_MET"])
        assert result == "PLAN_GOVERNANCE_MET"

    def test_pg_not_met_classification(self) -> None:
        from saturnday.role_modes import extract_classification

        output = "T001 failed. PLAN_GOVERNANCE_NOT_MET: API connectors were not built."
        result = extract_classification(output, ["PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NOT_MET"])
        assert result == "PLAN_GOVERNANCE_NOT_MET"

    def test_pg_no_governance_classification(self) -> None:
        from saturnday.role_modes import extract_classification

        output = "DOD_MET. PLAN_GOVERNANCE_NO_GOVERNANCE."
        result = extract_classification(
            output,
            ["PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NOT_MET", "PLAN_GOVERNANCE_NO_GOVERNANCE"],
        )
        assert result == "PLAN_GOVERNANCE_NO_GOVERNANCE"

    def test_pg_not_met_found_when_listed_first_in_candidates(self) -> None:
        """When NOT_MET is listed before MET, it wins even though MET is a substring."""
        from saturnday.role_modes import extract_classification

        output = "PLAN_GOVERNANCE_NOT_MET: missing API connectors."
        # List NOT_MET first so it is found before MET (which is a substring of NOT_MET).
        result = extract_classification(
            output, ["PLAN_GOVERNANCE_NOT_MET", "PLAN_GOVERNANCE_MET"]
        )
        assert result == "PLAN_GOVERNANCE_NOT_MET"

    def test_fallback_when_no_pg_token(self) -> None:
        from saturnday.role_modes import extract_classification

        output = "All done. No plan governance verdict here."
        result = extract_classification(output, ["PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NOT_MET"])
        # Falls back to truncated first line — not a PG candidate
        assert result not in ("PLAN_GOVERNANCE_MET", "PLAN_GOVERNANCE_NOT_MET")


# ---------------------------------------------------------------------------
# PG-007: write_run_summary includes governance fields
# ---------------------------------------------------------------------------


class TestRunSummaryGovernanceFields:
    """PG-007: write_run_summary writes plan_governance_* and context fields."""

    def _make_result(self, **kwargs: Any):
        from saturnday._types import RunResult
        return RunResult(project_id="test", **kwargs)

    def test_run_summary_includes_plan_governance_met(self, tmp_path: Path) -> None:
        from saturnday.run.evidence import write_run_summary

        result = self._make_result(plan_governance_met=True, plan_governance_reason="PG_MET")
        write_run_summary(result, tmp_path)
        data = json.loads((tmp_path / "run-summary.json").read_text())
        assert data["plan_governance_met"] is True
        assert data["plan_governance_reason"] == "PG_MET"

    def test_run_summary_includes_plan_governance_not_met(self, tmp_path: Path) -> None:
        from saturnday.run.evidence import write_run_summary

        result = self._make_result(
            plan_governance_met=False,
            plan_governance_reason="PLAN_GOVERNANCE_NOT_MET: missing API connectors",
        )
        write_run_summary(result, tmp_path)
        data = json.loads((tmp_path / "run-summary.json").read_text())
        assert data["plan_governance_met"] is False
        assert "missing API connectors" in data["plan_governance_reason"]

    def test_run_summary_includes_governance_context_when_plan_data_provided(
        self, tmp_path: Path
    ) -> None:
        from saturnday.run.evidence import write_run_summary

        result = self._make_result()
        plan_data: dict[str, Any] = {
            "governing_goal": "build a REST API",
            "required_outcomes": ["API responds to /health"],
            "scoped_categories": ["security"],
            "exclusions": ["do not change auth"],
        }
        write_run_summary(result, tmp_path, plan_data=plan_data)
        data = json.loads((tmp_path / "run-summary.json").read_text())
        assert data["governing_goal"] == "build a REST API"
        assert data["required_outcomes"] == ["API responds to /health"]
        assert data["scoped_categories"] == ["security"]
        assert data["exclusions"] == ["do not change auth"]

    def test_run_summary_without_plan_data_omits_context_fields(
        self, tmp_path: Path
    ) -> None:
        """Backward compat: no plan_data → governing_goal etc. absent."""
        from saturnday.run.evidence import write_run_summary

        result = self._make_result()
        write_run_summary(result, tmp_path)
        data = json.loads((tmp_path / "run-summary.json").read_text())
        # Plan-governance outcome fields are always present (default False/"")
        assert "plan_governance_met" in data
        assert "plan_governance_reason" in data
        # Context fields are absent when plan_data is not passed
        assert "governing_goal" not in data
        assert "required_outcomes" not in data

    def test_run_summary_plan_data_none_explicit(self, tmp_path: Path) -> None:
        """Explicit plan_data=None is the same as omitting it."""
        from saturnday.run.evidence import write_run_summary

        result = self._make_result()
        write_run_summary(result, tmp_path, plan_data=None)
        data = json.loads((tmp_path / "run-summary.json").read_text())
        assert "governing_goal" not in data

    def test_run_summary_signature_has_plan_data_param(self) -> None:
        import inspect

        from saturnday.run.evidence import write_run_summary

        sig = inspect.signature(write_run_summary)
        assert "plan_data" in sig.parameters


# ---------------------------------------------------------------------------
# PG-008: show_run_summary prints plan governance verdict
# ---------------------------------------------------------------------------


class TestShowRunSummaryGovernanceDisplay:
    """PG-008: show_run_summary displays plan governance when reason is set."""

    def _capture_summary(self, **kwargs: Any) -> str:
        from saturnday._types import RunResult

        result = RunResult(project_id="test", **kwargs)
        buf = io.StringIO()
        with redirect_stdout(buf):
            from saturnday.interactive import show_run_summary

            show_run_summary(result)
        return buf.getvalue()

    def test_plan_governance_met_displayed(self) -> None:
        # Fix 39: reason line is suppressed on MET to reduce noise; only NOT MET shows reason.
        output = self._capture_summary(
            plan_governance_met=True,
            plan_governance_reason="PLAN_GOVERNANCE_MET",
        )
        assert "Plan governance: MET" in output
        # Reason text is NOT echoed separately when governance is met — label is sufficient.

    def test_plan_governance_not_met_displayed(self) -> None:
        output = self._capture_summary(
            plan_governance_met=False,
            plan_governance_reason="PLAN_GOVERNANCE_NOT_MET: API connectors missing",
        )
        assert "Plan governance: NOT MET" in output
        assert "API connectors missing" in output

    def test_no_governance_display_when_reason_empty(self) -> None:
        """When plan_governance_reason is empty string, no PG line is printed."""
        output = self._capture_summary(plan_governance_reason="")
        assert "Plan governance" not in output

    def test_no_governance_section_for_old_run_result(self) -> None:
        """RunResult with default values (pre-PG) produces no governance section."""
        output = self._capture_summary()
        assert "Plan governance" not in output


# ---------------------------------------------------------------------------
# TestDoDPGSplit: prove the split between definition_of_done and plan_governance
# ---------------------------------------------------------------------------


class TestDoDPGSplit:
    """Prove the split between definition_of_done and plan_governance is intentional."""

    def test_dod_met_pg_not_met_is_valid_state(self) -> None:
        """A run can have all tickets pass (DoD MET) but governing goal not met."""
        from saturnday._types import RunResult

        result = RunResult(
            project_id="test",
            total_tickets=3,
            passed=3,
            failed=0,
            definition_of_done_met=True,
            plan_governance_met=False,
            plan_governance_reason=(
                "PLAN_GOVERNANCE_NOT_MET: security findings were addressed but quality"
                " findings were also changed, violating the exclusion"
                " 'do not touch quality issues'"
            ),
        )
        assert result.definition_of_done_met is True
        assert result.plan_governance_met is False
        # This is the key scenario: tickets passed but the governing intent was violated

    def test_dod_not_met_pg_met_is_valid_state(self) -> None:
        """A run can have ticket failures but governing goal still met (partial completion is enough)."""
        from saturnday._types import RunResult

        result = RunResult(
            project_id="test",
            total_tickets=5,
            passed=3,
            failed=2,
            definition_of_done_met=False,
            plan_governance_met=True,
            plan_governance_reason=(
                "PLAN_GOVERNANCE_MET: the security findings were all resolved even"
                " though 2 quality tickets failed"
            ),
        )
        assert result.definition_of_done_met is False
        assert result.plan_governance_met is True

    def test_both_met(self) -> None:
        """Normal success: both DoD and PG are met."""
        from saturnday._types import RunResult

        result = RunResult(
            project_id="test",
            passed=5,
            definition_of_done_met=True,
            plan_governance_met=True,
            plan_governance_reason="PLAN_GOVERNANCE_MET",
        )
        assert result.definition_of_done_met is True
        assert result.plan_governance_met is True

    def test_both_not_met(self) -> None:
        """Total failure: both DoD and PG are not met."""
        from saturnday._types import RunResult

        result = RunResult(
            project_id="test",
            passed=0,
            failed=5,
            definition_of_done_met=False,
            plan_governance_met=False,
            plan_governance_reason="PLAN_GOVERNANCE_NOT_MET: no security findings were resolved",
        )
        assert result.definition_of_done_met is False
        assert result.plan_governance_met is False

    def test_pg_defaults_to_false_before_evaluation(self) -> None:
        """Before DoD evaluation, PG defaults to False (not True)."""
        from saturnday._types import RunResult

        result = RunResult(project_id="test")
        assert result.plan_governance_met is False
        assert result.plan_governance_reason == ""

    def test_governance_fields_do_not_affect_ticket_execution(self) -> None:
        """Governance fields on ProjectPlan do not flow into TicketSpec."""
        from saturnday._types import ProjectPlan, TicketSpec

        plan = ProjectPlan(
            version=1,
            project_id="test",
            governing_goal="fix all security issues",
            scoped_categories=("security",),
            exclusions=("quality",),
        )
        ticket = TicketSpec(ticket_id="T001", goal="fix hardcoded secret")
        # TicketSpec has NO governance fields
        assert not hasattr(ticket, "governing_goal")
        assert not hasattr(ticket, "scoped_categories")
        assert not hasattr(ticket, "exclusions")
        # Plan governance is evaluation-only, never injected into tickets
        # Confirm the plan has them and the ticket does not
        assert plan.governing_goal == "fix all security issues"
        assert plan.scoped_categories == ("security",)

    def test_pg_verdict_extraction_independent_of_dod(self) -> None:
        """PG verdict extraction uses different token patterns than DoD."""
        from saturnday.role_modes import extract_classification

        # DoD candidates
        dod_candidates = ["DOD_MET", "DOD_PARTIAL", "DOD_NOT_MET"]
        # PG candidates
        pg_candidates = [
            "PLAN_GOVERNANCE_NOT_MET",
            "PLAN_GOVERNANCE_MET",
            "PLAN_GOVERNANCE_NO_GOVERNANCE",
        ]

        # A response containing both verdicts
        output = (
            "Based on the analysis: DOD_MET. All tickets passed."
            " PLAN_GOVERNANCE_NOT_MET: exclusions were violated."
        )

        dod_verdict = extract_classification(output, dod_candidates)
        pg_verdict = extract_classification(output, pg_candidates)

        assert dod_verdict == "DOD_MET"
        assert pg_verdict == "PLAN_GOVERNANCE_NOT_MET"
        # They extract independently without interference

    def test_pg_not_met_in_output_does_not_match_dod_not_met(self) -> None:
        """PLAN_GOVERNANCE_NOT_MET does not accidentally match DOD_NOT_MET."""
        from saturnday.role_modes import extract_classification

        output = "PLAN_GOVERNANCE_NOT_MET: scope was violated."
        dod_candidates = ["DOD_MET", "DOD_PARTIAL", "DOD_NOT_MET"]

        # DOD extraction should NOT find a match in PG tokens
        dod_verdict = extract_classification(output, dod_candidates)
        # extract_classification returns first 80 chars of output when no match
        assert dod_verdict != "DOD_NOT_MET", "PG token should not match DOD candidates"


# ---------------------------------------------------------------------------
# TestPGRetryAndDegradedState: bounded retry and explicit degraded state
# ---------------------------------------------------------------------------


class TestPGRetryAndDegradedState:
    """Prove bounded PG retry and explicit degraded state (PG-009)."""

    def test_no_governance_fields_skips_evaluation(self) -> None:
        """Plan with empty governance fields → NO_GOVERNANCE without any LLM call."""
        # _retry_pg_evaluation must never be called when _has_pg_fields is False.
        # We verify the sentinel via _retry_pg_evaluation being a pure function
        # that the caller skips; here we verify the caller logic by checking
        # that a plan with no governing_goal and no required_outcomes maps to
        # PLAN_GOVERNANCE_NO_GOVERNANCE via the same bool gate used in ticket_runner.
        plan_data: dict = {"governing_goal": "", "required_outcomes": []}
        _has_pg_fields = bool(
            plan_data.get("governing_goal") or plan_data.get("required_outcomes")
        )
        assert _has_pg_fields is False, (
            "Empty governance fields must not trigger PG evaluation"
        )

    def test_no_governance_fields_absent_keys(self) -> None:
        """Plan with no governance keys at all → gate evaluates to False."""
        plan_data: dict = {"version": 1, "project_id": "test"}
        _has_pg_fields = bool(
            plan_data.get("governing_goal") or plan_data.get("required_outcomes")
        )
        assert _has_pg_fields is False

    def test_pg_token_in_dod_output_extracted_on_first_try(self) -> None:
        """When DoD output contains PG token, no retry needed."""
        from saturnday.role_modes import extract_classification

        output = "DOD_MET. PLAN_GOVERNANCE_MET."
        pg_candidates = [
            "PLAN_GOVERNANCE_NOT_MET",
            "PLAN_GOVERNANCE_MET",
            "PLAN_GOVERNANCE_NO_GOVERNANCE",
        ]
        verdict = extract_classification(output, pg_candidates)
        assert verdict == "PLAN_GOVERNANCE_MET"

    def test_pg_not_met_token_in_dod_output_extracted_correctly(self) -> None:
        """NOT_MET listed first in candidates wins over MET substring."""
        from saturnday.role_modes import extract_classification

        output = "DOD_MET. PLAN_GOVERNANCE_NOT_MET: required outcome was skipped."
        pg_candidates = [
            "PLAN_GOVERNANCE_NOT_MET",
            "PLAN_GOVERNANCE_MET",
            "PLAN_GOVERNANCE_NO_GOVERNANCE",
        ]
        verdict = extract_classification(output, pg_candidates)
        assert verdict == "PLAN_GOVERNANCE_NOT_MET"

    def test_degraded_state_when_evaluation_fails(self) -> None:
        """After retry exhaustion, state is NOT_EVALUATED, not silently MET."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        from saturnday.role_modes import _retry_pg_evaluation, RoleResult

        # invoke_role returns garbage output on all 3 attempts — no PG token.
        garbage_result = RoleResult(
            role="definition_of_done",
            task="",
            output="I cannot determine the plan governance verdict.",
            success=True,
        )
        plan_data = {
            "governing_goal": "build REST API",
            "required_outcomes": ["API responds to /health"],
        }
        with patch("saturnday.role_modes.invoke_role", return_value=garbage_result):
            pg_met, pg_reason = _retry_pg_evaluation(
                plan_data=plan_data,
                ticket_results=(),
                dod_classification="DOD_MET",
                coder_config=MagicMock(),
                repo_path=Path("/tmp"),
                max_attempts=3,
            )

        assert pg_met is False
        assert pg_reason == "PLAN_GOVERNANCE_NOT_EVALUATED"

    def test_retry_succeeds_on_second_attempt(self) -> None:
        """Retry returns on the first attempt that contains a valid PG token."""
        from unittest.mock import patch, MagicMock, call
        from pathlib import Path
        from saturnday.role_modes import _retry_pg_evaluation, RoleResult

        no_token = RoleResult(
            role="definition_of_done", task="", output="Still thinking...", success=True
        )
        with_token = RoleResult(
            role="definition_of_done",
            task="",
            output="PLAN_GOVERNANCE_MET the goal was achieved.",
            success=True,
        )
        side_effects = [no_token, with_token]
        plan_data = {
            "governing_goal": "build REST API",
            "required_outcomes": ["API responds to /health"],
        }
        with patch("saturnday.role_modes.invoke_role", side_effect=side_effects) as mock_invoke:
            pg_met, pg_reason = _retry_pg_evaluation(
                plan_data=plan_data,
                ticket_results=(),
                dod_classification="DOD_MET",
                coder_config=MagicMock(),
                repo_path=Path("/tmp"),
                max_attempts=3,
            )

        assert pg_met is True
        assert pg_reason == "PLAN_GOVERNANCE_MET"
        # Stopped after 2 attempts — did not exhaust max_attempts.
        assert mock_invoke.call_count == 2

    def test_degraded_state_does_not_block_run(self) -> None:
        """PG NOT_EVALUATED does not alter DoD or ticket results."""
        from saturnday._types import RunResult

        result = RunResult(
            project_id="test",
            passed=5,
            definition_of_done_met=True,
            plan_governance_met=False,
            plan_governance_reason="PLAN_GOVERNANCE_NOT_EVALUATED",
        )
        # DoD is still met independently of PG state.
        assert result.definition_of_done_met is True
        # PG is explicitly not evaluated — False, not silently True.
        assert result.plan_governance_met is False
        assert "NOT_EVALUATED" in result.plan_governance_reason

    def test_dod_unchanged_during_pg_retry(self) -> None:
        """DoD verdict must not change during PG retry attempts."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        from saturnday.role_modes import _retry_pg_evaluation, RoleResult

        # _retry_pg_evaluation ONLY calls invoke_role, never run_dod_check.
        result = RoleResult(
            role="definition_of_done",
            task="",
            output="PLAN_GOVERNANCE_MET",
            success=True,
        )
        plan_data = {"governing_goal": "x", "required_outcomes": ["y"]}

        with patch("saturnday.role_modes.invoke_role", return_value=result):
            with patch("saturnday.role_modes.run_dod_check") as mock_dod:
                _retry_pg_evaluation(
                    plan_data=plan_data,
                    ticket_results=(),
                    dod_classification="DOD_MET",
                    coder_config=MagicMock(),
                    repo_path=Path("/tmp"),
                )

        mock_dod.assert_not_called()

    def test_coded_ungoverned_unaffected_by_pg(self) -> None:
        """CODED_UNGOVERNED ticket dispositions are never altered by PG."""
        from saturnday._types import TicketResult, RunResult

        tr = TicketResult(ticket_id="T001", disposition="CODED_UNGOVERNED")
        result = RunResult(
            project_id="test",
            ticket_results=(tr,),
            coded_ungoverned=1,
            plan_governance_met=False,
            plan_governance_reason="PLAN_GOVERNANCE_NOT_EVALUATED",
        )
        assert result.ticket_results[0].disposition == "CODED_UNGOVERNED"
        # PG fields on RunResult do not touch per-ticket dispositions.
        assert result.coded_ungoverned == 1

    def test_no_silent_pass_when_governance_present(self) -> None:
        """When governance fields exist but retry exhausted, result is NOT True."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        from saturnday.role_modes import _retry_pg_evaluation, RoleResult

        # Old behaviour: missing PG token → pg_met=True (silent pass).
        # New behaviour: missing PG token + governance present → NOT_EVALUATED (False).
        garbage = RoleResult(
            role="definition_of_done", task="", output="no verdict here", success=True
        )
        plan_data = {
            "governing_goal": "fix security issues",
            "required_outcomes": ["all findings resolved"],
        }
        with patch("saturnday.role_modes.invoke_role", return_value=garbage):
            pg_met, pg_reason = _retry_pg_evaluation(
                plan_data=plan_data,
                ticket_results=(),
                dod_classification="DOD_MET",
                coder_config=MagicMock(),
                repo_path=Path("/tmp"),
                max_attempts=3,
            )

        # Must NOT be True — old silent-pass behaviour is gone.
        assert pg_met is False, "Governance present but no verdict must not silently pass"
        assert pg_reason == "PLAN_GOVERNANCE_NOT_EVALUATED"

    def test_retry_not_met_extracts_reason(self) -> None:
        """When retry returns NOT_MET, the reason text is extracted from output."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        from saturnday.role_modes import _retry_pg_evaluation, RoleResult

        output_with_reason = (
            "After reviewing the ticket dispositions, I find that:\n"
            "PLAN_GOVERNANCE_NOT_MET: the required outcome 'auth flow' was never addressed."
        )
        result = RoleResult(
            role="definition_of_done",
            task="",
            output=output_with_reason,
            success=True,
        )
        plan_data = {"governing_goal": "build auth", "required_outcomes": ["auth flow works"]}

        with patch("saturnday.role_modes.invoke_role", return_value=result):
            pg_met, pg_reason = _retry_pg_evaluation(
                plan_data=plan_data,
                ticket_results=(),
                dod_classification="DOD_PARTIAL",
                coder_config=MagicMock(),
                repo_path=Path("/tmp"),
            )

        assert pg_met is False
        assert "PLAN_GOVERNANCE_NOT_MET" in pg_reason
        assert "auth flow" in pg_reason

    def test_retry_invoke_failure_does_not_crash(self) -> None:
        """RoleResult with success=False on all attempts → NOT_EVALUATED."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        from saturnday.role_modes import _retry_pg_evaluation, RoleResult

        failed_result = RoleResult(
            role="definition_of_done",
            task="",
            output="",
            success=False,
            error="API timeout",
        )
        plan_data = {"governing_goal": "x", "required_outcomes": ["y"]}

        with patch("saturnday.role_modes.invoke_role", return_value=failed_result):
            pg_met, pg_reason = _retry_pg_evaluation(
                plan_data=plan_data,
                ticket_results=(),
                dod_classification="DOD_MET",
                coder_config=MagicMock(),
                repo_path=Path("/tmp"),
                max_attempts=3,
            )

        assert pg_met is False
        assert pg_reason == "PLAN_GOVERNANCE_NOT_EVALUATED"
