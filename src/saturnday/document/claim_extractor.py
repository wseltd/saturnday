"""Claim extractor for Saturnday Document Mode.

Extracts check-worthy claims from document section content.  Only
sentences that are genuinely check-worthy are extracted — this is NOT
a sentence tokeniser for every sentence in the document.

Six claim types are detected:

* ``quantitative``  — numbers, percentages, currency amounts
* ``date``          — specific dates or date ranges
* ``named_assertion`` — factual claims about named entities
* ``recommendation`` — sentences with recommendation verbs
* ``cited``         — sentences that already carry a source reference
* ``regulatory``    — compliance / regulation references

Rule IDs are not produced here; this module is purely extraction.
Verification verdicts are set to empty strings (populated by
:mod:`~saturnday.document.claim_verifier`).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from saturnday.document._types import ClaimType, DocumentClaim

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Fenced code block stripper (strip before any extraction)
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)

# Inline citation patterns — same list as evidence_coverage.py for consistency
_CITATION_RE = re.compile(
    r"""
    \[[^\]]+\]          # [Source Name] or [1]
    | \(\w[^)]+\d{4}\)  # (Author, 2023)
    | \bSource:\s+\S+   # Source: filename.md
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Quantitative: $, €, £, percentages, or numbers greater than 10.
# We avoid matching single-digit numbers like "3 items" to reduce noise.
_QUANT_RE = re.compile(
    r"""
    [$€£]\s*[\d,]+          # currency prefix with amount
    | \b\d+\.?\d*\s*%       # percentages: 14.2%, 50%
    | \b\d{2,}(?:[,\.]\d+)* # integers >= 10 (with optional thousands/decimal)
    """,
    re.VERBOSE,
)

# Date patterns: ISO dates, Month Day Year, "Q1 2026", "FY2025", etc.
_DATE_RE = re.compile(
    r"""
    \b\d{4}-\d{2}-\d{2}\b   # ISO: 2026-03-15
    | \b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|
        September|October|November|December)\s+\d{4}\b   # 15 March 2026
    | \b(?:January|February|March|April|May|June|July|August|
        September|October|November|December)\s+\d{1,2},?\s+\d{4}\b  # March 15, 2026
    | \bQ[1-4]\s+\d{4}\b    # Q1 2026
    | \bFY\s*\d{2,4}\b       # FY2025, FY 25
    | \b\d{1,2}/\d{1,2}/\d{2,4}\b   # 03/15/2026
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Named assertion: a capitalised proper noun phrase followed by a
# factual verb.  We keep this conservative to avoid firing on every
# sentence that starts with a capital word.
_PROPER_NOUN_RE = re.compile(
    r"\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\s+"
    r"(?:acquired|merged|launched|announced|reported|released|signed|"
    r"agreed|published|disclosed|confirmed|denied|completed|filed|"
    r"achieved|reached|exceeded|missed|appointed|resigned|hired|"
    r"partnered|invested|divested|restructured|expanded|closed)\b",
)

# Recommendation verbs
_RECOMMENDATION_RE = re.compile(
    r"\b(?:should|must|recommend(?:s|ed)?|suggest(?:s|ed)?|"
    r"advis(?:e|ed|es)|require(?:s|d)?|propose(?:s|d)?|"
    r"it\s+is\s+(?:recommended|advised|suggested|proposed)|"
    r"the\s+board\s+should|we\s+recommend)\b",
    re.IGNORECASE,
)

# Regulatory: compliance/regulation/standard references
_REGULATORY_RE = re.compile(
    r"\b(?:in\s+compliance\s+with|under\s+regulation|"
    r"per\s+(?:regulation|rule|law|directive)|"
    r"pursuant\s+to|in\s+accordance\s+with|"
    r"as\s+required\s+by|GDPR|HIPAA|SOC\s*2|ISO\s*\d+|"
    r"SEC\s+rule|regulation\s+[A-Z]+|directive\s+\d+|"
    r"standard\s+[A-Z0-9/-]+)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_claims(
    content: str,
    section_id: str,
    spec: object,  # DocumentSpec — typed as object to avoid circular import if called standalone
) -> list[DocumentClaim]:
    """Extract check-worthy claims from a document section.

    Only sentences that match one or more claim patterns are returned.
    Plain declarative text with no quantitative, date, named-assertion,
    recommendation, citation, or regulatory signal is not extracted.

    Code fences are stripped before extraction to avoid false positives
    from code samples.

    Args:
        content: Raw Markdown text for the section.
        section_id: Identifier for the section (e.g. ``"S001"``).
        spec: :class:`~saturnday.document._types.DocumentSpec` providing
            ``approved_sources`` for linked-source detection.

    Returns:
        List of :class:`~saturnday.document._types.DocumentClaim` with
        ``support_verdict`` and ``uncertainty`` left at their defaults
        (empty string / False).  The verifier populates those fields.
    """
    approved_sources: list[str] = []
    if hasattr(spec, "approved_sources"):
        approved_sources = list(spec.approved_sources or [])

    clean = _FENCE_RE.sub("", content)
    sentences = _extract_sentences(clean)

    claims: list[DocumentClaim] = []
    seen: set[tuple[str, str]] = set()  # (claim_text, claim_type) dedup key
    counter = 0

    for sentence, _lineno in sentences:
        s = sentence.strip()
        if not s:
            continue

        claim_type = _classify_claim(s)
        if claim_type is None:
            continue

        # Deduplicate: same text + same type → one claim
        dedup_key = (s.lower(), claim_type)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        counter += 1
        claim_id = f"{section_id}_C{counter:03d}"
        linked = _find_linked_sources(s, approved_sources)

        claims.append(
            DocumentClaim(
                claim_id=claim_id,
                section_id=section_id,
                claim_text=s,
                claim_type=claim_type,
                linked_sources=linked,
                support_verdict="",
                notes="",
                uncertainty=False,
            )
        )

    logger.debug(
        "claim_extractor: section=%s extracted=%d claims", section_id, len(claims)
    )
    return claims


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_sentences(text: str) -> list[tuple[str, int]]:
    """Split text into (sentence, approximate_line_number) pairs.

    Uses a simple heuristic: split on sentence-ending punctuation followed
    by whitespace and a capital letter, or on newlines.  This is intentionally
    non-recursive and fast; perfect sentence segmentation is not required.

    Args:
        text: Cleaned text (code blocks already removed).

    Returns:
        List of (sentence_text, line_number) tuples.
    """
    results: list[tuple[str, int]] = []
    current_line = 1

    # Split on newlines first to track approximate line numbers
    for raw_line in text.split("\n"):
        # Skip Markdown headings
        stripped = raw_line.strip()
        if re.match(r"^#{1,6}\s", stripped):
            current_line += 1
            continue

        # Further split on ". ", "! ", "? " followed by a capital letter
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", stripped)
        for part in parts:
            part = part.strip()
            if len(part) > 15:  # skip very short fragments
                results.append((part, current_line))
        current_line += 1

    return results


def _classify_claim(sentence: str) -> Optional[str]:
    """Classify a sentence as a :class:`~saturnday.document._types.ClaimType` value.

    Returns the ClaimType string value, or ``None`` if the sentence is
    not check-worthy.

    Priority order (a sentence is assigned at most one type — the first
    matching rule wins, which keeps claim counts predictable):

    1. regulatory     (most specific, captures compliance assertions)
    2. cited          (already has a source reference — cheapest to verify)
    3. date           (date-anchored facts)
    4. quantitative   (number-anchored facts)
    5. recommendation (normative language)
    6. named_assertion (entity + factual verb)

    ``cited`` is checked before ``quantitative`` and ``date`` because a
    sentence that already carries a citation token should be classified as
    ``cited`` regardless of whether it also contains a number or date.
    This ensures the verifier uses the cheapest path (source-existence
    check) rather than numeric search.

    Args:
        sentence: A single sentence string.

    Returns:
        ClaimType value string or ``None``.
    """
    if _REGULATORY_RE.search(sentence):
        return ClaimType.REGULATORY.value
    if _CITATION_RE.search(sentence):
        return ClaimType.CITED.value
    if _DATE_RE.search(sentence):
        return ClaimType.DATE.value
    if _QUANT_RE.search(sentence):
        return ClaimType.QUANTITATIVE.value
    if _RECOMMENDATION_RE.search(sentence):
        return ClaimType.RECOMMENDATION.value
    if _PROPER_NOUN_RE.search(sentence):
        return ClaimType.NAMED_ASSERTION.value
    return None


def _find_linked_sources(sentence: str, approved_sources: list[str]) -> list[str]:
    """Identify approved sources referenced in or near a sentence.

    A source is considered "linked" if its basename appears in the sentence
    (case-insensitive) or if the sentence contains a citation token that
    matches the source basename.

    Args:
        sentence: The sentence text.
        approved_sources: Paths from ``DocumentSpec.approved_sources``.

    Returns:
        List of source paths (from ``approved_sources``) that are linked.
    """
    linked: list[str] = []
    lower_sentence = sentence.lower()
    for source_path in approved_sources:
        basename = source_path.replace("\\", "/").split("/")[-1]
        if basename.lower() in lower_sentence:
            linked.append(source_path)
    return linked
