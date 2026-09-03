"""Tests for capability-state fields written by write_run_summary (SPLIT-013).

Verifies that run-summary.json includes the seven new additive fields that
record premium capability availability at run time, without changing any of
the existing fields.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import saturnday.capability_registry as registry
from saturnday._types import RunResult, TicketResult
from saturnday.run.evidence import write_run_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_result(project_id: str = "test-project") -> RunResult:
    """Build the smallest valid RunResult for these tests."""
    return RunResult(project_id=project_id)


def _summary_data(tmp_path: Path, result: RunResult | None = None) -> dict:
    """Write a run summary and return the parsed JSON dict."""
    if result is None:
        result = _minimal_result()
    path = write_run_summary(result, tmp_path)
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_registry():
    """Ensure the capability registry is empty before and after every test."""
    registry.clear()
    yield
    registry.clear()


# ---------------------------------------------------------------------------
# Test: all capability fields present with no premium registered
# ---------------------------------------------------------------------------

class TestCapabilityFieldsNoPremium:
    def test_premium_capabilities_enabled_false(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        assert data["premium_capabilities_enabled"] is False

    def test_available_premium_hooks_empty(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        assert data["available_premium_hooks"] == []

    def test_skipped_premium_stages_all_eight(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        skipped = data["skipped_premium_stages"]
        assert isinstance(skipped, list)
        assert len(skipped) == 8
        reasons = {s["reason"] for s in skipped}
        assert reasons == {"premium_not_available"}

    def test_security_triage_available_false(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        assert data["security_triage_available"] is False

    def test_memory_available_false(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        assert data["memory_available"] is False

    def test_impact_analysis_available_false(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        assert data["impact_analysis_available"] is False

    def test_code_reviewer_available_false(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        assert data["code_reviewer_available"] is False


# ---------------------------------------------------------------------------
# Test: capability fields reflect a registered premium hook
# ---------------------------------------------------------------------------

class TestCapabilityFieldsWithPremium:
    def test_premium_capabilities_enabled_true_when_registered(
        self, tmp_path: Path
    ) -> None:
        registry.register("security_triage", object())
        data = _summary_data(tmp_path)
        assert data["premium_capabilities_enabled"] is True

    def test_available_premium_hooks_lists_registered_stage(
        self, tmp_path: Path
    ) -> None:
        registry.register("security_triage", object())
        data = _summary_data(tmp_path)
        assert "security_triage" in data["available_premium_hooks"]

    def test_security_triage_available_true_when_registered(
        self, tmp_path: Path
    ) -> None:
        registry.register("security_triage", object())
        data = _summary_data(tmp_path)
        assert data["security_triage_available"] is True

    def test_memory_available_true_when_registered(self, tmp_path: Path) -> None:
        registry.register("memory_provider", object())
        data = _summary_data(tmp_path)
        assert data["memory_available"] is True

    def test_impact_analysis_available_true_when_registered(
        self, tmp_path: Path
    ) -> None:
        registry.register("impact_analysis", object())
        data = _summary_data(tmp_path)
        assert data["impact_analysis_available"] is True

    def test_code_reviewer_available_true_when_registered(
        self, tmp_path: Path
    ) -> None:
        registry.register("code_reviewer", object())
        data = _summary_data(tmp_path)
        assert data["code_reviewer_available"] is True

    def test_registered_stage_not_in_skipped(self, tmp_path: Path) -> None:
        """A registered stage (with ran_stages=None) must NOT appear in skipped list."""
        registry.register("security_triage", object())
        data = _summary_data(tmp_path)
        stage_names = [s["stage"] for s in data["skipped_premium_stages"]]
        assert "security_triage" not in stage_names

    def test_unregistered_stages_still_skipped(self, tmp_path: Path) -> None:
        registry.register("security_triage", object())
        data = _summary_data(tmp_path)
        stage_names = [s["stage"] for s in data["skipped_premium_stages"]]
        # The remaining 6 stages should still be skipped
        assert "memory_provider" in stage_names
        assert "spec_verifier" in stage_names


# ---------------------------------------------------------------------------
# Test: existing fields are not affected
# ---------------------------------------------------------------------------

class TestExistingFieldsUnchanged:
    def test_project_id_still_present(self, tmp_path: Path) -> None:
        result = _minimal_result(project_id="my-project")
        data = _summary_data(tmp_path, result)
        assert data["project_id"] == "my-project"

    def test_ticket_results_still_present(self, tmp_path: Path) -> None:
        result = RunResult(
            project_id="p",
            total_tickets=1,
            passed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
            ),
        )
        data = _summary_data(tmp_path, result)
        assert len(data["ticket_results"]) == 1
        assert data["ticket_results"][0]["ticket_id"] == "T001"

    def test_core_count_fields_present(self, tmp_path: Path) -> None:
        data = _summary_data(tmp_path)
        for key in ("total_tickets", "passed", "failed", "skipped",
                    "definition_of_done_met", "stop_reason", "timestamp"):
            assert key in data, f"missing field: {key}"

    def test_new_fields_do_not_overwrite_existing(self, tmp_path: Path) -> None:
        """None of the 7 new keys collide with existing top-level keys."""
        result = RunResult(
            project_id="p",
            total_tickets=1,
            passed=1,
            ticket_results=(
                TicketResult(
                    ticket_id="T001",
                    disposition="PASS",
                    attempts=1,
                    failure_category="governance",
                ),
            ),
        )
        data = _summary_data(tmp_path, result)
        # failure_categories is only present when non-empty
        assert data["failure_categories"] == {"governance": 1}
        # new fields present alongside existing ones
        assert "premium_capabilities_enabled" in data
        assert "available_premium_hooks" in data
        assert "skipped_premium_stages" in data
