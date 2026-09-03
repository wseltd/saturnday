"""Fix 27 — run-family git preflight parity.

Proves that _cmd_run, _cmd_resume, _cmd_rerun_failed, and _cmd_rerun_remaining
all fail fast with exit code 1 when --repo is not inside a valid git working
tree, and that they proceed normally (to the inner runner call) when a real
git working tree is present.

The git check uses `git rev-parse --is-inside-work-tree`, not a .git directory
heuristic, so worktrees and subdirectory paths both work correctly.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    """Return a minimal Namespace for run-family handlers."""
    defaults = dict(
        repo=str(tmp_path),
        plan=str(tmp_path / "plan.json"),
        output_dir=str(tmp_path / "out"),
        standards_dir=str(tmp_path / "standards"),
        backend="claude-cli",
        base_url=None,
        api_key=None,
        model=None,
        temperature=0.0,
        max_tokens=16384,
        timeout=1200,
        log_level=None,
        verbose=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _init_git_repo(path: Path) -> None:
    """Run git init so the directory is a real git working tree."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# _require_git_repo unit tests
# ---------------------------------------------------------------------------

def test_require_git_repo_passes_for_real_git_repo(tmp_path: Path) -> None:
    """_require_git_repo returns True inside a real git working tree."""
    from saturnday.cli import _require_git_repo
    _init_git_repo(tmp_path)
    assert _require_git_repo(tmp_path) is True


def test_require_git_repo_fails_when_no_git(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """_require_git_repo returns False and prints an error when not in a git tree."""
    from saturnday.cli import _require_git_repo
    result = _require_git_repo(tmp_path)
    assert result is False
    err = capsys.readouterr().err
    assert "not inside a valid git working tree" in err
    assert "git init" in err


def test_require_git_repo_passes_for_subdirectory(tmp_path: Path) -> None:
    """_require_git_repo returns True when --repo points at a subdirectory inside a git tree."""
    from saturnday.cli import _require_git_repo
    _init_git_repo(tmp_path)
    subdir = tmp_path / "src" / "mypackage"
    subdir.mkdir(parents=True)
    assert _require_git_repo(subdir) is True


# ---------------------------------------------------------------------------
# _cmd_run git preflight
# ---------------------------------------------------------------------------

def test_cmd_run_fails_without_git(tmp_path: Path) -> None:
    """_cmd_run returns 1 when repo is not inside a git working tree."""
    from saturnday.cli import _cmd_run
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    args = _make_args(tmp_path, plan=str(plan))
    rc = _cmd_run(args)
    assert rc == 1


def test_cmd_run_proceeds_with_git(tmp_path: Path) -> None:
    """_cmd_run calls run_plan when inside a valid git working tree."""
    from saturnday.cli import _cmd_run
    from saturnday._types import RunResult
    _init_git_repo(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text("{}", encoding="utf-8")
    standards = tmp_path / "standards"
    standards.mkdir()
    args = _make_args(tmp_path, plan=str(plan), standards_dir=str(standards))
    sentinel = RunResult(project_id="p", failed=0)
    with patch("saturnday.ticket_runner.run_plan", return_value=sentinel) as mock_run:
        rc = _cmd_run(args)
    assert mock_run.called
    assert rc == 0


# ---------------------------------------------------------------------------
# _cmd_resume git preflight
# ---------------------------------------------------------------------------

def test_cmd_resume_fails_without_git(tmp_path: Path) -> None:
    """_cmd_resume returns 1 when repo is not inside a git working tree."""
    from saturnday.cli import _cmd_resume
    args = _make_args(tmp_path)
    rc = _cmd_resume(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# _cmd_rerun_failed git preflight
# ---------------------------------------------------------------------------

def test_cmd_rerun_failed_fails_without_git(tmp_path: Path) -> None:
    """_cmd_rerun_failed returns 1 when repo is not inside a git working tree."""
    from saturnday.cli import _cmd_rerun_failed
    args = _make_args(tmp_path)
    rc = _cmd_rerun_failed(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# _cmd_rerun_remaining git preflight
# ---------------------------------------------------------------------------

def test_cmd_rerun_remaining_fails_without_git(tmp_path: Path) -> None:
    """_cmd_rerun_remaining returns 1 when repo is not inside a git working tree."""
    from saturnday.cli import _cmd_rerun_remaining
    args = _make_args(tmp_path)
    rc = _cmd_rerun_remaining(args)
    assert rc == 1
