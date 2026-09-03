"""Tests for saturnday.document.checks.structure."""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentSection, DocumentSpec
from saturnday.document.checks.structure import (
    _find_empty_heading_blocks,
    _has_minimum_content,
    _has_required_heading,
    _strip_fenced_code_blocks,
    check_structure,
)


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


@pytest.fixture()
def spec() -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Q3 Analysis",
        audience="Board",
        risk_class="high",
        required_sections=["Executive Summary"],
        claim_policy={},
        approved_sources=["data.md"],
        sign_off_roles=["CFO"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckStructure:
    def test_valid_section_returns_no_findings(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\nQ3 performance exceeded targets by 12%."
        findings = check_structure(content, section, spec)
        assert findings == []

    def test_missing_heading_returns_error(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "Q3 performance exceeded targets."
        findings = check_structure(content, section, spec)
        assert any(f.kind == "missing_required_heading" for f in findings)
        assert any(f.severity == "error" for f in findings)

    def test_todo_placeholder_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\nTODO: add revenue numbers here."
        findings = check_structure(content, section, spec)
        assert any(f.kind == "placeholder_detected" for f in findings)

    def test_lorem_ipsum_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\nlorem ipsum dolor sit amet."
        findings = check_structure(content, section, spec)
        assert any(f.kind == "placeholder_detected" for f in findings)

    def test_citation_needed_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\nRevenue grew [citation needed]."
        findings = check_structure(content, section, spec)
        assert any(f.kind == "placeholder_detected" for f in findings)

    def test_placeholder_in_code_block_not_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = (
            "# Executive Summary\n\n"
            "Real content here.\n\n"
            "```python\n# TODO: implement this\nx = 1\n```"
        )
        findings = check_structure(content, section, spec)
        # No placeholder finding because TODO is inside code block
        placeholder_findings = [f for f in findings if f.kind == "placeholder_detected"]
        assert placeholder_findings == []

    def test_empty_heading_block_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\n## Background\n\n## Financials\n\nData here."
        findings = check_structure(content, section, spec)
        assert any(f.kind == "empty_heading_block" for f in findings)

    def test_heading_only_content_flagged_as_insufficient(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n## Sub-heading"
        findings = check_structure(content, section, spec)
        assert any(f.kind == "insufficient_content" for f in findings)

    def test_multiple_violations_multiple_findings(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\nTODO: fill this in.\n\n[PLACEHOLDER]"
        findings = check_structure(content, section, spec)
        placeholder_findings = [f for f in findings if f.kind == "placeholder_detected"]
        assert len(placeholder_findings) >= 2

    def test_finding_ids_use_doc_struct_prefix(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "No heading here. TODO: fix."
        findings = check_structure(content, section, spec)
        assert all(f.finding_id.startswith("DOC-STRUCT-") for f in findings)

    def test_case_insensitive_placeholder_detection(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Executive Summary\n\nfixme: this is broken."
        findings = check_structure(content, section, spec)
        assert any(f.kind == "placeholder_detected" for f in findings)


class TestHelpers:
    def test_strip_fenced_code_blocks_blanks_body(self) -> None:
        content = "Text before.\n```python\nTODO: code\n```\nText after."
        result = _strip_fenced_code_blocks(content)
        assert "TODO" not in result
        assert "Text before." in result
        assert "Text after." in result

    def test_has_required_heading_matches_h1(self) -> None:
        assert _has_required_heading("# Executive Summary\n\nContent.", "Executive Summary")

    def test_has_required_heading_case_insensitive(self) -> None:
        assert _has_required_heading("# executive summary\n\nContent.", "Executive Summary")

    def test_has_required_heading_false_when_absent(self) -> None:
        assert not _has_required_heading("## Wrong Heading\n\nContent.", "Executive Summary")

    def test_find_empty_heading_blocks_detects_empty(self) -> None:
        content = "# Intro\n\n## Background\n\n## Financials\n\nData."
        empty = _find_empty_heading_blocks(content)
        assert "Background" in empty

    def test_has_minimum_content_true_with_paragraph(self) -> None:
        assert _has_minimum_content("# Heading\n\nSome content here.")

    def test_has_minimum_content_false_with_only_heading(self) -> None:
        assert not _has_minimum_content("# Heading\n")
