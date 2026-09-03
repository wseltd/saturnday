"""Tests for saturnday.document.provisional.

Covers evaluate_publishability and compute_document_status across all
risk_class × section_status combinations.
"""

from __future__ import annotations

import pytest

from saturnday.document._types import DocumentApproval, DocumentSection, DocumentSpec
from saturnday.document.provisional import compute_document_status, evaluate_publishability


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _spec(
    risk_class: str = "high",
    required_sections: list[str] | None = None,
    sign_off_roles: list[str] | None = None,
) -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Test",
        audience="Internal",
        risk_class=risk_class,
        required_sections=required_sections or ["Executive Summary"],
        claim_policy={},
        approved_sources=[],
        sign_off_roles=sign_off_roles or [],
    )


def _sr(section_id: str, status: str, name: str = "") -> dict:
    return {"section_id": section_id, "status": status, "name": name}


def _section_obj(status: str) -> DocumentSection:
    return DocumentSection(
        section_id="S001",
        name="Executive Summary",
        purpose="Overview",
        status=status,
    )


def _approval(role: str, status: str = "approved") -> DocumentApproval:
    return DocumentApproval(
        document_id="doc-001",
        role=role,
        actor="test-user",
        status=status,
    )


# ---------------------------------------------------------------------------
# Tests: evaluate_publishability
# ---------------------------------------------------------------------------


class TestEvaluatePublishability:
    def test_all_pass_no_signoff_required_publishable(self):
        spec = _spec(risk_class="medium", required_sections=["S001"], sign_off_roles=[])
        sections = [_sr("S001", "PASS")]
        ok, reasons = evaluate_publishability(sections, spec)
        assert ok is True

    def test_required_fail_blocks_all_risk_classes(self):
        for risk in ("low", "medium", "high"):
            spec = _spec(risk_class=risk, required_sections=["S001"])
            sections = [_sr("S001", "FAIL")]
            ok, reasons = evaluate_publishability(sections, spec)
            assert ok is False, f"Expected block for risk={risk}"
            assert any("fail" in r.lower() for r in reasons)

    def test_high_risk_provisional_blocked(self):
        spec = _spec(risk_class="high", required_sections=["S001"])
        sections = [_sr("S001", "PROVISIONAL_UNVERIFIED")]
        ok, reasons = evaluate_publishability(sections, spec)
        assert ok is False
        assert any("high-risk" in r.lower() or "high risk" in r.lower() for r in reasons)

    def test_medium_risk_provisional_no_signoff_blocked(self):
        spec = _spec(risk_class="medium", required_sections=["S001"], sign_off_roles=["CFO"])
        sections = [_sr("S001", "PROVISIONAL_UNVERIFIED")]
        ok, reasons = evaluate_publishability(sections, spec, approvals=[])
        assert ok is False
        assert any("cfo" in r.lower() or "sign-off" in r.lower() or "missing" in r.lower() for r in reasons)

    def test_medium_risk_provisional_with_all_signoffs_publishable(self):
        spec = _spec(risk_class="medium", required_sections=["S001"], sign_off_roles=["CFO"])
        sections = [_sr("S001", "PROVISIONAL_UNVERIFIED")]
        approvals = [_approval("CFO")]
        ok, reasons = evaluate_publishability(sections, spec, approvals=approvals)
        assert ok is True
        # Warning still present in reasons
        assert len(reasons) > 0

    def test_low_risk_provisional_publishable_with_warning(self):
        spec = _spec(risk_class="low", required_sections=["S001"], sign_off_roles=[])
        sections = [_sr("S001", "PROVISIONAL_UNVERIFIED")]
        ok, reasons = evaluate_publishability(sections, spec)
        assert ok is True
        assert len(reasons) > 0  # Warning present

    def test_fail_trumps_provisional(self):
        spec = _spec(risk_class="low", required_sections=["S001", "S002"])
        sections = [
            _sr("S001", "FAIL"),
            _sr("S002", "PROVISIONAL_UNVERIFIED"),
        ]
        ok, reasons = evaluate_publishability(sections, spec)
        assert ok is False

    def test_reasons_list_explains_block(self):
        spec = _spec(risk_class="high", required_sections=["S001"])
        sections = [_sr("S001", "FAIL")]
        ok, reasons = evaluate_publishability(sections, spec)
        assert len(reasons) >= 1
        assert "S001" in reasons[0]


# ---------------------------------------------------------------------------
# Tests: compute_document_status
# ---------------------------------------------------------------------------


class TestComputeDocumentStatus:
    def test_all_pass_no_signoff_returns_pass(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [_section_obj("PASS")]
        result = compute_document_status(sections, [], spec)
        assert result == "PASS"

    def test_any_section_fail_returns_fail(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [_section_obj("FAIL"), _section_obj("PASS")]
        result = compute_document_status(sections, [], spec)
        assert result == "FAIL"

    def test_global_error_finding_returns_fail(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [_section_obj("PASS")]
        global_findings = [{"severity": "error", "detail": "Metric mismatch"}]
        result = compute_document_status(sections, global_findings, spec)
        assert result == "FAIL"

    def test_provisional_high_risk_returns_provisional(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [_section_obj("PROVISIONAL_UNVERIFIED")]
        result = compute_document_status(sections, [], spec)
        assert result == "PROVISIONAL_UNVERIFIED"

    def test_provisional_low_risk_returns_warn(self):
        spec = _spec(risk_class="low", sign_off_roles=[])
        sections = [_section_obj("PROVISIONAL_UNVERIFIED")]
        result = compute_document_status(sections, [], spec)
        assert result == "WARN"

    def test_all_pass_with_signoff_required_returns_blocked(self):
        spec = _spec(risk_class="high", sign_off_roles=["CFO"])
        sections = [_section_obj("PASS")]
        result = compute_document_status(sections, [], spec)
        assert result == "BLOCKED_FOR_SIGNOFF"

    def test_section_warn_returns_warn(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [_section_obj("WARN")]
        result = compute_document_status(sections, [], spec)
        assert result == "WARN"

    def test_section_fail_beats_provisional(self):
        spec = _spec(risk_class="medium", sign_off_roles=[])
        sections = [_section_obj("FAIL"), _section_obj("PROVISIONAL_UNVERIFIED")]
        result = compute_document_status(sections, [], spec)
        assert result == "FAIL"

    def test_accepts_dict_sections(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [{"status": "PASS"}, {"status": "PASS"}]
        result = compute_document_status(sections, [], spec)
        assert result == "PASS"

    def test_accepts_dict_global_findings(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        sections = [{"status": "PASS"}]
        global_findings = [{"severity": "warning", "detail": "Minor drift"}]
        result = compute_document_status(sections, global_findings, spec)
        assert result == "WARN"

    def test_empty_sections_no_findings_pass(self):
        spec = _spec(risk_class="high", sign_off_roles=[])
        result = compute_document_status([], [], spec)
        assert result == "PASS"

    def test_provisional_medium_risk_returns_provisional_not_warn(self):
        spec = _spec(risk_class="medium", sign_off_roles=[])
        sections = [_section_obj("PROVISIONAL_UNVERIFIED")]
        result = compute_document_status(sections, [], spec)
        assert result == "PROVISIONAL_UNVERIFIED"
