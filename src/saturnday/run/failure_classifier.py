"""Classify ticket failures into governance outcomes and failure categories.

Every ticket failure is mapped to:
1. A **governance outcome** (what Saturnday's governance layer should do).
2. A **failure category** (what kind of defect caused the failure).

These classifications drive remediation advice and retry logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from saturnday._exceptions import GIT_STATE_ERROR_MARKER

logger = logging.getLogger(__name__)


GovernanceOutcome = Literal[
    "PASS",
    "SOFT_FAIL_RETRYABLE",
    "HARD_FAIL_BLOCKING",
    "TIMEOUT_RETRYABLE",
    "PLAN_DEFECT_REQUIRES_EDIT",
    "UNSUPPORTED_FRAMEWORK_FAIL_CLOSED",
    "GIT_STATE_UNAVAILABLE",
]

FailureCategory = Literal[
    "",
    "plan_defect",
    "policy_defect",
    "timeout_or_complexity_defect",
    "unsupported_environment_defect",
    "coder_non_compliance_defect",
    "framework_compatibility_defect",
    "oversize_defect",  # Fix 18: prompt exceeded OVERSIZE_SPLIT_THRESHOLD after all retries
    "git_state_unavailable_defect",
]


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """Result of classifying a ticket failure.

    Attributes:
        governance_outcome: What governance should do with this failure.
        failure_category: The defect category for reporting.
        summary: Human-readable one-line explanation.
    """

    governance_outcome: GovernanceOutcome
    failure_category: FailureCategory
    summary: str


def classify_failure(
    error: str,
    governance_disposition: str = "",
    changed_files: tuple[str, ...] = (),
    post_check_findings: list[dict] | None = None,
) -> FailureClassification:
    """Classify a ticket failure based on available evidence.

    Args:
        error: The error message from the ticket execution.
        governance_disposition: ``PASS`` or ``FAIL`` from governance.
        changed_files: Files changed by the coder.
        post_check_findings: Findings from post-governance checks.

    Returns:
        A FailureClassification with governance outcome and category.
    """
    error_lower = error.lower() if error else ""

    # Git-state unavailable — change detection precondition failed.
    # Must be checked before the `not changed_files` branch to avoid
    # misclassifying as a plan/coder defect.
    if error and GIT_STATE_ERROR_MARKER in error:
        return FailureClassification(
            governance_outcome="GIT_STATE_UNAVAILABLE",
            failure_category="git_state_unavailable_defect",
            summary=(
                "Git-based change detection unavailable — "
                "this is a git-tracking precondition failure, "
                "not evidence that the coder failed to write files"
            ),
        )

    # Timeout or API errors
    if any(tok in error_lower for tok in ("timeout", "timed out", "api error", "connection")):
        return FailureClassification(
            governance_outcome="TIMEOUT_RETRYABLE",
            failure_category="timeout_or_complexity_defect",
            summary=f"Timeout or API error: {error[:120]}",
        )

    # No files changed — likely a plan defect
    if not changed_files:
        return FailureClassification(
            governance_outcome="PLAN_DEFECT_REQUIRES_EDIT",
            failure_category="plan_defect",
            summary="Coder produced response but no files were changed",
        )

    # Governance hard fail with findings
    if governance_disposition == "FAIL" and not post_check_findings:
        return FailureClassification(
            governance_outcome="HARD_FAIL_BLOCKING",
            failure_category="policy_defect",
            summary="Governance blocked the change",
        )

    # Post-check findings — coder non-compliance
    if post_check_findings:
        return FailureClassification(
            governance_outcome="SOFT_FAIL_RETRYABLE",
            failure_category="coder_non_compliance_defect",
            summary=f"Post-checks found {len(post_check_findings)} issue(s)",
        )

    # Unsupported framework patterns
    if any(tok in error_lower for tok in ("unsupported", "not installed", "module not found")):
        return FailureClassification(
            governance_outcome="UNSUPPORTED_FRAMEWORK_FAIL_CLOSED",
            failure_category="unsupported_environment_defect",
            summary=f"Unsupported environment: {error[:120]}",
        )

    # Framework compatibility
    if any(tok in error_lower for tok in ("import error", "syntax error", "compilation")):
        return FailureClassification(
            governance_outcome="SOFT_FAIL_RETRYABLE",
            failure_category="framework_compatibility_defect",
            summary=f"Compatibility issue: {error[:120]}",
        )

    # Default: plan defect
    return FailureClassification(
        governance_outcome="PLAN_DEFECT_REQUIRES_EDIT",
        failure_category="plan_defect",
        summary=f"Unclassified failure: {error[:120]}",
    )
