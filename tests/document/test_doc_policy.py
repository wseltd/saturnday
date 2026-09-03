"""Tests for Document Mode policy integration (T021).

Covers: DOC_HARD_CHECKS, DOC_SOFT_CHECKS constants; load_policy parsing of
document_claim_policy, document_sign_off_roles, document_expected_findings;
PolicyManifest.document_* fields; shared evidence schema constant.
"""

from __future__ import annotations

import pytest

from saturnday.policy_manifest import (
    DOC_HARD_CHECKS,
    DOC_SOFT_CHECKS,
    PolicyManifest,
    load_policy,
)
from saturnday.shared.evidence_schema import EVIDENCE_DIR_DOCUMENT


# ---------------------------------------------------------------------------
# Constant membership tests
# ---------------------------------------------------------------------------


class TestDocCheckConstants:
    def test_doc_hard_checks_contains_required_ids(self):
        assert "doc_structure" in DOC_HARD_CHECKS
        assert "doc_citation_existence" in DOC_HARD_CHECKS
        assert "doc_numeric_integrity" in DOC_HARD_CHECKS
        assert "doc_evidence_coverage" in DOC_HARD_CHECKS

    def test_doc_soft_checks_contains_required_ids(self):
        assert "doc_terminology" in DOC_SOFT_CHECKS
        assert "doc_placeholder" in DOC_SOFT_CHECKS

    def test_doc_hard_checks_are_frozenset(self):
        assert isinstance(DOC_HARD_CHECKS, frozenset)

    def test_doc_soft_checks_are_frozenset(self):
        assert isinstance(DOC_SOFT_CHECKS, frozenset)

    def test_no_overlap_between_hard_and_soft(self):
        assert DOC_HARD_CHECKS & DOC_SOFT_CHECKS == frozenset()


# ---------------------------------------------------------------------------
# PolicyManifest default field tests
# ---------------------------------------------------------------------------


class TestPolicyManifestDocumentFields:
    def test_default_document_fields_are_empty(self):
        manifest = PolicyManifest()
        assert manifest.document_claim_policy is None
        assert manifest.document_sign_off_roles == []
        assert manifest.document_expected_findings == []


# ---------------------------------------------------------------------------
# load_policy parsing of document keys
# ---------------------------------------------------------------------------


def _write_policy(tmp_path, content: str):
    p = tmp_path / ".saturnday-policy.yaml"
    p.write_text(content)
    return p


class TestLoadPolicyDocumentKeys:
    def test_document_sign_off_roles_parsed(self, tmp_path):
        p = _write_policy(tmp_path, "document_sign_off_roles:\n  - cfo\n  - legal\n")
        manifest = load_policy(p)
        assert manifest.document_sign_off_roles == ["cfo", "legal"]

    def test_document_expected_findings_parsed(self, tmp_path):
        p = _write_policy(
            tmp_path,
            "document_expected_findings:\n  - doc_terminology\n  - doc_placeholder\n",
        )
        manifest = load_policy(p)
        assert "doc_terminology" in manifest.document_expected_findings
        assert "doc_placeholder" in manifest.document_expected_findings

    def test_document_claim_policy_parsed(self, tmp_path):
        policy_yaml = (
            "document_claim_policy:\n"
            "  required_claim_types:\n"
            "    - quantitative\n"
            "    - regulatory\n"
            "  unsupported_verdict_action: warn\n"
        )
        p = _write_policy(tmp_path, policy_yaml)
        manifest = load_policy(p)
        assert manifest.document_claim_policy is not None
        assert "quantitative" in manifest.document_claim_policy.required_claim_types
        assert "regulatory" in manifest.document_claim_policy.required_claim_types
        assert manifest.document_claim_policy.unsupported_verdict_action == "warn"

    def test_missing_document_keys_leave_defaults(self, tmp_path):
        p = _write_policy(tmp_path, "strict_mode: false\n")
        manifest = load_policy(p)
        assert manifest.document_claim_policy is None
        assert manifest.document_sign_off_roles == []
        assert manifest.document_expected_findings == []

    def test_document_keys_do_not_affect_code_mode_checks(self, tmp_path):
        """Existing code-mode checks are unaffected by document keys."""
        policy_yaml = (
            "document_sign_off_roles:\n  - cfo\n"
            "document_expected_findings:\n  - doc_structure\n"
        )
        p = _write_policy(tmp_path, policy_yaml)
        manifest = load_policy(p)
        # Hard code checks still present
        assert "syntax" in manifest.checks
        assert "secrets" in manifest.checks

    def test_evidence_dir_document_constant(self):
        """EVIDENCE_DIR_DOCUMENT is exported from shared.evidence_schema."""
        assert EVIDENCE_DIR_DOCUMENT == "evidence/document"
