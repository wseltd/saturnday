"""Tests for saturnday.evidence."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from saturnday._types import CoderConfig, RunResult, TicketResult
from saturnday.run.evidence import (
    TicketEvidence,
    _compute_quality_verdict,
    _count_failure_categories,
    compute_run_analytics,
    write_analytics,
    write_phase_summary,
    write_run_metadata,
    write_run_summary,
    write_ticket_evidence,
)


class TestTicketEvidence:
    def test_auto_timestamp(self) -> None:
        ev = TicketEvidence(ticket_id="T001", attempt=1)
        assert ev.timestamp  # Non-empty
        assert "T" in ev.timestamp  # ISO format

    def test_explicit_timestamp(self) -> None:
        ev = TicketEvidence(ticket_id="T001", attempt=1, timestamp="2025-01-01T00:00:00Z")
        assert ev.timestamp == "2025-01-01T00:00:00Z"


class TestWriteTicketEvidence:
    def test_writes_json_file_under_tickets_subdir(self, tmp_path: Path) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            coder_response="some code",
            changed_files=["foo.py"],
            governance_disposition="PASS",
        )
        path = write_ticket_evidence(ev, tmp_path)
        expected = tmp_path / "tickets" / "T001" / "attempt_1.json"
        assert path == expected
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["ticket_id"] == "T001"
        assert data["governance_disposition"] == "PASS"

    def test_path_structure_attempt_number(self, tmp_path: Path) -> None:
        ev2 = TicketEvidence(ticket_id="T002", attempt=2)
        path = write_ticket_evidence(ev2, tmp_path)
        assert path == tmp_path / "tickets" / "T002" / "attempt_2.json"

    def test_truncates_long_response(self, tmp_path: Path) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            coder_response="x" * 100_000,
        )
        path = write_ticket_evidence(ev, tmp_path)
        data = json.loads(path.read_text())
        assert len(data["coder_response"]) < 60_000
        assert "truncated" in data["coder_response"]

    def test_creates_intermediate_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir"
        ev = TicketEvidence(ticket_id="T001", attempt=1)
        path = write_ticket_evidence(ev, out)
        # Should have created tickets/T001/ under the nested dir
        assert path == out / "tickets" / "T001" / "attempt_1.json"
        assert path.exists()

    def test_multiple_attempts_same_ticket(self, tmp_path: Path) -> None:
        for n in (1, 2, 3):
            ev = TicketEvidence(ticket_id="T003", attempt=n)
            write_ticket_evidence(ev, tmp_path)
        ticket_dir = tmp_path / "tickets" / "T003"
        written = sorted(p.name for p in ticket_dir.iterdir())
        assert written == ["attempt_1.json", "attempt_2.json", "attempt_3.json"]


class TestWriteRunSummary:
    def test_writes_summary_at_hyphen_path(self, tmp_path: Path) -> None:
        result = RunResult(
            project_id="test",
            total_tickets=2,
            passed=1,
            failed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T002", disposition="FAIL", attempts=3, error="broke"),
            ),
        )
        path = write_run_summary(result, tmp_path)
        assert path == tmp_path / "run-summary.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["project_id"] == "test"
        assert data["passed"] == 1
        assert data["failed"] == 1
        assert len(data["ticket_results"]) == 2


class TestWriteRunMetadata:
    def test_writes_metadata_file(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="anthropic", model="claude-3", api_key="k")
        path = write_run_metadata(config, "/some/plan.json", tmp_path)
        assert path == tmp_path / "run-metadata.json"
        assert path.exists()

    def test_metadata_content(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="openai", model="gpt-4", api_key="k")
        write_run_metadata(config, "/plans/my-plan.json", tmp_path)
        data = json.loads((tmp_path / "run-metadata.json").read_text())
        assert data["backend"] == "openai"
        assert data["plan_path"] == "/plans/my-plan.json"
        assert data["saturnday_version"] == "0.1.0"
        assert "start_time" in data
        assert "T" in data["start_time"]  # ISO format

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="codex-cli")
        out = tmp_path / "new" / "output"
        path = write_run_metadata(config, "plan.json", out)
        assert path.exists()

    def test_plan_path_as_path_object(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="anthropic")
        plan = Path("/tmp/plan.json")
        write_run_metadata(config, plan, tmp_path)
        data = json.loads((tmp_path / "run-metadata.json").read_text())
        assert data["plan_path"] == "/tmp/plan.json"

    def test_auth_mode_and_capabilities_written(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="openai", api_key="k")
        caps = {"api_key": True, "ci": True, "streaming": True, "file_writes": False}
        write_run_metadata(
            config,
            "/plans/my-plan.json",
            tmp_path,
            auth_mode="api_key",
            backend_capabilities=caps,
        )
        data = json.loads((tmp_path / "run-metadata.json").read_text())
        assert data["auth_mode"] == "api_key"
        assert data["backend_capabilities"] == caps

    def test_auth_mode_defaults_to_empty_string(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="openai", api_key="k")
        write_run_metadata(config, "plan.json", tmp_path)
        data = json.loads((tmp_path / "run-metadata.json").read_text())
        assert data["auth_mode"] == ""

    def test_backend_capabilities_defaults_to_empty_dict(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="openai", api_key="k")
        write_run_metadata(config, "plan.json", tmp_path)
        data = json.loads((tmp_path / "run-metadata.json").read_text())
        assert data["backend_capabilities"] == {}

    def test_none_capabilities_written_as_empty_dict(self, tmp_path: Path) -> None:
        config = CoderConfig(backend="openai", api_key="k")
        write_run_metadata(config, "plan.json", tmp_path, backend_capabilities=None)
        data = json.loads((tmp_path / "run-metadata.json").read_text())
        assert data["backend_capabilities"] == {}


class TestWritePhaseSummary:
    def _make_ledger(
        self,
        phases: list[dict],
        ticket_statuses: dict[str, dict],
    ) -> MagicMock:
        """Build a minimal ledger mock with phase and ticket state."""
        from saturnday.run.run_ledger import PhaseStatus, TicketStatus

        phase_objs: list[PhaseStatus] = []
        for p in phases:
            ps = PhaseStatus(
                phase_id=p["phase_id"],
                name=p.get("name", p["phase_id"]),
                ticket_ids=tuple(p["ticket_ids"]),
            )
            ps.status = p.get("status", "PASS")
            ps.started_utc = p.get("started_utc", "2026-01-01T00:00:00Z")
            ps.completed_utc = p.get("completed_utc", "2026-01-01T01:00:00Z")
            phase_objs.append(ps)

        ts_objs: dict[str, TicketStatus] = {}
        for tid, td in ticket_statuses.items():
            ts = TicketStatus(tid)
            ts.disposition = td.get("disposition", "PASS")
            ts.failure_category = td.get("failure_category", "")
            ts_objs[tid] = ts

        ledger = MagicMock()
        ledger.phase_statuses = phase_objs
        ledger.ticket_statuses = ts_objs
        return ledger

    def test_no_phases_returns_empty(self, tmp_path: Path) -> None:
        ledger = MagicMock()
        ledger.phase_statuses = []
        result = write_phase_summary(ledger, tmp_path)
        assert result == []
        assert not (tmp_path / "phases").exists()

    def test_writes_one_file_per_phase(self, tmp_path: Path) -> None:
        ledger = self._make_ledger(
            phases=[
                {"phase_id": "1", "name": "Phase One", "ticket_ids": ["T001", "T002"]},
                {"phase_id": "2", "name": "Phase Two", "ticket_ids": ["T003"]},
            ],
            ticket_statuses={
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "PASS"},
                "T003": {"disposition": "FAIL", "failure_category": "governance"},
            },
        )
        paths = write_phase_summary(ledger, tmp_path)
        assert len(paths) == 2
        assert paths[0] == tmp_path / "phases" / "phase-1.json"
        assert paths[1] == tmp_path / "phases" / "phase-2.json"
        assert all(p.exists() for p in paths)

    def test_phase_file_content(self, tmp_path: Path) -> None:
        ledger = self._make_ledger(
            phases=[
                {
                    "phase_id": "1",
                    "name": "Foundation",
                    "ticket_ids": ["T001", "T002"],
                    "status": "PASS",
                    "started_utc": "2026-01-01T00:00:00Z",
                    "completed_utc": "2026-01-01T01:00:00Z",
                },
            ],
            ticket_statuses={
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL", "failure_category": "timeout"},
            },
        )
        write_phase_summary(ledger, tmp_path)
        data = json.loads((tmp_path / "phases" / "phase-1.json").read_text())
        assert data["phase_id"] == "1"
        assert data["name"] == "Foundation"
        assert data["status"] == "PASS"
        assert data["started_utc"] == "2026-01-01T00:00:00Z"
        assert data["completed_utc"] == "2026-01-01T01:00:00Z"
        assert len(data["tickets"]) == 2
        t1 = next(t for t in data["tickets"] if t["ticket_id"] == "T001")
        assert t1["disposition"] == "PASS"
        t2 = next(t for t in data["tickets"] if t["ticket_id"] == "T002")
        assert t2["disposition"] == "FAIL"
        assert t2["failure_category"] == "timeout"

    def test_pending_ticket_not_in_ledger(self, tmp_path: Path) -> None:
        """Ticket referenced in phase but absent from ticket_statuses shows PENDING."""
        ledger = self._make_ledger(
            phases=[{"phase_id": "1", "ticket_ids": ["T001", "T999"]}],
            ticket_statuses={"T001": {"disposition": "PASS"}},
        )
        write_phase_summary(ledger, tmp_path)
        data = json.loads((tmp_path / "phases" / "phase-1.json").read_text())
        t999 = next(t for t in data["tickets"] if t["ticket_id"] == "T999")
        assert t999["disposition"] == "PENDING"

    def test_creates_phases_dir(self, tmp_path: Path) -> None:
        out = tmp_path / "new_out"
        ledger = self._make_ledger(
            phases=[{"phase_id": "alpha", "ticket_ids": ["T001"]}],
            ticket_statuses={"T001": {"disposition": "PASS"}},
        )
        write_phase_summary(ledger, out)
        assert (out / "phases" / "phase-alpha.json").exists()


# ---------------------------------------------------------------------------
# Analytics helpers and write_analytics
# ---------------------------------------------------------------------------


def _make_run_result(
    passed: int = 0,
    failed: int = 0,
    skipped: int = 0,
    ticket_results: tuple[TicketResult, ...] = (),
    definition_of_done_met: bool = False,
    stop_reason: str = "",
) -> RunResult:
    """Factory for RunResult in analytics tests."""
    return RunResult(
        project_id="test-project",
        total_tickets=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        ticket_results=ticket_results,
        definition_of_done_met=definition_of_done_met,
        stop_reason=stop_reason,
    )


class TestCountFailureCategories:
    def test_empty_returns_empty(self) -> None:
        result = _make_run_result()
        assert _count_failure_categories(result) == {}

    def test_only_passes_returns_empty(self) -> None:
        result = _make_run_result(
            passed=2,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS"),
                TicketResult(ticket_id="T002", disposition="PASS"),
            ),
        )
        assert _count_failure_categories(result) == {}

    def test_categorised_failures(self) -> None:
        result = _make_run_result(
            failed=3,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="FAIL", failure_category="governance"),
                TicketResult(ticket_id="T002", disposition="FAIL", failure_category="governance"),
                TicketResult(ticket_id="T003", disposition="FAIL", failure_category="timeout"),
            ),
        )
        dist = _count_failure_categories(result)
        assert dist == {"governance": 2, "timeout": 1}

    def test_uncategorised_fallback(self) -> None:
        result = _make_run_result(
            failed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="FAIL", failure_category=""),
            ),
        )
        dist = _count_failure_categories(result)
        assert dist == {"uncategorised": 1}

    def test_mixed_dispositions_only_fails_counted(self) -> None:
        result = _make_run_result(
            passed=1,
            failed=1,
            skipped=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS"),
                TicketResult(ticket_id="T002", disposition="FAIL", failure_category="coder_error"),
                TicketResult(ticket_id="T003", disposition="SKIP"),
            ),
        )
        dist = _count_failure_categories(result)
        assert dist == {"coder_error": 1}
        assert "PASS" not in dist
        assert "SKIP" not in dist


class TestComputeQualityVerdict:
    def test_all_pass_green(self) -> None:
        result = _make_run_result(
            passed=5,
            ticket_results=tuple(
                TicketResult(ticket_id=f"T00{i}", disposition="PASS", governance_disposition="PASS")
                for i in range(5)
            ),
        )
        verdict = _compute_quality_verdict(result)
        assert verdict["quality_level"] == "green"
        assert verdict["governance_pass_count"] == 5
        assert verdict["governance_fail_count"] == 0
        assert verdict["error_count"] == 0

    def test_high_failure_rate_red(self) -> None:
        result = _make_run_result(
            passed=1,
            failed=4,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", governance_disposition="PASS"),
                TicketResult(ticket_id="T002", disposition="FAIL", governance_disposition="FAIL", error="e"),
                TicketResult(ticket_id="T003", disposition="FAIL", governance_disposition="FAIL", error="e"),
                TicketResult(ticket_id="T004", disposition="FAIL", governance_disposition="FAIL", error="e"),
                TicketResult(ticket_id="T005", disposition="FAIL", governance_disposition="FAIL", error="e"),
            ),
        )
        verdict = _compute_quality_verdict(result)
        assert verdict["quality_level"] == "red"
        assert verdict["governance_fail_count"] == 4
        assert verdict["error_count"] == 4

    def test_partial_failure_yellow(self) -> None:
        result = _make_run_result(
            passed=3,
            failed=2,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", governance_disposition="PASS"),
                TicketResult(ticket_id="T002", disposition="PASS", governance_disposition="PASS"),
                TicketResult(ticket_id="T003", disposition="PASS", governance_disposition="PASS"),
                TicketResult(ticket_id="T004", disposition="FAIL", governance_disposition="FAIL", error="e"),
                TicketResult(ticket_id="T005", disposition="FAIL", governance_disposition="FAIL", error="e"),
            ),
        )
        verdict = _compute_quality_verdict(result)
        assert verdict["quality_level"] == "yellow"

    def test_empty_run_result(self) -> None:
        result = _make_run_result()
        verdict = _compute_quality_verdict(result)
        assert verdict["quality_level"] == "red"
        assert verdict["governance_pass_count"] == 0
        assert verdict["governance_fail_count"] == 0


class TestComputeRunAnalytics:
    def test_basic_structure(self) -> None:
        result = _make_run_result(
            passed=2,
            failed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T002", disposition="PASS", attempts=2),
                TicketResult(ticket_id="T003", disposition="FAIL", attempts=3, failure_category="governance"),
            ),
        )
        analytics = compute_run_analytics(result)

        required_keys = {
            "acceptance_rate",
            "avg_retries",
            "failure_category_distribution",
            "total_tickets",
            "passed",
            "failed",
            "skipped",
            "stop_reason",
            "definition_of_done_met",
            "backend",
            "auth_mode",
            "usage_accounting",
            "senior_quality_verdict",
        }
        assert required_keys <= set(analytics.keys())

    def test_acceptance_rate(self) -> None:
        result = _make_run_result(
            passed=3,
            failed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T002", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T003", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T004", disposition="FAIL", attempts=1),
            ),
        )
        analytics = compute_run_analytics(result)
        assert analytics["acceptance_rate"] == 0.75

    def test_zero_total_tickets_no_division_error(self) -> None:
        result = _make_run_result()
        analytics = compute_run_analytics(result)
        assert analytics["acceptance_rate"] == 0.0
        assert analytics["avg_retries"] == 0.0

    def test_avg_retries(self) -> None:
        result = _make_run_result(
            passed=2,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T002", disposition="PASS", attempts=3),
            ),
        )
        analytics = compute_run_analytics(result)
        assert analytics["avg_retries"] == 2.0

    def test_failure_category_distribution_present(self) -> None:
        result = _make_run_result(
            failed=2,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="FAIL", attempts=1, failure_category="timeout"),
                TicketResult(ticket_id="T002", disposition="FAIL", attempts=1, failure_category="timeout"),
            ),
        )
        analytics = compute_run_analytics(result)
        assert analytics["failure_category_distribution"] == {"timeout": 2}

    def test_senior_quality_verdict_included(self) -> None:
        result = _make_run_result(
            passed=4,
            ticket_results=tuple(
                TicketResult(ticket_id=f"T00{i}", disposition="PASS", governance_disposition="PASS", attempts=1)
                for i in range(4)
            ),
        )
        analytics = compute_run_analytics(result)
        verdict = analytics["senior_quality_verdict"]
        assert "quality_level" in verdict
        assert verdict["quality_level"] == "green"

    def test_backend_auth_defaults_empty(self) -> None:
        result = _make_run_result()
        analytics = compute_run_analytics(result)
        assert analytics["backend"] == ""
        assert analytics["auth_mode"] == ""

    def test_usage_accounting_present(self) -> None:
        result = _make_run_result()
        analytics = compute_run_analytics(result)
        assert "usage_accounting" in analytics
        ua = analytics["usage_accounting"]
        assert "build_cost" in ua
        assert "generated_product_runtime_cost" in ua

    def test_stop_reason_propagated(self) -> None:
        result = _make_run_result(stop_reason="consecutive_failure_limit: 3 consecutive failures")
        analytics = compute_run_analytics(result)
        assert "consecutive_failure_limit" in analytics["stop_reason"]

    def test_definition_of_done_met_propagated(self) -> None:
        result = _make_run_result(
            passed=2,
            definition_of_done_met=True,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T002", disposition="PASS", attempts=1),
            ),
        )
        analytics = compute_run_analytics(result)
        assert analytics["definition_of_done_met"] is True


class TestWriteAnalytics:
    def test_writes_analytics_json(self, tmp_path: Path) -> None:
        result = _make_run_result(
            passed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
            ),
        )
        analytics = compute_run_analytics(result)
        path = write_analytics(analytics, tmp_path)
        assert path == tmp_path / "analytics.json"
        assert path.exists()

    def test_analytics_json_content(self, tmp_path: Path) -> None:
        result = _make_run_result(
            passed=2,
            failed=1,
            ticket_results=(
                TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
                TicketResult(ticket_id="T002", disposition="PASS", attempts=2),
                TicketResult(ticket_id="T003", disposition="FAIL", attempts=3, failure_category="governance"),
            ),
        )
        analytics = compute_run_analytics(result)
        write_analytics(analytics, tmp_path)
        data = json.loads((tmp_path / "analytics.json").read_text())
        assert data["total_tickets"] == 3
        assert data["passed"] == 2
        assert data["failed"] == 1
        assert data["acceptance_rate"] == pytest.approx(2 / 3)
        assert "senior_quality_verdict" in data
        assert "quality_level" in data["senior_quality_verdict"]

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "out"
        result = _make_run_result()
        analytics = compute_run_analytics(result)
        path = write_analytics(analytics, out)
        assert path.exists()

    def test_analytics_json_is_valid_json(self, tmp_path: Path) -> None:
        result = _make_run_result()
        analytics = compute_run_analytics(result)
        write_analytics(analytics, tmp_path)
        raw = (tmp_path / "analytics.json").read_text()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
