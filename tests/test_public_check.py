"""Tests for saturnday public-check command.

Covers:
1. Blocked tracked traces detected
2. Review-required traces detected
3. Clean repo reports ready
4. Dirty working tree detected
5. Commit-message tooling signatures detected
6. Read-only — does not mutate repo state
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from saturnday.public_check import (
    Finding,
    PublicCheckResult,
    format_public_check,
    generate_history_guidance,
    run_public_check,
)


def _init_git(repo: Path) -> None:
    """Initialize a bare git repo and make an initial commit."""
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True, check=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo), capture_output=True, check=True)


def _add_tracked(repo: Path, rel_path: str, content: str = "x") -> None:
    """Create and track a file."""
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", rel_path], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", f"add {rel_path}"], cwd=str(repo), capture_output=True, check=True)


# ---------------------------------------------------------------------------
# 1. Blocked tracked traces
# ---------------------------------------------------------------------------

class TestBlockedTraces:
    def test_claude_settings_blocked(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".claude/settings.json", '{"hooks": {}}')
        result = run_public_check(tmp_path)
        blocked = [f for f in result.findings if f.category == "block"]
        paths = [f.path for f in blocked]
        assert ".claude/settings.json" in paths
        assert result.verdict == "NOT_READY"

    def test_bin_sat_blocked(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "bin/sat-governance", "#!/bin/sh")
        result = run_public_check(tmp_path)
        blocked = [f for f in result.findings if f.category == "block"]
        paths = [f.path for f in blocked]
        assert any("bin/sat-governance" in p for p in paths)

    def test_saturnday_dir_tracked_blocked(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".saturnday/evidence/report.json", "{}")
        result = run_public_check(tmp_path)
        blocked = [f for f in result.findings if f.category == "block"]
        assert len(blocked) >= 1
        assert any(".saturnday/" in f.path for f in blocked)


# ---------------------------------------------------------------------------
# 2. Review-required traces
# ---------------------------------------------------------------------------

class TestReviewTraces:
    def test_claude_md_review(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "CLAUDE.md", "# Governance rules")
        result = run_public_check(tmp_path)
        review = [f for f in result.findings if f.category == "review"]
        paths = [f.path for f in review]
        assert "CLAUDE.md" in paths

    def test_agents_md_review(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "AGENTS.md", "# Agents rules")
        result = run_public_check(tmp_path)
        review = [f for f in result.findings if f.category == "review"]
        paths = [f.path for f in review]
        assert "AGENTS.md" in paths


# ---------------------------------------------------------------------------
# 3. Clean repo reports ready
# ---------------------------------------------------------------------------

class TestCleanRepo:
    def test_clean_repo_ready(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        result = run_public_check(tmp_path)
        assert result.verdict == "READY"
        assert len([f for f in result.findings if f.category in ("block", "review")]) == 0

    def test_policy_file_not_flagged(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, ".saturnday-policy.yaml", "schema_version: '1.0.0'")
        result = run_public_check(tmp_path)
        paths = [f.path for f in result.findings]
        assert ".saturnday-policy.yaml" not in paths


# ---------------------------------------------------------------------------
# 4. Dirty working tree
# ---------------------------------------------------------------------------

class TestDirtyTree:
    def test_dirty_tree_flagged(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        (tmp_path / "dirty.txt").write_text("uncommitted", encoding="utf-8")
        result = run_public_check(tmp_path)
        review = [f for f in result.findings if f.category == "review"]
        assert any("working tree" in f.path.lower() or "uncommitted" in f.detail.lower() for f in review)


# ---------------------------------------------------------------------------
# 5. Commit-message signatures
# ---------------------------------------------------------------------------

class TestCommitMessages:
    def test_ticket_commit_flagged(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        (tmp_path / "code.py").write_text("x = 1", encoding="utf-8")
        subprocess.run(["git", "add", "code.py"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[T001] Apply ticket changes"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        # Scan all commits
        result = run_public_check(tmp_path, commit_range="HEAD~1..HEAD")
        review = [f for f in result.findings if f.category == "review"]
        assert any("tooling signature" in f.detail.lower() for f in review)

    def test_governance_commit_flagged(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        (tmp_path / "fix.py").write_text("y = 2", encoding="utf-8")
        subprocess.run(["git", "add", "fix.py"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "[T005] Apply ticket changes [GOVERNANCE: review required]"],
            cwd=str(tmp_path), capture_output=True, check=True,
        )
        result = run_public_check(tmp_path, commit_range="HEAD~1..HEAD")
        review = [f for f in result.findings if f.category == "review"]
        assert any("[GOVERNANCE:" in f.detail or "tooling signature" in f.detail.lower() for f in review)

    def test_clean_commit_not_flagged(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        result = run_public_check(tmp_path, commit_range="HEAD~1..HEAD")
        commit_findings = [f for f in result.findings if f.path.startswith("commit ")]
        assert len(commit_findings) == 0


# ---------------------------------------------------------------------------
# 6. Read-only — no mutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_no_files_created(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        before = set()
        for root, dirs, files in os.walk(tmp_path):
            if ".git" in root:
                continue
            for f in files:
                before.add(os.path.join(root, f))

        run_public_check(tmp_path)

        after = set()
        for root, dirs, files in os.walk(tmp_path):
            if ".git" in root:
                continue
            for f in files:
                after.add(os.path.join(root, f))

        assert before == after, "public-check must not create any files"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class TestCommitScanSkipExplicit:
    def test_skip_shown_when_no_range(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        result = run_public_check(tmp_path)  # no origin, no --commits
        assert result.commit_scan_performed is False
        output = format_public_check(result)
        assert "Commit-message scan was not performed" in output
        assert "--commits" in output

    def test_skip_not_shown_when_range_provided(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        result = run_public_check(tmp_path, commit_range="HEAD~1..HEAD")
        assert result.commit_scan_performed is True
        output = format_public_check(result)
        assert "Commit-message scan was not performed" not in output


class TestBinSatWildcard:
    def test_arbitrary_bin_sat_name_blocked(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "bin/sat-custom-tool", "#!/bin/sh")
        result = run_public_check(tmp_path)
        blocked = [f for f in result.findings if f.category == "block"]
        paths = [f.path for f in blocked]
        assert "bin/sat-custom-tool" in paths

    def test_bin_not_sat_not_blocked(self, tmp_path: Path) -> None:
        _init_git(tmp_path)
        _add_tracked(tmp_path, "bin/my-tool", "#!/bin/sh")
        result = run_public_check(tmp_path)
        blocked = [f for f in result.findings if f.category == "block"]
        paths = [f.path for f in blocked]
        assert "bin/my-tool" not in paths


class TestFormatting:
    def test_verdict_in_output(self) -> None:
        result = PublicCheckResult(verdict="READY")
        output = format_public_check(result)
        assert "READY FOR PUBLIC PUSH" in output

    def test_not_ready_verdict(self) -> None:
        result = PublicCheckResult(
            findings=[Finding(category="block", path=".claude/settings.json", detail="test", guidance="fix")],
            verdict="NOT_READY",
        )
        output = format_public_check(result)
        assert "NOT READY FOR PUBLIC PUSH" in output
        assert "BLOCK" in output


# ---------------------------------------------------------------------------
# Fix 25 — generate_history_guidance and format_public_check updates
# ---------------------------------------------------------------------------

class TestGenerateHistoryGuidance:
    def test_empty_returns_empty(self) -> None:
        """Test 1: empty flagged list returns empty string."""
        assert generate_history_guidance([]) == ""

    def test_coded_ungoverned_is_high(self) -> None:
        """Test 2: CODED_UNGOVERNED pattern maps to HIGH severity."""
        output = generate_history_guidance([("abc12345", "[T003] CODED_UNGOVERNED fix")])
        assert "[HIGH]" in output

    def test_governance_tag_is_medium(self) -> None:
        """Test 3: [GOVERNANCE: pattern maps to MEDIUM severity."""
        output = generate_history_guidance([("def67890", "[T002] fix [GOVERNANCE: reviewed]")])
        assert "[MEDIUM]" in output

    def test_saturnday_repair_is_medium(self) -> None:
        """Test 4: saturnday repair pattern maps to MEDIUM severity."""
        output = generate_history_guidance([("aaa11111", "saturnday repair run")])
        assert "[MEDIUM]" in output

    def test_ticket_ref_is_low(self) -> None:
        """Test 5: ticket ref ([T001]) without high/medium patterns maps to LOW severity."""
        output = generate_history_guidance([("bbb22222", "[T001] Add feature")])
        assert "[LOW]" in output

    def test_apply_ticket_changes_is_low(self) -> None:
        """Test 6: 'Apply ticket changes' without high/medium patterns maps to LOW severity."""
        output = generate_history_guidance([("ccc33333", "Apply ticket changes for T002")])
        assert "[LOW]" in output

    def test_precondition_checklist_present(self) -> None:
        """Test 7: output includes a precondition checklist."""
        output = generate_history_guidance([("abc12345", "[T001] Add feature")])
        assert "pushed" in output.lower()
        assert "backup" in output.lower()

    def test_shared_remote_caution_present(self) -> None:
        """Test 8: output warns against rewriting history on a shared branch."""
        output = generate_history_guidance([("abc12345", "[T001] Add feature")])
        assert "shared" in output.lower() or "protected" in output.lower()
        assert "Do not rewrite history" in output or "do not rewrite" in output.lower()

    def test_no_automation_warning_present(self) -> None:
        """Test 9: output states that this tool does not perform history rewrites."""
        output = generate_history_guidance([("abc12345", "[T001] Add feature")])
        assert "does not perform history rewrites" in output

    def test_no_rewrite_commands(self) -> None:
        """Test 10: output contains no actionable history-rewrite commands."""
        output = generate_history_guidance([("abc12345", "[T001] Add feature")])
        assert "rebase" not in output.lower()
        assert "filter-repo" not in output.lower()
        assert "filter-branch" not in output.lower()
        assert "squash" not in output.lower()
        assert "force-push" not in output.lower()
        assert "--force" not in output.lower()

    def test_multiple_commits_all_listed(self) -> None:
        """Test 11: all flagged commits appear in the output."""
        flagged = [
            ("aaa11111", "[T001] Add feature"),
            ("bbb22222", "CODED_UNGOVERNED stub"),
            ("ccc33333", "[GOVERNANCE: review]"),
        ]
        output = generate_history_guidance(flagged)
        assert "aaa11111" in output
        assert "bbb22222" in output
        assert "ccc33333" in output


class TestHistoryGuidanceFormatIntegration:
    def test_format_public_check_includes_history_guidance_when_flagged(self) -> None:
        """Test 10 (format): format_public_check appends history guidance when commits are flagged."""
        result = PublicCheckResult(
            findings=[Finding(
                category="review",
                path="commit abc12345",
                detail="Commit message contains tooling signature: [T001] Add feature",
                guidance="See history cleanup guidance below.",
            )],
            verdict="REVIEW_REQUIRED",
            commit_scan_performed=True,
            flagged_commits=[("abc12345", "[T001] Add feature")],
        )
        output = format_public_check(result)
        assert "History cleanup guidance" in output

    def test_format_public_check_no_git_rebase_in_rendered_output(self) -> None:
        """Test 11 (format): git rebase -i no longer appears in rendered output."""
        result = PublicCheckResult(
            findings=[Finding(
                category="review",
                path="commit abc12345",
                detail="Commit message contains tooling signature: [T001] Apply ticket changes",
                guidance="See history cleanup guidance below.",
            )],
            verdict="REVIEW_REQUIRED",
            commit_scan_performed=True,
            flagged_commits=[("abc12345", "[T001] Apply ticket changes")],
        )
        output = format_public_check(result)
        assert "git rebase -i" not in output
        assert "git rebase" not in output

    def test_format_public_check_no_history_guidance_when_no_flagged(self) -> None:
        """No history guidance block when flagged_commits is empty."""
        result = PublicCheckResult(
            findings=[],
            verdict="READY",
            commit_scan_performed=True,
            flagged_commits=[],
        )
        output = format_public_check(result)
        assert "History cleanup guidance" not in output
