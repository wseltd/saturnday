"""Tests for U3: --strict-dod enforcement flag on the ``run`` subcommand.

Verifies that:
- With ``--strict-dod``, an unmet DoD causes exit code 1.
- Without ``--strict-dod``, an unmet DoD does NOT cause exit code 1 (advisory only).
- A met DoD with ``--strict-dod`` still returns exit code 0 when all tickets pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import RunResult, TicketResult
from saturnday.cli import build_parser, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_plan(plan_data: dict, dir_path: Path) -> Path:
    """Write a plan dict to a temp JSON file."""
    path = dir_path / "plan.json"
    path.write_text(json.dumps(plan_data), encoding="utf-8")
    return path


def _minimal_plan(project_id: str = "dod-test") -> dict:
    return {
        "version": 1,
        "project_id": project_id,
        "definition_of_done": ["all_tickets_passed"],
        "tickets": [{"ticket_id": "T001", "goal": "Do something", "acceptance_criteria": ["done"]}],
    }


def _make_run_result(*, dod_met: bool, failed: int = 0) -> RunResult:
    disposition = "FAIL" if failed > 0 else "PASS"
    return RunResult(
        project_id="dod-test",
        total_tickets=1,
        passed=1 - failed,
        failed=failed,
        skipped=0,
        ticket_results=(
            TicketResult(ticket_id="T001", disposition=disposition),
        ),
        definition_of_done_met=dod_met,
        stop_reason="",
    )


# ---------------------------------------------------------------------------
# Parser-level tests
# ---------------------------------------------------------------------------

class TestStrictDodParserFlag:
    def test_strict_dod_flag_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "run",
            "--plan", "plan.json",
            "--repo", ".",
            "--backend", "openai",
            "--api-key", "key",
            "--strict-dod",
        ])
        assert args.strict_dod is True

    def test_strict_dod_default_false(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "run",
            "--plan", "plan.json",
            "--repo", ".",
            "--backend", "openai",
            "--api-key", "key",
        ])
        assert args.strict_dod is False


# ---------------------------------------------------------------------------
# Behaviour tests via the CLI ``run`` handler
# ---------------------------------------------------------------------------

class TestStrictDodBehaviour:
    """Test that --strict-dod makes unmet DoD return exit code 1."""

    def _invoke_run_cmd(
        self,
        tmp_path: Path,
        *,
        strict_dod: bool,
        run_result: RunResult,
    ) -> int:
        plan_data = _minimal_plan()
        plan_path = _write_plan(plan_data, tmp_path)

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        # Run commands now guard for a valid git working tree.
        import subprocess as _sp
        _sp.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
        _sp.run(
            ["git", "config", "user.email", "t@t"], cwd=str(repo_path),
            check=True, capture_output=True,
        )
        _sp.run(
            ["git", "config", "user.name", "t"], cwd=str(repo_path),
            check=True, capture_output=True,
        )
        _sp.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(repo_path), check=True, capture_output=True,
        )
        standards_dir = tmp_path / "standards"
        standards_dir.mkdir()

        argv = [
            "run",
            "--plan", str(plan_path),
            "--repo", str(repo_path),
            "--backend", "openai",
            "--api-key", "test-key",
            "--standards-dir", str(standards_dir),
            "--output-dir", str(tmp_path / "out"),
        ]
        if strict_dod:
            argv.append("--strict-dod")

        with patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            return main(argv)

    def test_strict_dod_unmet_returns_1(self, tmp_path: Path) -> None:
        """--strict-dod with unmet DoD must return exit code 1."""
        result = _make_run_result(dod_met=False, failed=0)
        exit_code = self._invoke_run_cmd(tmp_path, strict_dod=True, run_result=result)
        assert exit_code == 1

    def test_no_strict_dod_unmet_returns_0(self, tmp_path: Path) -> None:
        """Without --strict-dod, an unmet DoD does not raise exit code 1 when no tickets failed."""
        result = _make_run_result(dod_met=False, failed=0)
        exit_code = self._invoke_run_cmd(tmp_path, strict_dod=False, run_result=result)
        assert exit_code == 0

    def test_strict_dod_met_returns_0(self, tmp_path: Path) -> None:
        """--strict-dod with met DoD and all tickets passing returns 0."""
        result = _make_run_result(dod_met=True, failed=0)
        exit_code = self._invoke_run_cmd(tmp_path, strict_dod=True, run_result=result)
        assert exit_code == 0

    def test_strict_dod_with_failed_tickets_returns_1(self, tmp_path: Path) -> None:
        """--strict-dod with failed tickets returns 1 (both paths active)."""
        result = _make_run_result(dod_met=False, failed=1)
        exit_code = self._invoke_run_cmd(tmp_path, strict_dod=True, run_result=result)
        assert exit_code == 1

    def test_no_strict_dod_with_failed_tickets_returns_1(self, tmp_path: Path) -> None:
        """Without --strict-dod, a failed ticket still returns 1 (existing behavior unchanged)."""
        result = _make_run_result(dod_met=False, failed=1)
        exit_code = self._invoke_run_cmd(tmp_path, strict_dod=False, run_result=result)
        assert exit_code == 1
