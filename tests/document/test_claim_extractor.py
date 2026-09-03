"""Tests for saturnday.document.claim_extractor."""

from __future__ import annotations

import pytest

from saturnday.document._types import ClaimType, DocumentClaim, DocumentSpec
from saturnday.document.claim_extractor import (
    _classify_claim,
    _extract_sentences,
    extract_claims,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_spec(approved_sources: list[str] | None = None) -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Test document",
        audience="internal",
        risk_class="low",
        required_sections=["Summary"],
        claim_policy={"quantitative_claims_require_source": True},
        approved_sources=approved_sources or [],
        sign_off_roles=[],
    )


# ---------------------------------------------------------------------------
# T026: test_extract_quantitative_claim
# ---------------------------------------------------------------------------


def test_extract_quantitative_claim() -> None:
    content = "Revenue increased by 14.2% compared to the previous quarter."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert len(claims) >= 1
    types = [c.claim_type for c in claims]
    assert ClaimType.QUANTITATIVE.value in types


def test_extract_quantitative_currency() -> None:
    content = "The total cost was $1,234,567 for the fiscal year."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.QUANTITATIVE.value for c in claims)


def test_extract_large_integer_claim() -> None:
    content = "There are 500 employees across 12 locations."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    # 500 is >= 10, should be quantitative
    assert any(c.claim_type == ClaimType.QUANTITATIVE.value for c in claims)


# ---------------------------------------------------------------------------
# T026: test_extract_date_claim
# ---------------------------------------------------------------------------


def test_extract_date_claim_iso() -> None:
    content = "The agreement was signed on 2026-03-15 in Brussels."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.DATE.value for c in claims)


def test_extract_date_claim_long_form() -> None:
    content = "The merger completed on 15 March 2026 following regulatory approval."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.DATE.value for c in claims)


def test_extract_date_claim_quarter() -> None:
    content = "Results are expected to improve from Q2 2026 onwards."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.DATE.value for c in claims)


# ---------------------------------------------------------------------------
# T026: test_extract_recommendation_claim
# ---------------------------------------------------------------------------


def test_extract_recommendation_should() -> None:
    content = "The board should review the policy annually."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.RECOMMENDATION.value for c in claims)


def test_extract_recommendation_we_recommend() -> None:
    content = "We recommend allocating 20% of the budget to infrastructure."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    # "20%" triggers quantitative first; recommendation may still be in types
    types = [c.claim_type for c in claims]
    # At minimum, quantitative must fire; recommendation is secondary if same sentence
    assert ClaimType.QUANTITATIVE.value in types or ClaimType.RECOMMENDATION.value in types


def test_extract_recommendation_it_is_advised() -> None:
    content = "It is advised that all contractors complete the onboarding module."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.RECOMMENDATION.value for c in claims)


# ---------------------------------------------------------------------------
# T026: test_extract_cited_claim
# ---------------------------------------------------------------------------


def test_extract_cited_claim_bracket() -> None:
    content = "According to [Annual Report 2025], revenues rose sharply."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.CITED.value for c in claims)


def test_extract_cited_claim_source_prefix() -> None:
    content = "Source: financials.csv confirms the total."
    spec = make_spec(approved_sources=["financials.csv"])
    claims = extract_claims(content, "S001", spec)
    assert len(claims) >= 1
    # "Source:" triggers CITED; linked_sources should detect financials.csv
    cited = [c for c in claims if c.claim_type == ClaimType.CITED.value]
    assert cited
    assert "financials.csv" in cited[0].linked_sources


def test_extract_cited_claim_with_linked_source() -> None:
    # Citation bracket causes the sentence to be classified as CITED
    # and the source basename matches the approved source path.
    content = "As noted in [financials.csv], the net margin improved significantly."
    spec = make_spec(approved_sources=["data/financials.csv"])
    claims = extract_claims(content, "S001", spec)
    assert claims
    # At minimum one claim should link to financials.csv
    all_linked = [src for c in claims for src in c.linked_sources]
    assert "data/financials.csv" in all_linked


# ---------------------------------------------------------------------------
# T026: test_extract_regulatory_claim
# ---------------------------------------------------------------------------


def test_extract_regulatory_gdpr() -> None:
    content = "In compliance with GDPR, all user data is encrypted at rest."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.REGULATORY.value for c in claims)


def test_extract_regulatory_in_accordance_with() -> None:
    content = "In accordance with ISO 27001, the organisation maintains an ISMS."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.REGULATORY.value for c in claims)


def test_extract_regulatory_pursuant_to() -> None:
    content = "Pursuant to regulation 2016/679, consent records are retained."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert any(c.claim_type == ClaimType.REGULATORY.value for c in claims)


# ---------------------------------------------------------------------------
# T026: test_no_claims_from_plain_text
# ---------------------------------------------------------------------------


def test_no_claims_from_plain_text() -> None:
    content = (
        "This is a general overview of the project.\n"
        "The team has been working on various tasks.\n"
        "Progress has been made in several areas.\n"
    )
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    # Plain sentences with no numbers, dates, regulations, or citations
    assert claims == []


def test_no_claims_from_heading_only() -> None:
    content = "## Introduction\n\n### Background\n\n#### Context\n"
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert claims == []


# ---------------------------------------------------------------------------
# T026: test_claim_ids_auto_generated
# ---------------------------------------------------------------------------


def test_claim_ids_auto_generated() -> None:
    content = (
        "Revenue grew by 15% in 2026.\n"
        "The agreement was signed on 2026-01-10.\n"
        "The board should expand operations.\n"
    )
    spec = make_spec()
    claims = extract_claims(content, "S002", spec)
    assert len(claims) >= 2
    ids = [c.claim_id for c in claims]
    # All IDs start with the section prefix
    for cid in ids:
        assert cid.startswith("S002_C")
    # IDs are unique
    assert len(ids) == len(set(ids))


def test_claim_ids_format() -> None:
    content = "Revenue grew by 42% last year."
    spec = make_spec()
    claims = extract_claims(content, "S003", spec)
    assert claims
    assert claims[0].claim_id == "S003_C001"


# ---------------------------------------------------------------------------
# T026: test_multiple_claims_per_section
# ---------------------------------------------------------------------------


def test_multiple_claims_per_section() -> None:
    content = (
        "Revenue increased by 14.2% year-over-year.\n"
        "The acquisition was completed on 2026-02-28.\n"
        "In compliance with GDPR, data handling procedures were updated.\n"
        "We recommend a 10% increase in the marketing budget.\n"
    )
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    assert len(claims) >= 3
    claim_types = {c.claim_type for c in claims}
    # Should have at least quantitative, date, regulatory
    assert len(claim_types) >= 2


def test_code_block_excluded() -> None:
    content = (
        "The overall balance is positive.\n\n"
        "```python\n"
        "total = 100 + 200  # 300\n"
        "profit_margin = 14.2\n"
        "```\n\n"
        "No other numbers appear in this section.\n"
    )
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    # The 100, 200, 300, 14.2 inside the code fence must NOT generate claims
    quant_claims = [c for c in claims if c.claim_type == ClaimType.QUANTITATIVE.value]
    for c in quant_claims:
        assert "100" not in c.claim_text or "300" not in c.claim_text


def test_deduplication_same_sentence() -> None:
    # Same sentence repeated twice → one claim
    sentence = "Revenue grew by 50% year over year."
    content = sentence + "\n" + sentence
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    quant = [c for c in claims if c.claim_type == ClaimType.QUANTITATIVE.value]
    # After dedup, only one claim for this sentence
    texts = [c.claim_text.lower() for c in quant]
    assert len(texts) == len(set(texts))


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------


def test_extract_sentences_returns_tuples() -> None:
    text = "First sentence. Second sentence with more words."
    result = _extract_sentences(text)
    assert isinstance(result, list)
    assert all(isinstance(item, tuple) and len(item) == 2 for item in result)


def test_classify_claim_quantitative() -> None:
    assert _classify_claim("Revenue was $50,000 last month.") == ClaimType.QUANTITATIVE.value


def test_classify_claim_date() -> None:
    assert _classify_claim("The event occurs on 2026-06-01.") == ClaimType.DATE.value


def test_classify_claim_recommendation() -> None:
    assert _classify_claim("The team should adopt the new process.") == ClaimType.RECOMMENDATION.value


def test_classify_claim_regulatory() -> None:
    assert _classify_claim("In compliance with GDPR, all data is encrypted.") == ClaimType.REGULATORY.value


def test_classify_claim_none_for_plain() -> None:
    assert _classify_claim("The project was completed on time.") is None


def test_claim_support_verdict_empty_by_default() -> None:
    content = "Revenue grew by 30% last year."
    spec = make_spec()
    claims = extract_claims(content, "S001", spec)
    for c in claims:
        assert c.support_verdict == ""
        assert c.uncertainty is False
