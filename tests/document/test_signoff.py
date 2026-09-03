"""Tests for saturnday.document.signoff (T018).

Covers: record_approval, check_signoff_requirements, persistence, edge cases.
"""

from __future__ import annotations

import json

import pytest

from saturnday.document._types import DocumentApproval, DocumentSpec
from saturnday.document.signoff import (
    check_signoff_requirements,
    load_approvals_for_doc,
    record_approval,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    risk_class: str = "high",
    sign_off_roles: list[str] | None = None,
) -> DocumentSpec:
    roles = ["cfo", "legal"] if sign_off_roles is None else sign_off_roles
    return DocumentSpec(
        type="report",
        purpose="Test document",
        audience="internal",
        risk_class=risk_class,
        required_sections=["Executive Summary"],
        claim_policy={"quantitative_claims_require_source": True},
        approved_sources=[],
        sign_off_roles=roles,
    )


# ---------------------------------------------------------------------------
# record_approval tests
# ---------------------------------------------------------------------------


class TestRecordApproval:
    def test_returns_document_approval_instance(self):
        approval = record_approval("doc-001", "cfo", "Jane Doe", "approved")
        assert isinstance(approval, DocumentApproval)

    def test_fields_set_correctly(self):
        approval = record_approval(
            "doc-002", "legal", "Bob Smith", "rejected", notes="needs revision"
        )
        assert approval.document_id == "doc-002"
        assert approval.role == "legal"
        assert approval.actor == "Bob Smith"
        assert approval.status == "rejected"
        assert approval.notes == "needs revision"

    def test_timestamp_is_set(self):
        approval = record_approval("doc-003", "cfo", "Alice", "approved")
        assert approval.timestamp  # non-empty
        assert "T" in approval.timestamp  # ISO 8601 format

    def test_overridden_findings_stored(self):
        approval = record_approval(
            "doc-004", "cfo", "Jane", "approved",
            overridden_findings=["DOC-STRUCT-1", "DOC-NUM-2"],
        )
        assert "DOC-STRUCT-1" in approval.overridden_findings
        assert "DOC-NUM-2" in approval.overridden_findings

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid approval status"):
            record_approval("doc-005", "cfo", "Jane", "invalid_status")

    def test_persists_to_disk(self, tmp_path):
        approval = record_approval(
            "doc-006", "cfo", "Jane", "approved", repo_path=tmp_path
        )
        approvals_file = tmp_path / ".saturnday" / "document" / "approvals.json"
        assert approvals_file.is_file()
        data = json.loads(approvals_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["role"] == "cfo"
        assert data[0]["actor"] == "Jane"

    def test_re_approval_overwrites_previous(self, tmp_path):
        """Re-approving the same role replaces the previous record."""
        record_approval("doc-007", "cfo", "Jane", "approved", repo_path=tmp_path)
        record_approval("doc-007", "cfo", "Jane", "rejected", notes="changed mind", repo_path=tmp_path)
        loaded = load_approvals_for_doc("doc-007", tmp_path)
        assert len(loaded) == 1
        assert loaded[0].status == "rejected"

    def test_no_repo_path_does_not_write(self, tmp_path):
        """When repo_path is None the approval is returned but not persisted."""
        approval = record_approval("doc-008", "cfo", "Jane", "approved", repo_path=None)
        assert approval.document_id == "doc-008"
        approvals_file = tmp_path / ".saturnday" / "document" / "approvals.json"
        assert not approvals_file.exists()


# ---------------------------------------------------------------------------
# check_signoff_requirements tests
# ---------------------------------------------------------------------------


class TestCheckSignoffRequirements:
    def test_all_satisfied_when_all_approved(self):
        spec = _make_spec(sign_off_roles=["cfo", "legal"])
        approvals = [
            DocumentApproval("doc-010", "cfo", "Jane", "approved"),
            DocumentApproval("doc-010", "legal", "Bob", "approved"),
        ]
        satisfied, missing = check_signoff_requirements("doc-010", spec, approvals)
        assert satisfied is True
        assert missing == []

    def test_missing_role_returned(self):
        spec = _make_spec(sign_off_roles=["cfo", "legal"])
        approvals = [
            DocumentApproval("doc-011", "cfo", "Jane", "approved"),
        ]
        satisfied, missing = check_signoff_requirements("doc-011", spec, approvals)
        assert satisfied is False
        assert "legal" in missing

    def test_rejection_blocks_approval(self):
        spec = _make_spec(sign_off_roles=["cfo", "legal"])
        approvals = [
            DocumentApproval("doc-012", "cfo", "Jane", "approved"),
            DocumentApproval("doc-012", "legal", "Bob", "rejected"),
        ]
        satisfied, missing = check_signoff_requirements("doc-012", spec, approvals)
        assert satisfied is False

    def test_empty_approvals_all_missing(self):
        spec = _make_spec(sign_off_roles=["cfo", "legal"])
        satisfied, missing = check_signoff_requirements("doc-013", spec, [])
        assert satisfied is False
        assert "cfo" in missing
        assert "legal" in missing

    def test_no_required_roles_always_satisfied(self):
        spec = _make_spec(risk_class="low", sign_off_roles=[])
        satisfied, missing = check_signoff_requirements("doc-014", spec, [])
        assert satisfied is True
        assert missing == []

    def test_approvals_for_different_doc_ignored(self):
        spec = _make_spec(sign_off_roles=["cfo"])
        approvals = [
            DocumentApproval("OTHER-DOC", "cfo", "Jane", "approved"),
        ]
        satisfied, missing = check_signoff_requirements("doc-015", spec, approvals)
        assert satisfied is False
        assert "cfo" in missing

    def test_pending_status_not_counted_as_approval(self):
        spec = _make_spec(sign_off_roles=["cfo"])
        approvals = [
            DocumentApproval("doc-016", "cfo", "Jane", "pending"),
        ]
        satisfied, missing = check_signoff_requirements("doc-016", spec, approvals)
        assert satisfied is False
        assert "cfo" in missing
