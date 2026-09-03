"""Citation check for Saturnday Document Mode.

Validates citation integrity in a generated section: checks that inline
citation tokens are well-formed, DOI/URL references are syntactically valid,
and internal section cross-references resolve.

Rule ID prefix: ``DOC-CITE``.
"""

from __future__ import annotations

import re
import urllib.parse

from saturnday.document._types import DocumentFinding, DocumentSection

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# DOI pattern: starts with "10." — broad extraction, then validation filters
# We match any "10." token that looks like a DOI attempt (has a slash)
# to catch both valid and malformed DOIs.
_DOI_RE = re.compile(r"\b(10\.[^\s,;]+)", re.IGNORECASE)

# Inline citation bracket patterns: [Author, Year], [1], [Source Name]
# We look for [...] that are NOT part of Markdown links like [text](url)
_BRACKET_CITE_RE = re.compile(r"\[([^\]\[]+)\](?!\()", re.IGNORECASE)

# Parenthetical citation: (Author, Year) — four-digit year required
_PAREN_CITE_RE = re.compile(r"\(([^()]+,\s*\d{4}[a-z]?)\)", re.IGNORECASE)

# Internal cross-reference: [see SectionName] or [see Executive Summary]
_INTERNAL_REF_RE = re.compile(r"\[see\s+([^\]]+)\]", re.IGNORECASE)

# URL pattern inside content (not in Markdown link syntax)
_URL_RE = re.compile(r"https?://[^\s\]\)>\"']+", re.IGNORECASE)

# Broken bracket detection: unmatched [ not followed by ]
_BROKEN_OPEN_RE = re.compile(r"\[[^\]]*$", re.MULTILINE)

# Valid DOI suffix: must have at least one non-whitespace char after slash
_VALID_DOI_SUFFIX_RE = re.compile(r"^10\.\d{4,}/.+$")


def check_citations(
    content: str,
    section: DocumentSection,
    approved_sources: list[str],
    all_section_names: list[str],
) -> list[DocumentFinding]:
    """Check citation validity in section content.

    Checks performed:

    1. DOI format: if a DOI is cited, validate ``10.XXXX/...`` format.
    2. URL format: if a bare URL appears, validate scheme + netloc.
    3. Internal ref resolution: ``[see Section X]`` must reference a valid
       section name from the plan.
    4. Broken citation tokens: unmatched ``[`` bracket on a line (outside code blocks).

    Args:
        content: Raw Markdown string for the section.
        section: Section definition from the document plan.
        approved_sources: List of approved source path strings from the spec.
        all_section_names: Names of all sections in the plan for internal ref validation.

    Returns:
        List of :class:`~saturnday.document._types.DocumentFinding` objects.
        Empty list means no citation issues detected.
    """
    findings: list[DocumentFinding] = []
    counter = _Counter()

    non_code = _strip_fenced_code_blocks(content)

    # --- Check 1: DOI format validation ---
    for line_no, line in enumerate(non_code.splitlines(), start=1):
        for doi_match in _DOI_RE.finditer(line):
            doi_str = doi_match.group(1)
            if not _is_valid_doi(doi_str):
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-CITE-{counter.next():03d}",
                        section_id=section.section_id,
                        kind="malformed_doi",
                        severity="error",
                        detail=(
                            f"Malformed DOI at line ~{line_no}: '{doi_str}'. "
                            "Expected format: 10.XXXX/suffix."
                        ),
                        fixable_by_regeneration=False,
                    )
                )

    # --- Check 2: URL format validation ---
    for line_no, line in enumerate(non_code.splitlines(), start=1):
        for url_match in _URL_RE.finditer(line):
            url_str = url_match.group(0).rstrip(".,;:)")
            if not _is_valid_url(url_str):
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-CITE-{counter.next():03d}",
                        section_id=section.section_id,
                        kind="malformed_url",
                        severity="warning",
                        detail=(
                            f"Malformed URL at line ~{line_no}: '{url_str}'."
                        ),
                        fixable_by_regeneration=False,
                    )
                )

    # --- Check 3: Internal cross-reference resolution ---
    normalized_names = {name.strip().lower() for name in all_section_names}
    for line_no, line in enumerate(non_code.splitlines(), start=1):
        for ref_match in _INTERNAL_REF_RE.finditer(line):
            ref_target = ref_match.group(1).strip()
            if ref_target.lower() not in normalized_names:
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-CITE-{counter.next():03d}",
                        section_id=section.section_id,
                        kind="unresolved_internal_ref",
                        severity="warning",
                        detail=(
                            f"Internal cross-reference at line ~{line_no} refers to "
                            f"unknown section: '{ref_target}'."
                        ),
                        fixable_by_regeneration=True,
                    )
                )

    # --- Check 4: Broken citation tokens (unmatched brackets) ---
    for line_no, line in enumerate(non_code.splitlines(), start=1):
        # Count brackets on this line
        open_count = line.count("[")
        close_count = line.count("]")
        if open_count > close_count:
            # Confirm it looks like a citation attempt (has content after [)
            if re.search(r"\[[A-Za-z0-9]", line):
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-CITE-{counter.next():03d}",
                        section_id=section.section_id,
                        kind="broken_citation_token",
                        severity="warning",
                        detail=(
                            f"Unmatched citation bracket at line ~{line_no}: "
                            f"{line.strip()[:120]}"
                        ),
                        fixable_by_regeneration=True,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Counter:
    """Auto-increment counter for finding IDs."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _strip_fenced_code_blocks(content: str) -> str:
    """Replace fenced code block content with blank lines."""
    result_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
                result_lines.append(line)
            else:
                result_lines.append(line)
        else:
            if stripped.startswith(fence_marker):
                in_fence = False
                result_lines.append(line)
            else:
                result_lines.append("")
    return "\n".join(result_lines)


def _is_valid_doi(doi_str: str) -> bool:
    """Return True if the DOI string matches the canonical ``10.XXXX/suffix`` format.

    Valid DOI requirements:

    - Starts with ``10.``
    - Followed by 4 or more digits (registrant code)
    - Followed by ``/``
    - Followed by at least one non-whitespace character (the suffix)

    Args:
        doi_str: DOI string to validate (may include trailing punctuation).

    Returns:
        ``True`` for valid format, ``False`` otherwise.
    """
    doi_clean = doi_str.rstrip(".,;:)")
    return bool(_VALID_DOI_SUFFIX_RE.match(doi_clean))


def _is_valid_url(url_str: str) -> bool:
    """Return True if the URL has a valid scheme and netloc.

    Uses ``urllib.parse`` (stdlib only).

    Args:
        url_str: URL string to validate.

    Returns:
        ``True`` if scheme is http/https and netloc is non-empty.
    """
    try:
        parsed = urllib.parse.urlparse(url_str)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False
