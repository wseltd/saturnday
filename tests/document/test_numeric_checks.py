"""Tests for saturnday.document.checks.numeric."""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentSection, DocumentSpec
from saturnday.document.checks.numeric import (
    _approx_equal,
    _extract_tables,
    _parse_number,
    check_numeric,
    extract_numbers,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def section() -> DocumentSection:
    return DocumentSection(
        section_id="S002",
        name="Financial Results",
        purpose="Detailed financials",
    )


@pytest.fixture()
def spec() -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Q3 Analysis",
        audience="Board",
        risk_class="high",
        required_sections=["Financial Results"],
        claim_policy={},
        approved_sources=["data.md"],
        sign_off_roles=["CFO"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckNumeric:
    def test_correct_arithmetic_no_findings(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Financial Results\n\nTotal: 10 + 20 = 30."
        findings = check_numeric(content, section, {})
        arith_findings = [f for f in findings if f.kind == "arithmetic_error"]
        assert arith_findings == []

    def test_wrong_arithmetic_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "# Financial Results\n\nTotal: 10 + 20 = 35."
        findings = check_numeric(content, section, {})
        assert any(f.kind == "arithmetic_error" for f in findings)

    def test_arithmetic_finding_has_doc_num_prefix(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = "Total: 5 + 5 = 15."
        findings = check_numeric(content, section, {})
        if findings:
            assert all(f.finding_id.startswith("DOC-NUM-") for f in findings)

    def test_correct_percentage_formula_no_findings(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        # 10% of 200 = 20
        content = "10% of 200 = 20."
        findings = check_numeric(content, section, {})
        pct_findings = [f for f in findings if f.kind == "percentage_formula_error"]
        assert pct_findings == []

    def test_wrong_percentage_formula_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        # 10% of 200 ≠ 30
        content = "10% of 200 = 30."
        findings = check_numeric(content, section, {})
        assert any(f.kind == "percentage_formula_error" for f in findings)

    def test_arithmetic_in_code_block_not_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = (
            "# Financial Results\n\nClean prose.\n\n"
            "```python\ntotal = 5 + 5  # this is 15 in comments\n```"
        )
        findings = check_numeric(content, section, {})
        assert findings == []

    def test_tolerance_applied(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        # 10 + 20 = 30.1 — within 1% tolerance
        content = "Total: 10 + 20 = 30.1"
        findings = check_numeric(content, section, {}, tolerance=0.01)
        assert findings == []

    def test_tolerance_exceeded_flagged(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        # 10 + 20 = 32 — exceeds 1% tolerance
        content = "Total: 10 + 20 = 32"
        findings = check_numeric(content, section, {}, tolerance=0.01)
        assert any(f.kind == "arithmetic_error" for f in findings)

    def test_currency_numbers_parsed(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        # $10 + $20 = $35 should be flagged
        content = "$10 + $20 = $35"
        findings = check_numeric(content, section, {})
        assert any(f.kind == "arithmetic_error" for f in findings)

    def test_table_consistency_same_value_no_finding(
        self, section: DocumentSection, spec: DocumentSpec
    ) -> None:
        content = (
            "Revenue was 100 million.\n\n"
            "| Category | Amount |\n"
            "|----------|--------|\n"
            "| Revenue  | 100    |\n"
        )
        findings = check_numeric(content, section, {})
        table_findings = [f for f in findings if f.kind == "table_text_mismatch"]
        assert table_findings == []


class TestNumericHelpers:
    def test_parse_plain_integer(self) -> None:
        assert _parse_number("42") == 42.0

    def test_parse_currency_string(self) -> None:
        assert _parse_number("$1,234.56") == pytest.approx(1234.56)

    def test_parse_millions_suffix(self) -> None:
        assert _parse_number("1.2M") == pytest.approx(1_200_000)

    def test_parse_thousands_suffix(self) -> None:
        assert _parse_number("5K") == pytest.approx(5000)

    def test_parse_percentage(self) -> None:
        assert _parse_number("15%") == pytest.approx(15.0)

    def test_parse_invalid_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            _parse_number("not-a-number")

    def test_approx_equal_within_tolerance(self) -> None:
        assert _approx_equal(100.0, 100.5, 0.01)

    def test_approx_equal_outside_tolerance(self) -> None:
        assert not _approx_equal(100.0, 105.0, 0.01)

    def test_extract_tables_parses_pipe_table(self) -> None:
        content = (
            "| Name | Value |\n"
            "|------|-------|\n"
            "| A    | 100   |\n"
            "| B    | 200   |\n"
        )
        tables = _extract_tables(content)
        assert len(tables) == 1
        assert tables[0]["headers"] == ["Name", "Value"]
        assert len(tables[0]["rows"]) == 2

    def test_extract_numbers_returns_list(self) -> None:
        text = "Revenue grew to $1.2M, a 15% increase."
        nums = extract_numbers(text)
        assert isinstance(nums, list)
        assert len(nums) >= 1
