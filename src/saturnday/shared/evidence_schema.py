"""Shared evidence schema constants.

Both the Guard evidence module (``saturnday.evidence``) and the Run evidence
module (``saturnday.run.evidence``) import from here so that schema versioning
stays in one place.  Update ``SCHEMA_VERSION`` here when the evidence format
changes; both modules will pick it up automatically.

This module also provides capability-state helpers used by every evidence pack
to record which premium stages were available at run time.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1.1.0"

# Canonical output sub-directory names within any evidence root.
# These are conventions, not enforcement — callers decide their root.
EVIDENCE_DIR_GUARD = "evidence/governance"
EVIDENCE_DIR_RUN = "evidence/run"
EVIDENCE_DIR_REPAIR = "evidence/repair"
EVIDENCE_DIR_DOCUMENT = "evidence/document"
EVIDENCE_DIR_RELEASE = "evidence/release"

# ---------------------------------------------------------------------------
# Premium capability constants
# ---------------------------------------------------------------------------

#: Canonical stage names for all premium capabilities, in pipeline order.
#: These names are the registry keys used with ``capability_registry.register``.
PREMIUM_STAGE_NAMES: list[str] = [
    "security_triage",
    "memory_provider",
    "spec_verifier",
    "impact_analysis",
    "code_reviewer",
    "doc_post_global",
    "evidence_appender",
    "release_governance",
]


def build_capability_state() -> dict[str, Any]:
    """Build the premium capability state block for inclusion in evidence packs.

    The import of ``capability_registry`` is performed lazily inside the function
    body to avoid circular imports — ``evidence_schema`` is imported very early in
    the package graph and ``capability_registry`` may not yet be fully initialised
    at module load time.

    Returns:
        A dict with nine keys:

        ``premium_capabilities_enabled``
            ``True`` if at least one premium stage is registered; ``False``
            otherwise (public-only deployment).

        ``premium_package_installed``
            ``True`` if the ``saturnday_premium`` package is importable;
            ``False`` if it is not installed.

        ``entitlement_valid``
            ``True`` or ``False`` reflecting the entitlement check result, or
            ``None`` if the premium package is not installed.

        ``entitlement_reason``
            The reason string from the entitlement check, or ``None`` if the
            premium package is not installed.

        ``entitlement_org``
            Organisation name from the licence JWT, or ``None`` if the premium
            package is not installed or the licence does not carry an org claim.

        ``entitlement_edition``
            Edition string from the licence JWT (e.g. ``"premium"`` or
            ``"enterprise"``), or ``None`` if not installed or not present.

        ``entitlement_expires``
            ISO 8601 expiry timestamp string from the licence JWT, or ``None``
            if not installed or the token carries no expiry claim.

        ``available_premium_hooks``
            Sorted list of registered stage names as returned by
            ``capability_registry.registered_stages()``.

        ``skipped_premium_stages``
            Always an empty list here.  Callers that track per-stage execution
            should populate this field using ``build_skipped_stages()``.
    """
    from saturnday.capability_registry import registered_stages  # lazy import

    stages: list[str] = registered_stages()

    # Probe premium package presence and entitlement without requiring it.
    premium_installed: bool = False
    entitlement_valid: bool | None = None
    entitlement_reason: str | None = None
    entitlement_org: str | None = None
    entitlement_edition: str | None = None
    entitlement_expires: str | None = None
    try:
        import saturnday_premium  # noqa: F401

        premium_installed = True
        state = saturnday_premium.entitlement_state()
        entitlement_valid = state.valid
        entitlement_reason = state.reason
        entitlement_org = state.org
        entitlement_edition = getattr(state, "edition", None)
        entitlement_expires = state.expires
    except ImportError:
        pass

    return {
        "premium_capabilities_enabled": len(stages) > 0,
        "premium_package_installed": premium_installed,
        "entitlement_valid": entitlement_valid,
        "entitlement_reason": entitlement_reason,
        "entitlement_org": entitlement_org,
        "entitlement_edition": entitlement_edition,
        "entitlement_expires": entitlement_expires,
        "available_premium_hooks": stages,
        "skipped_premium_stages": [],
    }


def build_skipped_stages(
    ran_stages: set[str] | None = None,
) -> list[dict[str, str]]:
    """Build the skipped_premium_stages list for inclusion in evidence packs.

    Compares :data:`PREMIUM_STAGE_NAMES` against the capability registry and
    the set of stages that actually ran during this execution.  Each entry in
    the returned list is a dict with two keys:

    ``stage``
        The canonical stage name (one of :data:`PREMIUM_STAGE_NAMES`).

    ``reason``
        One of:

        * ``"premium_not_available"`` — the stage is not registered in the
          capability registry (premium is not installed or not wired).
        * ``"registered_but_not_executed"`` — the stage is registered but was
          not present in *ran_stages* (premium is available but the stage was
          skipped for this run).

    If *ran_stages* is ``None``, registered stages are treated as having run
    (backward-compatible default for callers that do not yet track per-stage
    execution).  Only unregistered stages appear in the output in that case,
    all with reason ``"premium_not_available"``.

    Args:
        ran_stages: Set of stage names that executed during this run, or
            ``None`` if the caller does not track per-stage execution yet.

    Returns:
        A list of ``{"stage": name, "reason": reason_str}`` dicts, one for
        each premium stage that was skipped.  Stages that ran successfully are
        not included.
    """
    from saturnday.capability_registry import registered_stages  # lazy import

    registered: set[str] = set(registered_stages())
    skipped: list[dict[str, str]] = []

    for name in PREMIUM_STAGE_NAMES:
        if name not in registered:
            skipped.append({"stage": name, "reason": "premium_not_available"})
        elif ran_stages is not None and name not in ran_stages:
            skipped.append({"stage": name, "reason": "registered_but_not_executed"})
        # else: registered and (ran_stages is None or name in ran_stages) — not skipped

    return skipped


__all__ = [
    "SCHEMA_VERSION",
    "EVIDENCE_DIR_GUARD",
    "EVIDENCE_DIR_RUN",
    "EVIDENCE_DIR_REPAIR",
    "EVIDENCE_DIR_DOCUMENT",
    "EVIDENCE_DIR_RELEASE",
    "PREMIUM_STAGE_NAMES",
    "build_capability_state",
    "build_skipped_stages",
]
