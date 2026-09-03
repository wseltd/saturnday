"""Tests for artefact containment and git-hygiene changes.

Covers:
- _ensure_git_exclude  (ticket_runner)
- save_state / load_state migration  (project_state)
- _handle_init_gitignore  (cli)
"""

import argparse
import subprocess
from pathlib import Path

import pytest

from saturnday.ticket_runner import _ensure_git_exclude
from saturnday.project_state import save_state, load_state, ProjectState
from saturnday.cli import _handle_init_gitignore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_init(path: Path) -> None:
    """Initialise a bare git repo at *path* (no commits needed)."""
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
    )


def _make_args(repo: Path) -> argparse.Namespace:
    """Return a minimal Namespace suitable for _handle_init_gitignore."""
    return argparse.Namespace(repo=str(repo))


def _make_project_state(**kwargs: object) -> ProjectState:
    """Return a minimal ProjectState, overriding any fields via kwargs."""
    state = ProjectState(project_id="test-proj-001", last_updated="2026-03-30")
    for k, v in kwargs.items():
        setattr(state, k, v)
    return state


# ---------------------------------------------------------------------------
# A. _ensure_git_exclude
# ---------------------------------------------------------------------------

class TestEnsureGitExclude:
    """Tests for _ensure_git_exclude from saturnday.ticket_runner."""

    def test_creates_exclude_with_saturnday_entry(self, tmp_path: Path) -> None:
        """Creates .git/info/exclude with .saturnday/ entry in a git repo."""
        _git_init(tmp_path)
        _ensure_git_exclude(tmp_path)

        exclude = tmp_path / ".git" / "info" / "exclude"
        assert exclude.is_file(), "exclude file should have been created"
        content = exclude.read_text(encoding="utf-8")
        assert ".saturnday/" in content

    def test_does_not_modify_tracked_gitignore(self, tmp_path: Path) -> None:
        """If .gitignore exists it must be completely unchanged after the call."""
        _git_init(tmp_path)
        gitignore = tmp_path / ".gitignore"
        original = "*.pyc\n__pycache__/\n"
        gitignore.write_text(original, encoding="utf-8")

        _ensure_git_exclude(tmp_path)

        assert gitignore.read_text(encoding="utf-8") == original

    def test_does_not_create_gitignore(self, tmp_path: Path) -> None:
        """Even when .gitignore is absent, the function must not create one."""
        _git_init(tmp_path)
        gitignore = tmp_path / ".gitignore"
        assert not gitignore.exists()

        _ensure_git_exclude(tmp_path)

        assert not gitignore.exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        """Running twice must not duplicate entries in .git/info/exclude."""
        _git_init(tmp_path)
        _ensure_git_exclude(tmp_path)
        _ensure_git_exclude(tmp_path)

        exclude = tmp_path / ".git" / "info" / "exclude"
        content = exclude.read_text(encoding="utf-8")
        assert content.count(".saturnday/") == 1

    def test_handles_non_git_directory_gracefully(self, tmp_path: Path) -> None:
        """Must not raise when called on a directory with no .git."""
        # tmp_path is not a git repo — should return silently
        _ensure_git_exclude(tmp_path)
        # No .git/info/exclude should be created
        assert not (tmp_path / ".git").exists()

    def test_excludes_saturnday_dir_only_not_policy_or_claude(
        self, tmp_path: Path
    ) -> None:
        """.saturnday-policy.yaml and CLAUDE.md must NOT appear in exclude."""
        _git_init(tmp_path)
        _ensure_git_exclude(tmp_path)

        exclude = tmp_path / ".git" / "info" / "exclude"
        content = exclude.read_text(encoding="utf-8")
        assert ".saturnday-policy.yaml" not in content, (
            ".saturnday-policy.yaml should not be auto-excluded"
        )
        assert "CLAUDE.md" not in content, (
            "CLAUDE.md should not be auto-excluded"
        )

    def test_works_when_exclude_already_has_content(self, tmp_path: Path) -> None:
        """Saturnday entry is appended after existing exclude content."""
        _git_init(tmp_path)
        info_dir = tmp_path / ".git" / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude = info_dir / "exclude"
        exclude.write_text("# pre-existing entry\n*.swp\n", encoding="utf-8")

        _ensure_git_exclude(tmp_path)

        content = exclude.read_text(encoding="utf-8")
        assert "*.swp" in content, "pre-existing content must be preserved"
        assert ".saturnday/" in content


# ---------------------------------------------------------------------------
# B. save_state / load_state migration
# ---------------------------------------------------------------------------

class TestStateFileMigration:
    """Tests for save_state / load_state in saturnday.project_state."""

    def test_save_writes_to_saturnday_subdir(self, tmp_path: Path) -> None:
        """save_state writes to .saturnday/state.json."""
        state = _make_project_state()
        save_state(state, tmp_path)

        new_path = tmp_path / ".saturnday" / "state.json"
        assert new_path.is_file(), ".saturnday/state.json should be created"

    def test_save_does_not_write_to_repo_root(self, tmp_path: Path) -> None:
        """save_state must not write saturnday-state.json to the repo root."""
        state = _make_project_state()
        save_state(state, tmp_path)

        legacy = tmp_path / "saturnday-state.json"
        assert not legacy.exists(), "legacy root-level state file must not be created"

    def test_save_removes_legacy_state_file(self, tmp_path: Path) -> None:
        """save_state removes legacy saturnday-state.json when it exists."""
        legacy = tmp_path / "saturnday-state.json"
        legacy.write_text('{"schema_version": "1.0.0", "project_id": "old"}', encoding="utf-8")
        assert legacy.exists()

        state = _make_project_state()
        save_state(state, tmp_path)

        assert not legacy.exists(), "legacy state file should be cleaned up after save"

    def test_load_reads_from_new_location(self, tmp_path: Path) -> None:
        """load_state reads from .saturnday/state.json."""
        state = _make_project_state(project_id="new-loc-001")
        save_state(state, tmp_path)

        loaded = load_state(tmp_path)
        assert loaded is not None
        assert loaded.project_id == "new-loc-001"

    def test_load_falls_back_to_legacy(self, tmp_path: Path) -> None:
        """load_state falls back to legacy saturnday-state.json when new location absent."""
        import json

        legacy_data = {
            "schema_version": "1.0.0",
            "project_id": "legacy-proj",
            "last_updated": "",
            "modules": {},
            "tests": {},
            "dependencies": {},
            "decisions": [],
        }
        legacy = tmp_path / "saturnday-state.json"
        legacy.write_text(json.dumps(legacy_data), encoding="utf-8")

        # Ensure new location is absent
        assert not (tmp_path / ".saturnday" / "state.json").exists()

        loaded = load_state(tmp_path)
        assert loaded is not None
        assert loaded.project_id == "legacy-proj"

    def test_load_returns_none_when_neither_exists(self, tmp_path: Path) -> None:
        """load_state returns None when no state file exists at either location."""
        loaded = load_state(tmp_path)
        assert loaded is None

    def test_new_location_takes_precedence_over_legacy(self, tmp_path: Path) -> None:
        """load_state prefers .saturnday/state.json over legacy root file."""
        import json

        # Write a legacy file with a distinct project_id
        legacy_data = {
            "schema_version": "1.0.0",
            "project_id": "legacy-stale",
            "last_updated": "",
            "modules": {},
            "tests": {},
            "dependencies": {},
            "decisions": [],
        }
        (tmp_path / "saturnday-state.json").write_text(
            json.dumps(legacy_data), encoding="utf-8"
        )

        # Write a new-location file with a different project_id
        sat_dir = tmp_path / ".saturnday"
        sat_dir.mkdir(parents=True, exist_ok=True)
        new_data = dict(legacy_data)
        new_data["project_id"] = "new-canonical"
        (sat_dir / "state.json").write_text(json.dumps(new_data), encoding="utf-8")

        loaded = load_state(tmp_path)
        assert loaded is not None
        assert loaded.project_id == "new-canonical"

    def test_save_state_roundtrip(self, tmp_path: Path) -> None:
        """save then load preserves key fields."""
        state = _make_project_state(
            project_id="roundtrip-99",
            last_updated="2026-03-30",
        )
        state.dependencies = {"requests": "2.31.0"}
        state.decisions = ["chose-fastapi"]

        save_state(state, tmp_path)
        loaded = load_state(tmp_path)

        assert loaded is not None
        assert loaded.project_id == "roundtrip-99"
        assert loaded.last_updated == "2026-03-30"
        assert loaded.dependencies == {"requests": "2.31.0"}
        assert loaded.decisions == ["chose-fastapi"]


# ---------------------------------------------------------------------------
# C. _handle_init_gitignore
# ---------------------------------------------------------------------------

class TestHandleInitGitignore:
    """Tests for _handle_init_gitignore from saturnday.cli."""

    def test_appends_to_existing_gitignore(self, tmp_path: Path) -> None:
        """Appends Saturnday entries to an existing .gitignore."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n", encoding="utf-8")

        rc = _handle_init_gitignore(_make_args(tmp_path))

        assert rc == 0
        content = gitignore.read_text(encoding="utf-8")
        assert "*.pyc" in content, "original content must be preserved"
        assert ".saturnday/" in content, "Saturnday entry must be appended"

    def test_creates_new_gitignore_when_absent(self, tmp_path: Path) -> None:
        """Creates a new .gitignore when none exists."""
        gitignore = tmp_path / ".gitignore"
        assert not gitignore.exists()

        rc = _handle_init_gitignore(_make_args(tmp_path))

        assert rc == 0
        assert gitignore.is_file(), ".gitignore should have been created"
        assert ".saturnday/" in gitignore.read_text(encoding="utf-8")

    def test_idempotent_existing_entries(self, tmp_path: Path) -> None:
        """Running twice does not duplicate .saturnday/ in .gitignore."""
        _handle_init_gitignore(_make_args(tmp_path))
        _handle_init_gitignore(_make_args(tmp_path))

        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert content.count(".saturnday/") == 1

    def test_output_mentions_policy_yaml_not_excluded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Output must state that .saturnday-policy.yaml is NOT excluded."""
        _handle_init_gitignore(_make_args(tmp_path))

        captured = capsys.readouterr()
        assert ".saturnday-policy.yaml" in captured.out

    def test_output_mentions_claude_md_not_excluded(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Output must state that CLAUDE.md is NOT excluded."""
        _handle_init_gitignore(_make_args(tmp_path))

        captured = capsys.readouterr()
        assert "CLAUDE.md" in captured.out

    def test_returns_zero_on_success(self, tmp_path: Path) -> None:
        """Returns 0 on successful write."""
        rc = _handle_init_gitignore(_make_args(tmp_path))
        assert rc == 0

    def test_does_not_exclude_policy_in_written_file(self, tmp_path: Path) -> None:
        """.saturnday-policy.yaml must not appear in the written .gitignore."""
        _handle_init_gitignore(_make_args(tmp_path))

        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".saturnday-policy.yaml" not in content

    def test_does_not_exclude_claude_md_in_written_file(self, tmp_path: Path) -> None:
        """CLAUDE.md must not appear in the written .gitignore."""
        _handle_init_gitignore(_make_args(tmp_path))

        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert "CLAUDE.md" not in content
