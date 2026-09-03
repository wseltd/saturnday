"""Tests for saturnday.guard (publish_preflight, sarif)."""

import json
from pathlib import Path

import pytest

from saturnday.guard.publish_preflight import run_publish_preflight
from saturnday.guard.sarif import (
    findings_to_sarif,
    skill_result_to_sarif,
    write_sarif,
)
from saturnday.guard.cloud_scanner import Finding, SkillScanResult


class TestPublishPreflight:
    def _create_clean_skill(self, tmp_path: Path) -> Path:
        skill_dir = tmp_path / "clean-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "# Clean Skill\n\nA well-structured skill with good metadata.\n"
            "This skill is safe and ready for publishing.\n",
            encoding="utf-8",
        )
        (skill_dir / "LICENSE").write_text("MIT", encoding="utf-8")
        tests_dir = skill_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_skill.py").write_text(
            "def test_placeholder(): pass\n", encoding="utf-8",
        )
        return skill_dir

    def test_clean_skill_passes(self, tmp_path: Path) -> None:
        skill_dir = self._create_clean_skill(tmp_path)
        result = run_publish_preflight(skill_dir)
        assert result.allowed is True
        assert result.blocking_reasons == ()

    def test_missing_skill_md_blocks(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "bad-skill"
        skill_dir.mkdir()
        result = run_publish_preflight(skill_dir)
        assert result.allowed is False
        assert any("SKILL.md" in r for r in result.blocking_reasons)

    def test_dangerous_pattern_blocks(self, tmp_path: Path) -> None:
        skill_dir = self._create_clean_skill(tmp_path)
        (skill_dir / "script.py").write_text(
            "import subprocess\nsubprocess.call('rm -rf /')\n",
            encoding="utf-8",
        )
        result = run_publish_preflight(skill_dir)
        assert result.allowed is False


class TestSarif:
    def test_findings_to_sarif_structure(self) -> None:
        findings = [
            Finding(
                check="shell_danger",
                severity="high",
                file="run.py",
                line=5,
                message="Dangerous call",
                kind="shell_danger",
            ),
            Finding(
                check="credential_leak",
                severity="high",
                file="config.py",
                line=10,
                message="API key exposed",
                kind="credential_leak",
            ),
        ]
        sarif = findings_to_sarif(findings)
        assert sarif["version"] == "2.1.0"
        assert len(sarif["runs"]) == 1
        assert len(sarif["runs"][0]["results"]) == 2
        assert sarif["runs"][0]["results"][0]["level"] == "error"

    def test_empty_findings(self) -> None:
        sarif = findings_to_sarif([])
        assert len(sarif["runs"][0]["results"]) == 0

    def test_severity_mapping(self) -> None:
        findings = [
            Finding(check="a", severity="high", message="x"),
            Finding(check="b", severity="medium", message="y"),
            Finding(check="c", severity="low", message="z"),
        ]
        sarif = findings_to_sarif(findings)
        levels = [r["level"] for r in sarif["runs"][0]["results"]]
        assert levels == ["error", "warning", "note"]

    def test_write_sarif(self, tmp_path: Path) -> None:
        sarif = findings_to_sarif([])
        path = write_sarif(sarif, tmp_path / "results.sarif")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["version"] == "2.1.0"

    def test_skill_result_to_sarif(self) -> None:
        result = SkillScanResult(
            relative_path="my-skill",
            skill_md_hash="abc123",
            status="scanned",
            disposition="FAIL",
            findings=[
                Finding(
                    check="shell_danger",
                    severity="high",
                    file="run.py",
                    line=1,
                    message="Bad",
                    kind="shell_danger",
                ),
            ],
        )
        sarif = skill_result_to_sarif(result)
        assert len(sarif["runs"][0]["results"]) == 1
        loc = sarif["runs"][0]["results"][0]["locations"][0]
        assert "my-skill/run.py" in loc["physicalLocation"]["artifactLocation"]["uri"]
