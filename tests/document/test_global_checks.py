"""Tests for saturnday.document.global_checks."""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentPlan, DocumentSection
from saturnday.document.global_checks import run_global_checks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(global_checks: list[str] | None = None) -> DocumentPlan:
    return DocumentPlan(
        document_id="doc-001",
        type="report",
        purpose="Test",
        risk_class="medium",
        sections=[],
        global_checks=global_checks or [],
    )


def _sr(section_id: str, content: str, status: str = "PASS") -> dict:
    return {"section_id": section_id, "content": content, "status": status}


# ---------------------------------------------------------------------------
# Tests: metric contradiction
# ---------------------------------------------------------------------------


class TestMetricContradiction:
    def test_consistent_metrics_no_findings(self):
        sections = [
            _sr("S001", "Revenue: $1.2M in Q3"),
            _sr("S002", "Revenue: $1.2M for the quarter"),
        ]
        findings = run_global_checks(sections, _make_plan())
        metric_findings = [f for f in findings if f.kind == "cross_section_metric_mismatch"]
        assert len(metric_findings) == 0

    def test_contradicting_metric_values_flagged(self):
        sections = [
            _sr("S001", "Revenue: $1.2M"),
            _sr("S002", "Revenue: $1.4M"),
        ]
        findings = run_global_checks(sections, _make_plan())
        metric_findings = [f for f in findings if f.kind == "cross_section_metric_mismatch"]
        assert len(metric_findings) >= 1
        assert metric_findings[0].severity == "error"
        assert "S001" in metric_findings[0].detail or "S002" in metric_findings[0].detail

    def test_metric_finding_has_global_prefix(self):
        sections = [
            _sr("S001", "Headcount: 100"),
            _sr("S002", "Headcount: 150"),
        ]
        findings = run_global_checks(sections, _make_plan())
        metric_findings = [f for f in findings if f.kind == "cross_section_metric_mismatch"]
        assert all(f.finding_id.startswith("DOC-GLOBAL-") for f in metric_findings)

    def test_same_metric_in_same_section_not_flagged(self):
        # Both references are in the same section — should not be flagged
        sections = [
            _sr("S001", "Revenue: $1.2M and later Revenue: $1.4M"),
        ]
        findings = run_global_checks(sections, _make_plan())
        metric_findings = [f for f in findings if f.kind == "cross_section_metric_mismatch"]
        assert len(metric_findings) == 0


# ---------------------------------------------------------------------------
# Tests: unresolved provisional
# ---------------------------------------------------------------------------


class TestUnresolvedProvisional:
    def test_no_provisional_no_findings(self):
        sections = [
            _sr("S001", "Content A", status="PASS"),
            _sr("S002", "Content B", status="WARN"),
        ]
        findings = run_global_checks(sections, _make_plan())
        prov_findings = [f for f in findings if f.kind == "unresolved_provisional"]
        assert len(prov_findings) == 0

    def test_provisional_section_flagged(self):
        sections = [
            _sr("S001", "Content A", status="PASS"),
            _sr("S002", "Content B", status="PROVISIONAL_UNVERIFIED"),
        ]
        findings = run_global_checks(sections, _make_plan())
        prov_findings = [f for f in findings if f.kind == "unresolved_provisional"]
        assert len(prov_findings) == 1
        assert prov_findings[0].section_id == "S002"
        assert prov_findings[0].severity == "warning"

    def test_multiple_provisional_sections_each_flagged(self):
        sections = [
            _sr("S001", "Content A", status="PROVISIONAL_UNVERIFIED"),
            _sr("S002", "Content B", status="PROVISIONAL_UNVERIFIED"),
        ]
        findings = run_global_checks(sections, _make_plan())
        prov_findings = [f for f in findings if f.kind == "unresolved_provisional"]
        assert len(prov_findings) == 2


# ---------------------------------------------------------------------------
# Tests: required sections missing
# ---------------------------------------------------------------------------


class TestRequiredSectionsMissing:
    def test_no_global_checks_list_is_noop(self):
        sections = [_sr("S001", "Content")]
        findings = run_global_checks(sections, _make_plan(global_checks=[]))
        missing_findings = [f for f in findings if f.kind == "required_section_missing"]
        assert len(missing_findings) == 0

    def test_present_required_section_not_flagged(self):
        sections = [_sr("S001", "Content"), _sr("S002", "Content")]
        plan = _make_plan(global_checks=["S001", "S002"])
        findings = run_global_checks(sections, plan)
        missing_findings = [f for f in findings if f.kind == "required_section_missing"]
        assert len(missing_findings) == 0

    def test_missing_required_section_flagged(self):
        sections = [_sr("S001", "Content")]
        plan = _make_plan(global_checks=["S001", "S003"])
        findings = run_global_checks(sections, plan)
        missing_findings = [f for f in findings if f.kind == "required_section_missing"]
        assert len(missing_findings) == 1
        assert "S003" in missing_findings[0].detail
        assert missing_findings[0].severity == "error"


# ---------------------------------------------------------------------------
# Tests: summary vs body inconsistency
# ---------------------------------------------------------------------------


class TestSummaryVsBodyInconsistency:
    def test_summary_metric_matches_body_no_finding(self):
        sections = [
            _sr("executive_summary", "Revenue: $5M"),
            _sr("S002", "Revenue: $5M for Q3"),
        ]
        findings = run_global_checks(sections, _make_plan())
        mismatch = [f for f in findings if f.kind == "summary_body_mismatch"]
        assert len(mismatch) == 0

    def test_summary_metric_contradicts_body_flagged(self):
        sections = [
            _sr("executive_summary", "Revenue: $5M"),
            _sr("S002", "Revenue: $6M"),
        ]
        findings = run_global_checks(sections, _make_plan())
        mismatch = [f for f in findings if f.kind == "summary_body_mismatch"]
        assert len(mismatch) >= 1
        assert mismatch[0].severity == "error"

    def test_no_summary_section_is_noop(self):
        sections = [
            _sr("S001", "Revenue: $5M"),
            _sr("S002", "Revenue: $6M"),
        ]
        # No executive_summary section — summary_body check should not run
        findings = run_global_checks(sections, _make_plan())
        mismatch = [f for f in findings if f.kind == "summary_body_mismatch"]
        assert len(mismatch) == 0


# ---------------------------------------------------------------------------
# Tests: empty and edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_sections_returns_empty_findings(self):
        findings = run_global_checks([], _make_plan())
        assert findings == []

    def test_single_section_no_findings(self):
        sections = [_sr("S001", "Revenue: $1.2M")]
        findings = run_global_checks(sections, _make_plan())
        # No cross-section findings possible with single section
        cross_findings = [
            f for f in findings
            if f.kind in ("cross_section_metric_mismatch", "date_inconsistency", "terminology_drift")
        ]
        assert len(cross_findings) == 0
