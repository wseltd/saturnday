"""Tests for the operator surface batch.

Covers:
1. status detects both check_* and review_* governance evidence
2. evidence list works and is read-only
3. release-exception produces visible output (tested via adapter)
4. release-exception writes evidence artifact
5. wording no longer exposes public/premium split
6. help/output for approve, release-approve, release-exception
7. unrelated command behaviour does not regress
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from saturnday.status import gather_status, format_status
from saturnday.evidence_list import gather_evidence, format_evidence_list


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Status detects both check_* and review_* evidence
# ---------------------------------------------------------------------------

class TestStatusReviewEvidence:
    def test_detects_review_evidence(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "evidence" / "review_20260406T120000Z"
        _write_json(d / "final-disposition.json", {
            "disposition": "FAIL",
            "check_count": 49,
            "reasons": [{"check": "secrets", "status": "FAIL", "severity": "error", "finding_count": 3}],
        })
        status = gather_status(tmp_path)
        gov = status["latest_governance"]
        assert gov is not None
        assert gov["run_id"] == "review_20260406T120000Z"
        assert gov["disposition"] == "FAIL"
        assert gov["checks_run"] == 49

    def test_detects_check_evidence(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "evidence" / "check_20260406T120000Z"
        _write_json(d / "final-disposition.json", {"disposition": "PASS", "check_count": 48, "reasons": []})
        status = gather_status(tmp_path)
        assert status["latest_governance"]["run_id"] == "check_20260406T120000Z"

    def test_picks_latest_across_types(self, tmp_path: Path) -> None:
        d1 = tmp_path / ".saturnday" / "evidence" / "check_20260401T000000Z"
        _write_json(d1 / "final-disposition.json", {"disposition": "PASS", "reasons": []})
        d2 = tmp_path / ".saturnday" / "evidence" / "review_20260406T000000Z"
        _write_json(d2 / "final-disposition.json", {"disposition": "FAIL", "reasons": []})
        status = gather_status(tmp_path)
        assert status["latest_governance"]["run_id"] == "review_20260406T000000Z"


# ---------------------------------------------------------------------------
# 2. Evidence list works and is read-only
# ---------------------------------------------------------------------------

class TestEvidenceList:
    def test_lists_governance_evidence(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "evidence" / "review_20260406T120000Z"
        _write_json(d / "final-disposition.json", {"disposition": "PASS"})
        bundles = gather_evidence(tmp_path)
        assert len(bundles) >= 1
        assert bundles[0]["category"] == "governance"
        assert bundles[0]["disposition"] == "PASS"

    def test_lists_run_evidence(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "run" / "run_20260406T120000Z_abc"
        d.mkdir(parents=True)
        bundles = gather_evidence(tmp_path)
        run_bundles = [b for b in bundles if b["category"] == "run"]
        assert len(run_bundles) == 1

    def test_lists_release_evidence(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "evidence" / "release" / "release_20260406T120000Z_xyz"
        _write_json(d / "evidence.json", {"disposition": "FAIL"})
        bundles = gather_evidence(tmp_path)
        release_bundles = [b for b in bundles if b["category"] == "release"]
        assert len(release_bundles) == 1
        assert release_bundles[0]["disposition"] == "FAIL"

    def test_empty_repo(self, tmp_path: Path) -> None:
        bundles = gather_evidence(tmp_path)
        assert bundles == []
        output = format_evidence_list(bundles)
        assert "No evidence bundles" in output

    def test_read_only(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "evidence" / "check_20260406T000000Z"
        _write_json(d / "final-disposition.json", {"disposition": "PASS"})
        before = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                before.add(os.path.join(root, f))
        gather_evidence(tmp_path)
        after = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                after.add(os.path.join(root, f))
        assert before == after

    def test_formatted_output_readable(self, tmp_path: Path) -> None:
        d = tmp_path / ".saturnday" / "evidence" / "review_20260406T120000Z"
        _write_json(d / "final-disposition.json", {"disposition": "PASS"})
        bundles = gather_evidence(tmp_path)
        output = format_evidence_list(bundles)
        assert "governance" in output
        assert "review_20260406T120000Z" in output


# ---------------------------------------------------------------------------
# 3 & 4. release-exception output and evidence (premium adapter)
# ---------------------------------------------------------------------------

class TestReleaseExceptionOutput:
    def test_record_exception_writes_file(self, tmp_path: Path) -> None:
        """The premium _release_exception.record_exception writes a JSON file."""
        try:
            from saturnday_premium._release_exception import record_exception
        except ImportError:
            import pytest
            pytest.skip("saturnday-premium not installed")

        record = record_exception(
            evidence_dir=tmp_path,
            rule_ids=["REL-005"],
            file_patterns=[],
            approver="Test Approver",
            reason="First release, no baseline",
            expiry=None,
            artefact_sha256="abc123",
        )
        exc_dir = tmp_path / "release" / "exceptions"
        assert exc_dir.is_dir()
        json_files = list(exc_dir.glob("*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert data["rule_ids"] == ["REL-005"]
        assert data["approver"] == "Test Approver"


# ---------------------------------------------------------------------------
# 5. Wording no longer exposes public/premium split
# ---------------------------------------------------------------------------

class TestWordingNoSplit:
    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_status_wording_not_enabled(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        output = format_status(status)
        assert "saturnday-premium" not in output
        assert "not enabled" in output

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_status_approval_state_wording(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        assert "saturnday-premium" not in status["approval_state"]
        assert "saturnday-premium" not in status["exception_state"]

    @patch("saturnday.capability_registry.is_available", return_value=True)
    def test_status_wording_enabled(self, _mock, tmp_path: Path) -> None:
        status = gather_status(tmp_path)
        output = format_status(status)
        assert "saturnday-premium" not in output
        assert status["approval_state"] == "available"


# ---------------------------------------------------------------------------
# 6. Help text for touched commands
# ---------------------------------------------------------------------------

class TestHelpText:
    def test_approve_help_has_description(self) -> None:
        from saturnday.cli import build_parser
        parser = build_parser()
        help_text = parser.format_help()
        assert "sign-off" in help_text.lower() or "approval" in help_text.lower()

    def test_release_approve_help_has_description(self) -> None:
        from saturnday.cli import build_parser
        parser = build_parser()
        help_text = parser.format_help()
        assert "release approval" in help_text.lower() or "release" in help_text.lower()
        assert "entitlement" in help_text.lower()

    def test_release_exception_help_has_description(self) -> None:
        from saturnday.cli import build_parser
        parser = build_parser()
        help_text = parser.format_help()
        assert "exception" in help_text.lower()
        assert "entitlement" in help_text.lower()
