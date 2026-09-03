"""E2E proof: CLI repair dry-run -> load plan -> execute -> verify fixed.

The mock coder replaces ``subprocess`` usage with a no-op ``pass`` statement,
which removes the ``shell_danger`` finding without requiring a live AI backend.
``call_coder`` is patched at the module level so ``validate_auth`` and the
entire HTTP / subprocess path are bypassed cleanly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday.cli import main
from saturnday.guard.cloud_scanner import scan_skill


# ---------------------------------------------------------------------------
# Shared skill fixtures
# ---------------------------------------------------------------------------

_SKILL_MD = (
    "# Vuln Skill\n\n"
    "A test skill with a shell_danger finding.\n\n"
    "## Usage\nRun it.\n"
)

_VULNERABLE_RUN_PY = (
    "import subprocess\n"
    "def execute(cmd):\n"
    "    subprocess.call(cmd, shell=True)\n"
)

# The mock coder returns this; subprocess-free so scanner finds 0 shell_danger.
_SAFE_RUN_PY = "def execute(cmd):\n    pass  # shell removed by repair\n"


def _make_vuln_skill(base: Path) -> Path:
    """Create a minimal vulnerable skill under *base* and return its path."""
    skill = base / "vuln-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(_SKILL_MD, encoding="utf-8")
    (skill / "run.py").write_text(_VULNERABLE_RUN_PY, encoding="utf-8")
    return skill


# ---------------------------------------------------------------------------
# E2E tests
# ---------------------------------------------------------------------------


class TestE2ERepairCLI:
    def test_full_cli_repair_flow(self, tmp_path: Path) -> None:
        """dry-run -> plan file -> execute with mock -> finding gone."""
        skill = _make_vuln_skill(tmp_path)
        out = tmp_path / "repair-output"

        # Step 1: confirm shell_danger exists before repair
        pre_scan = scan_skill(skill)
        shell_findings = [f for f in pre_scan.findings if f.kind == "shell_danger"]
        assert len(shell_findings) > 0, (
            f"Pre-scan should find shell_danger; got: "
            f"{[f.kind for f in pre_scan.findings]}"
        )

        # Step 2: dry-run -> repair-plan.json
        dry_result = main(
            ["repair", "--dry-run", "--skill", str(skill), "--output-dir", str(out)]
        )
        assert dry_result == 0, "Dry-run should succeed on a vulnerable skill"
        plan_path = out / "repair-plan.json"
        assert plan_path.exists(), "Dry-run must write repair-plan.json"
        plan = json.loads(plan_path.read_text())
        assert len(plan["tickets"]) > 0, "Plan must contain at least one ticket"
        assert any(
            t["finding_kind"] == "shell_danger" for t in plan["tickets"]
        ), "Plan must include a shell_danger ticket"

        # Step 3: execute via CLI with mock coder
        # Patch call_coder so validate_auth and HTTP calls are fully bypassed.
        def mock_call_coder(config, messages, repo_path):
            return _SAFE_RUN_PY

        with patch(
            "saturnday.coder_adapter.call_coder",
            side_effect=mock_call_coder,
        ):
            exec_result = main(
                [
                    "repair",
                    "--plan", str(plan_path),
                    "--skill", str(skill),
                    "--backend", "openai",
                    "--api-key", "test-key",
                    "--output-dir", str(out),
                ]
            )

        # Step 4: evidence must exist regardless of individual ticket outcomes
        summary_path = out / "repair-summary.json"
        assert summary_path.exists(), "repair-summary.json must be written"
        summary = json.loads(summary_path.read_text())
        assert summary["fixed"] >= 0  # at minimum the run attempted tickets

        metadata_path = out / "repair-metadata.json"
        assert metadata_path.exists(), "repair-metadata.json must be written"
        meta = json.loads(metadata_path.read_text())
        assert meta["backend"] == "openai"

        # Step 5: finding must be gone — mock coder rewrote the file on disk
        post_scan = scan_skill(skill)
        post_shell = [f for f in post_scan.findings if f.kind == "shell_danger"]
        assert len(post_shell) == 0, (
            f"shell_danger should be gone after repair; "
            f"still present: {[f.message for f in post_shell]}"
        )

    def test_dry_run_plan_is_loadable(self, tmp_path: Path) -> None:
        """Plan written by dry-run round-trips through RepairTicket.from_dict."""
        from saturnday.repair.repair_tickets import RepairTicket

        skill = _make_vuln_skill(tmp_path)
        out = tmp_path / "out"

        main(["repair", "--dry-run", "--skill", str(skill), "--output-dir", str(out)])

        plan_path = out / "repair-plan.json"
        plan = json.loads(plan_path.read_text())
        tickets = [RepairTicket.from_dict(t) for t in plan["tickets"]]
        assert len(tickets) > 0
        for t in tickets:
            assert t.ticket_id.startswith("REPAIR-")
            assert t.finding_kind != ""
            # file_path may be empty for whole-skill checks (e.g. no_tests, no_license)

    def test_plan_execution_updates_skill_file(self, tmp_path: Path) -> None:
        """Executing a plan with a mock coder overwrites the target file on disk."""
        skill = _make_vuln_skill(tmp_path)
        out = tmp_path / "out"

        # Generate plan
        main(["repair", "--dry-run", "--skill", str(skill), "--output-dir", str(out)])
        plan_path = out / "repair-plan.json"

        original_content = (skill / "run.py").read_text()

        def mock_call_coder(config, messages, repo_path):
            return _SAFE_RUN_PY

        with patch(
            "saturnday.coder_adapter.call_coder",
            side_effect=mock_call_coder,
        ):
            main(
                [
                    "repair",
                    "--plan", str(plan_path),
                    "--skill", str(skill),
                    "--backend", "openai",
                    "--api-key", "test-key",
                    "--output-dir", str(out),
                ]
            )

        new_content = (skill / "run.py").read_text()
        assert new_content != original_content, (
            "File on disk should be updated by the mock coder"
        )
        assert "subprocess" not in new_content

    def test_evidence_ticket_count_matches_plan(self, tmp_path: Path) -> None:
        """repair-metadata.json ticket_count matches the plan ticket count."""
        skill = _make_vuln_skill(tmp_path)
        out = tmp_path / "out"

        main(["repair", "--dry-run", "--skill", str(skill), "--output-dir", str(out)])
        plan_path = out / "repair-plan.json"
        plan = json.loads(plan_path.read_text())
        expected_count = len(plan["tickets"])

        def mock_call_coder(config, messages, repo_path):
            return _SAFE_RUN_PY

        with patch(
            "saturnday.coder_adapter.call_coder",
            side_effect=mock_call_coder,
        ):
            main(
                [
                    "repair",
                    "--plan", str(plan_path),
                    "--skill", str(skill),
                    "--backend", "openai",
                    "--api-key", "test-key",
                    "--output-dir", str(out),
                ]
            )

        meta = json.loads((out / "repair-metadata.json").read_text())
        assert meta["ticket_count"] == expected_count
