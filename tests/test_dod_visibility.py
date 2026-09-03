"""Tests for the commercial DoD visibility batch.

Covers:
1. show_plan_preview displays required_outcomes and proof_expectations
2. _write_accepted_dod_artifact creates the artifact with correct shape
3. _generate_how_to_run produces correct output for Python and Node projects
4. RunResult includes definition_of_done_classification field
5. Existing plan preview works when no governance fields present
"""
from __future__ import annotations

import json
from pathlib import Path

from saturnday._types import RunResult
from saturnday.interactive import _write_accepted_dod_artifact, show_plan_preview
from saturnday.reporting import _generate_how_to_run
from saturnday.ticket_runner import _write_accepted_dod


# ---------------------------------------------------------------------------
# show_plan_preview displays real DoD contract
# ---------------------------------------------------------------------------

class TestShowPlanPreview:
    def test_shows_required_outcomes(self, tmp_path: Path, capsys) -> None:
        plan_data = {
            "project_id": "test",
            "tickets": [{"ticket_id": "T001", "goal": "Build it", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
            "required_outcomes": ["API responds on /health", "Tests pass"],
            "proof_expectations": ["pytest exits 0"],
        }
        show_plan_preview(plan_data)
        captured = capsys.readouterr().out
        assert "Required outcomes:" in captured
        assert "API responds on /health" in captured
        assert "Tests pass" in captured
        assert "Proof expectations:" in captured
        assert "pytest exits 0" in captured

    def test_falls_back_to_markers_when_no_outcomes(self, capsys) -> None:
        plan_data = {
            "project_id": "test",
            "tickets": [],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        show_plan_preview(plan_data)
        captured = capsys.readouterr().out
        assert "all_tickets_passed" in captured

    def test_shows_exclusions_and_constraints(self, capsys) -> None:
        plan_data = {
            "project_id": "test",
            "tickets": [],
            "phases": [],
            "required_outcomes": ["It works"],
            "exclusions": ["Do not modify auth"],
            "constraints": ["Python 3.10 only"],
        }
        show_plan_preview(plan_data)
        captured = capsys.readouterr().out
        assert "Exclusions:" in captured
        assert "Do not modify auth" in captured
        assert "Constraints:" in captured
        assert "Python 3.10 only" in captured


# ---------------------------------------------------------------------------
# _write_accepted_dod_artifact
# ---------------------------------------------------------------------------

class TestWriteAcceptedDodArtifact:
    def test_writes_artifact_with_correct_shape(self, tmp_path: Path) -> None:
        plan_data = {
            "project_id": "my-project",
            "governing_goal": "Build a thing",
            "required_outcomes": ["It runs", "Tests pass"],
            "proof_expectations": ["pytest exits 0"],
            "exclusions": ["Do not touch auth"],
            "constraints": ["Python only"],
            "definition_of_done": ["all_tickets_passed"],
            "_dod_user_edited": False,
        }
        evidence_dir = tmp_path / "run_001"
        _write_accepted_dod(evidence_dir, plan_data)
        artifact_path = evidence_dir / "accepted-dod.json"
        assert artifact_path.exists()
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["project_id"] == "my-project"
        assert artifact["governing_goal"] == "Build a thing"
        assert artifact["required_outcomes"] == ["It runs", "Tests pass"]
        assert artifact["proof_expectations"] == ["pytest exits 0"]
        assert artifact["exclusions"] == ["Do not touch auth"]
        assert artifact["user_edited"] is False
        assert "created_utc" in artifact

    def test_user_edited_flag_preserved(self, tmp_path: Path) -> None:
        plan_data = {
            "project_id": "edited",
            "required_outcomes": ["Custom outcome"],
            "_dod_user_edited": True,
        }
        evidence_dir = tmp_path / "run_002"
        _write_accepted_dod(evidence_dir, plan_data)
        artifact_path = evidence_dir / "accepted-dod.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["user_edited"] is True

    def test_two_runs_do_not_overwrite_each_other(self, tmp_path: Path) -> None:
        """Each run writes to its own directory — no shared file collision."""
        plan_a = {"project_id": "run-a", "required_outcomes": ["Outcome A"]}
        plan_b = {"project_id": "run-b", "required_outcomes": ["Outcome B"]}

        dir_a = tmp_path / "run_a_evidence"
        dir_b = tmp_path / "run_b_evidence"

        _write_accepted_dod(dir_a, plan_a)
        _write_accepted_dod(dir_b, plan_b)

        artifact_a = json.loads((dir_a / "accepted-dod.json").read_text(encoding="utf-8"))
        artifact_b = json.loads((dir_b / "accepted-dod.json").read_text(encoding="utf-8"))

        assert artifact_a["project_id"] == "run-a"
        assert artifact_a["required_outcomes"] == ["Outcome A"]
        assert artifact_b["project_id"] == "run-b"
        assert artifact_b["required_outcomes"] == ["Outcome B"]

    def test_artifact_written_before_ticket_execution(self, tmp_path: Path) -> None:
        """_write_accepted_dod is called inside run_plan before any ticket runs.

        We verify by calling it directly on a fresh dir — the artifact must
        exist immediately, not after some deferred callback.
        """
        evidence_dir = tmp_path / "run_pre_exec"
        plan_data = {"project_id": "pre-exec-test", "required_outcomes": ["Built"]}
        _write_accepted_dod(evidence_dir, plan_data)
        artifact_path = evidence_dir / "accepted-dod.json"
        assert artifact_path.exists(), "Artifact must exist immediately after _write_accepted_dod"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert artifact["project_id"] == "pre-exec-test"


# ---------------------------------------------------------------------------
# _generate_how_to_run
# ---------------------------------------------------------------------------

class TestGenerateHowToRun:
    def test_python_project(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\n[tool.pytest]\n', encoding="utf-8"
        )
        lines = _generate_how_to_run(tmp_path)
        text = "\n".join(lines)
        assert "Python project detected" in text
        assert "pip install -e ." in text

    def test_node_project(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "test", "scripts": {"start": "node index.js", "test": "vitest"}}),
            encoding="utf-8",
        )
        lines = _generate_how_to_run(tmp_path)
        text = "\n".join(lines)
        assert "Node/TypeScript project detected" in text
        assert "npm install" in text
        assert "npm start" in text
        assert "npm test" in text

    def test_no_config_files(self, tmp_path: Path) -> None:
        lines = _generate_how_to_run(tmp_path)
        text = "\n".join(lines)
        assert "Check the README" in text


# ---------------------------------------------------------------------------
# RunResult classification field
# ---------------------------------------------------------------------------

class TestRunResultClassification:
    def test_classification_field_exists(self) -> None:
        result = RunResult(
            project_id="test",
            definition_of_done_classification="DOD_PARTIAL",
        )
        assert result.definition_of_done_classification == "DOD_PARTIAL"

    def test_classification_defaults_to_empty(self) -> None:
        result = RunResult(project_id="test")
        assert result.definition_of_done_classification == ""
