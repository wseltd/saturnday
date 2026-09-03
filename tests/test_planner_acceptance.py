"""Tests for Fix 45 — end-to-end proof generation in the normal planner path.

Proves:
1. Three-stage planner path produces non-empty acceptance_cmd for runnable products
2. Non-runnable/refactor briefs leave acceptance_cmd empty
3. Multi-module plans include integration coverage guidance
4. Validation surfaces a warning when runnable plan has no acceptance_cmd
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from saturnday.run.planner import (
    _detect_runnable_product,
    _generate_acceptance_cmd,
)
from saturnday.plan_parser import validate_plan


# ---------------------------------------------------------------------------
# Part A+B: runnable product detection
# ---------------------------------------------------------------------------

class TestDetectRunnableProduct:
    def test_cli_tool_detected(self) -> None:
        assert _detect_runnable_product("Command-line password generator", []) is True

    def test_web_app_detected(self) -> None:
        assert _detect_runnable_product("REST API web app with Flask", []) is True

    def test_library_detected(self) -> None:
        assert _detect_runnable_product("Python library for data harmonisation", []) is True

    def test_script_detected(self) -> None:
        assert _detect_runnable_product("Executable script to convert CSV files", []) is True

    def test_pipeline_detected(self) -> None:
        assert _detect_runnable_product("Data pipeline for ingestion and export", []) is True

    def test_refactor_not_detected(self) -> None:
        assert _detect_runnable_product("Refactor auth module for clarity", []) is False

    def test_docs_not_detected(self) -> None:
        assert _detect_runnable_product("Documentation update for API reference", []) is False

    def test_ticket_goal_detects_main(self) -> None:
        tickets = [{"ticket_id": "T006", "goal": "Wire main() entry point"}]
        assert _detect_runnable_product("Build the project", tickets) is True

    def test_plain_brief_no_signals(self) -> None:
        assert _detect_runnable_product("Fix the bug in line 42", []) is False


# ---------------------------------------------------------------------------
# Part A: acceptance_cmd generation
# ---------------------------------------------------------------------------

class TestGenerateAcceptanceCmd:
    def test_cli_tool_gets_acceptance_cmd(self) -> None:
        cmd = _generate_acceptance_cmd(
            "Command-line password generator",
            [{"ticket_id": "T001", "goal": "scaffold pyproject.toml"}],
            "password",
        )
        assert cmd != ""
        assert "password" in cmd.replace("-", "_")

    def test_web_app_gets_acceptance_cmd(self) -> None:
        cmd = _generate_acceptance_cmd("Flask REST API server", [], "flask-api")
        assert cmd != ""

    def test_library_gets_acceptance_cmd(self) -> None:
        cmd = _generate_acceptance_cmd("Python library for parsing", [], "mylib")
        assert cmd != ""
        assert "mylib" in cmd.replace("-", "_")

    def test_refactor_gets_empty(self) -> None:
        cmd = _generate_acceptance_cmd("Refactor the auth module", [], "auth")
        assert cmd == ""

    def test_docs_gets_empty(self) -> None:
        cmd = _generate_acceptance_cmd("Documentation update only", [], "docs")
        assert cmd == ""

    def test_pipeline_gets_acceptance_cmd(self) -> None:
        cmd = _generate_acceptance_cmd("Data pipeline for ingestion", [], "ingest")
        assert cmd != ""


# ---------------------------------------------------------------------------
# Part D: validation warning for missing acceptance_cmd
# ---------------------------------------------------------------------------

class TestValidationWarning:
    def test_runnable_plan_without_acceptance_cmd_warns(self, caplog) -> None:
        """Validation emits a warning for runnable plans with no acceptance_cmd."""
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "Build the CLI tool"},
            ],
            "notes": "A command-line tool for password generation",
        }
        with caplog.at_level(logging.WARNING):
            validate_plan(plan)
        assert any("Fix 45" in r.message and "acceptance_cmd" in r.message for r in caplog.records)

    def test_nonrunnable_plan_without_acceptance_cmd_no_warning(self, caplog) -> None:
        """Validation does NOT warn for non-runnable plans with no acceptance_cmd."""
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "Refactor module"},
            ],
            "notes": "Refactor the internal auth handling",
        }
        with caplog.at_level(logging.WARNING):
            validate_plan(plan)
        assert not any("Fix 45" in r.message for r in caplog.records)

    def test_runnable_plan_with_acceptance_cmd_no_warning(self, caplog) -> None:
        """Validation does NOT warn when acceptance_cmd is present."""
        plan = {
            "tickets": [
                {"ticket_id": "T001", "goal": "Build the CLI tool"},
            ],
            "notes": "A command-line tool for password generation",
            "acceptance_cmd": "python -m passgen --help",
        }
        with caplog.at_level(logging.WARNING):
            validate_plan(plan)
        assert not any("Fix 45" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix 47/48: path discipline and handoff contracts in planner prompts
# ---------------------------------------------------------------------------

class TestPlannerPathDiscipline:
    """Test that planner prompts include configurable path/interface guidance."""

    def test_enrichment_prompt_includes_path_discipline(self) -> None:
        """Enrichment prompt must mention configurable paths."""
        from saturnday.run.planner import _build_batch_enrich_prompt
        prompt = _build_batch_enrich_prompt(
            "Data pipeline CLI tool",
            [{"ticket_id": "T001", "goal": "Build ingester"}],
            "Python project",
        )
        assert "--input-dir" in prompt or "configurable" in prompt.lower()
        assert "hardcode" in prompt.lower() or "hardcoded" in prompt.lower()

    def test_skeleton_prompt_includes_path_discipline(self) -> None:
        """Skeleton prompt must mention configurable paths for pipelines."""
        from saturnday.run.planner import _build_skeleton_prompt
        prompt = _build_skeleton_prompt("Data pipeline CLI", "Python project")
        assert "--input-dir" in prompt or "--output-dir" in prompt

    def test_legacy_prompt_includes_path_discipline(self) -> None:
        """Legacy single-shot prompt must mention configurable paths."""
        from saturnday.run.planner import _build_planner_prompt
        prompt = _build_planner_prompt("CLI data pipeline tool", "Python project")
        assert "configurable" in prompt.lower() or "--input-dir" in prompt


class TestPlannerHandoffContracts:
    """Test that planner prompts include upstream/downstream contract language."""

    def test_enrichment_prompt_includes_handoff_contracts(self) -> None:
        """Enrichment prompt must require explicit file naming and schema contracts."""
        from saturnday.run.planner import _build_batch_enrich_prompt
        prompt = _build_batch_enrich_prompt(
            "Multi-module pipeline",
            [{"ticket_id": "T002", "goal": "Parse upstream output", "depends_on": ["T001"]}],
            "Python project",
        )
        assert "file naming pattern" in prompt.lower() or "exact" in prompt.lower()
        assert "schema" in prompt.lower() or "field names" in prompt.lower()
        assert "vague" in prompt.lower()

    def test_enrichment_prompt_requires_real_handoff_verification(self) -> None:
        """Enrichment prompt must push verify_cmd toward real handoff, not mocks."""
        from saturnday.run.planner import _build_batch_enrich_prompt
        prompt = _build_batch_enrich_prompt(
            "Data processing pipeline",
            [{"ticket_id": "T003", "goal": "Export results", "depends_on": ["T002"]}],
            "Python project",
        )
        assert "mock" in prompt.lower() or "actual upstream output" in prompt.lower() or "real handoff" in prompt.lower()

    def test_legacy_prompt_includes_handoff_contracts(self) -> None:
        """Legacy single-shot prompt must require explicit handoff contracts."""
        from saturnday.run.planner import _build_planner_prompt
        prompt = _build_planner_prompt("Multi-stage data processor", "Python project")
        assert "file naming pattern" in prompt.lower() or "exact" in prompt.lower()
        assert "vague" in prompt.lower()

    def test_skeleton_prompt_includes_downstream_consumption(self) -> None:
        """Skeleton prompt must mention downstream consuming upstream output format."""
        from saturnday.run.planner import _build_skeleton_prompt
        prompt = _build_skeleton_prompt("Pipeline with ingester and exporter", "Python project")
        assert "downstream" in prompt.lower() or "upstream" in prompt.lower() or "output format" in prompt.lower()


# ---------------------------------------------------------------------------
# Fix 52.a: acceptance setup planning tests
# ---------------------------------------------------------------------------

class TestAcceptanceSetupDetection:
    """Test _generate_acceptance_setup detects heavy product setup requirements."""

    def test_dataset_brief_produces_setup(self) -> None:
        from saturnday.run.planner import _generate_acceptance_setup
        steps = _generate_acceptance_setup("Download SDO imagery dataset and run inference")
        assert len(steps) >= 1
        assert any("download" in s.lower() for s in steps)

    def test_model_weights_brief_produces_setup(self) -> None:
        from saturnday.run.planner import _generate_acceptance_setup
        steps = _generate_acceptance_setup("Load pretrained model weights and evaluate")
        assert len(steps) >= 1
        assert any("model" in s.lower() or "pretrained" in s.lower() for s in steps)

    def test_docker_brief_produces_setup(self) -> None:
        from saturnday.run.planner import _generate_acceptance_setup
        steps = _generate_acceptance_setup("Build Docker container for the web service")
        assert len(steps) >= 1
        assert any("docker" in s.lower() or "container" in s.lower() for s in steps)

    def test_rdkit_brief_produces_setup(self) -> None:
        from saturnday.run.planner import _generate_acceptance_setup
        steps = _generate_acceptance_setup("Molecular property calculator using rdkit")
        assert len(steps) >= 1
        assert any("rdkit" in s.lower() for s in steps)

    def test_lightweight_brief_no_setup(self) -> None:
        from saturnday.run.planner import _generate_acceptance_setup
        steps = _generate_acceptance_setup("Command-line password generator using stdlib")
        assert steps == []

    def test_refactor_brief_no_setup(self) -> None:
        from saturnday.run.planner import _generate_acceptance_setup
        steps = _generate_acceptance_setup("Refactor auth module for clarity")
        assert steps == []


class TestAcceptanceSetupSchema:
    """Test acceptance_setup field in plan type/schema/parser."""

    def test_plan_type_has_acceptance_setup(self) -> None:
        from saturnday._types import ProjectPlan
        plan = ProjectPlan(version=1, project_id="test")
        assert plan.acceptance_setup == ()

    def test_plan_parser_reads_acceptance_setup(self) -> None:
        from saturnday.plan_parser import load_plan
        import json, tempfile
        from pathlib import Path

        plan_data = {
            "version": 1,
            "project_id": "test",
            "tickets": [{
                "ticket_id": "T001", "goal": "test",
                "acceptance_criteria": ["done"],
            }],
            "acceptance_setup": ["download dataset", "install rdkit-pypi"],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(plan_data, f)
            f.flush()
            plan = load_plan(f.name)
        assert plan.acceptance_setup == ("download dataset", "install rdkit-pypi")

    def test_plan_parser_handles_missing_acceptance_setup(self) -> None:
        from saturnday.plan_parser import load_plan
        import json, tempfile

        plan_data = {
            "version": 1,
            "project_id": "test",
            "tickets": [{
                "ticket_id": "T001", "goal": "test",
                "acceptance_criteria": ["done"],
            }],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(plan_data, f)
            f.flush()
            plan = load_plan(f.name)
        assert plan.acceptance_setup == ()
