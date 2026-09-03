"""Tests for saturnday.document.planner.

Covers plan generation from a spec, section ID format, global checks,
and the plan.json write-to-disk behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.document._types import DocumentPlan, DocumentSection, DocumentSpec
from saturnday.document.planner import (
    _GLOBAL_CHECKS,
    _SECTION_CHECKS,
    generate_document_plan,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_spec(
    tmp_path: Path,
    *,
    risk_class: str = "low",
    sections: list[str] | None = None,
) -> DocumentSpec:
    """Build a minimal valid DocumentSpec backed by a real source file."""
    src = tmp_path / "source.txt"
    src.write_text("evidence", encoding="utf-8")
    return DocumentSpec(
        type="report",
        purpose="Quarterly financial review",
        audience="Board of Directors",
        risk_class=risk_class,
        required_sections=sections or ["Executive Summary", "Financials", "Appendix"],
        claim_policy={
            "quantitative_claims_require_source": True,
            "cited_claims_only": False,
        },
        approved_sources=["source.txt"],
        sign_off_roles=["CFO"] if risk_class in ("medium", "high") else [],
    )


# ---------------------------------------------------------------------------
# test_generate_plan_basic
# ---------------------------------------------------------------------------


class TestGeneratePlanBasic:
    def test_returns_document_plan(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert isinstance(plan, DocumentPlan)

    def test_plan_type_matches_spec(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert plan.type == "report"

    def test_plan_purpose_matches_spec(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert plan.purpose == "Quarterly financial review"

    def test_plan_risk_class_matches_spec(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, risk_class="medium")
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert plan.risk_class == "medium"

    def test_document_id_is_non_empty_string(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert isinstance(plan.document_id, str)
        assert len(plan.document_id) > 0

    def test_section_count_matches_spec(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["Intro", "Body", "Conclusion"])
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert len(plan.sections) == 3

    def test_section_names_match_spec(self, tmp_path: Path) -> None:
        names = ["Executive Summary", "Financials", "Appendix"]
        spec = _make_spec(tmp_path, sections=names)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        plan_names = [s.name for s in plan.sections]
        assert plan_names == names

    def test_sections_are_document_section_instances(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        for section in plan.sections:
            assert isinstance(section, DocumentSection)

    def test_section_status_is_pending(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        for section in plan.sections:
            assert section.status == "PENDING"

    def test_section_required_sources_includes_approved_sources(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        for section in plan.sections:
            assert "source.txt" in section.required_sources

    def test_section_required_checks_are_standard(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        for section in plan.sections:
            assert set(section.required_checks) == set(_SECTION_CHECKS)

    def test_acceptance_criteria_derived_from_claim_policy(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        # claim_policy has "quantitative_claims_require_source: True"
        # so at least one criterion should be non-empty
        for section in plan.sections:
            assert len(section.acceptance_criteria) >= 1

    def test_section_purpose_contains_section_name(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["Risk Analysis"])
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert "Risk Analysis" in plan.sections[0].purpose

    def test_single_section_spec(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["Overview"])
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert len(plan.sections) == 1


# ---------------------------------------------------------------------------
# test_generate_plan_section_ids
# ---------------------------------------------------------------------------


class TestGeneratePlanSectionIds:
    def test_section_ids_start_at_s001(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["Intro", "Body"])
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert plan.sections[0].section_id == "S001"

    def test_section_ids_are_sequential(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["A", "B", "C", "D", "E"])
        plan = generate_document_plan(spec, repo_path=tmp_path)
        ids = [s.section_id for s in plan.sections]
        assert ids == ["S001", "S002", "S003", "S004", "S005"]

    def test_section_ids_zero_padded_to_three_digits(self, tmp_path: Path) -> None:
        # Nine sections: S001 through S009
        sections = [f"Section {i}" for i in range(1, 10)]
        spec = _make_spec(tmp_path, sections=sections)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        for i, section in enumerate(plan.sections, start=1):
            assert section.section_id == f"S{i:03d}"

    def test_section_ids_are_unique(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["A", "B", "C"])
        plan = generate_document_plan(spec, repo_path=tmp_path)
        ids = [s.section_id for s in plan.sections]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# test_generate_plan_global_checks
# ---------------------------------------------------------------------------


class TestGeneratePlanGlobalChecks:
    def test_global_checks_are_present(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert len(plan.global_checks) > 0

    def test_global_checks_include_cross_section_consistency(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert "cross_section_consistency" in plan.global_checks

    def test_global_checks_include_duplicate_metric_consistency(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert "duplicate_metric_consistency" in plan.global_checks

    def test_global_checks_include_summary_vs_body_consistency(
        self, tmp_path: Path
    ) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert "summary_vs_body_consistency" in plan.global_checks

    def test_global_checks_match_canonical_list(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        assert set(plan.global_checks) == set(_GLOBAL_CHECKS)


# ---------------------------------------------------------------------------
# test_generate_plan_writes_file
# ---------------------------------------------------------------------------


class TestGeneratePlanWritesFile:
    def test_plan_file_written(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        generate_document_plan(spec, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        assert plan_path.exists()

    def test_plan_file_is_valid_json(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        generate_document_plan(spec, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_plan_file_contains_document_id(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        plan = generate_document_plan(spec, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert data["document_id"] == plan.document_id

    def test_plan_file_contains_sections(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["Intro", "Body"])
        generate_document_plan(spec, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert len(data["sections"]) == 2

    def test_plan_file_sections_have_correct_ids(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path, sections=["Intro", "Body"])
        generate_document_plan(spec, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        ids = [s["section_id"] for s in data["sections"]]
        assert ids == ["S001", "S002"]

    def test_plan_dir_created_if_not_exists(self, tmp_path: Path) -> None:
        # .saturnday/document should not exist yet
        assert not (tmp_path / ".saturnday").exists()
        spec = _make_spec(tmp_path)
        generate_document_plan(spec, repo_path=tmp_path)
        assert (tmp_path / ".saturnday" / "document").is_dir()

    def test_plan_file_contains_global_checks(self, tmp_path: Path) -> None:
        spec = _make_spec(tmp_path)
        generate_document_plan(spec, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        assert "global_checks" in data
        assert "cross_section_consistency" in data["global_checks"]

    def test_second_call_overwrites_plan_file(self, tmp_path: Path) -> None:
        spec1 = _make_spec(tmp_path, sections=["Intro"])
        spec2 = _make_spec(tmp_path, sections=["Intro", "Body"])
        plan1 = generate_document_plan(spec1, repo_path=tmp_path)
        plan2 = generate_document_plan(spec2, repo_path=tmp_path)
        plan_path = tmp_path / ".saturnday" / "document" / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        # Most recent plan should be on disk
        assert data["document_id"] == plan2.document_id
        assert len(data["sections"]) == 2
