"""Global cross-section consistency checks for Saturnday Document Mode.

Runs after all sections have been generated and locally verified.  Each check
looks across the full set of section results for contradictions or omissions
that a per-section check cannot see.

Public API::

    findings = run_global_checks(section_results, plan)

Each finding uses the ``DOC-GLOBAL`` prefix and sets ``section_id`` to
``"global"`` for multi-section issues, or to the specific offending section ID
for single-section issues (e.g. provisional-section findings).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from saturnday.document._types import DocumentFinding, DocumentPlan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Date extraction: ISO-8601 and common prose forms
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})"          # 2025-03-29
    r"|\b(\d{1,2}/\d{1,2}/\d{2,4})"   # 03/29/2025 or 3/29/25
    r"|\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\s+\d{4})",                      # 29 March 2025
    re.IGNORECASE,
)

# Labelled-metric pattern: "Revenue: $1.2M" or "total cost: 500K" etc.
# Group 1 = label (normalised), Group 2 = raw number token
_LABELLED_METRIC_RE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z\s]{2,30}?)\s*:\s*"
    r"(?P<value>[$€£]?\d[\d,\.]*\s*[MBKmb%]?)",
    re.IGNORECASE,
)

# Simple number parse (currency prefix, commas, magnitude suffix)
_SIMPLE_NUMBER_RE = re.compile(
    r"^[$€£]?(?P<digits>\d[\d,\.]*)(?P<suffix>[MBKmb%]?)$"
)

_MAGNITUDE: dict[str, float] = {
    "M": 1_000_000,
    "m": 1_000_000,
    "B": 1_000_000_000,
    "b": 1_000_000_000,
    "K": 1_000,
    "k": 1_000,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_global_checks(
    section_results: list[dict],
    plan: DocumentPlan,
) -> list[DocumentFinding]:
    """Run all cross-section consistency checks and return combined findings.

    Each check is independently wrapped in ``try/except`` so a failure in one
    check cannot suppress findings from others.

    Checks performed:

    1. **Metric value contradiction**: same labelled metric, different values
       across sections (e.g. "Revenue: $1.2M" in S001 vs "Revenue: $1.4M"
       in S002).
    2. **Date inconsistency**: same literal date string that appears in two or
       more sections with different values in semantically comparable positions.
    3. **Terminology drift**: a concept name appears in multiple distinct
       spellings across sections (heuristic — looks for multi-word phrases that
       overlap at the first word).
    4. **Unresolved provisional sections**: any section with status
       ``PROVISIONAL_UNVERIFIED`` is flagged.
    5. **Required sections missing**: sections listed in
       ``plan.global_checks`` that are absent from ``section_results``.
    6. **Summary vs body inconsistency**: if a section named "executive_summary"
       (case-insensitive) exists, its labelled metrics are checked against all
       other sections.

    Args:
        section_results: List of section result dicts.  Each dict must have at
            minimum: ``section_id`` (str), ``status`` (str), ``content`` (str).
        plan: The document plan containing section definitions and global check
            list.

    Returns:
        List of :class:`~saturnday.document._types.DocumentFinding` objects
        with ``finding_id`` values prefixed ``DOC-GLOBAL``.
    """
    findings: list[DocumentFinding] = []
    counter = _Counter()

    # 1. Metric contradiction
    try:
        findings.extend(
            _check_metric_contradictions(section_results, counter)
        )
    except Exception as exc:
        logger.exception("Global metric contradiction check failed: %s", exc)
        findings.append(_error_finding("metric_contradiction_check_error", str(exc), counter))

    # 2. Date inconsistency
    try:
        findings.extend(
            _check_date_inconsistency(section_results, counter)
        )
    except Exception as exc:
        logger.exception("Global date inconsistency check failed: %s", exc)
        findings.append(_error_finding("date_inconsistency_check_error", str(exc), counter))

    # 3. Terminology drift
    try:
        findings.extend(
            _check_terminology_drift(section_results, counter)
        )
    except Exception as exc:
        logger.exception("Global terminology drift check failed: %s", exc)
        findings.append(_error_finding("terminology_drift_check_error", str(exc), counter))

    # 4. Unresolved provisional sections
    try:
        findings.extend(
            _check_unresolved_provisional(section_results, counter)
        )
    except Exception as exc:
        logger.exception("Global unresolved provisional check failed: %s", exc)
        findings.append(_error_finding("unresolved_provisional_check_error", str(exc), counter))

    # 5. Required sections missing
    try:
        findings.extend(
            _check_required_sections_present(section_results, plan, counter)
        )
    except Exception as exc:
        logger.exception("Global required sections check failed: %s", exc)
        findings.append(_error_finding("required_sections_check_error", str(exc), counter))

    # 6. Summary vs body inconsistency
    try:
        findings.extend(
            _check_summary_vs_body(section_results, counter)
        )
    except Exception as exc:
        logger.exception("Global summary vs body check failed: %s", exc)
        findings.append(_error_finding("summary_body_check_error", str(exc), counter))

    return findings


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_metric_contradictions(
    section_results: list[dict],
    counter: "_Counter",
) -> list[DocumentFinding]:
    """Check that labelled metrics have consistent values across sections.

    Extracts ``label: value`` pairs from each section and flags cases where
    the same normalised label appears with numerically distinct values (beyond
    a 1% tolerance).

    Args:
        section_results: Section result dicts.
        counter: Shared counter for finding IDs.

    Returns:
        List of findings.
    """
    # label_lower -> list[(section_id, raw_value_str, parsed_float)]
    label_occurrences: dict[str, list[tuple[str, str, float]]] = {}

    for sr in section_results:
        sid = sr.get("section_id", "?")
        content = sr.get("content", "")
        for m in _LABELLED_METRIC_RE.finditer(content):
            label = _normalise_label(m.group("label"))
            raw = m.group("value").strip()
            try:
                val = _parse_simple_number(raw)
            except ValueError:
                continue
            label_occurrences.setdefault(label, []).append((sid, raw, val))

    findings: list[DocumentFinding] = []
    for label, occurrences in label_occurrences.items():
        if len(occurrences) < 2:
            continue
        # Compare all pairs
        seen: set[frozenset[str]] = set()
        for i in range(len(occurrences)):
            for j in range(i + 1, len(occurrences)):
                sid_a, raw_a, val_a = occurrences[i]
                sid_b, raw_b, val_b = occurrences[j]
                if sid_a == sid_b:
                    continue
                pair = frozenset({sid_a, sid_b})
                if pair in seen:
                    continue
                if not _approx_equal(val_a, val_b, tolerance=0.01):
                    seen.add(pair)
                    findings.append(
                        DocumentFinding(
                            finding_id=f"DOC-GLOBAL-{counter.next():03d}",
                            section_id="global",
                            kind="cross_section_metric_mismatch",
                            severity="error",
                            detail=(
                                f"Metric '{label}' has conflicting values: "
                                f"{raw_a!r} in {sid_a} vs {raw_b!r} in {sid_b}."
                            ),
                            evidence_ref=f"{sid_a},{sid_b}",
                            fixable_by_regeneration=True,
                        )
                    )

    return findings


def _check_date_inconsistency(
    section_results: list[dict],
    counter: "_Counter",
) -> list[DocumentFinding]:
    """Check for dates that appear differently across sections.

    Looks for the same calendar-anchor keywords (years) that are paired with
    different months or day values across sections.  Also flags cases where
    a specific date appears in one section but a contradictory date appears
    in another section for the same apparent event.

    Implementation: extract all ISO-8601 date strings (``YYYY-MM-DD``) and
    look for the same year referenced with distinct month-day combinations
    in different sections when the surrounding context is similar.

    Args:
        section_results: Section result dicts.
        counter: Shared counter for finding IDs.

    Returns:
        List of findings.
    """
    # Map: normalised date string -> set of section_ids that mention it
    date_sections: dict[str, set[str]] = {}
    # Map: section_id -> set of ISO dates
    section_dates: dict[str, set[str]] = {}

    for sr in section_results:
        sid = sr.get("section_id", "?")
        content = sr.get("content", "")
        dates_here: set[str] = set()
        for m in _DATE_RE.finditer(content):
            raw = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            if raw:
                normalised = raw.lower()
                date_sections.setdefault(normalised, set()).add(sid)
                dates_here.add(normalised)
        section_dates[sid] = dates_here

    findings: list[DocumentFinding] = []
    # Flag: same year but different full dates appearing in the same doc context.
    # Extract years from all dates and find sections that reference the same
    # year with entirely different date strings.
    year_dates: dict[str, dict[str, set[str]]] = {}  # year -> {section_id -> set of dates}
    for sr in section_results:
        sid = sr.get("section_id", "?")
        for date_str in section_dates.get(sid, set()):
            # Extract year component
            year_match = re.match(r"(\d{4})", date_str)
            if year_match:
                yr = year_match.group(1)
                year_dates.setdefault(yr, {}).setdefault(sid, set()).add(date_str)

    for yr, sects in year_dates.items():
        if len(sects) < 2:
            continue
        # Collect all distinct dates across sections for this year
        all_dates: dict[str, list[str]] = {}  # date -> sections
        for sid, dates in sects.items():
            for d in dates:
                all_dates.setdefault(d, []).append(sid)

        # Look for contradicting dates: same date present in some sections
        # but NOT in others (where a different date for the same year is used).
        date_list = sorted(all_dates.keys())
        if len(date_list) > 1 and yr in (d[:4] for d in date_list):
            # Multiple distinct dates in the same year — check if they disagree
            # Only flag if they appear in disjoint sections (not just same section
            # having multiple dates which is normal).
            sections_per_date = {d: set(all_dates[d]) for d in date_list}
            dates_in_multiple: list[str] = [
                d for d, sids in sections_per_date.items() if len(sids) > 0
            ]
            if len(dates_in_multiple) >= 2:
                # Detect if the same year has different month/day combos referenced
                # in different sections as a single anchor date (heuristic: two
                # distinct full dates each appearing in exactly one section).
                solo_dates = [
                    (d, list(sections_per_date[d])[0])
                    for d in dates_in_multiple
                    if len(sections_per_date[d]) == 1
                ]
                if len(solo_dates) >= 2:
                    # Pairs of dates: each exclusive to a different section
                    for i in range(len(solo_dates)):
                        for j in range(i + 1, len(solo_dates)):
                            date_a, sid_a = solo_dates[i]
                            date_b, sid_b = solo_dates[j]
                            if sid_a != sid_b:
                                findings.append(
                                    DocumentFinding(
                                        finding_id=f"DOC-GLOBAL-{counter.next():03d}",
                                        section_id="global",
                                        kind="date_inconsistency",
                                        severity="warning",
                                        detail=(
                                            f"Date inconsistency in year {yr}: "
                                            f"{date_a!r} in {sid_a} vs "
                                            f"{date_b!r} in {sid_b}."
                                        ),
                                        evidence_ref=f"{sid_a},{sid_b}",
                                        fixable_by_regeneration=True,
                                    )
                                )

    return findings


def _check_terminology_drift(
    section_results: list[dict],
    counter: "_Counter",
) -> list[DocumentFinding]:
    """Check for the same concept referenced by different names.

    Strategy: extract multi-word capitalised phrases (likely proper nouns /
    product names / team names) and group by their first word.  If the same
    first word leads to two or more distinct phrases each used in different
    sections, flag it as potential drift.

    Args:
        section_results: Section result dicts.
        counter: Shared counter for finding IDs.

    Returns:
        List of findings.
    """
    # Multi-word capitalised phrase: e.g. "Customer Retention Rate", "Customer
    # Churn Rate" — same first word "Customer" but different full phrase.
    _PHRASE_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")

    # first_word -> set of (full_phrase, section_id)
    first_word_phrases: dict[str, set[tuple[str, str]]] = {}

    for sr in section_results:
        sid = sr.get("section_id", "?")
        content = sr.get("content", "")
        for m in _PHRASE_RE.finditer(content):
            phrase = m.group(1)
            first = phrase.split()[0].lower()
            first_word_phrases.setdefault(first, set()).add((phrase, sid))

    findings: list[DocumentFinding] = []
    for first_word, phrase_set in first_word_phrases.items():
        # Collect distinct phrases and their sections
        phrase_sections: dict[str, set[str]] = {}
        for phrase, sid in phrase_set:
            phrase_sections.setdefault(phrase, set()).add(sid)

        if len(phrase_sections) < 2:
            continue

        # Only flag if at least two distinct phrases appear in different sections
        phrases_list = sorted(phrase_sections.keys())
        # Check if any two phrases have disjoint section sets
        flagged: set[frozenset[str]] = set()
        for i in range(len(phrases_list)):
            for j in range(i + 1, len(phrases_list)):
                pa, pb = phrases_list[i], phrases_list[j]
                sids_a = phrase_sections[pa]
                sids_b = phrase_sections[pb]
                if sids_a.isdisjoint(sids_b):
                    pair = frozenset({pa, pb})
                    if pair not in flagged:
                        flagged.add(pair)
                        findings.append(
                            DocumentFinding(
                                finding_id=f"DOC-GLOBAL-{counter.next():03d}",
                                section_id="global",
                                kind="terminology_drift",
                                severity="warning",
                                detail=(
                                    f"Possible terminology drift: '{pa}' "
                                    f"(in {', '.join(sorted(sids_a))}) vs "
                                    f"'{pb}' "
                                    f"(in {', '.join(sorted(sids_b))})."
                                ),
                                fixable_by_regeneration=True,
                            )
                        )

    return findings


def _check_unresolved_provisional(
    section_results: list[dict],
    counter: "_Counter",
) -> list[DocumentFinding]:
    """Flag all sections whose status is PROVISIONAL_UNVERIFIED.

    Args:
        section_results: Section result dicts.
        counter: Shared counter for finding IDs.

    Returns:
        One finding per provisional section.
    """
    findings: list[DocumentFinding] = []
    for sr in section_results:
        if sr.get("status") == "PROVISIONAL_UNVERIFIED":
            sid = sr.get("section_id", "?")
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-GLOBAL-{counter.next():03d}",
                    section_id=sid,
                    kind="unresolved_provisional",
                    severity="warning",
                    detail=(
                        f"Section {sid!r} is PROVISIONAL_UNVERIFIED: "
                        "retry budget exhausted without verification success."
                    ),
                    fixable_by_regeneration=False,
                )
            )
    return findings


def _check_required_sections_present(
    section_results: list[dict],
    plan: DocumentPlan,
    counter: "_Counter",
) -> list[DocumentFinding]:
    """Flag required sections that are absent from the assembled results.

    Uses entries from ``plan.global_checks`` that look like section IDs
    (matching one of the section IDs defined in ``plan.sections``).  Entries
    that are check-type names (e.g. ``cross_section_consistency``) rather than
    section IDs are ignored — those are orchestration signals, not section
    presence requirements.

    If ``global_checks`` is empty, or if no entry matches a known section ID,
    this check is a no-op.

    Args:
        section_results: Section result dicts.
        plan: Document plan.
        counter: Shared counter for finding IDs.

    Returns:
        List of findings.
    """
    if not plan.global_checks:
        return []

    # Build the set of known section IDs from the plan so we can distinguish
    # section-ID entries from check-type-name entries.
    known_section_ids: set[str] = {s.section_id for s in (plan.sections or [])}

    present_ids = {sr.get("section_id", "") for sr in section_results}
    findings: list[DocumentFinding] = []
    for required_id in plan.global_checks:
        # Only treat entries as required section IDs if they appear in the
        # plan's section list.  This avoids false positives when global_checks
        # contains check-type names like "cross_section_consistency".
        if known_section_ids and required_id not in known_section_ids:
            continue
        if required_id not in present_ids:
            findings.append(
                DocumentFinding(
                    finding_id=f"DOC-GLOBAL-{counter.next():03d}",
                    section_id="global",
                    kind="required_section_missing",
                    severity="error",
                    detail=(
                        f"Required section {required_id!r} is missing from the "
                        "assembled document."
                    ),
                    fixable_by_regeneration=False,
                )
            )
    return findings


def _check_summary_vs_body(
    section_results: list[dict],
    counter: "_Counter",
) -> list[DocumentFinding]:
    """Compare labelled metrics in the executive summary against body sections.

    If no section whose ``section_id`` or generated content heading contains
    "executive_summary" (case-insensitive) is present, this check is a no-op.

    Args:
        section_results: Section result dicts.
        counter: Shared counter for finding IDs.

    Returns:
        List of findings.
    """
    # Identify the summary section
    summary_sr: dict | None = None
    for sr in section_results:
        sid = str(sr.get("section_id", "")).lower()
        content = str(sr.get("content", ""))
        if "executive" in sid or "executive summary" in content[:200].lower():
            summary_sr = sr
            break

    if summary_sr is None:
        return []

    summary_sid = summary_sr.get("section_id", "?")
    summary_content = summary_sr.get("content", "")
    summary_metrics = _extract_labelled_metrics(summary_content)

    if not summary_metrics:
        return []

    # Collect body metrics
    body_metrics: dict[str, list[tuple[str, str, float]]] = {}  # label -> [(sid, raw, val)]
    for sr in section_results:
        if sr.get("section_id") == summary_sr.get("section_id"):
            continue
        sid = sr.get("section_id", "?")
        for label, raw, val in _extract_labelled_metrics(sr.get("content", "")):
            body_metrics.setdefault(label, []).append((sid, raw, val))

    findings: list[DocumentFinding] = []
    for sum_label, sum_raw, sum_val in summary_metrics:
        body_occ = body_metrics.get(sum_label)
        if body_occ is None:
            continue
        for body_sid, body_raw, body_val in body_occ:
            if not _approx_equal(sum_val, body_val, tolerance=0.01):
                findings.append(
                    DocumentFinding(
                        finding_id=f"DOC-GLOBAL-{counter.next():03d}",
                        section_id="global",
                        kind="summary_body_mismatch",
                        severity="error",
                        detail=(
                            f"Summary vs body mismatch for '{sum_label}': "
                            f"{sum_raw!r} in {summary_sid} vs "
                            f"{body_raw!r} in {body_sid}."
                        ),
                        evidence_ref=f"{summary_sid},{body_sid}",
                        fixable_by_regeneration=True,
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Counter:
    """Monotonic integer counter for finding IDs."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def _extract_labelled_metrics(content: str) -> list[tuple[str, str, float]]:
    """Extract ``(normalised_label, raw_value, parsed_float)`` tuples.

    Args:
        content: Markdown text.

    Returns:
        List of metric tuples.
    """
    results: list[tuple[str, str, float]] = []
    for m in _LABELLED_METRIC_RE.finditer(content):
        label = _normalise_label(m.group("label"))
        raw = m.group("value").strip()
        try:
            val = _parse_simple_number(raw)
            results.append((label, raw, val))
        except ValueError:
            pass
    return results


def _normalise_label(label: str) -> str:
    """Lowercase and strip excess whitespace from a metric label.

    Args:
        label: Raw label string.

    Returns:
        Normalised label.
    """
    return " ".join(label.lower().split())


def _parse_simple_number(raw: str) -> float:
    """Parse a number token (currency prefix, commas, magnitude suffix).

    Args:
        raw: Raw value string e.g. ``"$1.2M"``, ``"15%"``, ``"1,000"``.

    Returns:
        Float value.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    s = raw.strip().lstrip("$€£").rstrip()
    suffix = ""
    if s and s[-1].upper() in ("M", "B", "K"):
        suffix = s[-1]
        s = s[:-1]
    elif s.endswith("%"):
        s = s[:-1]
    s = s.replace(",", "").strip()
    try:
        val = float(s)
    except ValueError as exc:
        raise ValueError(f"Cannot parse number from {raw!r}") from exc
    multiplier = _MAGNITUDE.get(suffix.upper(), 1)
    return val * multiplier


def _approx_equal(a: float, b: float, tolerance: float = 0.01) -> bool:
    """Return True if ``|a - b| / max(|a|, |b|, 1e-10) <= tolerance``.

    Args:
        a: First value.
        b: Second value.
        tolerance: Relative tolerance (default 1%).

    Returns:
        Bool.
    """
    denom = max(abs(a), abs(b), 1e-10)
    return abs(a - b) / denom <= tolerance


def _error_finding(kind: str, detail: str, counter: "_Counter") -> DocumentFinding:
    """Create an error-skipped finding when a check raises an exception.

    Args:
        kind: Check kind identifier.
        detail: Exception message.
        counter: Finding ID counter.

    Returns:
        :class:`~saturnday.document._types.DocumentFinding`.
    """
    return DocumentFinding(
        finding_id=f"DOC-GLOBAL-{counter.next():03d}",
        section_id="global",
        kind=kind,
        severity="info",
        detail=f"Check skipped due to internal error: {detail}",
        fixable_by_regeneration=False,
    )
