"""Tests for saturnday.document.checks.evidence_coverage."""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentSection, DocumentSpec
from saturnday.document.checks.evidence_coverage import check_evidence_coverage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def section() -> DocumentSection:
    return DocumentSection(
        section_id="S001",
        name="Financial Results",
        purpose="Detail Q3 financials",
        required_sources=["data/q3.csv"],
    )


def make_spec(**policy_overrides) -> DocumentSpec:
    policy = {
        "quantitative_claims_require_source": False,
        "recommendation_claims_require_support": False,
        "uncited_narrative_allowed": True,
    }
    policy.update(policy_overrides)
    return DocumentSpec(
        type="report",
        purpose="Q3 Analysis",
        audience="Board",
        risk_class="high",
        required_sections=["Financial Results"],
        claim_policy=policy,
        approved_sources=["data/q3.csv"],
        sign_off_roles=["CFO"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCheckEvidenceCoverage:
    def test_no_policy_constraints_no_findings(self) -> None:
        # Section without required_sources so source-utilization check does not fire
        section_no_sources = DocumentSection(
            section_id="S001",
            name="Financial Results",
            purpose="Detail Q3 financials",
            required_sources=[],
        )
        spec = make_spec()
        content = "# Financial Results\n\nRevenue grew 15% this quarter."
        findings = check_evidence_coverage(content, section_no_sources, spec)
        assert findings == []

    def test_quantitative_with_citation_no_finding(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(quantitative_claims_require_source=True)
        content = "Revenue grew 15% [q3.csv]."
        findings = check_evidence_coverage(content, section, spec)
        quant_findings = [f for f in findings if f.kind == "uncited_quantitative_claim"]
        assert quant_findings == []

    def test_quantitative_without_citation_flagged(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(quantitative_claims_require_source=True)
        content = "Revenue grew 15% this quarter with no citation."
        findings = check_evidence_coverage(content, section, spec)
        assert any(f.kind == "uncited_quantitative_claim" for f in findings)

    def test_recommendation_with_citation_no_finding(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(recommendation_claims_require_support=True)
        content = "We should expand operations [q3.csv]."
        findings = check_evidence_coverage(content, section, spec)
        rec_findings = [f for f in findings if f.kind == "unsupported_recommendation"]
        assert rec_findings == []

    def test_recommendation_without_citation_flagged(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(recommendation_claims_require_support=True)
        content = "We should expand operations in Q4."
        findings = check_evidence_coverage(content, section, spec)
        assert any(f.kind == "unsupported_recommendation" for f in findings)

    def test_uncited_narrative_allowed_no_finding(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(uncited_narrative_allowed=True)
        content = "# Financial Results\n\nThe market showed resilience this quarter."
        findings = check_evidence_coverage(content, section, spec)
        uncited = [f for f in findings if f.kind == "uncited_narrative"]
        assert uncited == []

    def test_uncited_narrative_not_allowed_flagged(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(uncited_narrative_allowed=False)
        content = "# Financial Results\n\nThe market showed resilience this quarter."
        findings = check_evidence_coverage(content, section, spec)
        assert any(f.kind == "uncited_narrative" for f in findings)

    def test_source_cited_by_basename_no_source_utilization_finding(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec()
        content = "Revenue per q3.csv was strong."
        findings = check_evidence_coverage(content, section, spec)
        util_findings = [f for f in findings if f.kind == "no_required_source_cited"]
        assert util_findings == []

    def test_no_required_source_cited_flagged(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec()
        content = "# Financial Results\n\nRevenue grew strongly."
        findings = check_evidence_coverage(content, section, spec)
        assert any(f.kind == "no_required_source_cited" for f in findings)

    def test_finding_ids_use_doc_evid_prefix(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(quantitative_claims_require_source=True)
        content = "Revenue grew 30% with no citation."
        findings = check_evidence_coverage(content, section, spec)
        if findings:
            assert all(f.finding_id.startswith("DOC-EVID-") for f in findings)

    def test_quantitative_in_code_block_not_flagged(
        self, section: DocumentSection
    ) -> None:
        spec = make_spec(quantitative_claims_require_source=True)
        content = (
            "# Financial Results\n\nClean prose [q3.csv].\n\n"
            "```python\nrevenue = 15_000_000\n```"
        )
        findings = check_evidence_coverage(content, section, spec)
        # The paragraph with the code block should not trigger uncited_quantitative
        quant_findings = [f for f in findings if f.kind == "uncited_quantitative_claim"]
        assert quant_findings == []
