"""Tests for saturnday.document.evidence_pack (T019).

Covers: write_document_evidence, directory structure, file contents,
artefact hash, assertions on run-summary.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saturnday.document._types import (
    DocumentApproval,
    DocumentClaim,
    DocumentFinding,
    DocumentRunResult,
    DocumentSection,
    DocumentSpec,
)
from saturnday.document.evidence_pack import write_document_evidence
from saturnday.shared.evidence_schema import EVIDENCE_DIR_DOCUMENT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec() -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Quarterly results",
        audience="board",
        risk_class="high",
        required_sections=["Summary", "Financials"],
        claim_policy={"quantitative_claims_require_source": True},
        approved_sources=[],
        sign_off_roles=["cfo", "legal"],
    )


def _make_result(doc_id: str = "DOC-001") -> DocumentRunResult:
    sections = [
        DocumentSection("S001", "Summary", "Overview section", status="PASS"),
        DocumentSection("S002", "Financials", "Financial details", status="PASS"),
    ]
    return DocumentRunResult(
        document_id=doc_id,
        type="report",
        total_sections=2,
        passed=2,
        failed=0,
        provisional=0,
        document_status="PASS",
        sections=sections,
        claims=[
            DocumentClaim("C001", "S001", "Revenue was $1M", "quantitative"),
        ],
        approvals=[
            DocumentApproval(doc_id, "cfo", "Jane Doe", "approved"),
        ],
        global_findings=[
            DocumentFinding(
                "GF-001", "", "cross_section_consistency", "warning", "Minor duplication"
            )
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWriteDocumentEvidence:
    def test_returns_output_dir_path(self, tmp_path):
        result = _make_result()
        spec = _make_spec()
        returned = write_document_evidence(result, spec, tmp_path)
        assert returned == tmp_path

    def test_creates_run_summary_json(self, tmp_path):
        result = _make_result()
        spec = _make_spec()
        write_document_evidence(result, spec, tmp_path)
        summary_path = tmp_path / "run-summary.json"
        assert summary_path.is_file()
        data = json.loads(summary_path.read_text())
        assert data["document_id"] == "DOC-001"
        assert data["document_status"] == "PASS"
        assert data["total_sections"] == 2
        assert data["passed"] == 2

    def test_creates_final_document_md(self, tmp_path):
        result = _make_result()
        spec = _make_spec()
        # Pre-populate section drafts so final-document has real content
        for section in result.sections:
            section_dir = tmp_path / "sections" / section.section_id
            section_dir.mkdir(parents=True)
            (section_dir / "section.md").write_text(
                f"## {section.name}\n\nContent for {section.name}.\n"
            )
        write_document_evidence(result, spec, tmp_path)
        final = tmp_path / "final-document.md"
        assert final.is_file()
        content = final.read_text()
        assert "Summary" in content
        assert "Financials" in content

    def test_creates_artefact_hash(self, tmp_path):
        result = _make_result()
        spec = _make_spec()
        write_document_evidence(result, spec, tmp_path)
        hash_file = tmp_path / "ARTEFACT_HASH.txt"
        assert hash_file.is_file()
        content = hash_file.read_text()
        assert content.startswith("sha256:")

    def test_creates_findings_files(self, tmp_path):
        result = _make_result()
        # Add a local finding to one section
        result.sections[0].findings = [
            {"finding_id": "LF-001", "kind": "placeholder", "severity": "error", "detail": "TODO found"}
        ]
        spec = _make_spec()
        write_document_evidence(result, spec, tmp_path)
        assert (tmp_path / "findings" / "local.json").is_file()
        assert (tmp_path / "findings" / "global.json").is_file()
        local = json.loads((tmp_path / "findings" / "local.json").read_text())
        assert len(local) == 1
        assert local[0]["finding_id"] == "LF-001"

    def test_creates_claims_json_when_claims_present(self, tmp_path):
        result = _make_result()
        spec = _make_spec()
        write_document_evidence(result, spec, tmp_path)
        claims_file = tmp_path / "claims" / "claims.json"
        assert claims_file.is_file()
        data = json.loads(claims_file.read_text())
        assert len(data) == 1
        assert data[0]["claim_id"] == "C001"

    def test_creates_approvals_json(self, tmp_path):
        result = _make_result()
        spec = _make_spec()
        write_document_evidence(result, spec, tmp_path)
        approvals_file = tmp_path / "approvals.json"
        assert approvals_file.is_file()
        data = json.loads(approvals_file.read_text())
        assert len(data) == 1
        assert data[0]["role"] == "cfo"

    def test_spec_source_copied_when_provided(self, tmp_path):
        spec_file = tmp_path / "doc-spec.yaml"
        spec_file.write_text("type: report\n")
        evidence_dir = tmp_path / "evidence"
        result = _make_result()
        spec = _make_spec()
        write_document_evidence(result, spec, evidence_dir, spec_source_path=spec_file)
        assert (evidence_dir / "doc-spec.yaml").is_file()

    def test_idempotent_on_rerun(self, tmp_path):
        """Calling write_document_evidence twice does not raise."""
        result = _make_result()
        spec = _make_spec()
        write_document_evidence(result, spec, tmp_path)
        write_document_evidence(result, spec, tmp_path)
        assert (tmp_path / "run-summary.json").is_file()

    def test_evidence_dir_document_constant_exists(self):
        assert EVIDENCE_DIR_DOCUMENT == "evidence/document"
