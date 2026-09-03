"""Tests for saturnday status command.

Covers:
1. Status on empty repo (no evidence)
2. Status shows policy path when present
3. Status shows baseline path when present
4. Status shows latest governance evidence when present
5. Status shows latest run evidence when present
6. Status handles missing premium capabilities cleanly
7. Status does not mutate repo state
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from unittest.mock import patch

from saturnday.status import format_status, gather_status


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Empty repo — no evidence
# ---------------------------------------------------------------------------

class TestEmptyRepo:
    def test_no_evidence(self, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        assert status["policy_path"] is None
        assert status["baseline_path"] is None
        assert status["latest_governance"] is None
        assert status["latest_run"] is None

    def test_formatted_output_says_none(self, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        output = format_status(status)
        assert "none" in output.lower()
        assert "saturnday governance" in output  # suggests what to run


# ---------------------------------------------------------------------------
# 2. Policy path
# ---------------------------------------------------------------------------

class TestPolicyPath:
    def test_shows_policy_when_present(self, tmp_path: Path) -> None:
        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
        status = gather_status(tmp_path)
        assert status["policy_path"] == str(policy)

    def test_formatted_output_includes_policy(self, tmp_path: Path) -> None:
        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text("schema_version: '1.0.0'\n", encoding="utf-8")
        output = format_status(gather_status(tmp_path))
        assert ".saturnday-policy.yaml" in output


# ---------------------------------------------------------------------------
# 3. Baseline path
# ---------------------------------------------------------------------------

class TestBaselinePath:
    def test_shows_baseline_when_present(self, tmp_path: Path) -> None:
        baseline = tmp_path / ".saturnday-baseline.json"
        baseline.write_text("{}", encoding="utf-8")
        status = gather_status(tmp_path)
        assert status["baseline_path"] == str(baseline)

    def test_no_baseline(self, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        assert status["baseline_path"] is None


# ---------------------------------------------------------------------------
# 4. Latest governance evidence
# ---------------------------------------------------------------------------

class TestLatestGovernance:
    def test_shows_latest_governance(self, tmp_path: Path) -> None:
        check_dir = tmp_path / ".saturnday" / "evidence" / "check_20260405T120000Z"
        _write_json(check_dir / "final-disposition.json", {
            "disposition": "PASS",
            "check_count": 48,
            "reasons": [
                {"check": "syntax", "status": "PASS", "severity": "error", "finding_count": 0},
                {"check": "secrets", "status": "PASS", "severity": "error", "finding_count": 0},
            ],
        })
        (check_dir / "governance-report.md").write_text("# Report\n", encoding="utf-8")

        status = gather_status(tmp_path)
        gov = status["latest_governance"]
        assert gov is not None
        assert gov["run_id"] == "check_20260405T120000Z"
        assert gov["disposition"] == "PASS"
        assert gov["checks_run"] == 48, "Must use check_count from disposition, not len(reasons)"
        assert gov["total_findings"] == 0
        assert gov["report_path"] is not None

    def test_checks_run_omitted_when_no_check_count(self, tmp_path: Path) -> None:
        """If check_count is not in disposition JSON, checks_run stays None."""
        check_dir = tmp_path / ".saturnday" / "evidence" / "check_20260405T120000Z"
        _write_json(check_dir / "final-disposition.json", {
            "disposition": "PASS",
            "reasons": [],
        })
        status = gather_status(tmp_path)
        assert status["latest_governance"]["checks_run"] is None

    def test_picks_latest_by_sort(self, tmp_path: Path) -> None:
        for run_id in ["check_20260401T000000Z", "check_20260405T000000Z"]:
            d = tmp_path / ".saturnday" / "evidence" / run_id
            _write_json(d / "final-disposition.json", {"disposition": "PASS", "reasons": []})
        status = gather_status(tmp_path)
        assert status["latest_governance"]["run_id"] == "check_20260405T000000Z"

    def test_shows_findings_count(self, tmp_path: Path) -> None:
        check_dir = tmp_path / ".saturnday" / "evidence" / "check_20260405T120000Z"
        _write_json(check_dir / "final-disposition.json", {
            "disposition": "FAIL",
            "reasons": [
                {"check": "secrets", "status": "FAIL", "severity": "error", "finding_count": 3},
                {"check": "ruff", "status": "FAIL", "severity": "warning", "finding_count": 5},
            ],
        })
        status = gather_status(tmp_path)
        assert status["latest_governance"]["total_findings"] == 8


# ---------------------------------------------------------------------------
# 5. Latest run evidence
# ---------------------------------------------------------------------------

class TestLatestRun:
    def test_shows_latest_run(self, tmp_path: Path) -> None:
        run_dir = tmp_path / ".saturnday" / "run" / "run_20260405T120000Z_abc"
        run_dir.mkdir(parents=True)
        (run_dir / "run-report.md").write_text("# Run Report\n", encoding="utf-8")
        _write_json(run_dir / "accepted-dod.json", {"project_id": "test"})

        status = gather_status(tmp_path)
        run = status["latest_run"]
        assert run is not None
        assert "run_20260405T120000Z_abc" in run["run_id"]
        assert run["report_path"] is not None
        assert run.get("accepted_dod_path") is not None


# ---------------------------------------------------------------------------
# 6. Premium capabilities
# ---------------------------------------------------------------------------

class TestPremiumCapabilities:
    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_premium_not_available(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        premium = status["premium_available"]
        assert premium["approval_workflow"] is False
        assert premium["release_governance"] is False

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_formatted_output_says_not_enabled(self, _mock, tmp_path: Path) -> None:
        output = format_status(gather_status(tmp_path))
        assert "not enabled" in output.lower()

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_approval_state_not_enabled(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        assert "not enabled" in status["approval_state"]

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_exception_state_not_enabled(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        assert "not enabled" in status["exception_state"]

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_formatted_approval_exception_lines(self, _mock, tmp_path: Path) -> None:
        output = format_status(gather_status(tmp_path))
        assert "Approval state:" in output
        assert "Exception state:" in output
        assert "not enabled" in output

    @patch("saturnday.capability_registry.is_available", return_value=True)
    def test_premium_enabled(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        assert status["premium_available"]["approval_workflow"] is True
        assert status["approval_state"] == "available"


# ---------------------------------------------------------------------------
# 7. Does not mutate repo state
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_no_files_created(self, tmp_path: Path) -> None:
        before = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                before.add(os.path.join(root, f))

        gather_status(tmp_path)

        after = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                after.add(os.path.join(root, f))

        assert before == after, "gather_status must not create any files"
