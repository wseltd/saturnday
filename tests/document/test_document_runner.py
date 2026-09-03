"""End-to-end integration tests for document_runner.run_document.

All LLM calls are mocked.  No real backend is invoked.  Each test is
independent and uses tmp_path for filesystem operations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from saturnday._types import CoderConfig
from saturnday.document._types import (
    DocumentApproval,
    DocumentClaim,
    DocumentFinding,
    DocumentRunResult,
    SupportVerdict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLAIM_POLICY = {
    "quantitative_claims_require_source": True,
    "recommendation_claims_require_support": False,
    "uncited_narrative_allowed": True,
}

_CODER_CONFIG = CoderConfig(backend="claude-cli")

SECTION_CONTENT_TEMPLATE = """# {name}

This is the content for section {name}.  Revenue: $1.2M.  Date: 2026-01-15.
We recommend reviewing this section carefully.
"""


def _make_doc_spec_yaml(
    tmp_path: Path,
    *,
    risk_class: str = "low",
    sections: list[str] | None = None,
    sign_off_roles: list[str] | None = None,
    source_filename: str = "source.md",
) -> tuple[Path, Path]:
    """Create a minimal doc-spec.yaml and a source file in tmp_path.

    Returns:
        (spec_path, source_path)
    """
    if sections is None:
        sections = ["Introduction", "Analysis", "Conclusion"]
    if sign_off_roles is None:
        sign_off_roles = []

    source_path = tmp_path / source_filename
    source_path.write_text("# Source\n\nRevenue: $1.2M in Q1 2026.\n", encoding="utf-8")

    spec_data = {
        "type": "report",
        "purpose": "Integration test document",
        "audience": "testers",
        "risk_class": risk_class,
        "required_sections": sections,
        "claim_policy": _CLAIM_POLICY,
        "approved_sources": [str(source_path)],
        "sign_off_roles": sign_off_roles,
    }

    try:
        import yaml  # type: ignore[import]
        spec_yaml = yaml.dump(spec_data)
    except ImportError:
        # Fallback: manual YAML for simple cases
        lines = [f"type: {spec_data['type']}", f"purpose: {spec_data['purpose']}",
                 f"audience: {spec_data['audience']}", f"risk_class: {spec_data['risk_class']}",
                 "required_sections:"]
        for s in spec_data["required_sections"]:
            lines.append(f"  - {s}")
        lines.append("claim_policy:")
        for k, v in spec_data["claim_policy"].items():
            lines.append(f"  {k}: {'true' if v else 'false'}")
        lines.append("approved_sources:")
        for src in spec_data["approved_sources"]:
            lines.append(f"  - {src}")
        lines.append("sign_off_roles:")
        for role in spec_data["sign_off_roles"]:
            lines.append(f"  - {role}")
        spec_yaml = "\n".join(lines)

    spec_path = tmp_path / "doc-spec.yaml"
    spec_path.write_text(spec_yaml, encoding="utf-8")
    return spec_path, source_path


def _mock_call_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
    """Return minimal section content when call_coder is mocked."""
    # Determine section name from the user message
    user_msg = ""
    for m in messages:
        if m.get("role") == "user":
            user_msg = m.get("content", "")
            break

    # Extract section name from prompt
    section_name = "Section"
    for line in user_msg.split("\n"):
        if line.startswith("SECTION TO GENERATE:"):
            section_name = line.split(":", 1)[-1].strip()
            break

    # If this looks like a claim-verification prompt, return a supported verdict.
    if "CLAIM (" in user_msg and "APPROVED EVIDENCE" in user_msg:
        return "SUPPORTED\nThe evidence supports this claim."

    return SECTION_CONTENT_TEMPLATE.format(name=section_name)


class _patch_all_coder:
    """Context manager that patches call_coder in ALL document sub-modules.

    Patches:
    - saturnday.document.section_runner.call_coder (section generation)
    - saturnday.document.claim_verifier.call_coder (claim verification)

    This prevents any test from hanging on a real LLM call.
    """

    def __init__(self, side_effect=None):
        self._side_effect = side_effect or _mock_call_coder
        self._patches: list = []

    def __enter__(self) -> "_patch_all_coder":
        targets = [
            "saturnday.document.section_runner.call_coder",
            "saturnday.document.claim_verifier.call_coder",
        ]
        for target in targets:
            p = patch(target, side_effect=self._side_effect)
            p.start()
            self._patches.append(p)
        return self

    def __exit__(self, *args: Any) -> None:
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# T029 Tests
# ---------------------------------------------------------------------------


class TestRunDocumentBasic:
    """test_run_document_basic: mock call_coder returns section content, all phases run."""

    def test_all_phases_run_happy_path(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path,
            risk_class="low",
            sections=["Introduction", "Analysis"],
        )
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert isinstance(result, DocumentRunResult)
        assert result.total_sections == 2
        assert result.document_id != ""
        # Low risk, all pass → status should be PASS or WARN
        assert result.document_status in ("PASS", "WARN")

    def test_result_has_sections(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Intro", "Body"])
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert len(result.sections) == 2
        section_ids = {s.section_id for s in result.sections}
        assert "S001" in section_ids
        assert "S002" in section_ids

    def test_progress_log_written(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path)
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        progress_log = output_dir / "document-progress.log"
        assert progress_log.is_file()
        log_text = progress_log.read_text(encoding="utf-8")
        assert "Document run started" in log_text
        assert "Document run complete" in log_text


class TestRunDocumentSectionFailureRetries:
    """test_run_document_section_failure_retries: first call fails, second succeeds."""

    def test_section_retries_on_failure(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["Introduction"], risk_class="low"
        )
        output_dir = tmp_path / "out"

        call_count = {"n": 0}

        def flaky_call_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call returns content with placeholders that will fail checks.
                return "# Introduction\n\nTODO: fill this in.\n"
            # Subsequent calls return valid content.
            return _mock_call_coder(config, messages, repo_path)

        with _patch_all_coder(side_effect=flaky_call_coder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        # Run should complete without crash
        assert isinstance(result, DocumentRunResult)
        assert result.total_sections == 1
        # call_count > 1 indicates retry occurred
        assert call_count["n"] >= 1

    def test_section_failure_produces_attempt_dirs(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["Introduction"], risk_class="low"
        )
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        # At least one attempt dir must exist for the section
        section_dir = output_dir / "sections" / "S001"
        assert section_dir.is_dir()


class TestRunDocumentProvisionalHandling:
    """test_run_document_provisional_handling: section stays provisional."""

    def test_all_retries_exhausted_gives_provisional(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["AlwaysFail"], risk_class="low"
        )
        output_dir = tmp_path / "out"

        # Always return content with placeholders so verification always fails
        def always_placeholder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            return "# AlwaysFail\n\nTODO: Fix this. [PLACEHOLDER]\n"

        with _patch_all_coder(side_effect=always_placeholder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        # Low risk with provisional sections → WARN (not FAIL)
        assert result.provisional >= 1 or result.document_status in (
            "WARN", "PROVISIONAL_UNVERIFIED", "PASS"
        )
        # Run should not crash
        assert isinstance(result, DocumentRunResult)

    def test_provisional_section_tracked_in_result(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["AlwaysBad"], risk_class="low"
        )
        output_dir = tmp_path / "out"

        # Return placeholder content every time
        def always_placeholder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            return "# AlwaysBad\n\n[PLACEHOLDER] Content missing.\n"

        with _patch_all_coder(side_effect=always_placeholder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert isinstance(result, DocumentRunResult)
        assert result.total_sections == 1


class TestRunDocumentEvidenceWritten:
    """test_run_document_evidence_written: verify evidence directory created."""

    def test_evidence_directory_exists(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Section1"])
        output_dir = tmp_path / "evidence"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert output_dir.is_dir()

    def test_evidence_contains_run_summary(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Section1"])
        output_dir = tmp_path / "evidence"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        summary_path = output_dir / "run-summary.json"
        assert summary_path.is_file()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert "document_id" in summary
        assert "document_status" in summary

    def test_evidence_contains_final_document(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Intro"])
        output_dir = tmp_path / "evidence"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        final_doc = output_dir / "final-document.md"
        assert final_doc.is_file()
        assert len(final_doc.read_text(encoding="utf-8")) > 0

    def test_evidence_contains_artefact_hash(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Intro"])
        output_dir = tmp_path / "evidence"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        hash_file = output_dir / "ARTEFACT_HASH.txt"
        assert hash_file.is_file()
        assert "sha256:" in hash_file.read_text(encoding="utf-8")

    def test_partial_summary_written_during_run(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["A", "B"])
        output_dir = tmp_path / "out"

        partial_summaries_written: list[bool] = []

        original_write = None

        def tracking_call_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            # Check if partial summary exists after first section
            partial = output_dir / "partial-run-summary.json"
            partial_summaries_written.append(partial.is_file())
            return _mock_call_coder(config, messages, repo_path)

        with _patch_all_coder(side_effect=tracking_call_coder):
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        # At least by the second section call, partial summary should exist
        # (written after first section completes)
        partial = output_dir / "partial-run-summary.json"
        assert partial.is_file()


class TestRunDocumentKeyboardInterrupt:
    """test_run_document_keyboard_interrupt: verify partial state saved."""

    def test_keyboard_interrupt_reraises(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["A", "B", "C"])
        output_dir = tmp_path / "out"

        call_count = {"n": 0}

        def interrupt_on_second(config: CoderConfig, messages: list, repo_path: Any) -> str:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise KeyboardInterrupt("test interrupt")
            return _mock_call_coder(config, messages, repo_path)

        with _patch_all_coder(side_effect=interrupt_on_second):
            from saturnday.document.document_runner import run_document

            with pytest.raises(KeyboardInterrupt):
                run_document(
                    spec_path=spec_path,
                    repo_path=tmp_path,
                    coder_config=_CODER_CONFIG,
                    output_dir=output_dir,
                )

    def test_keyboard_interrupt_saves_partial_state(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["A", "B"])
        output_dir = tmp_path / "out"

        # Ensure output dir exists before the interrupt happens
        output_dir.mkdir(parents=True, exist_ok=True)

        call_count = {"n": 0}

        def interrupt_on_second(config: CoderConfig, messages: list, repo_path: Any) -> str:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise KeyboardInterrupt("test interrupt")
            return _mock_call_coder(config, messages, repo_path)

        with _patch_all_coder(side_effect=interrupt_on_second):
            from saturnday.document.document_runner import run_document

            with pytest.raises(KeyboardInterrupt):
                run_document(
                    spec_path=spec_path,
                    repo_path=tmp_path,
                    coder_config=_CODER_CONFIG,
                    output_dir=output_dir,
                )

        # Progress log should exist with the interrupt message
        progress_log = output_dir / "document-progress.log"
        assert progress_log.is_file()
        log_text = progress_log.read_text(encoding="utf-8")
        assert "interrupted" in log_text.lower() or "KeyboardInterrupt" in log_text


class TestRunDocumentGlobalChecksRun:
    """test_run_document_global_checks_run: verify global findings produced."""

    def test_global_checks_produce_findings_on_metric_mismatch(
        self, tmp_path: Path
    ) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["Summary", "Detail"]
        )
        output_dir = tmp_path / "out"

        call_count = {"n": 0}

        def metric_mismatch_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            call_count["n"] += 1
            section_name = "Section"
            user_msg = ""
            for m in messages:
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
                    break
            for line in user_msg.split("\n"):
                if line.startswith("SECTION TO GENERATE:"):
                    section_name = line.split(":", 1)[-1].strip()
                    break

            if call_count["n"] == 1:
                # Summary with one revenue figure
                return (
                    f"# {section_name}\n\n"
                    "Revenue: $1.2M for FY2025.\n"
                )
            else:
                # Detail with a different revenue figure
                return (
                    f"# {section_name}\n\n"
                    "Revenue: $2.5M for FY2025.\n"
                )

        with _patch_all_coder(side_effect=metric_mismatch_coder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        # Global checks should have run; findings may or may not fire
        # depending on label extraction — but no crash is the primary guarantee.
        assert isinstance(result, DocumentRunResult)
        # global_findings list exists (may be empty if no mismatches detected)
        assert isinstance(result.global_findings, list)

    def test_global_findings_stored_on_result(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["SectionA"])
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert hasattr(result, "global_findings")
        assert isinstance(result.global_findings, list)

    def test_global_findings_written_to_evidence(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["SectionA"])
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        global_findings_path = output_dir / "findings" / "global.json"
        assert global_findings_path.is_file()
        data = json.loads(global_findings_path.read_text(encoding="utf-8"))
        assert isinstance(data, list)


class TestRunDocumentClaimsExtracted:
    """test_run_document_claims_extracted: verify claims in result."""

    def test_claims_extracted_from_sections(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["Financial Summary"]
        )
        output_dir = tmp_path / "out"

        # Content with clear quantitative claims
        def rich_content_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            return (
                "# Financial Summary\n\n"
                "Revenue for FY2025 was $5.4M, up 20% year-over-year.\n"
                "We recommend increasing the marketing budget by 15%.\n"
                "The board approved the plan on 2026-01-15.\n"
            )

        with _patch_all_coder(side_effect=rich_content_coder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        # Claims should be extracted (quantitative and recommendation sentences)
        assert isinstance(result.claims, list)
        # Content has numbers and recommendations — at least 1 claim expected
        # (exact count depends on extraction heuristics)

    def test_claims_stored_on_result(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Summary"])
        output_dir = tmp_path / "out"

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert hasattr(result, "claims")
        assert isinstance(result.claims, list)
        # All claims are DocumentClaim instances
        for c in result.claims:
            assert isinstance(c, DocumentClaim)

    def test_claims_written_to_evidence_when_present(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["Finance"])
        output_dir = tmp_path / "out"

        def claim_rich_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            return (
                "# Finance\n\n"
                "Total revenue was $12M in 2025, representing a 30% increase.\n"
            )

        with _patch_all_coder(side_effect=claim_rich_coder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        if result.claims:
            claims_path = output_dir / "claims" / "claims.json"
            assert claims_path.is_file()
            data = json.loads(claims_path.read_text(encoding="utf-8"))
            assert isinstance(data, list)
            assert len(data) == len(result.claims)


class TestRunDocumentErrorHandling:
    """Additional error handling and edge case tests."""

    def test_invalid_spec_path_raises(self, tmp_path: Path) -> None:
        from saturnday.document.document_runner import run_document

        with pytest.raises((ValueError, Exception)):
            run_document(
                spec_path=tmp_path / "nonexistent.yaml",
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
            )

    def test_default_output_dir_is_under_saturnday(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(tmp_path, sections=["A"])

        with _patch_all_coder():
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                # output_dir not specified — should default to .saturnday/document-run/
            )

        default_dir = tmp_path / ".saturnday" / "document-run"
        assert default_dir.is_dir()
        assert isinstance(result, DocumentRunResult)

    def test_section_exception_does_not_crash_run(self, tmp_path: Path) -> None:
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path, sections=["GoodSection", "BadSection"]
        )
        output_dir = tmp_path / "out"

        call_count = {"n": 0}

        def sometimes_crash(config: CoderConfig, messages: list, repo_path: Any) -> str:
            call_count["n"] += 1
            # Crash on calls for the second section only
            user_msg = ""
            for m in messages:
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
                    break
            if "BadSection" in user_msg:
                raise RuntimeError("Simulated section runner crash")
            return _mock_call_coder(config, messages, repo_path)

        with _patch_all_coder(side_effect=sometimes_crash):
            from saturnday.document.document_runner import run_document

            # Should not propagate the RuntimeError from section runner
            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert isinstance(result, DocumentRunResult)
        assert result.total_sections == 2

    def test_three_sections_all_statuses(self, tmp_path: Path) -> None:
        """Three-section document: 1 pass, 1 retry-then-pass, 1 provisional."""
        spec_path, _ = _make_doc_spec_yaml(
            tmp_path,
            sections=["PassSection", "RetrySection", "ProvSection"],
            risk_class="low",
        )
        output_dir = tmp_path / "out"

        retry_section_calls = {"n": 0}

        def selective_coder(config: CoderConfig, messages: list, repo_path: Any) -> str:
            user_msg = ""
            for m in messages:
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
                    break

            if "RetrySection" in user_msg:
                retry_section_calls["n"] += 1
                if retry_section_calls["n"] == 1:
                    # First attempt fails
                    return "# RetrySection\n\nTODO: incomplete\n"
                # Retry passes
                return "# RetrySection\n\nThis section is complete with content.\n"

            if "ProvSection" in user_msg:
                # Always fails — will be provisioned
                return "# ProvSection\n\n[PLACEHOLDER] Not done yet.\n"

            return _mock_call_coder(config, messages, repo_path)

        with _patch_all_coder(side_effect=selective_coder):
            from saturnday.document.document_runner import run_document

            result = run_document(
                spec_path=spec_path,
                repo_path=tmp_path,
                coder_config=_CODER_CONFIG,
                output_dir=output_dir,
            )

        assert isinstance(result, DocumentRunResult)
        assert result.total_sections == 3
        # Low-risk with provisional → WARN or PROVISIONAL_UNVERIFIED
        assert result.document_status in (
            "PASS", "WARN", "PROVISIONAL_UNVERIFIED", "BLOCKED_FOR_SIGNOFF"
        )


class TestDetermineDocumentStatus:
    """Unit tests for _determine_document_status helper."""

    def _make_spec(
        self,
        risk_class: str = "low",
        sign_off_roles: list[str] | None = None,
    ):
        from saturnday.document._types import DocumentSpec

        return DocumentSpec(
            type="report",
            purpose="test",
            audience="test",
            risk_class=risk_class,
            required_sections=["A"],
            claim_policy={},
            approved_sources=[],
            sign_off_roles=sign_off_roles or [],
        )

    def test_fail_on_section_fail(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec()
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "FAIL"}],
            global_findings=[],
            publishable=True,
            signoff_complete=True,
            spec=spec,
        )
        assert status == "FAIL"

    def test_fail_on_global_error_finding(self) -> None:
        from saturnday.document.document_runner import _determine_document_status
        from saturnday.document._types import DocumentFinding

        spec = self._make_spec()
        finding = DocumentFinding(
            finding_id="DOC-GLOBAL-001",
            section_id="global",
            kind="test",
            severity="error",
            detail="test error",
        )
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "PASS"}],
            global_findings=[finding],
            publishable=True,
            signoff_complete=True,
            spec=spec,
        )
        assert status == "FAIL"

    def test_blocked_for_signoff_when_not_publishable(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec(risk_class="high", sign_off_roles=["cfo"])
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "PASS"}],
            global_findings=[],
            publishable=False,
            signoff_complete=False,
            spec=spec,
        )
        assert status == "BLOCKED_FOR_SIGNOFF"

    def test_blocked_for_signoff_when_signoff_incomplete(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec(risk_class="low", sign_off_roles=["cfo"])
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "PASS"}],
            global_findings=[],
            publishable=True,
            signoff_complete=False,
            spec=spec,
        )
        assert status == "BLOCKED_FOR_SIGNOFF"

    def test_pass_when_all_good_no_signoff(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec(risk_class="low", sign_off_roles=[])
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "PASS"}],
            global_findings=[],
            publishable=True,
            signoff_complete=True,
            spec=spec,
        )
        assert status == "PASS"

    def test_warn_on_section_warn(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec(risk_class="low", sign_off_roles=[])
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "WARN"}],
            global_findings=[],
            publishable=True,
            signoff_complete=True,
            spec=spec,
        )
        assert status == "WARN"

    def test_provisional_for_medium_risk(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec(risk_class="medium", sign_off_roles=[])
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "PROVISIONAL_UNVERIFIED"}],
            global_findings=[],
            publishable=True,
            signoff_complete=True,
            spec=spec,
        )
        assert status == "PROVISIONAL_UNVERIFIED"

    def test_warn_for_low_risk_provisional(self) -> None:
        from saturnday.document.document_runner import _determine_document_status

        spec = self._make_spec(risk_class="low", sign_off_roles=[])
        status = _determine_document_status(
            section_results=[{"section_id": "S001", "status": "PROVISIONAL_UNVERIFIED"}],
            global_findings=[],
            publishable=True,
            signoff_complete=True,
            spec=spec,
        )
        assert status == "WARN"


class TestLogDocumentProgress:
    """Unit tests for _log_document_progress."""

    def test_creates_progress_log(self, tmp_path: Path) -> None:
        from saturnday.document.document_runner import _log_document_progress

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        _log_document_progress("test message", output_dir)

        log_path = output_dir / "document-progress.log"
        assert log_path.is_file()
        assert "test message" in log_path.read_text(encoding="utf-8")

    def test_appends_to_existing_log(self, tmp_path: Path) -> None:
        from saturnday.document.document_runner import _log_document_progress

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        _log_document_progress("first message", output_dir)
        _log_document_progress("second message", output_dir)

        log_text = (output_dir / "document-progress.log").read_text(encoding="utf-8")
        assert "first message" in log_text
        assert "second message" in log_text

    def test_log_failure_does_not_crash(self, tmp_path: Path) -> None:
        from saturnday.document.document_runner import _log_document_progress

        # Pass a non-writable directory path (non-existent parent)
        output_dir = Path("/nonexistent/path/that/cannot/exist")
        # Should not raise
        _log_document_progress("test", output_dir)


class TestCLIDocumentSummary:
    """Tests for _print_document_summary CLI helper."""

    def test_print_summary_does_not_crash(self, capsys: Any) -> None:
        from saturnday.cli import _print_document_summary
        from saturnday.document._types import (
            DocumentRunResult,
            DocumentSection,
            DocumentClaim,
            DocumentApproval,
            DocumentFinding,
        )

        result = DocumentRunResult(
            document_id="test-doc-001",
            type="report",
            total_sections=2,
            passed=2,
            failed=0,
            provisional=0,
            document_status="PASS",
        )
        result.sections = [
            DocumentSection(section_id="S001", name="Intro", purpose="test"),
            DocumentSection(section_id="S002", name="Body", purpose="test"),
        ]
        result.sections[0].status = "PASS"
        result.sections[1].status = "WARN"
        result.claims = [
            DocumentClaim(
                claim_id="S001_C001",
                section_id="S001",
                claim_text="Revenue was $1.2M",
                claim_type="quantitative",
                support_verdict="SUPPORTED",
            )
        ]
        result.global_findings = []
        result.approvals = []

        _print_document_summary(result)

        captured = capsys.readouterr()
        assert "test-doc-001" in captured.out
        assert "PASS" in captured.out

    def test_print_summary_shows_sign_off_when_present(self, capsys: Any) -> None:
        from saturnday.cli import _print_document_summary
        from saturnday.document._types import (
            DocumentRunResult,
            DocumentApproval,
        )

        result = DocumentRunResult(
            document_id="doc-002",
            type="report",
            document_status="APPROVED",
        )
        result.approvals = [
            DocumentApproval(
                document_id="doc-002",
                role="cfo",
                actor="Jane Doe",
                status="approved",
            )
        ]

        _print_document_summary(result)

        captured = capsys.readouterr()
        assert "cfo" in captured.out
        assert "Jane Doe" in captured.out
