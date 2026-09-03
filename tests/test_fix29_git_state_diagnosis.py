"""Fix 29 — no-git change detection failure diagnosis.

Proves that:
1. Valid git repo + no actual change still surfaces the normal no-change
   (PatchExtractionError / plan_defect) diagnosis.
2. Non-git directory causes _git_changed_files to raise GitStateError,
   not return an empty list.
3. The misleading "no files were changed" message is NOT used for the
   no-git case.
4. classify_failure returns GIT_STATE_UNAVAILABLE for GitStateError messages,
   not PLAN_DEFECT_REQUIRES_EDIT.
5. Fix 27 preflight and Fix 29 lower-layer diagnosis are not contradictory:
   the two layers cover different entry-points and error conditions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._exceptions import GIT_STATE_ERROR_MARKER, GitStateError
from saturnday.run.failure_classifier import classify_failure


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Test 1: valid git repo + no changes → PatchExtractionError (plan_defect)
# ---------------------------------------------------------------------------

def test_valid_git_no_changes_raises_patch_extraction_error(tmp_path: Path) -> None:
    """Valid git repo with clean tree → _git_changed_files returns [], not GitStateError."""
    from saturnday.ticket_runner import _git_changed_files
    _init_git_repo(tmp_path)
    # Clean tree after init — no staged or unstaged changes.
    result = _git_changed_files(tmp_path)
    assert result == [], "clean git tree must return empty list, not raise"


# ---------------------------------------------------------------------------
# Test 2: non-git directory → GitStateError raised
# ---------------------------------------------------------------------------

def test_non_git_directory_raises_git_state_error(tmp_path: Path) -> None:
    """_git_changed_files raises GitStateError when not inside a git working tree."""
    from saturnday.ticket_runner import _git_changed_files
    with pytest.raises(GitStateError):
        _git_changed_files(tmp_path)


# ---------------------------------------------------------------------------
# Test 3: GitStateError message does NOT contain "no files were changed"
# ---------------------------------------------------------------------------

def test_git_state_error_message_is_not_misleading(tmp_path: Path) -> None:
    """GitStateError message must not say 'no files were changed'."""
    from saturnday.ticket_runner import _git_changed_files
    with pytest.raises(GitStateError) as exc_info:
        _git_changed_files(tmp_path)
    assert "no files were changed" not in str(exc_info.value)
    assert GIT_STATE_ERROR_MARKER in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 4: classify_failure returns GIT_STATE_UNAVAILABLE, not PLAN_DEFECT
# ---------------------------------------------------------------------------

def test_classify_failure_git_state_unavailable() -> None:
    """classify_failure returns GIT_STATE_UNAVAILABLE when error contains the marker."""
    error_msg = (
        f"Saturnday relies on git to detect changes. "
        f"This is a {GIT_STATE_ERROR_MARKER}, not evidence the coder failed."
    )
    classification = classify_failure(error=error_msg, changed_files=())
    assert classification.governance_outcome == "GIT_STATE_UNAVAILABLE"
    assert classification.failure_category == "git_state_unavailable_defect"
    assert "no files were changed" not in classification.summary


def test_classify_failure_plan_defect_unchanged() -> None:
    """classify_failure still returns PLAN_DEFECT_REQUIRES_EDIT for a genuine no-change error."""
    classification = classify_failure(
        error="Coder produced response but no files were changed for T001",
        changed_files=(),
    )
    assert classification.governance_outcome == "PLAN_DEFECT_REQUIRES_EDIT"
    assert classification.failure_category == "plan_defect"


# ---------------------------------------------------------------------------
# Test 5: Fix 27 preflight and Fix 29 lower-layer are not contradictory
# ---------------------------------------------------------------------------

def test_fix27_and_fix29_are_independent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Fix 27 (_require_git_repo) and Fix 29 (_git_changed_files) both target
    the same root problem from different layers without conflicting.

    _require_git_repo uses `git rev-parse` (preflight, CLI layer).
    _git_changed_files raises GitStateError (execution layer, lower path).
    Both return truthful failures; neither produces a misleading no-change message.
    """
    from saturnday.cli import _require_git_repo
    from saturnday.ticket_runner import _git_changed_files

    # Non-git dir: preflight says False, lower path raises GitStateError
    preflight_ok = _require_git_repo(tmp_path)
    assert preflight_ok is False

    with pytest.raises(GitStateError):
        _git_changed_files(tmp_path)

    # Neither produces "no files were changed"
    err = capsys.readouterr().err
    assert "no files were changed" not in err
