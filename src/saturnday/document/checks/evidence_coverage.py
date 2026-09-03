"""Evidence coverage check for Saturnday Document Mode.

Verifies that quantitative claims and recommendations are grounded in approved
sources, and that at least one approved source is cited per section.

Rule ID prefix: ``DOC-EVID``.
"""

from __future__ import annotations

import re

from saturnday.document._types import DocumentFinding, DocumentSection, DocumentSpec

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------

# Inline citation patterns (any of these count as a citation in a paragraph)
_CITATION_RE = re.compile(
    r"""
    \[[^\]]+\]          # [Source Name] or [1]
    | \(\w[^)]+\d{4}\)  # (Author, 2023)
    | \bSource:\s+\S+   # Source: filename.md
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Numbers that indicate a quantitative claim
# Match: $, %, integers > 10, decimals, currency amounts
_QUANT_RE = re.compile(
    r"""
    [$€£]          # currency prefix
    | \d+\%        # percentage
    | \b\d{2,}\b   # integers >= 10
    | \b\d+\.\d+\b # decimal numbers
    """,
    re.VERBOSE,
)

# Recommendation verbs (conservative list — must be full word matches)
_RECOMMENDATION_RE = re.compile(
    r"\b(?:should|must|recommend|suggests?|advise[sd]?|requires?|propose[sd]?)\b",
    re.IGNORECASE,
)

# Fenced code block stripper
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)


def check_evidence_coverage(
    content: str,
    section: DocumentSection,
    spec: DocumentSpec,
) -> list[DocumentFinding]:
    """Check evidence coverage in section content.

    Checks performed:

    1. Quantitative paragraphs linked to sources: if
       ``claim_policy.quantitative_claims_require_source`` is true, paragraphs
       containing quantitative expressions must contain a citation.
    2. Recommendation sentences linked to sources: if
       ``claim_policy.recommendation_claims_require_support`` is true, sentences
       with recommendation verbs must have a citation in the same paragraph.
    3. Uncited narrative: if ``claim_policy.uncited_narrative_allowed`` is false,
       all non-empty paragraphs must contain a citation.
    4. Source utilization: if ``section.required_sources`` is non-empty, at least
       one source should be cited (basename match).

    Args:
        content: Raw Markdown string for the section.
        section: Section definition from the document plan.
        spec: Document spec containing claim policy settings.

    Returns:
        List of :class:`~saturnday.document._types.DocumentFinding` objects.
        Empty list means coverage is acceptable under the configured policy.
    """
    findings: list[DocumentFinding] = []
    counter = _Counter()

    # Strip fenced code blocks before any paragraph analysis
    clean_content = _FENCE_RE.sub("", content)

    paragraphs = _split_paragraphs(clean_content)

    quant_requires_source = spec.claim_policy.get("quantitative_claims_require_source", False)
    rec_requires_support = spec.claim_policy.get("recommendation_claims_require_support", False)
    uncited_narrative_allowed = spec.claim_policy.get("uncited_narrative_allowed", True)

    for para_no, para in enumerate(paragraphs, start=1):
        if not para.strip():
            continue
        # Skip headings-only paragraphs
        if re.match(r"^#{1,6}\s+", para.strip()):
            continue

        has_citation = bool(_CITATION_RE.search(para))
        has_quant = bool(_QUANT_RE.search(para))
        has_recommendation = bool(_RECOMMENDATION_RE.search(para))

        # --- Check 1: Quantitative claims require source ---
        if quant_requires_source and has_quant and not has_citation:
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-EVID-{counter.next():03d}",
                    section_id=section.section_id,
                    kind="uncited_quantitative_claim",
                    severity="error",
                    detail=(
                        f"Paragraph {para_no} contains quantitative expressions but "
                        f"no source citation. Policy requires citations for quantitative claims."
                    ),
                    fixable_by_regeneration=True,
                )
            )

        # --- Check 2: Recommendation claims require support ---
        if rec_requires_support and has_recommendation and not has_citation:
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-EVID-{counter.next():03d}",
                    section_id=section.section_id,
                    kind="unsupported_recommendation",
                    severity="warning",
                    detail=(
                        f"Paragraph {para_no} contains a recommendation but no "
                        f"supporting citation. Policy requires evidence for recommendations."
                    ),
                    fixable_by_regeneration=True,
                )
            )

        # --- Check 3: Uncited narrative ---
        if not uncited_narrative_allowed and not has_citation:
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-EVID-{counter.next():03d}",
                    section_id=section.section_id,
                    kind="uncited_narrative",
                    severity="warning",
                    detail=(
                        f"Paragraph {para_no} has no citation. Policy requires "
                        f"citations for all narrative paragraphs."
                    ),
                    fixable_by_regeneration=True,
                )
            )

    # --- Check 4: Source utilization ---
    if section.required_sources:
        cited_any = False
        for source_path in section.required_sources:
            # Check for basename in content (case-insensitive)
            basename = source_path.split("/")[-1].split("\\")[-1]
            if basename.lower() in content.lower():
                cited_any = True
                break
        if not cited_any:
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-EVID-{counter.next():03d}",
                    section_id=section.section_id,
                    kind="no_required_source_cited",
                    severity="warning",
                    detail=(
                        f"None of the required sources for section '{section.name}' "
                        f"appear to be cited. Required: {section.required_sources}."
                    ),
                    fixable_by_regeneration=True,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _split_paragraphs(content: str) -> list[str]:
    """Split Markdown content into paragraphs separated by blank lines.

    Args:
        content: Markdown text (code blocks already stripped).

    Returns:
        List of paragraph strings.
    """
    # Split on one or more blank lines
    paragraphs = re.split(r"\n\s*\n", content)
    return [p.strip() for p in paragraphs if p.strip()]
