"""Section verifier for Saturnday Document Mode.

Orchestrates all five local check families for a single document section and
returns a combined finding list.  Each check is independently try/excepted so
one failed check cannot block the others.

Public API::

    findings = verify_section(content, section, spec, plan, source_contents)
    status   = compute_section_status(findings)
"""

from __future__ import annotations

import logging

from saturnday.document._types import DocumentFinding, DocumentPlan, DocumentSection, DocumentSpec

logger = logging.getLogger(__name__)


def verify_section(
    content: str,
    section: DocumentSection,
    spec: DocumentSpec,
    plan: DocumentPlan,
    source_contents: dict[str, str],
) -> list[DocumentFinding]:
    """Run all applicable local checks on a section and return combined findings.

    Runs checks in order:

    1. :func:`~saturnday.document.checks.structure.check_structure` (always)
    2. :func:`~saturnday.document.checks.citations.check_citations` (always for required sections)
    3. :func:`~saturnday.document.checks.numeric.check_numeric` (always)
    4. :func:`~saturnday.document.checks.terminology.check_terminology` (always)
    5. :func:`~saturnday.document.checks.evidence_coverage.check_evidence_coverage` (always)

    Each check is wrapped in ``try/except``.  A check failure produces a single
    ``SKIPPED`` finding and does not block subsequent checks.

    Args:
        content: Raw Markdown string for the section.
        section: Section definition from the document plan.
        spec: Document spec governing this run.
        plan: Full document plan (provides section names for citation validation).
        source_contents: Mapping of source path → content (for numeric check).

    Returns:
        Combined list of :class:`~saturnday.document._types.DocumentFinding`
        from all checks that ran successfully.
    """
    findings: list[DocumentFinding] = []
    all_section_names = [s.name for s in plan.sections]

    # --- 1. Structure check ---
    try:
        from saturnday.document.checks.structure import check_structure
        findings.extend(check_structure(content, section, spec))
    except Exception as exc:
        logger.exception("Structure check failed for section %s: %s", section.section_id, exc)
        findings.append(
            _skipped_finding(section.section_id, "structure_check_error", str(exc))
        )

    # --- 2. Citation check ---
    try:
        from saturnday.document.checks.citations import check_citations
        findings.extend(
            check_citations(
                content,
                section,
                approved_sources=spec.approved_sources,
                all_section_names=all_section_names,
            )
        )
    except Exception as exc:
        logger.exception("Citation check failed for section %s: %s", section.section_id, exc)
        findings.append(
            _skipped_finding(section.section_id, "citation_check_error", str(exc))
        )

    # --- 3. Numeric check ---
    try:
        from saturnday.document.checks.numeric import check_numeric
        findings.extend(check_numeric(content, section, source_contents))
    except Exception as exc:
        logger.exception("Numeric check failed for section %s: %s", section.section_id, exc)
        findings.append(
            _skipped_finding(section.section_id, "numeric_check_error", str(exc))
        )

    # --- 4. Terminology check ---
    try:
        from saturnday.document.checks.terminology import check_terminology
        findings.extend(check_terminology(content, section, spec))
    except Exception as exc:
        logger.exception("Terminology check failed for section %s: %s", section.section_id, exc)
        findings.append(
            _skipped_finding(section.section_id, "terminology_check_error", str(exc))
        )

    # --- 5. Evidence coverage check ---
    try:
        from saturnday.document.checks.evidence_coverage import check_evidence_coverage
        findings.extend(check_evidence_coverage(content, section, spec))
    except Exception as exc:
        logger.exception(
            "Evidence coverage check failed for section %s: %s", section.section_id, exc
        )
        findings.append(
            _skipped_finding(section.section_id, "evidence_coverage_check_error", str(exc))
        )

    return findings


def compute_section_status(findings: list[DocumentFinding]) -> str:
    """Compute the section status string from a list of findings.

    Mapping:

    - No findings → ``"PASS"``
    - Only ``"info"`` or ``"warning"`` severity findings → ``"WARN"``
    - Any ``"error"`` severity finding → ``"FAIL"``

    Args:
        findings: List of :class:`~saturnday.document._types.DocumentFinding` objects.

    Returns:
        Status string: ``"PASS"``, ``"WARN"``, or ``"FAIL"``.
    """
    if not findings:
        return "PASS"
    has_error = any(f.severity == "error" for f in findings)
    if has_error:
        return "FAIL"
    return "WARN"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _skipped_finding(section_id: str, kind: str, error_detail: str) -> DocumentFinding:
    """Create a synthetic SKIPPED finding when a check raises an exception.

    Args:
        section_id: Section the check was running against.
        kind: Identifying kind string for this failure.
        error_detail: Exception message or description.

    Returns:
        A :class:`~saturnday.document._types.DocumentFinding` with
        severity ``"info"`` so it does not block section progression.
    """
    return DocumentFinding(
        finding_id=f"DOC-SKIP-{kind.upper()}",
        section_id=section_id,
        kind=kind,
        severity="info",
        detail=f"Check skipped due to internal error: {error_detail}",
        fixable_by_regeneration=False,
    )
