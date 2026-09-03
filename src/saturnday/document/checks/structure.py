"""Structure check for Saturnday Document Mode.

Validates that a generated section contains a required heading, has no
placeholder text, and has at least one non-empty content paragraph.

Rule ID prefix: ``DOC-STRUCT``.
"""

from __future__ import annotations

import re
from typing import Any

from saturnday.document._types import DocumentFinding, DocumentSection, DocumentSpec

# ---------------------------------------------------------------------------
# Placeholder patterns — case-insensitive, matched outside fenced code blocks
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\[citation needed\]", re.IGNORECASE),
    re.compile(r"\[add source\]", re.IGNORECASE),
    re.compile(r"\blorem ipsum\b", re.IGNORECASE),
    re.compile(r"\[insert here\]", re.IGNORECASE),
    re.compile(r"\[TBD\]", re.IGNORECASE),
    re.compile(r"\[PLACEHOLDER\]", re.IGNORECASE),
    re.compile(r"\[INSERT[^\]]*\]", re.IGNORECASE),
    re.compile(r"\bXXX\b"),
    re.compile(r"\bHACK\b"),
    # Bracketed drafting notes like [add table here], [expand this section]
    re.compile(r"\[(?:add|expand|include|insert|fill in|complete|update)[^\]]{0,60}\]", re.IGNORECASE),
]

# Heading levels H1/H2
_HEADING_RE = re.compile(r"^(#{1,2})\s+(.+)$", re.MULTILINE)


def check_structure(
    content: str,
    section: DocumentSection,
    spec: DocumentSpec,
) -> list[DocumentFinding]:
    """Check structural compliance of section content.

    Checks performed:

    1. Required heading present (H1 or H2 matching the section name).
    2. Placeholder detection outside fenced code blocks.
    3. Empty blocks: headings with no content between them.
    4. Minimum content: at least one non-empty paragraph outside headings.

    Args:
        content: Raw Markdown string for the section.
        section: Section definition from the document plan.
        spec: Document spec (used for context; not mutated).

    Returns:
        List of :class:`~saturnday.document._types.DocumentFinding` objects,
        one per violation.  Empty list means no structural issues detected.
    """
    findings: list[DocumentFinding] = []
    _counter = _Counter()

    non_code_content = _strip_fenced_code_blocks(content)

    # --- Check 1: Required heading ---
    if not _has_required_heading(content, section.name):
        findings.append(
            DocumentFinding(
                finding_id=f"DOC-STRUCT-{_counter.next():03d}",
                section_id=section.section_id,
                kind="missing_required_heading",
                severity="error",
                detail=(
                    f"Section '{section.name}' does not contain a required H1 or H2 "
                    f"heading matching '{section.name}'."
                ),
                fixable_by_regeneration=True,
            )
        )

    # --- Check 2: Placeholder detection ---
    for line_no, line in enumerate(non_code_content.splitlines(), start=1):
        for pattern in _PLACEHOLDER_PATTERNS:
            if pattern.search(line):
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-STRUCT-{_counter.next():03d}",
                        section_id=section.section_id,
                        kind="placeholder_detected",
                        severity="error",
                        detail=(
                            f"Placeholder text detected at line ~{line_no}: "
                            f"{line.strip()[:120]}"
                        ),
                        fixable_by_regeneration=True,
                    )
                )
                break  # one finding per line, even if multiple patterns match

    # --- Check 3: Empty heading blocks ---
    for empty_heading in _find_empty_heading_blocks(content):
        findings.append(
            DocumentFinding(
                finding_id=f"DOC-STRUCT-{_counter.next():03d}",
                section_id=section.section_id,
                kind="empty_heading_block",
                severity="error",
                detail=(
                    f"Heading '{empty_heading}' has no content below it."
                ),
                fixable_by_regeneration=True,
            )
        )

    # --- Check 4: Minimum content ---
    if not _has_minimum_content(content):
        findings.append(
            DocumentFinding(
                finding_id=f"DOC-STRUCT-{_counter.next():03d}",
                section_id=section.section_id,
                kind="insufficient_content",
                severity="error",
                detail=(
                    "Section has no non-empty paragraph content (only headings or whitespace)."
                ),
                fixable_by_regeneration=True,
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Counter:
    """Simple auto-increment counter for finding IDs within one check call."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _strip_fenced_code_blocks(content: str) -> str:
    """Replace fenced code block content with blank lines.

    Preserves line count so line numbers in findings remain meaningful.

    Args:
        content: Raw Markdown string.

    Returns:
        Markdown with code block bodies replaced by blank lines.
    """
    result_lines: list[str] = []
    in_fence = False
    fence_marker = ""
    for line in content.splitlines():
        stripped = line.strip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
                result_lines.append(line)  # keep the fence opener
            else:
                result_lines.append(line)
        else:
            if stripped.startswith(fence_marker):
                in_fence = False
                result_lines.append(line)  # keep the fence closer
            else:
                result_lines.append("")  # blank out code content
    return "\n".join(result_lines)


def _has_required_heading(content: str, section_name: str) -> bool:
    """Check that the content has a H1 or H2 heading matching ``section_name``.

    Matching is case-insensitive and strips leading/trailing whitespace from
    both the heading text and the section name.

    Args:
        content: Raw Markdown.
        section_name: Expected section name.

    Returns:
        ``True`` if a matching heading is found.
    """
    normalized = section_name.strip().lower()
    for match in _HEADING_RE.finditer(content):
        heading_text = match.group(2).strip().lower()
        if heading_text == normalized:
            return True
    return False


def _find_empty_heading_blocks(content: str) -> list[str]:
    """Return headings that are followed immediately by another heading or end-of-content.

    Args:
        content: Raw Markdown.

    Returns:
        List of heading text strings that have no body content below them.
    """
    lines = content.splitlines()
    empty_headings: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^#{1,6}\s+(.+)$", line)
        if m:
            heading_text = m.group(1).strip()
            # Look ahead for non-empty, non-heading content
            j = i + 1
            has_body = False
            while j < len(lines):
                next_line = lines[j].strip()
                if re.match(r"^#{1,6}\s+", next_line):
                    # Hit next heading without finding body
                    break
                if next_line:
                    has_body = True
                    break
                j += 1
            if not has_body:
                empty_headings.append(heading_text)
        i += 1
    return empty_headings


def _has_minimum_content(content: str) -> bool:
    """Return True if content has at least one non-empty non-heading paragraph.

    Args:
        content: Raw Markdown.

    Returns:
        ``True`` if at least one line is not a heading and not whitespace-only.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not re.match(r"^#{1,6}\s+", stripped):
            return True
    return False
