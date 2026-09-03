"""Tests for Document Mode CLI integration (T020).

All tests are argument-parsing and handler-routing focused.
No real LLM calls, no real filesystem side-effects beyond tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.cli import build_parser, main


# ---------------------------------------------------------------------------
# Parser construction tests
# ---------------------------------------------------------------------------


class TestParserDocumentFlags:
    """Verify that document-mode CLI flags are registered correctly."""

    def test_start_parser_has_document_flag(self):
        parser = build_parser()
        args = parser.parse_args(["start", "--document"])
        assert args.document is True

    def test_start_parser_document_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args(["start"])
        assert args.document is False

    def test_start_parser_has_doc_spec_flag(self):
        parser = build_parser()
        args = parser.parse_args(["start", "--document", "--doc-spec", "my-spec.yaml"])
        assert args.doc_spec == "my-spec.yaml"

    def test_plan_parser_has_doc_spec_flag(self):
        parser = build_parser()
        args = parser.parse_args([
            "plan",
            "--repo", ".",
            "--backend", "claude-cli",
            "--doc-spec", "doc-spec.yaml",
        ])
        assert args.doc_spec == "doc-spec.yaml"

    def test_approve_subcommand_registered(self):
        parser = build_parser()
        args = parser.parse_args([
            "approve",
            "--document", "DOC-001",
            "--role", "cfo",
            "--actor", "Jane Doe",
        ])
        assert args.command == "approve"
        assert args.document == "DOC-001"
        assert args.role == "cfo"
        assert args.actor == "Jane Doe"

    def test_approve_status_defaults_approved(self):
        parser = build_parser()
        args = parser.parse_args([
            "approve",
            "--document", "DOC-001",
            "--role", "cfo",
            "--actor", "Jane",
        ])
        assert args.status == "approved"

    def test_approve_status_can_be_rejected(self):
        parser = build_parser()
        args = parser.parse_args([
            "approve",
            "--document", "DOC-001",
            "--role", "cfo",
            "--actor", "Jane",
            "--status", "rejected",
        ])
        assert args.status == "rejected"

    def test_governance_has_document_flag(self):
        parser = build_parser()
        args = parser.parse_args(["governance", "--document", "--full"])
        assert args.document is True


# ---------------------------------------------------------------------------
# main() routing tests
# ---------------------------------------------------------------------------


class TestMainDocumentRouting:
    """Verify that main() routes document commands correctly."""

    def test_approve_command_dispatched(self, tmp_path):
        """saturnday approve should call record_approval and exit 0."""
        from saturnday import capability_registry
        with patch("saturnday.document.signoff.record_approval") as mock_approve:
            with patch.object(capability_registry, "is_available", return_value=True):
                from saturnday.document._types import DocumentApproval
                from datetime import datetime, timezone
                mock_approve.return_value = DocumentApproval(
                    document_id="DOC-001",
                    role="cfo",
                    actor="Jane",
                    status="approved",
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
                rc = main([
                    "approve",
                    "--document", "DOC-001",
                    "--role", "cfo",
                    "--actor", "Jane",
                    "--repo", str(tmp_path),
                ])
        assert rc == 0
        mock_approve.assert_called_once()

    def test_start_document_missing_doc_spec_returns_1(self, tmp_path):
        """saturnday start --document without --doc-spec should return 1."""
        rc = main(["start", "--document", "--repo", str(tmp_path)])
        assert rc == 1

    def test_start_document_nonexistent_spec_returns_1(self, tmp_path):
        """saturnday start --document with nonexistent spec should return 1."""
        rc = main([
            "start", "--document",
            "--doc-spec", str(tmp_path / "nonexistent.yaml"),
            "--repo", str(tmp_path),
        ])
        assert rc == 1

    def test_plan_doc_spec_nonexistent_returns_1(self, tmp_path):
        """saturnday plan --doc-spec with nonexistent file should return 1."""
        rc = main([
            "plan",
            "--repo", str(tmp_path),
            "--backend", "claude-cli",
            "--doc-spec", str(tmp_path / "nonexistent.yaml"),
        ])
        assert rc == 1

    def test_plan_without_brief_or_doc_spec_returns_1(self, tmp_path):
        """saturnday plan without --brief or --doc-spec should return 1."""
        rc = main([
            "plan",
            "--repo", str(tmp_path),
            "--backend", "claude-cli",
        ])
        assert rc == 1

    def test_start_document_with_valid_spec_succeeds(self, tmp_path):
        """saturnday start --document with valid spec should proceed."""
        # Create a minimal source file so the validator doesn't reject it
        source_file = tmp_path / "data.md"
        source_file.write_text("# Data\n\nSome content.\n")
        spec_content = f"""
type: report
purpose: Quarterly results
audience: board
risk_class: low
required_sections:
  - Executive Summary
claim_policy:
  quantitative_claims_require_source: true
approved_sources:
  - {source_file}
sign_off_roles: []
"""
        spec_file = tmp_path / "doc-spec.yaml"
        spec_file.write_text(spec_content)

        with patch("saturnday.document.planner.generate_document_plan") as mock_plan:
            from saturnday.document._types import DocumentPlan, DocumentSection
            mock_plan.return_value = DocumentPlan(
                document_id="DOC-TEST-001",
                type="report",
                purpose="Quarterly results",
                risk_class="low",
                sections=[
                    DocumentSection("S001", "Executive Summary", "Overview"),
                ],
                global_checks=["cross_section_consistency"],
            )
            # Non-interactive: stdin is not a tty in test runner
            rc = main([
                "start", "--document",
                "--doc-spec", str(spec_file),
                "--repo", str(tmp_path),
            ])
        assert rc == 0
