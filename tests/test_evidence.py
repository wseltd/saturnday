"""Tests for evidence.py — structured evidence pack writer."""

import json
from pathlib import Path

import pytest

from saturnday.evidence import (
    SCHEMA_VERSION,
    CheckResult,
    EvidencePack,
    compute_disposition,
    generate_summary_md,
    write_evidence_dir,
)


def _make_check(name, status="PASS", severity="error", findings=None):
    return CheckResult(
        name=name,
        status=status,
        severity=severity,
        findings=findings or [],
        files_checked=["src/app.py"],
        elapsed_s=0.5,
    )


def _make_pack(**overrides):
    defaults = dict(
        schema_version=SCHEMA_VERSION,
        run_id="test-run-001",
        mode="check",
        repo_path="/tmp/repo",
        diff_range="HEAD~1..HEAD",
        saturnday_version="0.1.0",
        created_utc="2026-03-08T12:00:00Z",
        ended_utc="2026-03-08T12:00:05Z",
        check_results=[],
        disposition="PASS",
        disposition_reasons=[],
    )
    defaults.update(overrides)
    return EvidencePack(**defaults)


class TestComputeDisposition:
    def test_all_pass(self):
        results = [_make_check("syntax"), _make_check("secrets")]
        disposition, reasons = compute_disposition(results)
        assert disposition == "PASS"
        assert reasons == []

    def test_error_severity_fail(self):
        results = [
            _make_check("syntax", status="FAIL", severity="error", findings=[{"msg": "bad"}]),
            _make_check("secrets"),
        ]
        disposition, reasons = compute_disposition(results)
        assert disposition == "FAIL"
        assert len(reasons) == 1
        assert reasons[0]["check"] == "syntax"

    def test_warning_severity_fail(self):
        results = [
            _make_check("ruff", status="FAIL", severity="warning", findings=[{"msg": "lint"}]),
            _make_check("secrets"),
        ]
        disposition, reasons = compute_disposition(results)
        assert disposition == "WARN"
        assert len(reasons) == 1

    def test_warn_status(self):
        results = [
            CheckResult(name="ruff", status="WARN", severity="warning", findings=[{"msg": "x"}]),
        ]
        disposition, reasons = compute_disposition(results)
        assert disposition == "WARN"

    def test_error_trumps_warning(self):
        results = [
            _make_check("syntax", status="FAIL", severity="error", findings=[{"a": 1}]),
            _make_check("ruff", status="FAIL", severity="warning", findings=[{"b": 2}]),
        ]
        disposition, reasons = compute_disposition(results)
        assert disposition == "FAIL"
        assert len(reasons) == 2

    def test_empty_results(self):
        disposition, reasons = compute_disposition([])
        assert disposition == "PASS"
        assert reasons == []

    def test_skipped_does_not_affect(self):
        results = [_make_check("syntax", status="SKIPPED", severity="error")]
        disposition, reasons = compute_disposition(results)
        assert disposition == "PASS"

    def test_info_severity_fail(self):
        results = [_make_check("ruff", status="FAIL", severity="info")]
        disposition, reasons = compute_disposition(results)
        assert disposition == "PASS"  # info-severity failures don't trigger warn or fail


class TestSummaryMd:
    def test_basic_summary(self):
        pack = _make_pack(
            check_results=[_make_check("syntax"), _make_check("secrets")],
        )
        md = generate_summary_md(pack)
        assert "# Evidence Pack Summary" in md
        assert "syntax" in md
        assert "secrets" in md
        assert "PASS" in md

    def test_summary_with_issues(self):
        pack = _make_pack(
            disposition="FAIL",
            disposition_reasons=[
                {"check": "syntax", "status": "FAIL", "severity": "error", "finding_count": 2}
            ],
            check_results=[
                _make_check("syntax", status="FAIL", severity="error", findings=[{}, {}]),
            ],
        )
        md = generate_summary_md(pack)
        assert "## Issues" in md
        assert "syntax" in md

    def test_summary_includes_diff_range(self):
        pack = _make_pack(diff_range="abc..def")
        md = generate_summary_md(pack)
        assert "abc..def" in md

    def test_summary_includes_policy(self):
        pack = _make_pack(policy_path="/tmp/policy.yaml")
        md = generate_summary_md(pack)
        assert "/tmp/policy.yaml" in md


class TestWriteEvidenceDir:
    def test_creates_directory_structure(self, tmp_path):
        pack = _make_pack(
            check_results=[_make_check("syntax"), _make_check("ruff", severity="warning")],
        )
        result = write_evidence_dir(pack, tmp_path / "evidence")
        assert result.exists()
        assert (result / "run-metadata.json").exists()
        assert (result / "final-disposition.json").exists()
        assert (result / "summary.md").exists()
        assert (result / "verification" / "syntax.json").exists()
        assert (result / "verification" / "ruff.json").exists()

    def test_metadata_content(self, tmp_path):
        pack = _make_pack()
        write_evidence_dir(pack, tmp_path / "evidence")
        metadata = json.loads((tmp_path / "evidence" / "run-metadata.json").read_text())
        assert metadata["run_id"] == "test-run-001"
        assert metadata["mode"] == "check"
        assert metadata["schema_version"] == SCHEMA_VERSION

    def test_disposition_content(self, tmp_path):
        pack = _make_pack(
            disposition="FAIL",
            disposition_reasons=[{"check": "x", "status": "FAIL", "severity": "error", "finding_count": 1}],
            check_results=[_make_check("x", status="FAIL")],
        )
        write_evidence_dir(pack, tmp_path / "evidence")
        disp = json.loads((tmp_path / "evidence" / "final-disposition.json").read_text())
        assert disp["disposition"] == "FAIL"
        assert disp["fail_count"] == 1

    def test_verification_json_content(self, tmp_path):
        findings = [{"file": "a.py", "line": 1, "message": "bad"}]
        pack = _make_pack(
            check_results=[_make_check("syntax", findings=findings)],
        )
        write_evidence_dir(pack, tmp_path / "evidence")
        data = json.loads((tmp_path / "evidence" / "verification" / "syntax.json").read_text())
        assert data["name"] == "syntax"
        assert len(data["findings"]) == 1

    def test_empty_checks(self, tmp_path):
        pack = _make_pack()
        result = write_evidence_dir(pack, tmp_path / "evidence")
        assert result.exists()
        disp = json.loads((result / "final-disposition.json").read_text())
        assert disp["check_count"] == 0

    def test_idempotent_overwrite(self, tmp_path):
        pack = _make_pack(check_results=[_make_check("syntax")])
        write_evidence_dir(pack, tmp_path / "evidence")
        write_evidence_dir(pack, tmp_path / "evidence")  # no error
        assert (tmp_path / "evidence" / "run-metadata.json").exists()
