"""Tests for saturnday.document._types.

Covers dataclass construction, default values, and enum membership for the
Document Mode artefact model.
"""

from __future__ import annotations

import pytest

from saturnday.document._types import (
    ClaimType,
    DocumentApproval,
    DocumentClaim,
    DocumentFinding,
    DocumentPlan,
    DocumentRunResult,
    DocumentSection,
    DocumentSpec,
    DocumentStatus,
    SupportVerdict,
)


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestDocumentStatusEnum:
    def test_all_values_present(self) -> None:
        expected = {
            "PASS",
            "WARN",
            "PROVISIONAL_UNVERIFIED",
            "FAIL",
            "BLOCKED_FOR_SIGNOFF",
            "APPROVED",
            "REJECTED",
        }
        assert {s.value for s in DocumentStatus} == expected

    def test_is_str_subclass(self) -> None:
        assert isinstance(DocumentStatus.PASS, str)

    def test_equality_with_string(self) -> None:
        assert DocumentStatus.FAIL == "FAIL"

    def test_string_comparison_roundtrip(self) -> None:
        for status in DocumentStatus:
            assert DocumentStatus(status.value) == status


class TestClaimTypeEnum:
    def test_all_values_present(self) -> None:
        expected = {
            "quantitative",
            "date",
            "named_assertion",
            "recommendation",
            "cited",
            "summary",
            "regulatory",
        }
        assert {c.value for c in ClaimType} == expected

    def test_is_str_subclass(self) -> None:
        assert isinstance(ClaimType.QUANTITATIVE, str)

    def test_equality_with_string(self) -> None:
        assert ClaimType.DATE == "date"

    def test_roundtrip(self) -> None:
        for ct in ClaimType:
            assert ClaimType(ct.value) == ct


class TestSupportVerdictEnum:
    def test_all_values_present(self) -> None:
        expected = {
            "SUPPORTED",
            "WEAKLY_SUPPORTED",
            "UNSUPPORTED",
            "CONTRADICTED",
            "UNCERTAIN_REQUIRES_REVIEW",
        }
        assert {v.value for v in SupportVerdict} == expected

    def test_is_str_subclass(self) -> None:
        assert isinstance(SupportVerdict.SUPPORTED, str)

    def test_equality_with_string(self) -> None:
        assert SupportVerdict.UNSUPPORTED == "UNSUPPORTED"

    def test_roundtrip(self) -> None:
        for sv in SupportVerdict:
            assert SupportVerdict(sv.value) == sv


# ---------------------------------------------------------------------------
# DocumentSpec
# ---------------------------------------------------------------------------


class TestDocumentSpecCreation:
    def _minimal_spec(self) -> DocumentSpec:
        return DocumentSpec(
            type="report",
            purpose="Describe quarterly results",
            audience="Board",
            risk_class="medium",
            required_sections=["Executive Summary", "Financials"],
            claim_policy={"quantitative_claims_require_source": True},
            approved_sources=["data/q4.csv"],
            sign_off_roles=["CFO"],
        )

    def test_required_fields_stored(self) -> None:
        spec = self._minimal_spec()
        assert spec.type == "report"
        assert spec.purpose == "Describe quarterly results"
        assert spec.audience == "Board"
        assert spec.risk_class == "medium"
        assert spec.required_sections == ["Executive Summary", "Financials"]
        assert spec.claim_policy == {"quantitative_claims_require_source": True}
        assert spec.approved_sources == ["data/q4.csv"]
        assert spec.sign_off_roles == ["CFO"]

    def test_optional_defaults(self) -> None:
        spec = self._minimal_spec()
        assert spec.jurisdiction == ""
        assert spec.template == ""
        assert spec.required_terminology == []
        assert spec.banned_terminology == []
        assert spec.numeric_tolerance == {}
        assert spec.citation_style == "internal_reference"
        assert spec.max_retry_per_section == 2

    def test_optional_overrides(self) -> None:
        spec = DocumentSpec(
            type="policy",
            purpose="Govern data retention",
            audience="Legal",
            risk_class="high",
            required_sections=["Scope"],
            claim_policy={"cited_claims_only": True},
            approved_sources=["policies/base.md"],
            sign_off_roles=["CTO", "Legal"],
            jurisdiction="EU",
            template="templates/policy.md",
            required_terminology=["GDPR"],
            banned_terminology=["delete immediately"],
            numeric_tolerance={"retention_days": 5},
            citation_style="footnote",
            max_retry_per_section=3,
        )
        assert spec.jurisdiction == "EU"
        assert spec.template == "templates/policy.md"
        assert spec.required_terminology == ["GDPR"]
        assert spec.banned_terminology == ["delete immediately"]
        assert spec.numeric_tolerance == {"retention_days": 5}
        assert spec.citation_style == "footnote"
        assert spec.max_retry_per_section == 3

    def test_mutable_list_fields_are_independent(self) -> None:
        """Each instance gets its own list; mutation does not bleed."""
        s1 = self._minimal_spec()
        s2 = self._minimal_spec()
        s1.required_terminology.append("delta")
        assert "delta" not in s2.required_terminology


# ---------------------------------------------------------------------------
# DocumentSection
# ---------------------------------------------------------------------------


class TestDocumentSectionDefaults:
    def test_minimal_creation(self) -> None:
        section = DocumentSection(
            section_id="S001",
            name="Introduction",
            purpose="Provide background",
        )
        assert section.section_id == "S001"
        assert section.name == "Introduction"
        assert section.purpose == "Provide background"

    def test_default_values(self) -> None:
        section = DocumentSection(
            section_id="S002",
            name="Methods",
            purpose="Describe approach",
        )
        assert section.required_sources == []
        assert section.acceptance_criteria == []
        assert section.required_checks == []
        assert section.status == "PENDING"
        assert section.content_path == ""
        assert section.retry_count == 0
        assert section.findings == []

    def test_mutable_defaults_are_independent(self) -> None:
        s1 = DocumentSection(section_id="S001", name="A", purpose="p")
        s2 = DocumentSection(section_id="S002", name="B", purpose="p")
        s1.findings.append({"detail": "test"})
        assert s2.findings == []

    def test_full_construction(self) -> None:
        section = DocumentSection(
            section_id="S003",
            name="Results",
            purpose="Present findings",
            required_sources=["data.csv"],
            acceptance_criteria=["All tables have headers"],
            required_checks=["structure", "placeholder_detection"],
            status="PASS",
            content_path=".saturnday/document/sections/S003.md",
            retry_count=1,
            findings=[{"finding_id": "F001", "detail": "missing citation"}],
        )
        assert section.status == "PASS"
        assert section.retry_count == 1
        assert len(section.findings) == 1


# ---------------------------------------------------------------------------
# DocumentPlan
# ---------------------------------------------------------------------------


class TestDocumentPlanCreation:
    def test_minimal_plan(self) -> None:
        plan = DocumentPlan(
            document_id="abc-123",
            type="report",
            purpose="Quarterly results",
            risk_class="low",
        )
        assert plan.document_id == "abc-123"
        assert plan.sections == []
        assert plan.global_checks == []

    def test_plan_with_sections(self) -> None:
        sections = [
            DocumentSection(section_id="S001", name="Intro", purpose="p"),
            DocumentSection(section_id="S002", name="Body", purpose="p"),
        ]
        plan = DocumentPlan(
            document_id="xyz",
            type="policy",
            purpose="Data retention",
            risk_class="high",
            sections=sections,
            global_checks=["cross_section_consistency"],
        )
        assert len(plan.sections) == 2
        assert plan.global_checks == ["cross_section_consistency"]


# ---------------------------------------------------------------------------
# DocumentClaim
# ---------------------------------------------------------------------------


class TestDocumentClaimCreation:
    def test_minimal_claim(self) -> None:
        claim = DocumentClaim(
            claim_id="C001",
            section_id="S001",
            claim_text="Revenue grew 12% YoY",
            claim_type=ClaimType.QUANTITATIVE,
        )
        assert claim.claim_id == "C001"
        assert claim.linked_sources == []
        assert claim.support_verdict == ""
        assert claim.notes == ""
        assert claim.uncertainty is False

    def test_claim_type_stored_as_string_value(self) -> None:
        claim = DocumentClaim(
            claim_id="C002",
            section_id="S001",
            claim_text="See regulation 42",
            claim_type=ClaimType.REGULATORY,
        )
        # ClaimType is a str Enum — value is the string itself
        assert claim.claim_type == "regulatory"


# ---------------------------------------------------------------------------
# DocumentFinding
# ---------------------------------------------------------------------------


class TestDocumentFindingCreation:
    def test_minimal_finding(self) -> None:
        finding = DocumentFinding(
            finding_id="F001",
            section_id="S001",
            kind="placeholder_detection",
            severity="error",
            detail="TODO found in line 5",
        )
        assert finding.finding_id == "F001"
        assert finding.evidence_ref == ""
        assert finding.fixable_by_regeneration is False

    def test_fixable_finding(self) -> None:
        finding = DocumentFinding(
            finding_id="F002",
            section_id="S002",
            kind="citation_existence",
            severity="warning",
            detail="cited source does not exist",
            fixable_by_regeneration=True,
        )
        assert finding.fixable_by_regeneration is True


# ---------------------------------------------------------------------------
# DocumentApproval
# ---------------------------------------------------------------------------


class TestDocumentApprovalCreation:
    def test_minimal_approval(self) -> None:
        approval = DocumentApproval(
            document_id="doc-1",
            role="CFO",
            actor="alice@example.com",
            status="approved",
        )
        assert approval.document_id == "doc-1"
        assert approval.timestamp == ""
        assert approval.notes == ""
        assert approval.overridden_findings == []

    def test_approval_with_overrides(self) -> None:
        approval = DocumentApproval(
            document_id="doc-2",
            role="Legal",
            actor="bob@example.com",
            status="approved",
            timestamp="2026-01-01T00:00:00Z",
            notes="Accepted under waiver W-001",
            overridden_findings=["F003", "F007"],
        )
        assert approval.overridden_findings == ["F003", "F007"]


# ---------------------------------------------------------------------------
# DocumentRunResult
# ---------------------------------------------------------------------------


class TestDocumentRunResultCreation:
    def test_minimal_run_result(self) -> None:
        result = DocumentRunResult(
            document_id="run-1",
            type="report",
        )
        assert result.total_sections == 0
        assert result.passed == 0
        assert result.failed == 0
        assert result.provisional == 0
        assert result.document_status == "PENDING"
        assert result.sections == []
        assert result.claims == []
        assert result.approvals == []
        assert result.global_findings == []

    def test_mutable_fields_independent(self) -> None:
        r1 = DocumentRunResult(document_id="r1", type="report")
        r2 = DocumentRunResult(document_id="r2", type="report")
        r1.global_findings.append(
            DocumentFinding(
                finding_id="F001",
                section_id="",
                kind="cross_section_consistency",
                severity="error",
                detail="sections contradict",
            )
        )
        assert r2.global_findings == []
