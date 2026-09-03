"""Provisional handling and document-level status for Saturnday Document Mode.

Two public functions:

``evaluate_publishability``
    Determines whether a document is ready to publish given section results,
    spec risk class, and sign-off records.  Returns ``(is_publishable, reasons)``.

``compute_document_status``
    Synthesises all section statuses and global findings into a single
    :class:`~saturnday.document._types.DocumentStatus` string.  This is the
    top-level disposition used in evidence packs and run summaries.
"""

from __future__ import annotations

import logging
from typing import Any

from saturnday.document._types import DocumentApproval, DocumentSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_publishability(
    section_results: list[dict],
    spec: DocumentSpec,
    approvals: list[DocumentApproval] | None = None,
) -> tuple[bool, list[str]]:
    """Determine whether a document is publishable.

    Publishability rules (applied in order, first block wins for FAIL/block):

    1. **Any required section FAIL → not publishable** (all risk classes).
    2. **high risk + any required section PROVISIONAL_UNVERIFIED → not publishable**.
    3. **medium risk + provisional + not all sign-offs approved → not publishable**.
    4. **medium risk + provisional + all sign-offs approved → publishable with warning**.
    5. **low risk + provisional → publishable with warning** (warning in reasons).
    6. **No blocking condition → publishable**.

    "Required sections" are those whose names appear in ``spec.required_sections``.
    The match is by section ``section_id`` or ``name`` field in the result dict.

    Args:
        section_results: List of section result dicts.  Each dict should have
            at minimum: ``section_id`` (str), ``status`` (str).  An optional
            ``name`` key is used for matching against ``spec.required_sections``.
        spec: Document spec providing ``risk_class`` and ``sign_off_roles``.
        approvals: Optional list of :class:`~saturnday.document._types.DocumentApproval`
            records.  Only ``"approved"`` status entries count.

    Returns:
        ``(is_publishable, reasons)`` where ``reasons`` is a list of human-readable
        explanation strings.  ``reasons`` may be non-empty even when publishable
        (warnings).
    """
    approvals = approvals or []
    reasons: list[str] = []

    risk_class = (spec.risk_class or "high").lower()

    # Classify section results as required vs optional
    required_names_lower = {s.lower() for s in (spec.required_sections or [])}

    failed_required: list[str] = []
    provisional_required: list[str] = []

    for sr in section_results:
        sid = str(sr.get("section_id", ""))
        name = str(sr.get("name", ""))
        status = str(sr.get("status", ""))

        # A section is "required" if its id or name matches spec.required_sections.
        is_required = (
            sid.lower() in required_names_lower
            or name.lower() in required_names_lower
        )

        if not is_required:
            # Also consider it required if required_sections is empty
            # (all sections treated as required when list is not restricted).
            if not spec.required_sections:
                is_required = True

        if not is_required:
            continue

        if status == "FAIL":
            failed_required.append(sid)
        elif status == "PROVISIONAL_UNVERIFIED":
            provisional_required.append(sid)

    # Rule 1: Any required FAIL → blocked regardless of risk class.
    if failed_required:
        reasons.append(
            f"Required section(s) failed verification: {', '.join(failed_required)}. "
            "Document cannot be published until these sections pass."
        )
        return False, reasons

    # Rules 2–5: Provisional handling depends on risk class.
    if provisional_required:
        if risk_class == "high":
            reasons.append(
                f"High-risk document has provisional required section(s): "
                f"{', '.join(provisional_required)}. "
                "All required sections must pass verification for high-risk documents."
            )
            return False, reasons

        if risk_class == "medium":
            approved_roles = _approved_role_set(approvals)
            required_roles = set(spec.sign_off_roles or [])
            missing_roles = required_roles - approved_roles
            if missing_roles:
                reasons.append(
                    f"Medium-risk document has provisional required section(s): "
                    f"{', '.join(provisional_required)}. "
                    f"Missing sign-offs from: {', '.join(sorted(missing_roles))}."
                )
                return False, reasons
            # All sign-offs present — publishable with a warning.
            reasons.append(
                f"Document contains provisional section(s) "
                f"({', '.join(provisional_required)}) but all required sign-offs "
                "are present. Published with provisional warning."
            )

        if risk_class == "low":
            # Warn only — do not block.
            reasons.append(
                f"Document contains provisional section(s) "
                f"({', '.join(provisional_required)}). "
                "Low-risk: published with provisional warning."
            )

    return True, reasons


def compute_document_status(
    sections: list[Any],
    global_findings: list[Any],
    spec: DocumentSpec,
) -> str:
    """Compute the top-level document status from section statuses and global findings.

    Precedence (highest to lowest):

    1. ``FAIL`` — any section has status ``"FAIL"`` OR any global finding has
       severity ``"error"``.
    2. ``PROVISIONAL_UNVERIFIED`` — any section has status
       ``"PROVISIONAL_UNVERIFIED"`` AND ``spec.risk_class != "low"``.
       For low-risk documents, provisional sections produce ``"WARN"`` instead.
    3. ``BLOCKED_FOR_SIGNOFF`` — all sections PASS/WARN and no global errors,
       but ``spec.sign_off_roles`` is non-empty.
    4. ``WARN`` — all sections PASS/WARN (or provisional on low-risk), no global
       errors, no sign-off requirement.
    5. ``PASS`` — all sections pass, no global findings, no sign-off requirement.

    Args:
        sections: List of objects with a ``.status`` attribute, or dicts with a
            ``"status"`` key.  Accepts both :class:`~saturnday.document._types.DocumentSection`
            objects and plain dicts from section result pipelines.
        global_findings: List of finding objects with a ``.severity`` attribute,
            or dicts with a ``"severity"`` key.
        spec: Document spec providing ``risk_class`` and ``sign_off_roles``.

    Returns:
        Status string — one of ``"PASS"``, ``"WARN"``,
        ``"PROVISIONAL_UNVERIFIED"``, ``"FAIL"``,
        ``"BLOCKED_FOR_SIGNOFF"``.
    """
    risk_class = (spec.risk_class or "high").lower()

    # Normalise sections to status strings.
    section_statuses = [_get_status(s) for s in sections]

    # Normalise global findings to severity strings.
    global_severities = [_get_severity(f) for f in global_findings]

    has_section_fail = any(s == "FAIL" for s in section_statuses)
    has_global_error = any(s == "error" for s in global_severities)
    has_provisional = any(s == "PROVISIONAL_UNVERIFIED" for s in section_statuses)
    has_section_warn = any(s == "WARN" for s in section_statuses)
    has_global_warn = any(s == "warning" for s in global_severities)
    needs_signoff = bool(spec.sign_off_roles)

    # Rule 1: Hard FAIL.
    if has_section_fail or has_global_error:
        return "FAIL"

    # Rule 2: Provisional.
    if has_provisional:
        if risk_class == "low":
            # Low-risk: demote to WARN.
            return "WARN"
        return "PROVISIONAL_UNVERIFIED"

    # Rule 3: Blocked for sign-off.
    if needs_signoff and not has_section_fail and not has_provisional:
        return "BLOCKED_FOR_SIGNOFF"

    # Rule 4: Warn.
    if has_section_warn or has_global_warn:
        return "WARN"

    # Rule 5: Pass.
    return "PASS"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approved_role_set(approvals: list[DocumentApproval]) -> set[str]:
    """Return the set of roles that have ``status == "approved"``.

    Args:
        approvals: List of :class:`~saturnday.document._types.DocumentApproval`.

    Returns:
        Set of approved role strings.
    """
    return {a.role for a in approvals if a.status == "approved"}


def _get_status(obj: Any) -> str:
    """Extract status string from a DocumentSection or dict.

    Args:
        obj: Object with ``.status`` attribute or dict with ``"status"`` key.

    Returns:
        Status string, defaulting to ``""`` if not found.
    """
    if isinstance(obj, dict):
        return str(obj.get("status", ""))
    return str(getattr(obj, "status", ""))


def _get_severity(obj: Any) -> str:
    """Extract severity string from a DocumentFinding or dict.

    Args:
        obj: Object with ``.severity`` attribute or dict with ``"severity"`` key.

    Returns:
        Severity string, defaulting to ``""`` if not found.
    """
    if isinstance(obj, dict):
        return str(obj.get("severity", ""))
    return str(getattr(obj, "severity", ""))
