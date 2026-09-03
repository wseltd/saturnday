"""Tests for saturnday.document.section_retry.

All LLM calls are mocked.  verify_fn is injected to control pass/fail
outcomes without touching the real section_verifier.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import CoderConfig
from saturnday.document._types import DocumentFinding, DocumentPlan, DocumentSection, DocumentSpec
from saturnday.document.section_retry import (
    _build_enriched_prior,
    _finding_to_dict,
    run_section_with_retry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def coder_config() -> CoderConfig:
    return CoderConfig(backend="anthropic", api_key="test-key")


@pytest.fixture()
def spec() -> DocumentSpec:
    return DocumentSpec(
        type="report",
        purpose="Q3 revenue analysis",
        audience="Board",
        risk_class="high",
        required_sections=["Executive Summary"],
        claim_policy={},
        approved_sources=[],
        sign_off_roles=["CFO"],
    )


@pytest.fixture()
def section() -> DocumentSection:
    return DocumentSection(
        section_id="S001",
        name="Executive Summary",
        purpose="High-level Q3 summary",
    )


@pytest.fixture()
def plan(section: DocumentSection) -> DocumentPlan:
    return DocumentPlan(
        document_id="doc-001",
        type="report",
        purpose="Q3 revenue analysis",
        risk_class="high",
        sections=[section],
    )


def _make_finding(severity: str = "error") -> DocumentFinding:
    return DocumentFinding(
        finding_id="DOC-001",
        section_id="S001",
        kind="placeholder_detected",
        severity=severity,
        detail="Placeholder text found.",
        fixable_by_regeneration=True,
    )


def _pass_verify(*args, **kwargs):
    return "PASS", []


def _fail_verify(*args, **kwargs):
    return "FAIL", [_make_finding()]


def _warn_verify(*args, **kwargs):
    return "WARN", [_make_finding("warning")]


# ---------------------------------------------------------------------------
# Tests: run_section_with_retry
# ---------------------------------------------------------------------------


class TestRunSectionWithRetry:
    def test_first_attempt_pass_returns_immediately(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            result = run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=2,
                verify_fn=_pass_verify,
            )

        assert result["status"] == "PASS"
        assert result["retry_count"] == 0
        assert result["attempt"] == 1

    def test_first_attempt_warn_returns_warn(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            result = run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=2,
                verify_fn=_warn_verify,
            )

        assert result["status"] == "WARN"
        assert result["retry_count"] == 0

    def test_first_fail_then_pass_returns_pass(
        self, tmp_path, coder_config, spec, section, plan
    ):
        call_count = {"n": 0}

        def alternating_verify(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return "FAIL", [_make_finding()]
            return "PASS", []

        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            result = run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=2,
                verify_fn=alternating_verify,
            )

        assert result["status"] == "PASS"
        assert result["retry_count"] == 1
        assert result["attempt"] == 2

    def test_all_retries_exhausted_returns_provisional(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            result = run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=2,
                verify_fn=_fail_verify,
            )

        assert result["status"] == "PROVISIONAL_UNVERIFIED"
        # 1 initial + 2 retries = 3 attempts, retry_count = 2
        assert result["retry_count"] == 2

    def test_attempt_subdirectories_created(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=1,
                verify_fn=_fail_verify,
            )

        section_dir = tmp_path / "sections" / "S001"
        assert (section_dir / "attempt_1").exists()
        assert (section_dir / "attempt_2").exists()

    def test_attempt_subdirectory_contains_summary_json(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=1,
                verify_fn=_pass_verify,
            )

        attempt_dir = tmp_path / "sections" / "S001" / "attempt_1"
        summary_file = attempt_dir / "attempt_summary.json"
        assert summary_file.exists()
        data = json.loads(summary_file.read_text())
        assert data["attempt"] == 1

    def test_findings_from_prior_attempt_in_result(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            return_value="# Executive Summary\n\nOK.",
        ):
            result = run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=2,
                verify_fn=_fail_verify,
            )

        # After exhaustion, findings list should be populated
        assert isinstance(result["findings"], list)
        assert len(result["findings"]) > 0

    def test_coder_error_returns_provisional(
        self, tmp_path, coder_config, spec, section, plan
    ):
        with patch(
            "saturnday.document.section_runner.call_coder",
            side_effect=RuntimeError("Backend unavailable"),
        ):
            result = run_section_with_retry(
                section=section,
                spec=spec,
                plan=plan,
                repo_path=tmp_path,
                coder_config=coder_config,
                output_dir=tmp_path,
                max_retries=2,
                verify_fn=_pass_verify,
            )

        assert result["status"] == "PROVISIONAL_UNVERIFIED"


# ---------------------------------------------------------------------------
# Tests: _build_enriched_prior
# ---------------------------------------------------------------------------


class TestBuildEnrichedPrior:
    def test_attempt_1_returns_unchanged_prior(self):
        prior = [{"section_id": "S000", "content": "Background."}]
        result = _build_enriched_prior(prior, [], attempt_n=1)
        assert result is prior

    def test_attempt_2_no_findings_returns_unchanged(self):
        prior = [{"section_id": "S000", "content": "Background."}]
        result = _build_enriched_prior(prior, [], attempt_n=2)
        assert result is prior

    def test_retry_context_prepended_with_findings(self):
        prior_findings = [
            {"severity": "error", "kind": "placeholder_detected", "detail": "Found [TODO]."}
        ]
        result = _build_enriched_prior(None, prior_findings, attempt_n=2)
        assert result is not None
        assert len(result) == 1
        retry_entry = result[0]
        assert retry_entry["section_id"] == "RETRY_CONTEXT"
        assert "placeholder_detected" in retry_entry["content"]
        assert "Found [TODO]" in retry_entry["content"]

    def test_retry_context_prepended_before_prior_sections(self):
        prior = [{"section_id": "S000", "content": "Background."}]
        prior_findings = [
            {"severity": "error", "kind": "missing_heading", "detail": "No heading found."}
        ]
        result = _build_enriched_prior(prior, prior_findings, attempt_n=3)
        assert result is not None
        assert result[0]["section_id"] == "RETRY_CONTEXT"
        assert result[1]["section_id"] == "S000"


# ---------------------------------------------------------------------------
# Tests: _finding_to_dict
# ---------------------------------------------------------------------------


class TestFindingToDict:
    def test_dict_passthrough(self):
        d = {"finding_id": "F1", "severity": "error"}
        assert _finding_to_dict(d) is d

    def test_dataclass_to_dict(self):
        f = _make_finding("warning")
        d = _finding_to_dict(f)
        assert d["severity"] == "warning"
        assert d["kind"] == "placeholder_detected"
        assert d["finding_id"] == "DOC-001"
