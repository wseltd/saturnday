"""Tests for saturnday.document.spec_parser.

Covers happy-path parsing, missing required fields, filesystem validation,
duplicate section detection, and sign-off rules per risk class.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from saturnday.document._types import DocumentSpec
from saturnday.document.spec_parser import parse_doc_spec, validate_doc_spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_spec(tmp_path: Path, content: str) -> Path:
    """Write YAML content to a doc-spec.yaml in tmp_path and return the path."""
    spec_path = tmp_path / "doc-spec.yaml"
    spec_path.write_text(textwrap.dedent(content), encoding="utf-8")
    return spec_path


def _minimal_yaml(
    *,
    type_val: str = "report",
    purpose: str = "Describe results",
    audience: str = "Board",
    risk_class: str = "low",
    required_sections: str = "  - Executive Summary\n  - Financials",
    claim_policy: str = "  quantitative_claims_require_source: true",
    approved_sources: str = "  - sources/data.csv",
    sign_off_roles: str = "",
) -> str:
    sign_off_block = (
        f"sign_off_roles:\n{sign_off_roles}" if sign_off_roles else "sign_off_roles: []"
    )
    return (
        f"type: {type_val}\n"
        f"purpose: {purpose}\n"
        f"audience: {audience}\n"
        f"risk_class: {risk_class}\n"
        f"required_sections:\n{required_sections}\n"
        f"claim_policy:\n{claim_policy}\n"
        f"approved_sources:\n{approved_sources}\n"
        f"{sign_off_block}\n"
    )


# ---------------------------------------------------------------------------
# parse_doc_spec — happy path
# ---------------------------------------------------------------------------


class TestParseValidSpec:
    def test_parse_returns_document_spec(self, tmp_path: Path) -> None:
        src = tmp_path / "sources"
        src.mkdir()
        (src / "data.csv").write_text("col1,col2", encoding="utf-8")

        spec_path = _write_spec(
            tmp_path,
            _minimal_yaml(approved_sources=f"  - {src / 'data.csv'}"),
        )
        spec = parse_doc_spec(spec_path)
        assert isinstance(spec, DocumentSpec)

    def test_type_stored(self, tmp_path: Path) -> None:
        spec_path = _write_spec(tmp_path, _minimal_yaml())
        spec = parse_doc_spec(spec_path)
        assert spec.type == "report"

    def test_sections_stored(self, tmp_path: Path) -> None:
        spec_path = _write_spec(tmp_path, _minimal_yaml())
        spec = parse_doc_spec(spec_path)
        assert spec.required_sections == ["Executive Summary", "Financials"]

    def test_claim_policy_dict(self, tmp_path: Path) -> None:
        spec_path = _write_spec(tmp_path, _minimal_yaml())
        spec = parse_doc_spec(spec_path)
        assert spec.claim_policy == {"quantitative_claims_require_source": True}

    def test_optional_fields_defaults(self, tmp_path: Path) -> None:
        spec_path = _write_spec(tmp_path, _minimal_yaml())
        spec = parse_doc_spec(spec_path)
        assert spec.jurisdiction == ""
        assert spec.template == ""
        assert spec.citation_style == "internal_reference"
        assert spec.max_retry_per_section == 2

    def test_optional_fields_parsed(self, tmp_path: Path) -> None:
        yaml_content = _minimal_yaml() + (
            "jurisdiction: EU\n"
            "template: templates/report.md\n"
            "citation_style: footnote\n"
            "max_retry_per_section: 4\n"
        )
        spec_path = _write_spec(tmp_path, yaml_content)
        spec = parse_doc_spec(spec_path)
        assert spec.jurisdiction == "EU"
        assert spec.template == "templates/report.md"
        assert spec.citation_style == "footnote"
        assert spec.max_retry_per_section == 4

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_doc_spec(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_raises_value_error(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "doc-spec.yaml"
        spec_path.write_text("key: [unclosed bracket", encoding="utf-8")
        with pytest.raises(ValueError, match="YAML parse error"):
            parse_doc_spec(spec_path)

    def test_non_mapping_yaml_raises(self, tmp_path: Path) -> None:
        spec_path = tmp_path / "doc-spec.yaml"
        spec_path.write_text("- just a list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="mapping"):
            parse_doc_spec(spec_path)


# ---------------------------------------------------------------------------
# parse_doc_spec — missing required fields
# ---------------------------------------------------------------------------


class TestParseMissingRequiredField:
    """Each test omits exactly one required key and expects ValueError."""

    def _spec_without(self, missing_key: str) -> str:
        """Return a valid YAML spec string with ``missing_key`` removed.

        Each key is omitted cleanly by building the YAML from scratch rather
        than stripping lines — avoids producing malformed YAML when block
        scalars span multiple lines.
        """
        parts: dict[str, str] = {
            "type": "type: report",
            "purpose": "purpose: Describe results",
            "audience": "audience: Board",
            "risk_class": "risk_class: low",
            "required_sections": "required_sections:\n  - Executive Summary\n  - Financials",
            "claim_policy": "claim_policy:\n  quantitative_claims_require_source: true",
            "approved_sources": "approved_sources:\n  - sources/data.csv",
            "sign_off_roles": "sign_off_roles: []",
        }
        return "\n".join(v for k, v in parts.items() if k != missing_key) + "\n"

    @pytest.mark.parametrize(
        "missing_key",
        [
            "type",
            "purpose",
            "audience",
            "risk_class",
            "required_sections",
            "claim_policy",
            "approved_sources",
            "sign_off_roles",
        ],
    )
    def test_missing_key_raises_value_error(
        self, tmp_path: Path, missing_key: str
    ) -> None:
        spec_path = tmp_path / "doc-spec.yaml"
        spec_path.write_text(self._spec_without(missing_key), encoding="utf-8")
        with pytest.raises(ValueError, match="missing required keys"):
            parse_doc_spec(spec_path)


# ---------------------------------------------------------------------------
# validate_doc_spec — filesystem checks
# ---------------------------------------------------------------------------


class TestValidateMissingSources:
    def test_missing_source_file_produces_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["does_not_exist.csv"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("does_not_exist.csv" in e for e in errors)

    def test_existing_source_passes(self, tmp_path: Path) -> None:
        src = tmp_path / "data.csv"
        src.write_text("col", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["data.csv"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert not any("data.csv" in e for e in errors)

    def test_multiple_missing_sources_all_reported(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["missing_a.csv", "missing_b.csv"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("missing_a.csv" in e for e in errors)
        assert any("missing_b.csv" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_doc_spec — empty sections
# ---------------------------------------------------------------------------


class TestValidateEmptySections:
    def test_empty_required_sections_is_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=[],
            claim_policy={"key": True},
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("required_sections" in e for e in errors)

    def test_non_empty_sections_no_error_for_that_check(self, tmp_path: Path) -> None:
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["s.txt"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert not any("required_sections" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_doc_spec — sign-off rules
# ---------------------------------------------------------------------------


class TestValidateSignOffRequiredForHighRisk:
    def test_high_risk_empty_sign_off_roles_is_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="high",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("sign_off_roles" in e for e in errors)

    def test_medium_risk_empty_sign_off_roles_is_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="medium",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("sign_off_roles" in e for e in errors)

    def test_high_risk_with_sign_off_passes_that_check(self, tmp_path: Path) -> None:
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="high",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["s.txt"],
            sign_off_roles=["CTO"],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert not any("sign_off_roles" in e for e in errors)


class TestValidateSignOffOptionalForLowRisk:
    def test_low_risk_empty_sign_off_is_not_an_error(self, tmp_path: Path) -> None:
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["s.txt"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert not any("sign_off_roles" in e for e in errors)

    def test_low_risk_with_sign_off_also_passes(self, tmp_path: Path) -> None:
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=["s.txt"],
            sign_off_roles=["Reviewer"],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert not any("sign_off_roles" in e for e in errors)


# ---------------------------------------------------------------------------
# validate_doc_spec — duplicate sections
# ---------------------------------------------------------------------------


class TestValidateDuplicateSectionsRejected:
    def test_duplicate_section_name_produces_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro", "Body", "Intro"],  # duplicate
            claim_policy={"key": True},
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("duplicate" in e.lower() for e in errors)

    def test_unique_sections_no_duplicate_error(self, tmp_path: Path) -> None:
        src = tmp_path / "s.txt"
        src.write_text("x", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro", "Body", "Conclusion"],
            claim_policy={"key": True},
            approved_sources=["s.txt"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert not any("duplicate" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# validate_doc_spec — other scalar checks
# ---------------------------------------------------------------------------


class TestValidateScalarFields:
    def test_invalid_risk_class_produces_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="critical",  # invalid
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("risk_class" in e for e in errors)

    def test_empty_type_produces_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={"key": True},
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("type" in e for e in errors)

    def test_empty_claim_policy_produces_error(self, tmp_path: Path) -> None:
        spec = DocumentSpec(
            type="report",
            purpose="X",
            audience="Y",
            risk_class="low",
            required_sections=["Intro"],
            claim_policy={},  # empty
            approved_sources=[],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert any("claim_policy" in e for e in errors)

    def test_valid_spec_returns_empty_errors(self, tmp_path: Path) -> None:
        src = tmp_path / "data.csv"
        src.write_text("col", encoding="utf-8")
        spec = DocumentSpec(
            type="report",
            purpose="Quarterly results",
            audience="Board",
            risk_class="low",
            required_sections=["Intro", "Financials"],
            claim_policy={"quantitative_claims_require_source": True},
            approved_sources=["data.csv"],
            sign_off_roles=[],
        )
        errors = validate_doc_spec(spec, repo_path=tmp_path)
        assert errors == []
