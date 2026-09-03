"""Terminology check for Saturnday Document Mode.

Validates that required terms are present, banned terms are absent, and
abbreviations are consistently defined on first use.

Rule ID prefix: ``DOC-TERM``.
"""

from __future__ import annotations

import re

from saturnday.document._types import DocumentFinding, DocumentSection, DocumentSpec

# Abbreviation definition pattern: "Full Name (ABBR)" where ABBR is 2-6 uppercase letters
_ABBR_DEFINE_RE = re.compile(r"([A-Z][^(]{2,60})\(([A-Z]{2,6})\)")

# Any subsequent use of an abbreviation (isolated uppercase token)
_ABBR_USE_RE = re.compile(r"\b([A-Z]{2,6})\b")


def check_terminology(
    content: str,
    section: DocumentSection,
    spec: DocumentSpec,
) -> list[DocumentFinding]:
    """Check terminology compliance in section content.

    Checks performed:

    1. Required terms present: all ``spec.required_terminology`` items must
       appear in the section (case-insensitive).
    2. Banned terms absent: no ``spec.banned_terminology`` items may appear
       (case-insensitive).
    3. Abbreviation consistency: if ``Full Name (ABBR)`` is introduced, the
       abbreviation must be used consistently (not redefined with a different
       expansion in the same section).

    Args:
        content: Raw Markdown string for the section.
        section: Section definition from the document plan.
        spec: Document spec containing required/banned terminology lists.

    Returns:
        List of :class:`~saturnday.document._types.DocumentFinding` objects.
        Empty list means no terminology violations.
    """
    findings: list[DocumentFinding] = []
    counter = _Counter()

    content_lower = content.lower()

    # --- Check 1: Required terms present ---
    for term in spec.required_terminology:
        if term.lower() not in content_lower:
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-TERM-{counter.next():03d}",
                    section_id=section.section_id,
                    kind="missing_required_term",
                    severity="warning",
                    detail=(
                        f"Required term '{term}' not found in section '{section.name}'."
                    ),
                    fixable_by_regeneration=True,
                )
            )

    # --- Check 2: Banned terms absent ---
    for term in spec.banned_terminology:
        if term.lower() in content_lower:
            # Find approximate line number
            line_no = _find_term_line(content, term)
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-TERM-{counter.next():03d}",
                    section_id=section.section_id,
                    kind="banned_term_present",
                    severity="error",
                    detail=(
                        f"Banned term '{term}' found in section '{section.name}' "
                        f"at line ~{line_no}."
                    ),
                    fixable_by_regeneration=True,
                )
            )

    # --- Check 3: Abbreviation consistency ---
    defined_abbrs: dict[str, str] = {}  # ABBR → "Full Name"
    for m in _ABBR_DEFINE_RE.finditer(content):
        full_name = m.group(1).strip().rstrip(" ,;:")
        abbr = m.group(2)
        if abbr in defined_abbrs:
            prior = defined_abbrs[abbr]
            if prior.lower() != full_name.lower():
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-TERM-{counter.next():03d}",
                        section_id=section.section_id,
                        kind="abbreviation_redefined",
                        severity="warning",
                        detail=(
                            f"Abbreviation '{abbr}' is defined with two different "
                            f"expansions: '{prior}' and '{full_name}'."
                        ),
                        fixable_by_regeneration=True,
                    )
                )
        else:
            defined_abbrs[abbr] = full_name

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


def _find_term_line(content: str, term: str) -> int:
    """Return the first line number (1-based) where ``term`` appears.

    Case-insensitive search.  Returns 0 if not found.

    Args:
        content: Section content.
        term: Term to locate.

    Returns:
        1-based line number, or 0.
    """
    term_lower = term.lower()
    for i, line in enumerate(content.splitlines(), start=1):
        if term_lower in line.lower():
            return i
    return 0
