"""Numeric consistency check for Saturnday Document Mode.

Validates arithmetic in ``A + B = C`` patterns, percentage formulas, and
table-to-text number agreement.

Rule ID prefix: ``DOC-NUM``.
"""

from __future__ import annotations

import re
from typing import Any

from saturnday.document._types import DocumentFinding, DocumentSection, DocumentSpec

# ---------------------------------------------------------------------------
# Number extraction regex
# ---------------------------------------------------------------------------

# Matches:
#   $1,234.56  EUR 1,234  1.2M  1.2B  15%  1,000  42  3.14
# Groups: (value_string, suffix)
_NUMBER_RE = re.compile(
    r"""
    (?:[$€£])?           # optional currency prefix
    (\d{1,3}(?:,\d{3})*  # integer with optional thousands separators
    (?:\.\d+)?)          # optional decimal
    \s*([MBKmb%]?)       # optional magnitude suffix or %
    """,
    re.VERBOSE,
)

# Simple arithmetic pattern: NUMBER + NUMBER = NUMBER (within a line)
_ARITH_RE = re.compile(
    r"""
    ([$€£]?\d[\d,\.]*[MBKmb%]?)   # left operand
    \s*\+\s*
    ([$€£]?\d[\d,\.]*[MBKmb%]?)   # right operand
    \s*=\s*
    ([$€£]?\d[\d,\.]*[MBKmb%]?)   # stated result
    """,
    re.VERBOSE,
)

# Percentage formula: X% of Y = Z  (or "X% of Y is Z")
_PCT_FORMULA_RE = re.compile(
    r"""
    (\d+(?:\.\d+)?)\%   # percentage
    \s+of\s+
    ([$€£]?\d[\d,\.]*[MBKmb%]?)  # base
    \s+(?:=|is|equals)\s+
    ([$€£]?\d[\d,\.]*[MBKmb%]?)  # stated result
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Markdown pipe table row: | cell | cell | ...
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")

# Magnitude multipliers
_MAGNITUDE: dict[str, float] = {
    "M": 1_000_000,
    "m": 1_000_000,
    "B": 1_000_000_000,
    "b": 1_000_000_000,
    "K": 1_000,
    "k": 1_000,
}


def check_numeric(
    content: str,
    section: DocumentSection,
    source_contents: dict[str, str],
    tolerance: float = 0.01,
) -> list[DocumentFinding]:
    """Check numeric consistency in section content.

    Checks performed:

    1. Arithmetic recomputation: ``A + B = C`` patterns verified.
    2. Percentage formula: ``X% of Y = Z`` verified within tolerance.
    3. Table-to-text consistency: numbers in Markdown tables checked against
       prose repetitions.

    Args:
        content: Raw Markdown string for the section.
        section: Section definition from the document plan.
        source_contents: Approved source file contents (not used in arithmetic
            check, reserved for future source-grounding check).
        tolerance: Relative tolerance for float comparison (default 0.01 = 1%).

    Returns:
        List of :class:`~saturnday.document._types.DocumentFinding` objects.
    """
    findings: list[DocumentFinding] = []
    counter = _Counter()

    non_code = _strip_fenced_code_blocks(content)

    # --- Check 1: Arithmetic A + B = C ---
    for line_no, line in enumerate(non_code.splitlines(), start=1):
        for m in _ARITH_RE.finditer(line):
            try:
                left = _parse_number(m.group(1))
                right = _parse_number(m.group(2))
                stated = _parse_number(m.group(3))
                expected = left + right
                if not _approx_equal(expected, stated, tolerance):
                    findings.append(
                        DocumentFinding(
                            finding_id=f"DOC-NUM-{counter.next():03d}",
                            section_id=section.section_id,
                            kind="arithmetic_error",
                            severity="error",
                            detail=(
                                f"Arithmetic mismatch at line ~{line_no}: "
                                f"{m.group(1)} + {m.group(2)} = {m.group(3)}, "
                                f"but computed {expected:.4g}."
                            ),
                            fixable_by_regeneration=True,
                        )
                    )
            except ValueError:
                pass  # could not parse — skip gracefully

    # --- Check 2: Percentage formula X% of Y = Z ---
    for line_no, line in enumerate(non_code.splitlines(), start=1):
        for m in _PCT_FORMULA_RE.finditer(line):
            try:
                pct = float(m.group(1)) / 100.0
                base = _parse_number(m.group(2))
                stated = _parse_number(m.group(3))
                expected = pct * base
                if not _approx_equal(expected, stated, tolerance):
                    findings.append(
                        DocumentFinding(
                            finding_id=f"DOC-NUM-{counter.next():03d}",
                            section_id=section.section_id,
                            kind="percentage_formula_error",
                            severity="error",
                            detail=(
                                f"Percentage mismatch at line ~{line_no}: "
                                f"{m.group(1)}% of {m.group(2)} = {m.group(3)}, "
                                f"but computed {expected:.4g}."
                            ),
                            fixable_by_regeneration=True,
                        )
                    )
            except (ValueError, ZeroDivisionError):
                pass

    # --- Check 3: Table-to-text consistency ---
    table_numbers = _extract_table_numbers(non_code)
    prose_numbers = _extract_prose_numbers(non_code)

    for table_val, table_ctx in table_numbers:
        for prose_val, prose_ctx in prose_numbers:
            if _same_magnitude_class(table_val, prose_val):
                if not _approx_equal(table_val, prose_val, tolerance):
                    # Only flag when values are close enough to be the same metric
                    # but not within tolerance (avoid false positives on unrelated numbers)
                    if abs(table_val - prose_val) / max(abs(table_val), 1e-10) < 0.25:
                        findings.append(
                            DocumentFinding(
                                finding_id=f"DOC-NUM-{counter.next():03d}",
                                section_id=section.section_id,
                                kind="table_text_mismatch",
                                severity="warning",
                                detail=(
                                    f"Table value {table_val:.4g} (context: '{table_ctx[:60]}') "
                                    f"differs from prose value {prose_val:.4g} "
                                    f"(context: '{prose_ctx[:60]}')."
                                ),
                                fixable_by_regeneration=True,
                            )
                        )

    return findings


# ---------------------------------------------------------------------------
# Helper: _extract_numbers (public helper for reuse in global_checks)
# ---------------------------------------------------------------------------


def extract_numbers(text: str) -> list[tuple[float, str, int]]:
    """Extract numbers with their context and approximate line number.

    Handles: ``$1,234.56``, ``1.2M``, ``15%``, ``1,000``, plain integers,
    and ``EUR``/``£`` prefixed values.

    Args:
        text: Markdown or plain text.

    Returns:
        List of ``(value, context_string, line_number)`` tuples.  ``context_string``
        is up to 60 characters surrounding the match.
    """
    results: list[tuple[float, str, int]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in _NUMBER_RE.finditer(line):
            val_str = m.group(1)
            suffix = m.group(2) or ""
            try:
                val = _parse_number_from_parts(val_str, suffix)
                start = max(0, m.start() - 20)
                end = min(len(line), m.end() + 20)
                ctx = line[start:end].strip()
                results.append((val, ctx, line_no))
            except ValueError:
                pass
    return results


def _extract_tables(text: str) -> list[dict[str, Any]]:
    """Extract Markdown pipe tables as list of ``{headers, rows}`` dicts.

    Args:
        text: Markdown text.

    Returns:
        List of table dicts with keys ``headers`` (list[str]) and
        ``rows`` (list[list[str]]).
    """
    tables: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if _TABLE_ROW_RE.match(line.strip()):
            # Check next line is separator
            if i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1].strip()):
                headers = [c.strip() for c in line.strip().strip("|").split("|")]
                rows: list[list[str]] = []
                j = i + 2
                while j < len(lines) and _TABLE_ROW_RE.match(lines[j].strip()):
                    row = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                    rows.append(row)
                    j += 1
                tables.append({"headers": headers, "rows": rows})
                i = j
                continue
        i += 1
    return tables


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _strip_fenced_code_blocks(content: str) -> str:
    """Replace fenced code block body with blank lines."""
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


def _parse_number(s: str) -> float:
    """Parse a number string (may have $, commas, M/B/K suffix).

    Args:
        s: Raw number token e.g. ``"$1,234.56"``, ``"1.2M"``, ``"15%"``.

    Returns:
        Float value.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    s = s.strip().lstrip("$€£")
    suffix = ""
    if s and s[-1].upper() in ("M", "B", "K"):
        suffix = s[-1]
        s = s[:-1]
    elif s.endswith("%"):
        s = s[:-1]
        suffix = ""
    s = s.replace(",", "")
    try:
        val = float(s)
    except ValueError as exc:
        raise ValueError(f"Cannot parse number: {s!r}") from exc
    multiplier = _MAGNITUDE.get(suffix.upper(), 1)
    return val * multiplier


def _parse_number_from_parts(val_str: str, suffix: str) -> float:
    """Parse from pre-split value string and suffix."""
    val_str = val_str.replace(",", "")
    try:
        val = float(val_str)
    except ValueError as exc:
        raise ValueError(f"Cannot parse: {val_str!r}") from exc
    if suffix in ("%",):
        return val / 100.0
    multiplier = _MAGNITUDE.get(suffix.upper(), 1)
    return val * multiplier


def _approx_equal(a: float, b: float, tolerance: float) -> bool:
    """Return True if ``|a - b| / max(|a|, |b|, 1e-10) <= tolerance``."""
    denom = max(abs(a), abs(b), 1e-10)
    return abs(a - b) / denom <= tolerance


def _same_magnitude_class(a: float, b: float) -> bool:
    """Return True if both values are in the same order of magnitude."""
    if a == 0 and b == 0:
        return True
    if a == 0 or b == 0:
        return False
    import math
    diff = abs(math.log10(abs(a)) - math.log10(abs(b)))
    return diff < 2.0  # within 2 orders of magnitude


def _extract_table_numbers(text: str) -> list[tuple[float, str]]:
    """Extract numeric values from Markdown table cells."""
    results: list[tuple[float, str]] = []
    for table in _extract_tables(text):
        for row in table["rows"]:
            for cell in row:
                for m in _NUMBER_RE.finditer(cell):
                    try:
                        val = _parse_number_from_parts(m.group(1), m.group(2) or "")
                        results.append((val, cell))
                    except ValueError:
                        pass
    return results


def _extract_prose_numbers(text: str) -> list[tuple[float, str]]:
    """Extract numeric values from non-table lines."""
    results: list[tuple[float, str]] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if _TABLE_ROW_RE.match(stripped) or _TABLE_SEP_RE.match(stripped):
            in_table = True
            continue
        if in_table and not stripped:
            in_table = False
            continue
        if in_table:
            continue
        for m in _NUMBER_RE.finditer(line):
            try:
                val = _parse_number_from_parts(m.group(1), m.group(2) or "")
                start = max(0, m.start() - 20)
                end = min(len(line), m.end() + 20)
                results.append((val, line[start:end].strip()))
            except ValueError:
                pass
    return results
