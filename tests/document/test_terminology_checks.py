"""Tests for saturnday.document.checks.terminology."""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentSection, DocumentSpec
from saturnday.document.checks.terminology import check_terminology


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def section() -> DocumentSection:
    return DocumentSection(
        section_id="S001",
        name="Executive Summary",
        purpose="High-level overview",
    )


def make_spec(required_terms: list[str] | None = None, banned_terms: list[str] | None = None) -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Q3 Analysis",
        audience="Board",
        risk_class="high",
        required_sections=["Executive Summary"],
        claim_policy={},
        approved_sources=["data.md"],
        sign_off_roles=["CFO"],
        required_terminology=required_terms or [],
        banned_terminology=banned_terms or [],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckTerminology:
    def test_no_terms_configured_no_findings(self, section: DocumentSection) -> None:
        spec = make_spec()
        content = "# Executive Summary\n\nQ3 was strong."
        findings = check_terminology(content, section, spec)
        assert findings == []

    def test_required_term_present_no_finding(self, section: DocumentSection) -> None:
        spec = make_spec(required_terms=["EBITDA"])
        content = "# Executive Summary\n\nEBITDA grew by 12% this quarter."
        findings = check_terminology(content, section, spec)
        assert not any(f.kind == "missing_required_term" for f in findings)

    def test_required_term_missing_flagged(self, section: DocumentSection) -> None:
        spec = make_spec(required_terms=["EBITDA"])
        content = "# Executive Summary\n\nRevenue grew this quarter."
        findings = check_terminology(content, section, spec)
        assert any(f.kind == "missing_required_term" for f in findings)

    def test_required_term_case_insensitive(self, section: DocumentSection) -> None:
        spec = make_spec(required_terms=["ebitda"])
        content = "# Executive Summary\n\nEBITDA grew strongly."
        findings = check_terminology(content, section, spec)
        assert not any(f.kind == "missing_required_term" for f in findings)

    def test_banned_term_absent_no_finding(self, section: DocumentSection) -> None:
        spec = make_spec(banned_terms=["synergy"])
        content = "# Executive Summary\n\nRevenue increased substantially."
        findings = check_terminology(content, section, spec)
        assert not any(f.kind == "banned_term_present" for f in findings)

    def test_banned_term_present_flagged(self, section: DocumentSection) -> None:
        spec = make_spec(banned_terms=["synergy"])
        content = "# Executive Summary\n\nLeverage synergy to drive growth."
        findings = check_terminology(content, section, spec)
        assert any(f.kind == "banned_term_present" for f in findings)

    def test_banned_term_finding_severity_error(self, section: DocumentSection) -> None:
        spec = make_spec(banned_terms=["leverage"])
        content = "# Executive Summary\n\nLeverage the opportunity."
        findings = check_terminology(content, section, spec)
        banned = [f for f in findings if f.kind == "banned_term_present"]
        assert all(f.severity == "error" for f in banned)

    def test_abbreviation_defined_once_no_finding(self, section: DocumentSection) -> None:
        spec = make_spec()
        content = (
            "# Executive Summary\n\n"
            "Earnings Before Interest Tax Depreciation Amortisation (EBITDA) grew."
        )
        findings = check_terminology(content, section, spec)
        abbr_findings = [f for f in findings if f.kind == "abbreviation_redefined"]
        assert abbr_findings == []

    def test_abbreviation_redefined_flagged(self, section: DocumentSection) -> None:
        spec = make_spec()
        content = (
            "# Executive Summary\n\n"
            "Net Revenue (NR) was strong. "
            "Net Result (NR) is another metric."
        )
        findings = check_terminology(content, section, spec)
        assert any(f.kind == "abbreviation_redefined" for f in findings)

    def test_finding_ids_use_doc_term_prefix(self, section: DocumentSection) -> None:
        spec = make_spec(required_terms=["EBITDA"], banned_terms=["synergy"])
        content = "# Executive Summary\n\nLeverage synergy to maximise gains."
        findings = check_terminology(content, section, spec)
        if findings:
            assert all(f.finding_id.startswith("DOC-TERM-") for f in findings)
