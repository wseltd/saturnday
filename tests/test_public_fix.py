"""Tests for saturnday public-fix command.

Covers:
1. --dry-run makes no repo changes
2. --apply removes BLOCK items from tracking, keeps files on disk
3. CLAUDE.md prompt defaults to NO
4. AGENTS.md prompt defaults to NO
5. Safe files not removed
6. Manual-only items reported but not acted on
7. Idempotent — safe to rerun
8. No unexpected mutation
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from saturnday.public_fix import (
    format_apply_summary,
    format_dry_run,
    generate_next_steps,
    run_public_fix_apply,
    run_public_fix_dry_run,
)


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), capture_output=True, check=True)


def _add_tracked(repo: Path, rel_path: str, content: str = "x") -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {rel_path}"], cwd=str(repo), capture_output=True, check=True)


def _is_tracked(repo: Path, rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "ls-files", rel_path],
        cwd=str(repo), capture_output=True, text=True, check=False,
    )
    return rel_path in proc.stdout


# ---------------------------------------------------------------------------
# 1. Dry-run makes no changes
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_changes(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{}')
        _add_tracked(tmp_path, "bin/sat-governance", '#!/bin/sh')

        plan = run_public_fix_dry_run(tmp_path)
        assert len(plan["block_targets"]) == 2

        # Files still tracked
        assert _is_tracked(tmp_path, ".claude/settings.json")
        assert _is_tracked(tmp_path, "bin/sat-governance")

    def test_dry_run_output_shows_targets(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{}')

        plan = run_public_fix_dry_run(tmp_path)
        output = format_dry_run(plan)
        assert ".claude/settings.json" in output
        assert "git rm --cached" in output
        assert "To apply" in output


# ---------------------------------------------------------------------------
# 2. Apply removes BLOCK items, keeps files on disk
# ---------------------------------------------------------------------------

class TestApplyBlock:
    def test_apply_removes_from_tracking(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{}')
        _add_tracked(tmp_path, "bin/sat-custom", '#!/bin/sh')

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")

        assert ".claude/settings.json" in summary["removed"]
        assert "bin/sat-custom" in summary["removed"]

        # Files on disk
        assert (tmp_path / ".claude/settings.json").exists()
        assert (tmp_path / "bin/sat-custom").exists()

        # NOT tracked anymore
        assert not _is_tracked(tmp_path, ".claude/settings.json")
        assert not _is_tracked(tmp_path, "bin/sat-custom")

    def test_apply_declined_skips(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{}')

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "n")

        assert len(summary["removed"]) == 0
        assert ".claude/settings.json" in summary["skipped"]
        assert _is_tracked(tmp_path, ".claude/settings.json")


# ---------------------------------------------------------------------------
# 3 & 4. CLAUDE.md and AGENTS.md default to NO
# ---------------------------------------------------------------------------

class TestReviewDefaults:
    def test_claude_md_default_no(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "CLAUDE.md", "# Rules")

        # Empty input = default NO
        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "")

        assert "CLAUDE.md" in summary["review_declined"]
        assert "CLAUDE.md" not in summary["removed"]
        assert _is_tracked(tmp_path, "CLAUDE.md")

    def test_agents_md_default_no(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "AGENTS.md", "# Agents")

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "")

        assert "AGENTS.md" in summary["review_declined"]
        assert _is_tracked(tmp_path, "AGENTS.md")

    def test_claude_md_explicit_yes(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "CLAUDE.md", "# Rules")

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")

        assert "CLAUDE.md" in summary["removed"]
        assert not _is_tracked(tmp_path, "CLAUDE.md")
        assert (tmp_path / "CLAUDE.md").exists()  # kept on disk


# ---------------------------------------------------------------------------
# 5. Safe files not removed
# ---------------------------------------------------------------------------

class TestSafeFilesPreserved:
    def test_policy_not_removed(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".saturnday-policy.yaml", "schema_version: '1.0.0'")

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")

        # Policy should NOT appear in removed
        assert ".saturnday-policy.yaml" not in summary["removed"]
        assert _is_tracked(tmp_path, ".saturnday-policy.yaml")

    def test_baseline_not_removed(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".saturnday-baseline.json", "{}")

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")
        assert ".saturnday-baseline.json" not in summary["removed"]


# ---------------------------------------------------------------------------
# 6. Manual items reported but not acted on
# ---------------------------------------------------------------------------

class TestManualItems:
    def test_dirty_tree_reported(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        (tmp_path / "dirty.txt").write_text("uncommitted", encoding="utf-8")

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")
        assert any("uncommitted" in m.lower() for m in summary["manual_items"])

    def test_commit_messages_reported(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        (tmp_path / "code.py").write_text("x=1", encoding="utf-8")
        subprocess.run(["git", "add", "code.py"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[T001] Apply ticket changes"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )

        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")
        assert any("tooling signature" in m.lower() or "commit" in m.lower() for m in summary["manual_items"])


# ---------------------------------------------------------------------------
# 7. Idempotent — safe to rerun
# ---------------------------------------------------------------------------

class TestIdempotent:
    def test_rerun_after_apply(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{}')

        # First apply
        run_public_fix_apply(tmp_path, input_fn=lambda _: "y")
        assert not _is_tracked(tmp_path, ".claude/settings.json")

        # Second apply — should not error
        summary = run_public_fix_apply(tmp_path, input_fn=lambda _: "y")
        assert len(summary["removed"]) == 0  # already removed


# ---------------------------------------------------------------------------
# 8. No unexpected mutation
# ---------------------------------------------------------------------------

class TestMutuallyExclusiveFlags:
    def test_both_flags_rejected(self, tmp_path: Path) -> None:
        """--dry-run and --apply together must fail with exit code 2."""
        import argparse
        from saturnday.cli import main

        # Simulate CLI args
        exit_code = main(["public-fix", "--repo", str(tmp_path), "--dry-run", "--apply"])
        assert exit_code == 2


class TestNoUnexpectedMutation:
    def test_dry_run_no_files_changed(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{}')

        # Snapshot tracked files
        before = set(subprocess.run(
            ["git", "ls-files"], cwd=str(tmp_path),
            capture_output=True, text=True, check=True,
        ).stdout.splitlines())

        run_public_fix_dry_run(tmp_path)

        after = set(subprocess.run(
            ["git", "ls-files"], cwd=str(tmp_path),
            capture_output=True, text=True, check=True,
        ).stdout.splitlines())

        assert before == after


# ---------------------------------------------------------------------------
# Fix 24 — generate_next_steps and format_apply_summary helper block
# ---------------------------------------------------------------------------

class TestGenerateNextStepsEmpty:
    def test_empty_when_no_removals(self) -> None:
        """Test 1: returns empty string when removed is empty."""
        result = generate_next_steps(removed=[], manual_items=[], dirty=False)
        assert result == ""

    def test_empty_when_removed_is_empty_regardless_of_manual(self) -> None:
        """Still empty when manual_items present but removed is empty."""
        result = generate_next_steps(
            removed=[],
            manual_items=["3 commit(s) contain tooling signatures — ..."],
            dirty=True,
        )
        assert result == ""


class TestGenerateNextStepsCommitGuidance:
    def test_git_status_before_commit_command(self) -> None:
        """Test 2: git status must appear before the commit command."""
        output = generate_next_steps(
            removed=[".claude/settings.json"], manual_items=[], dirty=False,
        )
        status_pos = output.find("git status")
        commit_pos = output.find("git commit")
        assert status_pos != -1, "git status must appear in output"
        assert commit_pos != -1, "git commit must appear in output"
        assert status_pos < commit_pos, "git status must precede git commit"

    def test_commit_command_is_conditional(self) -> None:
        """Test 3: commit command is presented as conditional, not unconditional."""
        output = generate_next_steps(
            removed=[".claude/settings.json"], manual_items=[], dirty=False,
        )
        assert "if only" in output.lower(), (
            "Commit command must be explicitly conditional"
        )

    def test_dirty_warning_when_dirty_true(self) -> None:
        """Test 4: dirty=True produces a warning about uncommitted changes."""
        output = generate_next_steps(
            removed=[".claude/settings.json"], manual_items=[], dirty=True,
        )
        assert "uncommitted" in output.lower(), (
            "dirty=True must produce uncommitted-changes warning"
        )

    def test_no_dirty_warning_when_dirty_false(self) -> None:
        """dirty=False must not produce the uncommitted warning."""
        output = generate_next_steps(
            removed=[".claude/settings.json"], manual_items=[], dirty=False,
        )
        assert "uncommitted" not in output.lower()


class TestGenerateNextStepsGitignore:
    def test_exact_path_entry(self) -> None:
        """Test 5: exact matched file produces /<path> gitignore entry."""
        output = generate_next_steps(
            removed=[".cursor/rules"], manual_items=[], dirty=False,
        )
        assert "/.cursor/rules" in output

    def test_saturnday_subtree_collapses(self) -> None:
        """Test 6: .saturnday/ subtree collapses to /.saturnday/."""
        output = generate_next_steps(
            removed=[
                ".saturnday/evidence/report.json",
                ".saturnday/run/ledger.json",
            ],
            manual_items=[],
            dirty=False,
        )
        assert "/.saturnday/" in output
        # Must appear only once
        assert output.count("/.saturnday/") == 1

    def test_bin_sat_collapses_to_wildcard(self) -> None:
        """Test 7: multiple bin/sat-* removals collapse to a single /bin/sat-*."""
        output = generate_next_steps(
            removed=["bin/sat-governance", "bin/sat-custom"],
            manual_items=[],
            dirty=False,
        )
        assert "/bin/sat-*" in output
        assert output.count("/bin/sat-*") == 1

    def test_duplicates_collapsed(self) -> None:
        """Test 8: identical logical patterns appear only once."""
        output = generate_next_steps(
            removed=[
                ".saturnday/evidence/a.json",
                ".saturnday/evidence/b.json",
                ".saturnday/run/ledger.json",
            ],
            manual_items=[],
            dirty=False,
        )
        assert output.count("/.saturnday/") == 1

    def test_review_caution_present(self) -> None:
        """Caution about reviewing entries before adding must be present."""
        output = generate_next_steps(
            removed=[".claude/settings.json"], manual_items=[], dirty=False,
        )
        assert "review" in output.lower()
        assert "unrelated" in output.lower()


class TestGenerateNextStepsLocalExclusion:
    def test_local_only_exclusion_option_printed(self) -> None:
        """Test 9: local-only exclusion (.git/info/exclude) option is printed."""
        output = generate_next_steps(
            removed=[".claude/settings.json"], manual_items=[], dirty=False,
        )
        assert ".git/info/exclude" in output

    def test_entry_list_not_repeated_in_section3(self) -> None:
        """Entry list from section 2 must not be repeated in section 3."""
        output = generate_next_steps(
            removed=[".cursor/rules"], manual_items=[], dirty=False,
        )
        # /.cursor/rules appears in section 2; section 3 should not list it again
        assert output.count("/.cursor/rules") == 1


class TestGenerateNextStepsHistorySection:
    def test_history_section_present_when_commit_signatures(self) -> None:
        """Test 10: deferred history section appears when commit-signature findings present."""
        output = generate_next_steps(
            removed=[".claude/settings.json"],
            manual_items=[
                "3 commit(s) contain tooling signatures — consider squashing"
            ],
            dirty=False,
        )
        assert "history" in output.lower() or "deferred" in output.lower()
        assert "tooling signature" in output.lower() or "commit" in output.lower()

    def test_history_section_absent_when_no_commit_signatures(self) -> None:
        """History section must not appear when no commit-signature findings present."""
        output = generate_next_steps(
            removed=[".claude/settings.json"],
            manual_items=["Working tree has uncommitted changes"],
            dirty=False,
        )
        assert "future Saturnday release" not in output

    def test_no_rebase_command_in_history_section(self) -> None:
        """Test 11: deferred history section contains no actionable history-rewrite command."""
        output = generate_next_steps(
            removed=[".claude/settings.json"],
            manual_items=[
                "2 commit(s) contain tooling signatures — consider squashing"
            ],
            dirty=False,
        )
        assert "rebase" not in output.lower()
        assert "filter-repo" not in output.lower()
        assert "filter-branch" not in output.lower()
        assert "squash" not in output.lower()


class TestFormatApplySummaryIntegration:
    def test_helper_block_included_when_removals_exist(self) -> None:
        """Test 12: format_apply_summary includes helper block when removed is non-empty."""
        summary = {
            "removed": [".claude/settings.json"],
            "skipped": [],
            "review_accepted": [],
            "review_declined": [],
            "manual_items": [],
            "dirty_tree": False,
        }
        output = format_apply_summary(summary)
        assert "git status" in output
        assert "git commit" in output
        assert ".gitignore" in output

    def test_helper_block_absent_when_no_removals(self) -> None:
        """Test 13: format_apply_summary does not include helper block when removed is empty."""
        summary = {
            "removed": [],
            "skipped": [],
            "review_accepted": [],
            "review_declined": [],
            "manual_items": [],
            "dirty_tree": False,
        }
        output = format_apply_summary(summary)
        assert "git status" not in output
        assert ".gitignore" not in output

    def test_dirty_warning_propagated_from_summary(self) -> None:
        """dirty_tree from summary dict is passed correctly to the helper block."""
        summary = {
            "removed": [".claude/settings.json"],
            "skipped": [],
            "review_accepted": [],
            "review_declined": [],
            "manual_items": [],
            "dirty_tree": True,
        }
        output = format_apply_summary(summary)
        assert "uncommitted" in output.lower()


# ---------------------------------------------------------------------------
# Fix 25 — deferred history text points to public-check --commits
# ---------------------------------------------------------------------------

class TestDeferredHistoryGuidancePointer:
    def test_deferred_history_section_points_to_public_check(self) -> None:
        """Test 14: deferred history section now references public-check --commits, not 'future Saturnday release'."""
        output = generate_next_steps(
            removed=[".claude/settings.json"],
            manual_items=["3 commit(s) contain tooling signatures — consider squashing"],
            dirty=False,
        )
        assert "public-check" in output
        assert "--commits" in output
        assert "future Saturnday release" not in output
