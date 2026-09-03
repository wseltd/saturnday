"""Tests for saturnday.run.report."""

import json
from pathlib import Path

import pytest

from saturnday.run.report import generate_failure_report


def _create_ledger(tmp_path: Path, data: dict) -> Path:
    """Helper: create a ledger.json in evidence directory."""
    evidence_dir = tmp_path / "evidence" / "run"
    evidence_dir.mkdir(parents=True)
    ledger_path = evidence_dir / "ledger.json"
    ledger_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return tmp_path


class TestGenerateFailureReport:
    def test_basic_report(self, tmp_path: Path) -> None:
        output_dir = _create_ledger(tmp_path, {
            "definition_of_done": ["all_tickets_passed"],
            "total_executed": 3,
            "total_failed": 1,
            "ticket_statuses": {
                "T001": {"ticket_id": "T001", "disposition": "PASS", "failure_category": "", "reasons": []},
                "T002": {"ticket_id": "T002", "disposition": "FAIL", "failure_category": "timeout", "reasons": ["timed out"]},
                "T003": {"ticket_id": "T003", "disposition": "SKIP", "failure_category": "", "reasons": []},
            },
            "phase_statuses": [],
            "stop_reason": "",
        })
        report = generate_failure_report(output_dir)
        assert "Saturnday Run Report" in report
        assert "T002" in report
        assert "Failed" in report

    def test_with_stop_reason(self, tmp_path: Path) -> None:
        output_dir = _create_ledger(tmp_path, {
            "definition_of_done": [],
            "total_executed": 3,
            "total_failed": 3,
            "ticket_statuses": {
                "T001": {"ticket_id": "T001", "disposition": "FAIL", "failure_category": "", "reasons": []},
                "T002": {"ticket_id": "T002", "disposition": "FAIL", "failure_category": "", "reasons": []},
                "T003": {"ticket_id": "T003", "disposition": "FAIL", "failure_category": "", "reasons": []},
            },
            "phase_statuses": [],
            "stop_reason": "consecutive_failure_limit: 3",
        })
        report = generate_failure_report(output_dir)
        assert "consecutive_failure_limit" in report

    def test_missing_ledger_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            generate_failure_report(tmp_path)

    def test_writes_report_md(self, tmp_path: Path) -> None:
        output_dir = _create_ledger(tmp_path, {
            "definition_of_done": [],
            "total_executed": 1,
            "total_failed": 0,
            "ticket_statuses": {
                "T001": {"ticket_id": "T001", "disposition": "PASS", "failure_category": "", "reasons": []},
            },
            "phase_statuses": [],
            "stop_reason": "",
        })
        generate_failure_report(output_dir)
        report_path = output_dir / "evidence" / "run" / "report.md"
        assert report_path.exists()
