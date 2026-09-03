"""Tests for saturnday.document.claim_verifier."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.document._types import ClaimType, DocumentClaim, DocumentSpec, SupportVerdict
from saturnday.document.claim_verifier import (
    _build_verification_prompt,
    _parse_verdict,
    _verify_cited,
    _verify_quantitative,
    run_claim_analysis,
    verify_claims,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_spec(approved_sources: list[str] | None = None) -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Test",
        audience="internal",
        risk_class="low",
        required_sections=["Summary"],
        claim_policy={},
        approved_sources=approved_sources or [],
        sign_off_roles=[],
    )


def make_coder_config() -> MagicMock:
    cfg = MagicMock()
    cfg.backend = "anthropic"
    return cfg


def make_claim(
    claim_type: str = ClaimType.QUANTITATIVE.value,
    claim_text: str = "Revenue grew by 14.2%.",
    linked_sources: list[str] | None = None,
    section_id: str = "S001",
) -> DocumentClaim:
    return DocumentClaim(
        claim_id=f"{section_id}_C001",
        section_id=section_id,
        claim_text=claim_text,
        claim_type=claim_type,
        linked_sources=linked_sources or [],
    )


# ---------------------------------------------------------------------------
# T026: test_verify_quantitative_supported
# ---------------------------------------------------------------------------


def test_verify_quantitative_supported() -> None:
    claim = make_claim(
        claim_type=ClaimType.QUANTITATIVE.value,
        claim_text="Revenue grew by 14.2% last quarter.",
    )
    source_contents = {"financials.csv": "Q3 results: 14.2% revenue growth vs prior year"}
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.SUPPORTED.value
    assert result[0].uncertainty is False


def test_verify_quantitative_supported_currency() -> None:
    claim = make_claim(
        claim_type=ClaimType.QUANTITATIVE.value,
        claim_text="Total revenue was $5,000,000.",
        linked_sources=["financials.csv"],
    )
    source_contents = {"financials.csv": "Total revenue: $5000000 for the period."}
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.SUPPORTED.value


# ---------------------------------------------------------------------------
# T026: test_verify_quantitative_unsupported
# ---------------------------------------------------------------------------


def test_verify_quantitative_unsupported() -> None:
    claim = make_claim(
        claim_type=ClaimType.QUANTITATIVE.value,
        claim_text="Revenue grew by 99.9% last quarter.",
    )
    source_contents = {"financials.csv": "Q3 results: 14.2% growth"}
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.UNSUPPORTED.value


def test_verify_quantitative_no_sources() -> None:
    claim = make_claim(
        claim_type=ClaimType.QUANTITATIVE.value,
        claim_text="Costs increased by 25%.",
    )
    config = make_coder_config()

    result = verify_claims([claim], {}, config, Path("/repo"))
    # No sources → UNSUPPORTED (number not found)
    assert result[0].support_verdict == SupportVerdict.UNSUPPORTED.value


def test_verify_quantitative_no_number_in_claim() -> None:
    claim = make_claim(
        claim_type=ClaimType.QUANTITATIVE.value,
        claim_text="Revenue grew significantly.",  # no actual number
    )
    source_contents = {"data.csv": "revenue data here"}
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    # Can't extract a number → UNCERTAIN
    assert result[0].support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value


# ---------------------------------------------------------------------------
# T026: test_verify_cited_source_exists
# ---------------------------------------------------------------------------


def test_verify_cited_source_exists() -> None:
    claim = make_claim(
        claim_type=ClaimType.CITED.value,
        claim_text="As noted in [Annual Report 2025], revenues rose.",
        linked_sources=["reports/annual_2025.pdf"],
    )
    source_contents = {"reports/annual_2025.pdf": "Annual Report 2025 content..."}
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.SUPPORTED.value
    assert result[0].uncertainty is False


def test_verify_cited_no_linked_sources() -> None:
    claim = make_claim(
        claim_type=ClaimType.CITED.value,
        claim_text="According to [Unknown Source], this is true.",
        linked_sources=[],  # citation token but no matched approved source
    )
    source_contents = {"some_file.csv": "data"}
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
    assert result[0].uncertainty is True


# ---------------------------------------------------------------------------
# T026: test_verify_cited_source_missing
# ---------------------------------------------------------------------------


def test_verify_cited_source_missing() -> None:
    claim = make_claim(
        claim_type=ClaimType.CITED.value,
        claim_text="As noted in missing_report.pdf, all is well.",
        linked_sources=["missing_report.pdf"],
    )
    source_contents = {}  # source not in approved set
    config = make_coder_config()

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.UNSUPPORTED.value


# ---------------------------------------------------------------------------
# T026: test_verify_with_llm_supported
# ---------------------------------------------------------------------------


def test_verify_with_llm_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = make_claim(
        claim_type=ClaimType.RECOMMENDATION.value,
        claim_text="The board should expand operations into new markets.",
    )
    source_contents = {"strategy.md": "Expansion into new markets is recommended by the board."}
    config = make_coder_config()

    monkeypatch.setattr(
        "saturnday.document.claim_verifier.call_coder",
        lambda *args, **kwargs: "SUPPORTED\nThe evidence directly recommends expansion.",
    )

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.SUPPORTED.value
    assert result[0].uncertainty is False


def test_verify_with_llm_contradicted(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = make_claim(
        claim_type=ClaimType.NAMED_ASSERTION.value,
        claim_text="Acme Corp acquired RivCo in 2025.",
    )
    source_contents = {"news.md": "Acme Corp denied acquisition rumours."}
    config = make_coder_config()

    monkeypatch.setattr(
        "saturnday.document.claim_verifier.call_coder",
        lambda *args, **kwargs: "CONTRADICTED\nEvidence states the acquisition was denied.",
    )

    result = verify_claims([claim], source_contents, config, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.CONTRADICTED.value


# ---------------------------------------------------------------------------
# T026: test_verify_without_coder_config_uncertain
# ---------------------------------------------------------------------------


def test_verify_without_coder_config_uncertain() -> None:
    claim = make_claim(
        claim_type=ClaimType.RECOMMENDATION.value,
        claim_text="The board should increase headcount.",
    )
    source_contents = {"strategy.md": "headcount data"}

    # coder_config=None
    result = verify_claims([claim], source_contents, None, Path("/repo"))
    assert result[0].support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
    assert result[0].uncertainty is True


def test_verify_multiple_without_coder_config() -> None:
    claims = [
        make_claim(claim_type=ClaimType.QUANTITATIVE.value, claim_text="Revenue grew 15%."),
        make_claim(claim_type=ClaimType.DATE.value, claim_text="Signed on 2026-01-01.", section_id="S001"),
    ]
    claims[1].claim_id = "S001_C002"

    result = verify_claims(claims, {}, None, Path("/repo"))
    for c in result:
        assert c.support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
        assert c.uncertainty is True


# ---------------------------------------------------------------------------
# T026: test_verify_uncertain_escalates
# ---------------------------------------------------------------------------


def test_verify_uncertain_escalates() -> None:
    """UNCERTAIN_REQUIRES_REVIEW must set uncertainty=True, never silently pass."""
    claim = make_claim(
        claim_type=ClaimType.RECOMMENDATION.value,
        claim_text="It is advised that teams adopt the new process.",
    )
    source_contents = {"policy.md": "general guidance"}
    config = make_coder_config()

    with patch(
        "saturnday.document.claim_verifier.call_coder",
        return_value="UNCERTAIN_REQUIRES_REVIEW\nInsufficient evidence.",
    ):
        result = verify_claims([claim], source_contents, config, Path("/repo"))

    assert result[0].support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
    assert result[0].uncertainty is True


def test_parse_failure_escalates_to_uncertain() -> None:
    claim = make_claim(
        claim_type=ClaimType.RECOMMENDATION.value,
        claim_text="Something important.",
    )
    source_contents = {"policy.md": "data"}
    config = make_coder_config()

    with patch(
        "saturnday.document.claim_verifier.call_coder",
        return_value="I think it looks fine maybe yes",  # no valid verdict token
    ):
        result = verify_claims([claim], source_contents, config, Path("/repo"))

    assert result[0].support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
    assert result[0].uncertainty is True


# ---------------------------------------------------------------------------
# T026: test_no_internet_lookup — verify prompt content
# ---------------------------------------------------------------------------


def test_no_internet_lookup_constraint_in_prompt() -> None:
    """Prompt must explicitly forbid use of external or training knowledge."""
    claim = make_claim(
        claim_type=ClaimType.RECOMMENDATION.value,
        claim_text="The team should expand into APAC.",
    )
    relevant_sources = {"strategy.md": "APAC expansion considered by leadership."}
    prompt = _build_verification_prompt(claim, relevant_sources)

    assert "ONLY" in prompt or "only" in prompt
    assert "training" in prompt.lower() or "external" in prompt.lower()
    # Verdict tokens must all appear in the prompt
    assert "SUPPORTED" in prompt
    assert "UNSUPPORTED" in prompt
    assert "CONTRADICTED" in prompt
    assert "UNCERTAIN_REQUIRES_REVIEW" in prompt


def test_prompt_includes_claim_text() -> None:
    claim = make_claim(
        claim_type=ClaimType.DATE.value,
        claim_text="Agreement signed on 2026-03-15.",
    )
    prompt = _build_verification_prompt(claim, {"source.md": "signed on 2026-03-15"})
    assert "2026-03-15" in prompt
    assert "source.md" in prompt


def test_llm_timeout_graceful(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM timeout / exception must yield UNCERTAIN, not crash."""
    claim = make_claim(
        claim_type=ClaimType.REGULATORY.value,
        claim_text="In compliance with GDPR, data is encrypted.",
    )
    source_contents = {"policy.md": "GDPR compliance policy."}
    config = make_coder_config()

    def _raise(*args, **kwargs):
        raise TimeoutError("LLM timed out")

    with patch("saturnday.document.claim_verifier.call_coder", _raise):
        result = verify_claims([claim], source_contents, config, Path("/repo"))

    assert result[0].support_verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
    assert result[0].uncertainty is True
    assert "timed out" in result[0].notes.lower()


# ---------------------------------------------------------------------------
# _parse_verdict tests
# ---------------------------------------------------------------------------


def test_parse_verdict_supported() -> None:
    verdict, notes = _parse_verdict("SUPPORTED\nThe evidence clearly confirms this.")
    assert verdict == SupportVerdict.SUPPORTED.value
    assert "evidence" in notes.lower()


def test_parse_verdict_weakly_supported() -> None:
    verdict, _notes = _parse_verdict("WEAKLY_SUPPORTED - some partial evidence exists.")
    assert verdict == SupportVerdict.WEAKLY_SUPPORTED.value


def test_parse_verdict_unsupported() -> None:
    verdict, _notes = _parse_verdict("UNSUPPORTED")
    assert verdict == SupportVerdict.UNSUPPORTED.value


def test_parse_verdict_empty_response() -> None:
    verdict, notes = _parse_verdict("")
    assert verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
    assert notes


def test_parse_verdict_no_token() -> None:
    verdict, notes = _parse_verdict("I cannot determine the answer from available data.")
    assert verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value


# ---------------------------------------------------------------------------
# run_claim_analysis convenience function
# ---------------------------------------------------------------------------


def test_run_claim_analysis_no_coder_config(tmp_path: Path) -> None:
    """Without coder_config, claims are extracted but not verified."""
    spec = make_spec(approved_sources=[])
    sections = {
        "S001": "Revenue grew by 42% year over year.",
        "S002": "The agreement was signed on 2026-01-15.",
    }
    claims = run_claim_analysis(sections, spec, tmp_path, coder_config=None)
    assert len(claims) >= 2
    for c in claims:
        # No verification ran — verdicts empty
        assert c.support_verdict == ""
        assert c.uncertainty is False


def test_run_claim_analysis_empty_sections(tmp_path: Path) -> None:
    spec = make_spec()
    claims = run_claim_analysis({}, spec, tmp_path)
    assert claims == []


def test_run_claim_analysis_no_check_worthy_content(tmp_path: Path) -> None:
    spec = make_spec()
    sections = {"S001": "This is a general introduction to the document."}
    claims = run_claim_analysis(sections, spec, tmp_path)
    assert claims == []


def test_run_claim_analysis_with_coder_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With coder_config and a real source file, verification runs."""
    source_file = tmp_path / "data.csv"
    source_file.write_text("Revenue: 42%\nGrowth confirmed.")

    spec = make_spec(approved_sources=[str(source_file)])
    sections = {"S001": "Revenue grew by 42% according to the latest data."}
    config = make_coder_config()

    claims = run_claim_analysis(sections, spec, tmp_path, coder_config=config)
    # Quantitative claim should be SUPPORTED (42 found in source)
    quant = [c for c in claims if c.claim_type == ClaimType.QUANTITATIVE.value]
    assert quant
    assert quant[0].support_verdict == SupportVerdict.SUPPORTED.value
