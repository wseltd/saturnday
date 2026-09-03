"""Fix #1 — scope-isolation pre-attempt untracked delta.

Before this batch, :func:`saturnday.ticket_runner._git_changed_files`
returned every currently-untracked file in the repo (via
``git status --porcelain --untracked-files=all``).  The CLI-backend
scope gate at ``ticket_runner.py:3651`` then ran those paths through
``_detect_scope_violations``.  Any repo with pre-existing untracked
scratch docs, research notes, local ``CLAUDE.md``, ``prompt.md``, etc.
tripped false scope violations even when the coder touched nothing.

This batch:

1. Adds :func:`_git_untracked_snapshot` which returns the current
   ``??``-status set.
2. Widens :func:`_git_changed_files` with a
   ``pre_attempt_untracked: frozenset[str] = frozenset()`` kwarg.
   Untracked entries whose path is in that set are skipped.
3. Wires the caller in ``_run_coder_and_check_scope`` so that the
   snapshot is captured BEFORE ``call_coder`` runs.

Known limitation (documented, not fixed here): a coder that
**modifies in place** a pre-existing untracked file is blind to this
delta — the path is already in the pre-set, still shows as ``??``
after, and gets filtered out.  Catching that case requires a content
fingerprint (mtime+size or hash) and is the scope of a follow-up
batch.  Test :class:`TestKnownLimitation` pins the current
behaviour so the follow-up batch can flip it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saturnday.ticket_runner import (
    _detect_scope_violations,
    _git_changed_files,
    _git_untracked_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> None:
    """Init a git repo with a default identity + an initial commit so
    subsequent HEAD-based diffs work.  The initial commit lands a
    single benign tracked file so the tree is non-empty."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# 1. Snapshot helper
# ---------------------------------------------------------------------------


class TestUntrackedSnapshot:
    def test_empty_repo_returns_empty(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert _git_untracked_snapshot(tmp_path) == frozenset()

    def test_single_untracked_file_captured(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "prompt.md").write_text("x", encoding="utf-8")
        assert _git_untracked_snapshot(tmp_path) == frozenset({"prompt.md"})

    def test_modified_tracked_files_not_in_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """The snapshot covers ONLY untracked (``??``).  A modified tracked
        file must not appear."""
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("modified\n", encoding="utf-8")
        snap = _git_untracked_snapshot(tmp_path)
        assert "README.md" not in snap

    def test_non_git_returns_empty_not_raise(self, tmp_path: Path) -> None:
        """Best-effort: a non-git directory must return ``frozenset()``
        rather than crash.  ``_git_changed_files`` itself still raises
        ``GitStateError`` on broken git state."""
        assert _git_untracked_snapshot(tmp_path) == frozenset()


# ---------------------------------------------------------------------------
# 2. _git_changed_files with pre_attempt_untracked — the five memo cases
# ---------------------------------------------------------------------------


class TestChangedFilesWithPreAttemptDelta:
    def test_no_edits_no_new_untracked_returns_empty(
        self, tmp_path: Path,
    ) -> None:
        """Memo case 1 — pre-existing untracked prompt.md and research.md,
        no ticket edits.  Scope check must see nothing."""
        _init_repo(tmp_path)
        (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
        (tmp_path / "research.md").write_text("r", encoding="utf-8")
        pre = _git_untracked_snapshot(tmp_path)
        assert {"prompt.md", "research.md"}.issubset(pre)

        # No coder edits between snapshot and changed_files — the delta
        # is empty, so the return must be empty.
        out = _git_changed_files(tmp_path, pre_attempt_untracked=pre)
        assert out == []

    def test_new_out_of_scope_file_is_flagged(self, tmp_path: Path) -> None:
        """Memo case 2 — pre-existing prompt.md untracked, ticket creates
        a new out-of-scope src/bad.py.  Scope check must flag only
        src/bad.py, NOT prompt.md."""
        _init_repo(tmp_path)
        (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
        pre = _git_untracked_snapshot(tmp_path)
        # Ticket's fake coder now drops a new file.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "bad.py").write_text("x", encoding="utf-8")

        out = _git_changed_files(tmp_path, pre_attempt_untracked=pre)
        assert "src/bad.py" in out
        assert "prompt.md" not in out

        # Scope gate: allowed_globs = "app/**" rejects src/bad.py.
        viols = _detect_scope_violations(
            out, allowed_globs=("app/**",), forbidden_globs=(),
        )
        assert viols == ["src/bad.py"]

    def test_tracked_out_of_scope_edit_still_flagged(
        self, tmp_path: Path,
    ) -> None:
        """Memo case 3 — tracked src/in_scope.py exists, ticket edits
        tracked out-of-scope src/other.py.  Scope check must still
        flag src/other.py."""
        _init_repo(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "in_scope.py").write_text("a", encoding="utf-8")
        (tmp_path / "src" / "other.py").write_text("b", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "src/in_scope.py", "src/other.py"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "seed src/"],
            check=True, capture_output=True,
        )
        pre = _git_untracked_snapshot(tmp_path)
        # Coder edits tracked out-of-scope file.
        (tmp_path / "src" / "other.py").write_text("EDITED\n", encoding="utf-8")

        out = _git_changed_files(tmp_path, pre_attempt_untracked=pre)
        assert "src/other.py" in out

        viols = _detect_scope_violations(
            out,
            allowed_globs=("src/in_scope.py",),
            forbidden_globs=(),
        )
        assert viols == ["src/other.py"]

    def test_new_in_scope_file_passes(self, tmp_path: Path) -> None:
        """Memo case 4 — pre-existing untracked notes.md, ticket creates
        allowed in-scope file only.  Scope check passes."""
        _init_repo(tmp_path)
        (tmp_path / "notes.md").write_text("n", encoding="utf-8")
        pre = _git_untracked_snapshot(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "feature.py").write_text("x", encoding="utf-8")

        out = _git_changed_files(tmp_path, pre_attempt_untracked=pre)
        assert "src/feature.py" in out
        assert "notes.md" not in out

        viols = _detect_scope_violations(
            out, allowed_globs=("src/**",), forbidden_globs=(),
        )
        assert viols == []


# ---------------------------------------------------------------------------
# 3. Regression pin — pre-batch behaviour preserved when no delta passed
# ---------------------------------------------------------------------------


class TestRegressionPreservesPreBatchBehaviour:
    def test_default_empty_set_matches_pre_batch_output(
        self, tmp_path: Path,
    ) -> None:
        """Memo case 5 — callers that don't pass pre_attempt_untracked
        must see identical behaviour to the pre-batch function.  This
        covers the secondary call site in ticket_runner.py:4189 (the
        post-execution proof resolver) which does not participate in
        pre/post delta accounting."""
        _init_repo(tmp_path)
        # A mix: one pre-existing untracked file + one new untracked file.
        (tmp_path / "old_prompt.md").write_text("o", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "feature.py").write_text("n", encoding="utf-8")

        # No pre_attempt_untracked argument → both files returned, like
        # before the fix.
        out = _git_changed_files(tmp_path)
        assert set(out) == {"old_prompt.md", "src/feature.py"}


# ---------------------------------------------------------------------------
# 4. Known limitation — in-place modification of pre-existing untracked
# ---------------------------------------------------------------------------


class TestKnownLimitation:
    def test_in_place_modification_of_pre_existing_untracked_not_detected(
        self, tmp_path: Path,
    ) -> None:
        """DOCUMENTED LIMITATION of the minimum-viable fix.

        If the coder modifies a pre-existing untracked file IN PLACE
        (no new path created), the set-delta is blind to it: the path
        appears in both the pre-set and the post-set.  Content
        fingerprinting (mtime+size or hash) is required to catch this,
        and is the scope of the follow-up Fix #1.a batch.

        This test pins the current behaviour so the follow-up batch
        can flip it without touching anything else.
        """
        _init_repo(tmp_path)
        (tmp_path / "prompt.md").write_text("original\n", encoding="utf-8")
        pre = _git_untracked_snapshot(tmp_path)
        # Coder modifies the untracked file in place.
        (tmp_path / "prompt.md").write_text("edited\n", encoding="utf-8")

        out = _git_changed_files(tmp_path, pre_attempt_untracked=pre)
        # Minimum-viable fix: does NOT detect in-place modification.
        # When Fix #1.a ships, this assertion inverts.
        assert "prompt.md" not in out, (
            "Fix #1 set-delta cannot see in-place modification by design; "
            "follow-up Fix #1.a adds content fingerprinting. If this "
            "assertion starts failing, update the test rather than the fix."
        )


# ---------------------------------------------------------------------------
# 5. Commits-since-head_before path still works alongside the delta
# ---------------------------------------------------------------------------


class TestHeadBeforePathCoexists:
    def test_commits_since_head_collected_regardless_of_delta(
        self, tmp_path: Path,
    ) -> None:
        """The scope gate relies on BOTH the working-tree diff (filtered
        by the delta) AND the diff of commits made since ``head_before``.
        Commits are tracked; the untracked-delta filter must not touch
        them."""
        _init_repo(tmp_path)
        pre = _git_untracked_snapshot(tmp_path)
        # Capture HEAD before the simulated coder commit.
        head_before = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Coder stages + commits a new file (CLI-backend pattern).
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "added.py").write_text("x", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "src/added.py"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "[T001] add"],
            check=True, capture_output=True,
        )

        out = _git_changed_files(
            tmp_path,
            head_before=head_before,
            pre_attempt_untracked=pre,
        )
        assert "src/added.py" in out
