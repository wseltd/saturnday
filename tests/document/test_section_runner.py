"""Tests for saturnday.document.section_runner.

All tests mock call_coder so no LLM backend is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import CoderConfig
from saturnday.document._types import DocumentPlan, DocumentSection, DocumentSpec
from saturnday.document.section_runner import (
    _build_section_prompt,
    _read_all_sources,
    _read_source_content,
    run_section,
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
        purpose="Summarise Q3 revenue performance",
        audience="Board of Directors",
        risk_class="high",
        required_sections=["Executive Summary", "Financial Results"],
        claim_policy={"quantitative_claims_require_source": True},
        approved_sources=["data/q3.csv", "data/forecast.md"],
        sign_off_roles=["CFO", "CEO"],
    )


@pytest.fixture()
def section() -> DocumentSection:
    return DocumentSection(
        section_id="S001",
        name="Executive Summary",
        purpose="High-level summary of Q3 performance for board review",
        required_sources=["data/q3.csv"],
        acceptance_criteria=["Must include revenue figure", "Must state YoY comparison"],
    )


@pytest.fixture()
def plan(section: DocumentSection) -> DocumentPlan:
    return DocumentPlan(
        document_id="doc-001",
        type="report",
        purpose="Summarise Q3 revenue performance",
        risk_class="high",
        sections=[section],
    )


# ---------------------------------------------------------------------------
# T005 Tests
# ---------------------------------------------------------------------------


class TestRunSection:
    def test_successful_generation_writes_content_file(
        self,
        tmp_path: Path,
        coder_config: CoderConfig,
        spec: DocumentSpec,
        section: DocumentSection,
        plan: DocumentPlan,
    ) -> None:
        with patch("saturnday.document.section_runner.call_coder", return_value="# Executive Summary\n\nContent here."):
            result = run_section(section, spec, plan, tmp_path, coder_config, tmp_path)

        content_path = Path(result["content_path"])
        assert content_path.exists()
        assert content_path.read_text() == "# Executive Summary\n\nContent here."

    def test_successful_generation_writes_metadata_json(
        self,
        tmp_path: Path,
        coder_config: CoderConfig,
        spec: DocumentSpec,
        section: DocumentSection,
        plan: DocumentPlan,
    ) -> None:
        with patch("saturnday.document.section_runner.call_coder", return_value="# Executive Summary\n\nContent."):
            result = run_section(section, spec, plan, tmp_path, coder_config, tmp_path)

        meta_path = Path(result["metadata_path"])
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["section_id"] == "S001"
        assert "timestamp" in meta
        assert meta["attempt_number"] == 1
        assert meta["status"] == "GENERATED"

    def test_result_dict_has_required_keys(
        self,
        tmp_path: Path,
        coder_config: CoderConfig,
        spec: DocumentSpec,
        section: DocumentSection,
        plan: DocumentPlan,
    ) -> None:
        with patch("saturnday.document.section_runner.call_coder", return_value="# Executive Summary\n\nOK."):
            result = run_section(section, spec, plan, tmp_path, coder_config, tmp_path)

        for key in ("section_id", "status", "content_path", "metadata_path", "content", "attempt", "error"):
            assert key in result

    def test_coder_failure_returns_error_result(
        self,
        tmp_path: Path,
        coder_config: CoderConfig,
        spec: DocumentSpec,
        section: DocumentSection,
        plan: DocumentPlan,
    ) -> None:
        with patch(
            "saturnday.document.section_runner.call_coder",
            side_effect=RuntimeError("LLM unavailable"),
        ):
            result = run_section(section, spec, plan, tmp_path, coder_config, tmp_path)

        assert result["status"] == "ERROR"
        assert result["content_path"] == ""
        assert "LLM unavailable" in result["error"]

    def test_attempt_number_reflected_in_result(
        self,
        tmp_path: Path,
        coder_config: CoderConfig,
        spec: DocumentSpec,
        section: DocumentSection,
        plan: DocumentPlan,
    ) -> None:
        with patch("saturnday.document.section_runner.call_coder", return_value="# Executive Summary\n\nOK."):
            result = run_section(section, spec, plan, tmp_path, coder_config, tmp_path, attempt=2)

        assert result["attempt"] == 2
        meta = json.loads(Path(result["metadata_path"]).read_text())
        assert meta["attempt_number"] == 2

    def test_prior_sections_included_in_prompt(
        self,
        tmp_path: Path,
        coder_config: CoderConfig,
        spec: DocumentSpec,
        section: DocumentSection,
        plan: DocumentPlan,
    ) -> None:
        captured_prompts: list[str] = []

        def mock_coder(config, messages, repo_path, **kwargs):
            for m in messages:
                if m["role"] == "user":
                    captured_prompts.append(m["content"])
            return "# Executive Summary\n\nOK."

        prior = [{"section_id": "S000", "content": "Previous section content here."}]
        with patch("saturnday.document.section_runner.call_coder", side_effect=mock_coder):
            run_section(section, spec, plan, tmp_path, coder_config, tmp_path, prior_sections=prior)

        assert captured_prompts
        assert "S000" in captured_prompts[0]

    def test_source_content_capped_at_limit(self, tmp_path: Path) -> None:
        source = tmp_path / "big_file.txt"
        source.write_bytes(b"A" * 20000)
        content = _read_source_content(str(source), tmp_path)
        assert len(content) <= 8192

    def test_missing_source_returns_empty_string(self, tmp_path: Path) -> None:
        content = _read_source_content("nonexistent/path.md", tmp_path)
        assert content == ""

    def test_total_source_cap_stops_reading(self, tmp_path: Path) -> None:
        # Create 6 files of 8K each — total cap is 32K so 5th file should be skipped
        sources: list[str] = []
        for i in range(6):
            p = tmp_path / f"source_{i}.txt"
            p.write_bytes(b"B" * 8192)
            sources.append(str(p))

        result = _read_all_sources(sources, tmp_path)
        total_chars = sum(len(v) for v in result.values())
        assert total_chars <= 32768


class TestBuildSectionPrompt:
    def test_prompt_contains_document_purpose(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        prompt = _build_section_prompt(section, spec, {}, None)
        assert "Summarise Q3 revenue performance" in prompt

    def test_prompt_contains_section_name(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        prompt = _build_section_prompt(section, spec, {}, None)
        assert "Executive Summary" in prompt

    def test_prompt_contains_claim_policy_constraint(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        prompt = _build_section_prompt(section, spec, {}, None)
        assert "quantitative" in prompt.lower() or "source" in prompt.lower()

    def test_prompt_contains_source_content(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        sources = {"data/q3.csv": "Revenue: $12M"}
        prompt = _build_section_prompt(section, spec, sources, None)
        assert "Revenue: $12M" in prompt

    def test_prompt_contains_prior_section_excerpt(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        prior = [{"section_id": "S000", "content": "Background info about the quarter."}]
        prompt = _build_section_prompt(section, spec, {}, prior)
        assert "S000" in prompt
        assert "Background info" in prompt

    def test_prompt_contains_acceptance_criteria(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        prompt = _build_section_prompt(section, spec, {}, None)
        assert "Must include revenue figure" in prompt

    def test_no_open_web_sourcing_rule_in_prompt(
        self, spec: DocumentSpec, section: DocumentSection
    ) -> None:
        prompt = _build_section_prompt(section, spec, {}, None)
        assert "open web" in prompt.lower() or "web" in prompt.lower()
