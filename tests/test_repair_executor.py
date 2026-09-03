"""Tests for saturnday.guard.repair_executor.

Each test creates a temporary skill directory with SKILL.md.
The ``coder_fn`` callable is mocked deterministically — no live AI calls.

Design note: ``_SHELL_DANGER_RE`` matches any ``subprocess.*`` call pattern,
so a "fixed" file must remove subprocess usage entirely (not merely switch
from ``subprocess.call`` to ``subprocess.run``).  Tests use a coder that
replaces subprocess calls with non-shell-based equivalents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.repair.repair_executor import RepairResult, execute_repair
from saturnday.repair.repair_tickets import RepairTicket


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_SKILL_MD = (
    "# Test Skill\n\n"
    "A test skill for repair executor tests.\n\n"
    "## Usage\nRun the skill to execute commands.\n"
)

_VULNERABLE_RUN_PY = (
    "import subprocess\n"
    "def execute(cmd):\n"
    "    subprocess.call(cmd, shell=True)\n"
)

# Fixed version replaces shell execution with a safe no-op.
# Using ``pass`` avoids triggering any scanner patterns.
_SAFE_RUN_PY = (
    "def execute(cmd):\n"
    "    pass  # shell execution removed\n"
)


def _make_skill(tmp_path: Path, run_content: str = _VULNERABLE_RUN_PY) -> Path:
    """Create a minimal skill directory with SKILL.md and run.py."""
    skill = tmp_path / "test-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (skill / "run.py").write_text(run_content, encoding="utf-8")
    return skill


def _make_ticket(
    *,
    kind: str = "shell_danger",
    file_path: str = "run.py",
    severity: str = "high",
    remediation: dict | None = None,
) -> RepairTicket:
    return RepairTicket(
        ticket_id="REPAIR-001",
        title=f"Fix {kind} in {file_path}",
        severity=severity,
        file_path=file_path,
        line=3,
        finding_kind=kind,
        evidence=[f"{file_path}:3: Dangerous shell call"],
        remediation=remediation,
        group_key=f"{file_path}:{kind}",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExecuteRepair:
    def test_successful_repair(self, tmp_path: Path) -> None:
        """Coder replaces vulnerable code; re-scan confirms 0 shell_danger findings."""
        skill = _make_skill(tmp_path, _VULNERABLE_RUN_PY)
        ticket = _make_ticket(kind="shell_danger", file_path="run.py")

        def mock_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            return _SAFE_RUN_PY

        result = execute_repair(ticket, skill, coder_fn=mock_coder)

        assert result.status == "fixed", (
            f"Expected 'fixed', got '{result.status}'. "
            f"findings_before={result.findings_before}, "
            f"findings_after={result.findings_after}"
        )
        assert result.findings_before > 0
        assert result.findings_after == 0
        assert "shell_danger" in result.findings_resolved

    def test_failed_repair_no_coder(self, tmp_path: Path) -> None:
        """No coder_fn provided → status='failed' with descriptive error."""
        skill = _make_skill(tmp_path, _VULNERABLE_RUN_PY)
        ticket = _make_ticket(kind="shell_danger")

        result = execute_repair(ticket, skill, coder_fn=None)

        assert result.status == "failed"
        assert result.error is not None
        assert "No coder" in result.error
        assert result.findings_resolved == []

    def test_failed_repair_no_change(self, tmp_path: Path) -> None:
        """Coder returns same content → status='failed'."""
        skill = _make_skill(tmp_path, _VULNERABLE_RUN_PY)
        original_content = (skill / "run.py").read_text()
        ticket = _make_ticket(kind="shell_danger")

        def identity_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            return original_content  # unchanged — finding persists

        result = execute_repair(ticket, skill, coder_fn=identity_coder)

        assert result.status == "failed"
        assert result.findings_before > 0
        assert result.findings_after >= result.findings_before
        assert result.findings_resolved == []

    def test_partial_repair(self, tmp_path: Path) -> None:
        """Skill has 2 shell-danger lines; coder fixes only 1 → status='partial'."""
        two_line_vuln = (
            "import subprocess\n"
            "def execute_a(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n"
            "def execute_b(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n"
        )
        # Partial fix: remove first call, leave second
        partial_fix = (
            "def execute_a(cmd):\n"
            "    pass  # fixed\n"
            "import subprocess\n"
            "def execute_b(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n"
        )
        skill = _make_skill(tmp_path, two_line_vuln)
        ticket = _make_ticket(kind="shell_danger")

        def partial_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            return partial_fix

        result = execute_repair(ticket, skill, coder_fn=partial_coder)

        # findings_before should be 2; findings_after should be 1
        assert result.findings_before == 2, (
            f"Expected 2 shell_danger findings before repair, got {result.findings_before}"
        )
        assert result.status == "partial", (
            f"Expected 'partial', got '{result.status}' "
            f"(after={result.findings_after})"
        )
        assert result.findings_after < result.findings_before
        assert "shell_danger" in result.findings_resolved

    def test_error_handling(self, tmp_path: Path) -> None:
        """Coder raises exception → status='failed', error field populated."""
        skill = _make_skill(tmp_path, _VULNERABLE_RUN_PY)
        ticket = _make_ticket(kind="shell_danger")

        def exploding_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            raise RuntimeError("AI backend unavailable")

        result = execute_repair(ticket, skill, coder_fn=exploding_coder)

        assert result.status == "failed"
        assert result.error is not None
        assert "AI backend unavailable" in result.error
        assert result.findings_resolved == []

    def test_already_clean(self, tmp_path: Path) -> None:
        """Skill has no findings of the ticket kind → status='fixed', findings_before=0."""
        clean_content = "def execute(cmd):\n    pass\n"
        skill = _make_skill(tmp_path, clean_content)
        ticket = _make_ticket(kind="shell_danger")

        # coder_fn should never be called when no findings exist
        called = []

        def sentinel_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            called.append(True)
            return clean_content

        result = execute_repair(ticket, skill, coder_fn=sentinel_coder)

        assert result.status == "fixed"
        assert result.findings_before == 0
        assert result.findings_after == 0
        assert called == [], "coder_fn must not be invoked when skill is already clean"

    def test_empty_coder_output_rejected(self, tmp_path: Path) -> None:
        """Coder returning empty string should fail, not overwrite file."""
        skill = _make_skill(tmp_path, _VULNERABLE_RUN_PY)
        ticket = _make_ticket(kind="shell_danger", file_path="run.py")
        original_content = (skill / "run.py").read_text(encoding="utf-8")

        def empty_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            return ""

        result = execute_repair(ticket, skill, coder_fn=empty_coder)

        assert result.status == "failed"
        assert result.error is not None
        assert "empty" in result.error.lower()
        assert result.findings_resolved == []
        # Original file must be preserved
        assert (skill / "run.py").read_text(encoding="utf-8") == original_content

    def test_oversized_coder_output_rejected(self, tmp_path: Path) -> None:
        """Coder returning >1MB should fail, not overwrite file."""
        skill = _make_skill(tmp_path, _VULNERABLE_RUN_PY)
        ticket = _make_ticket(kind="shell_danger", file_path="run.py")
        original_content = (skill / "run.py").read_text(encoding="utf-8")

        def oversized_coder(prompt: str, file_path: str, repo_path: Path) -> str:
            return "x" * 2_000_000

        result = execute_repair(ticket, skill, coder_fn=oversized_coder)

        assert result.status == "failed"
        assert result.error is not None
        assert "safety limit" in result.error.lower()
        assert result.findings_resolved == []
        # Original file must be preserved
        assert (skill / "run.py").read_text(encoding="utf-8") == original_content
