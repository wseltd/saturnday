"""Document artefact types for Saturnday Document Mode.

These dataclasses define the full artefact model for governed document
generation: spec, plan, sections, claims, findings, approvals, and run result.

All mutable dataclasses use ``field(default_factory=...)`` for list/dict
fields to avoid shared-mutable-default bugs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DocumentStatus(str, Enum):
    """Top-level disposition for a governed document run."""

    PASS = "PASS"
    WARN = "WARN"
    PROVISIONAL_UNVERIFIED = "PROVISIONAL_UNVERIFIED"
    FAIL = "FAIL"
    BLOCKED_FOR_SIGNOFF = "BLOCKED_FOR_SIGNOFF"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ClaimType(str, Enum):
    """Classification of a factual claim extracted from a document section."""

    QUANTITATIVE = "quantitative"
    DATE = "date"
    NAMED_ASSERTION = "named_assertion"
    RECOMMENDATION = "recommendation"
    CITED = "cited"
    SUMMARY = "summary"
    REGULATORY = "regulatory"


class SupportVerdict(str, Enum):
    """Verdict from evidence-grounding check on a single claim."""

    SUPPORTED = "SUPPORTED"
    WEAKLY_SUPPORTED = "WEAKLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    UNCERTAIN_REQUIRES_REVIEW = "UNCERTAIN_REQUIRES_REVIEW"


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass
class DocumentSpec:
    """Parsed and validated representation of a doc-spec.yaml file.

    Attributes:
        type: Document type label (e.g. ``"report"``, ``"policy"``).
        purpose: Free-form statement of what this document is for.
        audience: Intended readership (e.g. ``"internal engineers"``).
        risk_class: Governance risk tier — ``"low"``, ``"medium"``, or ``"high"``.
        required_sections: Ordered list of section names that must appear.
        claim_policy: Dict of claim-handling rules
            (e.g. ``{"quantitative_claims_require_source": true}``).
        approved_sources: Paths to source files the document may reference.
        sign_off_roles: Roles required to approve the document before publish.
        jurisdiction: Optional jurisdiction string (e.g. ``"EU"``, ``"US"``).
        template: Optional path to a markdown template for this document.
        required_terminology: Terms that must appear in the final document.
        banned_terminology: Terms that must not appear in the final document.
        numeric_tolerance: Per-metric tolerance overrides for numeric checks.
        citation_style: Citation format (default ``"internal_reference"``).
        max_retry_per_section: Maximum section regeneration attempts.
    """

    type: str
    purpose: str
    audience: str
    risk_class: str  # "low", "medium", "high"
    required_sections: list[str]
    claim_policy: dict  # keys: quantitative_claims_require_source, etc.
    approved_sources: list[str]
    sign_off_roles: list[str]
    # Optional fields with defaults
    jurisdiction: str = ""
    template: str = ""
    required_terminology: list[str] = field(default_factory=list)
    banned_terminology: list[str] = field(default_factory=list)
    numeric_tolerance: dict = field(default_factory=dict)
    citation_style: str = "internal_reference"
    max_retry_per_section: int = 2


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class DocumentSection:
    """A single section within a document plan.

    Attributes:
        section_id: Auto-assigned identifier (e.g. ``"S001"``).
        name: Human-readable section name from the spec.
        purpose: Derived purpose statement for this section.
        required_sources: Source paths this section may draw from.
        acceptance_criteria: Machine-readable criteria the section must satisfy.
        required_checks: Check IDs that must pass for this section.
        status: Current status (``"PENDING"``, ``"PASS"``, ``"FAIL"``, etc.).
        content_path: Filesystem path where generated content is written.
        retry_count: Number of times this section has been regenerated.
        findings: List of finding dicts accumulated for this section.
    """

    section_id: str
    name: str
    purpose: str
    required_sources: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    status: str = "PENDING"
    content_path: str = ""
    retry_count: int = 0
    findings: list[dict] = field(default_factory=list)


@dataclass
class DocumentPlan:
    """Full plan for a governed document run, derived from a DocumentSpec.

    Attributes:
        document_id: Unique run identifier for this document.
        type: Document type from the spec.
        purpose: Document purpose from the spec.
        risk_class: Risk tier from the spec.
        sections: Ordered sections derived from ``required_sections``.
        global_checks: Cross-section checks run after all sections complete.
    """

    document_id: str
    type: str
    purpose: str
    risk_class: str
    sections: list[DocumentSection] = field(default_factory=list)
    global_checks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@dataclass
class DocumentClaim:
    """A factual claim extracted from a document section.

    Attributes:
        claim_id: Unique claim identifier within this document.
        section_id: The section this claim belongs to.
        claim_text: Verbatim or paraphrased claim text.
        claim_type: Classification from ``ClaimType``.
        linked_sources: Source paths cited in support of this claim.
        support_verdict: Grounding verdict from ``SupportVerdict``.
        notes: Reviewer or checker notes about this claim.
        uncertainty: True if the claim is hedged or flagged as uncertain.
    """

    claim_id: str
    section_id: str
    claim_text: str
    claim_type: str  # ClaimType value
    linked_sources: list[str] = field(default_factory=list)
    support_verdict: str = ""  # SupportVerdict value
    notes: str = ""
    uncertainty: bool = False


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class DocumentFinding:
    """A single finding produced by a document check.

    Attributes:
        finding_id: Unique finding identifier within this document.
        section_id: The section this finding applies to (empty = global).
        kind: Check kind (e.g. ``"placeholder_detection"``, ``"citation_existence"``).
        severity: Severity level — ``"error"``, ``"warning"``, or ``"info"``.
        detail: Human-readable description of the finding.
        evidence_ref: Optional path or ID pointing to supporting evidence.
        fixable_by_regeneration: True if regenerating the section may fix this.
    """

    finding_id: str
    section_id: str
    kind: str
    severity: str  # "error", "warning", "info"
    detail: str
    evidence_ref: str = ""
    fixable_by_regeneration: bool = False


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@dataclass
class DocumentApproval:
    """A sign-off record for a governed document.

    Attributes:
        document_id: The document this approval applies to.
        role: The required sign-off role (e.g. ``"legal"``, ``"eng-lead"``).
        actor: The person or system that submitted this approval.
        status: Current status — ``"approved"``, ``"rejected"``, or ``"pending"``.
        timestamp: UTC ISO-8601 timestamp of the approval action.
        notes: Optional rationale or reviewer comment.
        overridden_findings: Finding IDs that this approver explicitly accepted.
    """

    document_id: str
    role: str
    actor: str
    status: str  # "approved", "rejected", "pending"
    timestamp: str = ""
    notes: str = ""
    overridden_findings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Run result
# ---------------------------------------------------------------------------


@dataclass
class DocumentRunResult:
    """Summary of a complete governed document run.

    Attributes:
        document_id: Unique identifier for this document run.
        type: Document type from the spec.
        total_sections: Total number of sections in the plan.
        passed: Sections that reached PASS status.
        failed: Sections that reached FAIL status.
        provisional: Sections with PROVISIONAL_UNVERIFIED status.
        document_status: Overall document disposition (``DocumentStatus`` value).
        sections: Per-section results.
        claims: All claims extracted and verified across sections.
        approvals: All sign-off records collected.
        global_findings: Findings from cross-section checks.
    """

    document_id: str
    type: str
    total_sections: int = 0
    passed: int = 0
    failed: int = 0
    provisional: int = 0
    document_status: str = "PENDING"
    sections: list[DocumentSection] = field(default_factory=list)
    claims: list[DocumentClaim] = field(default_factory=list)
    approvals: list[DocumentApproval] = field(default_factory=list)
    global_findings: list[DocumentFinding] = field(default_factory=list)
