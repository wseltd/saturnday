"""Fix 30 — recovery guidance after manual baseline commit.

Proves that:
1. Ordinary continuation logic is unchanged for normal FAIL states.
2. Recovered-baseline state (all FAILs are git_state_unavailable_defect +
   repo is now a valid git working tree) produces specialised guidance.
3. That guidance points to rerun-remaining.
4. The guidance does not incorrectly trigger for ordinary FAIL states.
5. Existing continuation output remains otherwise stable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


def _write_ledger(tmp_path: Path, ticket_statuses: dict) -> None:
    ledger_dir = tmp_path / "evidence" / "run"
    ledger_dir.mkdir(parents=True)
    ledger_data = {
        "project_id": "test",
        "ticket_statuses": ticket_statuses,
    }
    (ledger_dir / "ledger.json").write_text(json.dumps(ledger_data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests for detect_recovered_baseline_state
# ---------------------------------------------------------------------------

class TestDetectRecoveredBaselineState:
    def test_returns_true_when_all_fails_are_git_state_and_repo_valid(
        self, tmp_path: Path
    ) -> None:
        """Returns True when all FAILs are git_state_unavailable_defect and repo is git."""
        from saturnday.run.resume import detect_recovered_baseline_state
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "git_state_unavailable_defect"},
            "T002": {"disposition": "SKIP", "failure_category": ""},
        })
        assert detect_recovered_baseline_state(tmp_path, tmp_path) is True

    def test_returns_false_when_no_git(self, tmp_path: Path) -> None:
        """Returns False when repo is not a git working tree (condition 1 fails)."""
        from saturnday.run.resume import detect_recovered_baseline_state
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "git_state_unavailable_defect"},
        })
        assert detect_recovered_baseline_state(tmp_path, tmp_path) is False

    def test_returns_false_when_ordinary_plan_defect(self, tmp_path: Path) -> None:
        """Returns False when FAIL tickets are ordinary plan_defect (not git-state)."""
        from saturnday.run.resume import detect_recovered_baseline_state
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "plan_defect"},
            "T002": {"disposition": "SKIP", "failure_category": ""},
        })
        assert detect_recovered_baseline_state(tmp_path, tmp_path) is False

    def test_returns_false_when_mixed_fail_categories(self, tmp_path: Path) -> None:
        """Returns False when FAIL tickets are a mix of git-state and plan_defect."""
        from saturnday.run.resume import detect_recovered_baseline_state
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "git_state_unavailable_defect"},
            "T002": {"disposition": "FAIL", "failure_category": "plan_defect"},
        })
        assert detect_recovered_baseline_state(tmp_path, tmp_path) is False

    def test_returns_false_when_no_fail_tickets(self, tmp_path: Path) -> None:
        """Returns False when there are no FAIL tickets at all."""
        from saturnday.run.resume import detect_recovered_baseline_state
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "PASS", "failure_category": ""},
        })
        assert detect_recovered_baseline_state(tmp_path, tmp_path) is False


# ---------------------------------------------------------------------------
# Tests for _print_continuation_block guidance
# ---------------------------------------------------------------------------

class TestContinuationBlockRecoveredBaseline:
    def test_recovered_baseline_suggests_rerun_remaining(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Recovered-baseline state: guidance says rerun-remaining, not resume."""
        from saturnday.cli import _print_continuation_block
        from saturnday._types import RunResult, TicketResult
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "git_state_unavailable_defect"},
            "T002": {"disposition": "SKIP", "failure_category": ""},
        })

        tr1 = TicketResult(ticket_id="T001", disposition="FAIL")
        tr2 = TicketResult(ticket_id="T002", disposition="SKIP")
        result = RunResult(project_id="test", ticket_results=[tr1, tr2])

        _print_continuation_block(
            result,
            output_dir=str(tmp_path),
            plan_path="/path/plan.json",
            repo_path=str(tmp_path),
            backend="claude-cli",
        )
        out = capsys.readouterr().out

        assert "rerun-remaining" in out, "guidance must suggest rerun-remaining"
        # The Next step: line must be rerun-remaining, not resume
        next_step_line = [ln for ln in out.splitlines() if "Next step" in ln or "saturnday rerun" in ln or "saturnday resume" in ln.replace("'saturnday resume'", "")]
        assert any("rerun-remaining" in ln for ln in out.splitlines() if ln.strip().startswith("saturnday")), \
            "the saturnday command in Next step must be rerun-remaining"

    def test_recovered_baseline_warning_message_content(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Recovered-baseline warning explains why resume is wrong."""
        from saturnday.cli import _print_continuation_block
        from saturnday._types import RunResult, TicketResult
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "git_state_unavailable_defect"},
            "T002": {"disposition": "SKIP", "failure_category": ""},
        })

        tr1 = TicketResult(ticket_id="T001", disposition="FAIL")
        tr2 = TicketResult(ticket_id="T002", disposition="SKIP")
        result = RunResult(project_id="test", ticket_results=[tr1, tr2])

        _print_continuation_block(
            result,
            output_dir=str(tmp_path),
            plan_path="/path/plan.json",
            repo_path=str(tmp_path),
            backend="claude-cli",
        )
        out = capsys.readouterr().out

        assert "git-state" in out or "git state" in out.lower() or "git working tree" in out
        assert "baseline" in out.lower()
        assert "rerun-remaining" in out

    def test_ordinary_fail_still_suggests_resume(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Ordinary plan_defect FAIL + SKIP → resume (recovered-baseline does not trigger)."""
        from saturnday.cli import _print_continuation_block
        from saturnday._types import RunResult, TicketResult
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "plan_defect"},
            "T002": {"disposition": "SKIP", "failure_category": ""},
        })

        tr1 = TicketResult(ticket_id="T001", disposition="FAIL")
        tr2 = TicketResult(ticket_id="T002", disposition="SKIP")
        result = RunResult(project_id="test", ticket_results=[tr1, tr2])

        _print_continuation_block(
            result,
            output_dir=str(tmp_path),
            plan_path="/path/plan.json",
            repo_path=str(tmp_path),
            backend="claude-cli",
        )
        out = capsys.readouterr().out

        assert "saturnday resume" in out, "ordinary FAIL+SKIP must still suggest resume"

    def test_fail_only_no_remaining_unaffected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """FAIL-only (no SKIP/PENDING) → rerun-failed; recovered-baseline does not trigger."""
        from saturnday.cli import _print_continuation_block
        from saturnday._types import RunResult, TicketResult
        _init_git_repo(tmp_path)
        _write_ledger(tmp_path, {
            "T001": {"disposition": "FAIL", "failure_category": "git_state_unavailable_defect"},
        })

        tr1 = TicketResult(ticket_id="T001", disposition="FAIL")
        result = RunResult(project_id="test", ticket_results=[tr1])

        _print_continuation_block(
            result,
            output_dir=str(tmp_path),
            plan_path="/path/plan.json",
            repo_path=str(tmp_path),
            backend="claude-cli",
        )
        out = capsys.readouterr().out

        # cmd_name would be rerun-failed (no remaining), not resume → detection not triggered
        assert "rerun-failed" in out
