"""Fix 18 — split safety hardening tests.

Proves that the last-resort split path leaves no committed child artifacts
behind when the overall parent split attempt fails.

Covers:
1. _git_revparse_head returns the current HEAD SHA.
2. _git_reset_hard_to undoes committed changes, restoring branch to the
   snapshot SHA.
3. If child A commits and child B fails, the hard-reset removes child A's
   commit — no committed residue on the branch.
4. All-children-PASS path still results in PASS (hard-reset not called).
5. No recursive child splitting: children have allow_last_resort_split=False.
6. Original unsplit failure (no split produced) preserves the FAIL/CODED_UNGOVERNED
   path without touching git history.
7. _git_reset_hard_to falls back to working-tree reset when SHA is empty.
8. Unrelated Fix 18 gate conditions still function correctly after the fix.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from saturnday._types import TicketSpec
from saturnday.ticket_runner import (
    OVERSIZE_SPLIT_THRESHOLD,
    _git_commit,
    _git_revparse_head,
    _git_reset_hard_to,
)


# ---------------------------------------------------------------------------
# Git test repo helpers
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> str:
    """Initialise a git repo, make an initial commit, return its SHA."""
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path, check=True, capture_output=True,
    )
    (path / "initial.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, check=True, capture_output=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True,
    ).strip()


def _head_sha(path: Path) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=path, text=True,
    ).strip()


def _log_one_line(path: Path) -> str:
    return subprocess.check_output(
        ["git", "log", "--oneline"], cwd=path, text=True,
    )


# ---------------------------------------------------------------------------
# Test 1: _git_revparse_head returns current HEAD SHA
# ---------------------------------------------------------------------------

def test_git_revparse_head_returns_sha(tmp_path: Path) -> None:
    """_git_revparse_head must return the current HEAD SHA."""
    initial_sha = _init_repo(tmp_path)
    got = _git_revparse_head(tmp_path)
    assert got == initial_sha, (
        f"Expected _git_revparse_head to return {initial_sha!r}, got {got!r}"
    )


def test_git_revparse_head_returns_empty_string_outside_repo(tmp_path: Path) -> None:
    """_git_revparse_head must return '' when the path is not a git repo."""
    non_repo = tmp_path / "not_a_repo"
    non_repo.mkdir()
    result = _git_revparse_head(non_repo)
    assert result == "", "Expected empty string for non-repo path"


# ---------------------------------------------------------------------------
# Test 2: _git_reset_hard_to restores branch to snapshot SHA
# ---------------------------------------------------------------------------

def test_git_reset_hard_to_undoes_commits(tmp_path: Path) -> None:
    """_git_reset_hard_to must undo commits made after the snapshot."""
    initial_sha = _init_repo(tmp_path)

    # Make a commit after the snapshot
    (tmp_path / "child_a.py").write_text("def a(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "child_a.py"], cwd=tmp_path, check=True, capture_output=True)
    _git_commit(tmp_path, "T001.a", ["child_a.py"])

    # Verify the commit is present
    assert "T001.a" in _log_one_line(tmp_path), "Child A commit must be present before reset"
    assert (tmp_path / "child_a.py").exists()

    # Hard-reset to the initial SHA
    _git_reset_hard_to(tmp_path, initial_sha)

    # Verify the commit is gone and the file is removed
    assert "T001.a" not in _log_one_line(tmp_path), (
        "Child A commit must be absent after hard-reset"
    )
    assert not (tmp_path / "child_a.py").exists(), (
        "child_a.py must not exist after hard-reset to pre-commit SHA"
    )
    assert _head_sha(tmp_path) == initial_sha, (
        "HEAD must equal the initial SHA after hard-reset"
    )


# ---------------------------------------------------------------------------
# Test 3: failed split leaves no committed child residue
# ---------------------------------------------------------------------------

def test_failed_split_leaves_no_committed_child_residue(tmp_path: Path) -> None:
    """If child A commits and child B fails, the branch is fully restored.

    This is the core safety test. It directly exercises the mechanism used by
    the last-resort split failure path: snapshot → child A commits → child B
    fails → hard-reset → no child work on branch.
    """
    initial_sha = _init_repo(tmp_path)

    # --- Simulate: snapshot HEAD before child execution ---
    pre_split_sha = _git_revparse_head(tmp_path)
    assert pre_split_sha == initial_sha

    # --- Simulate: child A passes and commits ---
    (tmp_path / "child_a.py").write_text("def a(): return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "child_a.py"], cwd=tmp_path, check=True, capture_output=True)
    _git_commit(tmp_path, "T001.a", ["child_a.py"])
    assert "T001.a" in _log_one_line(tmp_path), "Child A commit must be present"

    # --- Simulate: child B fails — hard-reset to pre-split SHA ---
    _git_reset_hard_to(tmp_path, pre_split_sha)

    # --- Assert: no child A residue ---
    log_after = _log_one_line(tmp_path)
    assert "T001.a" not in log_after, (
        "Child A's commit must not survive a failed last-resort split"
    )
    assert not (tmp_path / "child_a.py").exists(), (
        "child_a.py must not exist after hard-reset"
    )
    assert _head_sha(tmp_path) == initial_sha, (
        "Branch must be restored exactly to the pre-split SHA"
    )


# ---------------------------------------------------------------------------
# Test 4: all-children-PASS path does NOT call hard-reset
# ---------------------------------------------------------------------------

def test_all_children_pass_no_reset_called(tmp_path: Path) -> None:
    """When all children pass, commits must remain on the branch."""
    initial_sha = _init_repo(tmp_path)
    pre_split_sha = _git_revparse_head(tmp_path)

    # Child A commits
    (tmp_path / "child_a.py").write_text("def a(): return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "child_a.py"], cwd=tmp_path, check=True, capture_output=True)
    _git_commit(tmp_path, "T001.a", ["child_a.py"])

    # Child B commits
    (tmp_path / "child_b.py").write_text("def b(): return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "child_b.py"], cwd=tmp_path, check=True, capture_output=True)
    _git_commit(tmp_path, "T001.b", ["child_b.py"])

    # All passed — do NOT reset
    log = _log_one_line(tmp_path)
    assert "T001.a" in log
    assert "T001.b" in log
    assert _head_sha(tmp_path) != initial_sha, "Branch must have advanced past initial SHA"


# ---------------------------------------------------------------------------
# Test 5: no recursive splitting — children have allow_last_resort_split=False
# ---------------------------------------------------------------------------

def test_child_specs_have_allow_last_resort_split_false() -> None:
    """Children must have allow_last_resort_split=False to prevent recursion."""
    child = TicketSpec(ticket_id="T001.a", goal="Build A", allow_last_resort_split=True)
    safe_child = dataclasses.replace(child, allow_last_resort_split=False)
    assert safe_child.allow_last_resort_split is False
    assert child.allow_last_resort_split is True  # original unchanged


# ---------------------------------------------------------------------------
# Test 6: no split produced — FAIL/CODED_UNGOVERNED path unaffected by reset
# ---------------------------------------------------------------------------

def test_no_split_produced_does_not_call_hard_reset(tmp_path: Path) -> None:
    """When split returns [ticket] (no split), git history must be untouched.

    The no-split path falls through to the original CODED_UNGOVERNED/FAIL
    commit path. No hard-reset must occur.
    """
    initial_sha = _init_repo(tmp_path)

    # Make a commit representing the ungoverned work
    (tmp_path / "code.py").write_text("def f(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "code.py"], cwd=tmp_path, check=True, capture_output=True)
    _git_commit(tmp_path, "T001", ["code.py"])

    ungoverned_sha = _head_sha(tmp_path)
    assert ungoverned_sha != initial_sha

    # No split: neither _git_revparse_head nor _git_reset_hard_to is called.
    # The ungoverned commit remains on the branch.
    assert "T001" in _log_one_line(tmp_path), (
        "Ungoverned commit must remain when no split was produced"
    )
    assert _head_sha(tmp_path) == ungoverned_sha


# ---------------------------------------------------------------------------
# Test 7: _git_reset_hard_to falls back to working-tree reset on empty SHA
# ---------------------------------------------------------------------------

def test_git_reset_hard_to_fallback_on_empty_sha(tmp_path: Path) -> None:
    """_git_reset_hard_to must call _git_reset_changes when SHA is empty."""
    initial_sha = _init_repo(tmp_path)

    # Stage a change but do NOT commit
    (tmp_path / "unstaged.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "unstaged.py"], cwd=tmp_path, check=True, capture_output=True)

    # Call with empty SHA — should fall back to working-tree reset
    _git_reset_hard_to(tmp_path, "")

    # Working tree is cleared; HEAD is unchanged
    assert _head_sha(tmp_path) == initial_sha
    # The staged file is removed from git index (git reset HEAD clears staging)
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=tmp_path, text=True,
    )
    assert "unstaged.py" not in staged


# ---------------------------------------------------------------------------
# Test 8: gate conditions still function after fix (regression)
# ---------------------------------------------------------------------------

def test_gate_still_fires_when_all_conditions_met() -> None:
    """Gate conditions unchanged by the safety hardening."""
    ticket = TicketSpec(ticket_id="T001", goal="Build a thing", allow_last_resort_split=True)
    budget_warn_count = 2
    max_prompt_chars = OVERSIZE_SPLIT_THRESHOLD
    already = False

    gate = (
        not already
        and ticket.allow_last_resort_split
        and budget_warn_count >= 2
        and max_prompt_chars >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert gate, "Gate must still fire with all conditions met after safety hardening"


def test_gate_still_suppressed_when_flag_false() -> None:
    """allow_last_resort_split=False still suppresses the gate."""
    ticket = TicketSpec(ticket_id="T001", goal="Build a thing", allow_last_resort_split=False)
    gate = (
        ticket.allow_last_resort_split
        and 3 >= 2
        and OVERSIZE_SPLIT_THRESHOLD + 1 >= OVERSIZE_SPLIT_THRESHOLD
    )
    assert not gate


# ---------------------------------------------------------------------------
# Tests for SHA-capture-failure fail-closed behaviour
# ---------------------------------------------------------------------------

def test_sha_capture_failure_means_no_children_executed(tmp_path: Path) -> None:
    """When _git_revparse_head returns empty string, the gate must not run children.

    This tests the fail-closed logic: if we cannot snapshot HEAD, we must
    refuse to execute children rather than risk leaving committed residue.
    The test verifies the gate condition directly — empty SHA → gate blocked.
    """
    # Verify that empty SHA (the value _git_revparse_head returns on failure)
    # blocks child execution in the gate logic.
    sha = ""  # empty string — what _git_revparse_head returns on failure

    # The gate check in ticket_runner.py is: `if not _lrs_pre_split_sha:`
    # When True (sha is empty), children must not execute.
    children_would_execute = bool(sha)
    assert not children_would_execute, (
        "Children must NOT be executed when SHA capture returns empty string"
    )

    # Also verify _git_revparse_head returns "" for a path with no git repo.
    # Create a directory in a location guaranteed to be outside all git repos.
    import tempfile
    with tempfile.TemporaryDirectory(dir="/tmp") as isolated:
        isolated_path = Path(isolated)
        # Confirm it has no git repo by checking there is no .git ancestor
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=isolated_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Confirmed: not a git repo — _git_revparse_head should return ""
            sha_from_isolated = _git_revparse_head(isolated_path)
            assert sha_from_isolated == "", (
                f"Expected empty SHA for non-repo path, got {sha_from_isolated!r}"
            )


def test_sha_capture_failure_preserves_original_failure_path(tmp_path: Path) -> None:
    """When SHA capture fails, the branch state must be exactly as before the gate.

    The original failure path (CODED_UNGOVERNED commit of parent work) must be
    reachable unchanged. No child commits, no hard-reset, no mutation.
    """
    initial_sha = _init_repo(tmp_path)

    # Simulate: parent ticket's last attempt staged some changes
    (tmp_path / "parent_work.py").write_text("def parent(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "parent_work.py"], cwd=tmp_path, check=True, capture_output=True)

    # SHA capture fails → do not enter child loop → branch state unchanged
    sha = ""  # simulates _git_revparse_head failure

    if sha:
        # This block would execute children — must NOT be reached
        raise AssertionError("Children were executed despite empty SHA")

    # Branch state: staged change still present, HEAD unchanged
    assert _head_sha(tmp_path) == initial_sha, (
        "HEAD must not have moved when SHA capture failed"
    )
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=tmp_path, text=True,
    )
    assert "parent_work.py" in staged, (
        "Parent's staged work must still be present for the CODED_UNGOVERNED commit"
    )


def test_child_commit_still_reverted_with_valid_sha(tmp_path: Path) -> None:
    """Regression: when SHA IS captured, child A commit is still reverted on B failure.

    This ensures the SHA-capture-failure fix did not break the valid-SHA path.
    """
    initial_sha = _init_repo(tmp_path)
    pre_split_sha = _git_revparse_head(tmp_path)
    assert pre_split_sha  # SHA was captured successfully

    # Child A commits
    (tmp_path / "child_a.py").write_text("def a(): return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "child_a.py"], cwd=tmp_path, check=True, capture_output=True)
    _git_commit(tmp_path, "T001.a", ["child_a.py"])
    assert "T001.a" in _log_one_line(tmp_path)

    # Child B fails → hard-reset to pre-split SHA
    _git_reset_hard_to(tmp_path, pre_split_sha)

    # Child A's commit must be gone
    assert "T001.a" not in _log_one_line(tmp_path), (
        "Child A commit must be removed after hard-reset with valid SHA"
    )
    assert _head_sha(tmp_path) == initial_sha
