"""Tests for the shared evidence-type detector.

Covers:
1. Run evidence classified correctly
2. Governance evidence classified correctly
3. Release evidence classified correctly
4. Unknown directories classified as unknown
"""
from __future__ import annotations

import json
from pathlib import Path

from saturnday.evidence_type import detect_evidence_type


class TestRunEvidence:
    def test_run_with_ledger(self, tmp_path: Path) -> None:
        ledger = tmp_path / "evidence" / "run" / "ledger.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("{}", encoding="utf-8")
        assert detect_evidence_type(tmp_path) == "run"


class TestGovernanceEvidence:
    def test_governance_with_report(self, tmp_path: Path) -> None:
        (tmp_path / "governance-report.md").write_text("# Report", encoding="utf-8")
        assert detect_evidence_type(tmp_path) == "governance"

    def test_governance_with_disposition_only(self, tmp_path: Path) -> None:
        (tmp_path / "final-disposition.json").write_text("{}", encoding="utf-8")
        assert detect_evidence_type(tmp_path) == "governance"


class TestReleaseEvidence:
    def test_release_with_evidence_json(self, tmp_path: Path) -> None:
        (tmp_path / "evidence.json").write_text("{}", encoding="utf-8")
        assert detect_evidence_type(tmp_path) == "release"


class TestUnknown:
    def test_empty_dir(self, tmp_path: Path) -> None:
        assert detect_evidence_type(tmp_path) == "unknown"

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert detect_evidence_type(tmp_path / "nonexistent") == "unknown"

    def test_random_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").write_text("hello", encoding="utf-8")
        assert detect_evidence_type(tmp_path) == "unknown"
