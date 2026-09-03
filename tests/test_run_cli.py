"""Tests for CLI commands (saturnday.__main__)."""

import json
from pathlib import Path

import pytest

from saturnday.cli import main


class TestValidatePlan:
    def test_valid_plan(self, tmp_path: Path) -> None:
        plan = {
            "version": 1,
            "project_id": "test",
            "tickets": [{
                "ticket_id": "T001", "goal": "Create scaffold",
                "acceptance_criteria": ["scaffold exists"],
            }],
        }
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        exit_code = main(["validate-plan", "--plan", str(plan_path)])
        assert exit_code == 0

    def test_invalid_plan(self, tmp_path: Path) -> None:
        plan = {"version": 1}  # Missing tickets
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        exit_code = main(["validate-plan", "--plan", str(plan_path)])
        assert exit_code == 1

    def test_missing_file(self) -> None:
        exit_code = main(["validate-plan", "--plan", "/nonexistent/plan.json"])
        assert exit_code == 1


class TestExplainFailure:
    def test_with_ledger(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "evidence" / "run"
        evidence_dir.mkdir(parents=True)
        ledger = {
            "definition_of_done": ["all_tickets_passed"],
            "total_executed": 1,
            "total_failed": 1,
            "ticket_statuses": {
                "T001": {"ticket_id": "T001", "disposition": "FAIL", "failure_category": "", "reasons": []},
            },
            "phase_statuses": [],
            "stop_reason": "",
        }
        (evidence_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
        exit_code = main(["explain-failure", "--output-dir", str(tmp_path)])
        assert exit_code == 0

    def test_missing_ledger(self, tmp_path: Path) -> None:
        exit_code = main(["explain-failure", "--output-dir", str(tmp_path)])
        assert exit_code == 1


class TestScanCommand:
    def test_scan_skill(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "# Test Skill\n\nA test skill for scanning.\n"
            "This demonstrates the guard scanner capabilities.\n",
            encoding="utf-8",
        )
        output_dir = tmp_path / "output"
        exit_code = main(["scan", "--skill", str(skill_dir), "--output", str(output_dir)])
        assert exit_code == 0

    def test_scan_missing_skill(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        exit_code = main(["scan", "--skill", "/nonexistent/skill", "--output", str(output_dir)])
        assert exit_code in (1, 2)


class TestPublishPreflight:
    def test_clean_skill(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "clean-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "# Clean Skill\n\nA safe and well-documented skill.\n"
            "This skill follows all the best practices.\n",
            encoding="utf-8",
        )
        (skill_dir / "LICENSE").write_text("MIT", encoding="utf-8")
        exit_code = main(["publish-preflight", "--skill", str(skill_dir)])
        assert exit_code == 0

    def test_missing_skill_dir(self) -> None:
        exit_code = main(["publish-preflight", "--skill", "/nonexistent/skill"])
        assert exit_code == 1


class TestNoCommand:
    def test_no_args(self) -> None:
        # saturnday-public returns 0 on no-args (prints help); cloud-core returned 1
        exit_code = main([])
        assert exit_code in (0, 1)
