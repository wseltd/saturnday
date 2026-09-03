"""Fix #3 — baseline generation on no-Python repos.

Before this batch, ``saturnday.cli._handle_baseline_generate`` listed
only tracked ``*.py`` files and aborted with ``"No Python files found
in repo"`` when that list was empty.  That silently kept docs-heavy,
scaffold-stage, and pre-first-Python-file repos locked out of
baseline generation — while the pre-commit hook, which enforces a
broader surface (secrets in ``.md``, missing ``README``, missing
``LICENSE``, etc.), still blocked commits on those same findings.

The fix widens the generator's tracked-file surface to all tracked
files (``git ls-files`` with no filter) AND removes the empty-list
short-circuit entirely.  An empty ``files`` list is now a valid
baseline input — repo-level checks (``_check_license``,
``_check_readme``, …) still fire in ``run_review`` regardless of
``changed_files``, so the resulting baseline can still capture them.

Also pinned: the existing Python-repo baseline path is preserved.
"""
from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path

import pytest

from saturnday import cli as cli_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=root, check=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)


def _commit_all(root: Path, msg: str = "seed") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True)


def _baseline_args(repo: Path, output_path: Path) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        repo=str(repo),
        policy=None,
        output=str(output_path),
        enforcement_date="",
        ratchet_mode="block_new",
        verbose=False,
        quiet=False,
    )


# ---------------------------------------------------------------------------
# 1. The pre-fix short-circuit is gone
# ---------------------------------------------------------------------------


class TestNoPythonShortCircuitRemoved:
    def test_docs_only_repo_generates_baseline_not_exit_2(
        self, tmp_path: Path, capsys,
    ) -> None:
        """A repo containing only Markdown/reference files must now
        produce a baseline.  Before the fix this returned 2 with the
        'No Python files found in repo' message."""
        repo = tmp_path / "repo"
        _git_init_repo(repo)
        (repo / "research-and-resources.md").write_text(
            "notes\n", encoding="utf-8",
        )
        (repo / "prompt.md").write_text("prompt\n", encoding="utf-8")
        _commit_all(repo)

        baseline_path = tmp_path / "baseline.json"
        rc = cli_mod._handle_baseline_generate(
            _baseline_args(repo, baseline_path),
        )
        assert rc == 0, "baseline generation must succeed on a docs-only repo"
        assert baseline_path.is_file()
        out = capsys.readouterr().out
        # The pre-fix message must no longer appear.
        assert "No Python files found in repo" not in out

    def test_empty_tracked_repo_generates_valid_baseline(
        self, tmp_path: Path,
    ) -> None:
        """A git repo with zero tracked files should still produce a
        baseline — repo-level checks can still register findings, and
        an empty findings set is itself a valid baseline.  Before the
        fix, this returned 2."""
        repo = tmp_path / "repo"
        _git_init_repo(repo)
        # No files committed at all — git ls-files returns empty.
        baseline_path = tmp_path / "baseline.json"
        rc = cli_mod._handle_baseline_generate(
            _baseline_args(repo, baseline_path),
        )
        assert rc == 0
        assert baseline_path.is_file()
        # The JSON is well-formed.
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert "findings" in data
        assert "ratchet_mode" in data


# ---------------------------------------------------------------------------
# 2. Generator surface now covers all tracked files, not just *.py
# ---------------------------------------------------------------------------


class TestGeneratorSurfaceIsAllTrackedFiles:
    def test_markdown_secret_lands_in_baseline(self, tmp_path: Path) -> None:
        """A plausible-secret string in a tracked Markdown file must
        appear in the generated baseline.  Before the fix the file
        wouldn't even be handed to _scan_secrets because git ls-files
        was filtered to *.py."""
        repo = tmp_path / "repo"
        _git_init_repo(repo)
        # Use a 40-char hex-like string that the secret regex will
        # pick up.  Not a real secret — just plausibly secret-shaped.
        fake_secret = "AKIA" + "B" * 36
        (repo / "notes.md").write_text(
            f"secret-ish: {fake_secret}\n", encoding="utf-8",
        )
        _commit_all(repo)

        baseline_path = tmp_path / "baseline.json"
        rc = cli_mod._handle_baseline_generate(
            _baseline_args(repo, baseline_path),
        )
        assert rc == 0
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        # The contract under test: the scan surface now reaches
        # non-Python tracked files, evidenced by at least one
        # secrets-rule finding landing in the baseline.  The
        # fingerprint representation is intentionally path-less
        # (snippet_hash), so we match on rule_id rather than raw
        # path substring.
        findings = data.get("findings", [])
        secret_fingerprints = [
            f for f in findings if f.get("rule_id") == "secrets"
        ]
        assert secret_fingerprints, (
            "expected at least one secrets-rule fingerprint in the "
            "baseline — if this fails, the generator's scan surface "
            "regressed to Python-only"
        )


# ---------------------------------------------------------------------------
# 3. Regression — Python-repo path preserved
# ---------------------------------------------------------------------------


class TestPythonRepoPathPreserved:
    def test_python_repo_still_generates_baseline(
        self, tmp_path: Path, capsys,
    ) -> None:
        """The existing behaviour for Python repos (the dominant pre-fix
        case) must still work.  Baseline is created, the file exists,
        exit code 0."""
        repo = tmp_path / "repo"
        _git_init_repo(repo)
        (repo / "main.py").write_text("def foo(): pass\n", encoding="utf-8")
        (repo / "README.md").write_text("hi\n", encoding="utf-8")
        _commit_all(repo)

        baseline_path = tmp_path / "baseline.json"
        rc = cli_mod._handle_baseline_generate(
            _baseline_args(repo, baseline_path),
        )
        assert rc == 0
        assert baseline_path.is_file()
        out = capsys.readouterr().out
        # Still reports "Scanning N files" for non-empty tracked sets.
        assert "Scanning" in out


# ---------------------------------------------------------------------------
# 4. Hook-install bootstrap end-to-end on a docs-only repo
# ---------------------------------------------------------------------------


class TestHookInstallOnDocsOnlyRepo:
    def test_hook_install_creates_baseline_on_docs_only_repo(
        self, tmp_path: Path, capsys, monkeypatch,
    ) -> None:
        """Before the fix, ``saturnday hook install`` on a repo with
        only docs would leave no baseline (bootstrap fails with
        ``No Python files found in repo``).  After the fix the
        baseline is created."""
        repo = tmp_path / "repo"
        _git_init_repo(repo)
        (repo / "research-and-resources.md").write_text(
            "notes\n", encoding="utf-8",
        )
        _commit_all(repo)
        assert not (repo / ".saturnday-baseline.json").exists()

        args = types.SimpleNamespace(
            hook_action="install",
            repo=str(repo),
            verbose=False,
            quiet=False,
        )

        # Stub shutil.which so the hook renderer resolves to a
        # concrete path regardless of the test host's PATH.
        monkeypatch.setattr(
            "shutil.which", lambda _name: "/opt/venv/bin/saturnday",
        )

        rc = cli_mod._handle_hook(args)
        assert rc == 0
        assert (repo / ".saturnday-baseline.json").is_file(), (
            "hook install must bootstrap a baseline even on a docs-only repo"
        )
        # The pre-fix message must NOT appear.
        out = capsys.readouterr().out
        assert "No Python files found in repo" not in out


# ---------------------------------------------------------------------------
# 5. Non-git directory still refuses cleanly
# ---------------------------------------------------------------------------


class TestNonGitDirectory:
    def test_non_git_dir_still_returns_2(self, tmp_path: Path, capsys) -> None:
        """A non-git directory is still a clean refusal with rc=2.
        This check predates Fix #3 and must be preserved — we only
        removed the 'no Python files' short-circuit, not the 'not a
        git repo' guard."""
        baseline_path = tmp_path / "baseline.json"
        rc = cli_mod._handle_baseline_generate(
            _baseline_args(tmp_path, baseline_path),
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "Not a git repository" in err
