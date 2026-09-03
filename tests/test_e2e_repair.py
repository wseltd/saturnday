"""End-to-end proof: scan → tickets → repair → re-scan → finding gone.

Design note: ``_SHELL_DANGER_RE`` matches any ``subprocess.*`` call pattern,
so the mock coder must produce subprocess-free content to achieve a clean
re-scan.  A function that returns ``pass`` (no shell execution) satisfies the
scanner without requiring a real AI backend.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.guard.cloud_scanner import scan_skill
from saturnday.repair.repair_executor import execute_repair
from saturnday.repair.repair_tickets import generate_repair_tickets


class TestE2ERepair:
    def test_scan_ticket_repair_rescan(self, tmp_path: Path) -> None:
        """Full pipeline: vulnerable skill → scan → tickets → repair → clean."""
        # 1. Create vulnerable skill
        skill = tmp_path / "test-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "# Test Skill\n\n"
            "A test skill demonstrating the repair pipeline.\n\n"
            "## Usage\nRun it with a command.\n",
            encoding="utf-8",
        )
        (skill / "run.py").write_text(
            "import subprocess\n"
            "def execute(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n",
            encoding="utf-8",
        )

        # 2. Scan — expect shell_danger
        result = scan_skill(skill)
        assert any(f.kind == "shell_danger" for f in result.findings), (
            f"Expected shell_danger finding; got: {[f.kind for f in result.findings]}"
        )

        # 3. Generate repair tickets
        tickets = generate_repair_tickets(result.findings)
        shell_tickets = [t for t in tickets if t.finding_kind == "shell_danger"]
        assert len(shell_tickets) >= 1, (
            f"Expected at least 1 shell_danger ticket; got {len(shell_tickets)}"
        )

        # 4. Execute repair with a deterministic mock coder.
        #    The mock replaces shell execution with a no-op so the scanner
        #    produces 0 shell_danger findings on re-scan.
        def mock_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            return (
                "def execute(cmd):\n"
                "    pass  # shell execution removed by repair pipeline\n"
            )

        repair_result = execute_repair(shell_tickets[0], skill, coder_fn=mock_coder)

        # 5. Assert fixed
        assert repair_result.status == "fixed", (
            f"Expected status='fixed', got '{repair_result.status}'. "
            f"findings_before={repair_result.findings_before}, "
            f"findings_after={repair_result.findings_after}, "
            f"error={repair_result.error!r}"
        )
        assert repair_result.findings_before > 0
        assert repair_result.findings_after == 0
        assert "shell_danger" in repair_result.findings_resolved

    def test_remediation_guidance_flows_through_pipeline(self, tmp_path: Path) -> None:
        """Guidance attached by scanner propagates to the repair ticket."""
        skill = tmp_path / "guided-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "# Guided Skill\n\n"
            "A skill for testing guidance propagation.\n\n"
            "## Usage\nRun it.\n",
            encoding="utf-8",
        )
        (skill / "run.py").write_text(
            "import subprocess\n"
            "def run(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n",
            encoding="utf-8",
        )

        result = scan_skill(skill)
        shell_findings = [f for f in result.findings if f.kind == "shell_danger"]
        assert shell_findings, "No shell_danger findings — check scanner fixture"

        # Guidance should be attached by _enrich_with_guidance
        first = shell_findings[0]
        assert first.remediation is not None, (
            "Expected remediation dict on shell_danger finding; got None. "
            "Check that saturnday.remediation_guidance is installed and "
            "_enrich_with_guidance is wired in scan_skill."
        )
        assert "why" in first.remediation
        assert "fix" in first.remediation

        # Guidance flows into the repair ticket
        tickets = generate_repair_tickets(shell_findings)
        assert tickets[0].remediation is not None
        assert "why" in tickets[0].remediation
