"""Tests for saturnday.document.checks.citations."""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentSection
from saturnday.document.checks.citations import (
    _is_valid_doi,
    _is_valid_url,
    _strip_fenced_code_blocks,
    check_citations,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def section() -> DocumentSection:
    return DocumentSection(
        section_id="S001",
        name="Financial Results",
        purpose="Detail Q3 financials",
    )


APPROVED_SOURCES = ["data/q3.csv", "reports/forecast.md"]
ALL_SECTIONS = ["Executive Summary", "Financial Results", "Conclusion"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckCitations:
    def test_clean_content_no_findings(self, section: DocumentSection) -> None:
        content = "# Financial Results\n\nRevenue grew 15% YoY."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        assert findings == []

    def test_valid_doi_no_finding(self, section: DocumentSection) -> None:
        content = "See study 10.1000/xyz123 for details."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        doi_findings = [f for f in findings if f.kind == "malformed_doi"]
        assert doi_findings == []

    def test_malformed_doi_flagged(self, section: DocumentSection) -> None:
        content = "See study 10.xyz/missing for details."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        assert any(f.kind == "malformed_doi" for f in findings)

    def test_doi_missing_suffix_flagged(self, section: DocumentSection) -> None:
        # DOI with no suffix after slash
        content = "Reference: 10.1234/"
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        assert any(f.kind == "malformed_doi" for f in findings)

    def test_valid_url_no_finding(self, section: DocumentSection) -> None:
        content = "See https://example.com/report for more."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        url_findings = [f for f in findings if f.kind == "malformed_url"]
        assert url_findings == []

    def test_malformed_url_flagged(self, section: DocumentSection) -> None:
        content = "See http:///no-netloc for details."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        assert any(f.kind == "malformed_url" for f in findings)

    def test_internal_ref_to_valid_section_no_finding(
        self, section: DocumentSection
    ) -> None:
        content = "As discussed in [see Executive Summary], revenue grew."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        ref_findings = [f for f in findings if f.kind == "unresolved_internal_ref"]
        assert ref_findings == []

    def test_internal_ref_to_unknown_section_flagged(
        self, section: DocumentSection
    ) -> None:
        content = "See [see Market Analysis] for context."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        assert any(f.kind == "unresolved_internal_ref" for f in findings)

    def test_broken_bracket_flagged(self, section: DocumentSection) -> None:
        content = "Revenue grew per [Source: q3.csv"
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        assert any(f.kind == "broken_citation_token" for f in findings)

    def test_citation_in_code_block_not_flagged(self, section: DocumentSection) -> None:
        content = (
            "# Financial Results\n\n"
            "Clean narrative.\n\n"
            "```\n# TODO: broken [ bracket here\n```"
        )
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        broken = [f for f in findings if f.kind == "broken_citation_token"]
        assert broken == []

    def test_finding_ids_use_doc_cite_prefix(self, section: DocumentSection) -> None:
        content = "See [see Unknown Section] and 10.bad/doi for details."
        findings = check_citations(content, section, APPROVED_SOURCES, ALL_SECTIONS)
        if findings:
            assert all(f.finding_id.startswith("DOC-CITE-") for f in findings)


class TestCitationHelpers:
    def test_valid_doi_format(self) -> None:
        assert _is_valid_doi("10.1000/xyz123")
        assert _is_valid_doi("10.12345/some.suffix-here")

    def test_invalid_doi_not_starting_with_10(self) -> None:
        assert not _is_valid_doi("11.1000/xyz")

    def test_invalid_doi_non_digit_after_10(self) -> None:
        assert not _is_valid_doi("10.xyz/abc")

    def test_valid_url_https(self) -> None:
        assert _is_valid_url("https://example.com/page")

    def test_valid_url_http(self) -> None:
        assert _is_valid_url("http://example.org")

    def test_invalid_url_no_netloc(self) -> None:
        assert not _is_valid_url("http:///path")

    def test_invalid_url_ftp_scheme(self) -> None:
        assert not _is_valid_url("ftp://example.com/file")

    def test_strip_fenced_code_blocks(self) -> None:
        content = "Before.\n```\ncode here\n```\nAfter."
        result = _strip_fenced_code_blocks(content)
        assert "code here" not in result
        assert "Before." in result
        assert "After." in result
