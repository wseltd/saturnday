"""Tests for governance.py — governance mode orchestration."""

import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from saturnday.evidence import CheckResult, EvidencePack, SCHEMA_VERSION
from saturnday.governance import (
    _convert_review_result,
    extract_changed_files,
    run_governance_check,
)
from saturnday.policy_manifest import default_policy, load_policy


def _init_git_repo(tmp_path):
    """Create a minimal git repo with a commit."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    (tmp_path / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path), capture_output=True, check=True,
    )
    return tmp_path


class TestExtractChangedFiles:
    def test_diff_range(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "new_file.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add file"],
            cwd=str(repo), capture_output=True, check=True,
        )
        files, diff = extract_changed_files(repo, "HEAD~1..HEAD")
        assert "new_file.py" in files
        assert len(diff) > 0

    def test_staged_mode(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "staged.py").write_text("y = 2\n")
        subprocess.run(["git", "add", "staged.py"], cwd=str(repo), capture_output=True, check=True)
        files, diff = extract_changed_files(repo, "", staged=True)
        assert "staged.py" in files

    def test_initial_commit(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        # Add a second commit so HEAD~1..HEAD works
        (repo / "second.py").write_text("pass\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "second"],
            cwd=str(repo), capture_output=True, check=True,
        )
        files, diff = extract_changed_files(repo, "HEAD~1..HEAD")
        assert "second.py" in files

    def test_invalid_diff_range(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        with pytest.raises(RuntimeError, match="git diff"):
            extract_changed_files(repo, "nonexistent..branch")


class TestConvertReviewResult:
    def test_pass_result(self):
        tool_result = {"status": "PASS", "findings": [], "error": None}
        policy = default_policy()
        cr = _convert_review_result("syntax", tool_result, policy, 0.5)
        assert cr.status == "PASS"
        assert cr.severity == "error"
        assert cr.elapsed_s == 0.5

    def test_fail_result(self):
        tool_result = {
            "status": "FAIL",
            "findings": [{"message": "bad syntax"}],
            "error": None,
        }
        policy = default_policy()
        cr = _convert_review_result("syntax", tool_result, policy, 1.0)
        assert cr.status == "FAIL"
        assert len(cr.findings) == 1

    def test_skipped_result(self):
        tool_result = {"status": "SKIPPED", "findings": [], "error": "syntax_failed"}
        policy = default_policy()
        cr = _convert_review_result("ruff", tool_result, policy, 0.0)
        assert cr.status == "SKIPPED"
        assert cr.error == "syntax_failed"

    def test_soft_check_severity(self):
        tool_result = {"status": "FAIL", "findings": [{"msg": "lint"}]}
        policy = default_policy()
        cr = _convert_review_result("ruff", tool_result, policy, 0.5)
        assert cr.severity == "warning"

    def test_string_findings_normalized(self):
        tool_result = {"status": "FAIL", "findings": ["error one", "error two"]}
        policy = default_policy()
        cr = _convert_review_result("syntax", tool_result, policy, 0.5)
        assert all(isinstance(f, dict) for f in cr.findings)
        assert cr.findings[0]["message"] == "error one"

    def test_check_result_rule_id_populated(self):
        """_convert_review_result populates rule_id from RULE_IDS."""
        policy = default_policy()
        result = _convert_review_result(
            "hardcoded_jwt",
            {"status": "FAIL", "findings": [{"message": "found secret"}]},
            policy,
            0.1,
        )
        assert result.rule_id == "SEC-001"
        assert result.cwe is not None
        assert result.owasp is not None

    def test_ts_check_rule_id_via_family_map(self):
        """TS checks get rule_id through FAMILY_MAP resolution."""
        policy = default_policy()
        result = _convert_review_result(
            "hardcoded_jwt_ts",
            {"status": "FAIL", "findings": [{"message": "found secret"}]},
            policy,
            0.1,
        )
        assert result.rule_id == "SEC-001"

    def test_quality_check_rule_id_is_none(self):
        """Quality checks without SEC rule_ids get None."""
        policy = default_policy()
        result = _convert_review_result(
            "ruff",
            {"status": "PASS", "findings": []},
            policy,
            0.05,
        )
        assert result.rule_id is None


class TestRunGovernanceCheck:
    def test_not_a_git_repo(self, tmp_path):
        with pytest.raises(RuntimeError, match="Not a git repository"):
            run_governance_check(tmp_path, "HEAD~1..HEAD")

    def test_basic_pass(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "good.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add good file"],
            cwd=str(repo), capture_output=True, check=True,
        )
        output_dir = tmp_path / "output"
        pack, evidence_path = run_governance_check(
            repo, "HEAD~1..HEAD", output_dir=output_dir,
        )
        assert isinstance(pack, EvidencePack)
        assert pack.mode == "check"
        assert evidence_path.exists()
        assert (evidence_path / "run-metadata.json").exists()
        assert (evidence_path / "final-disposition.json").exists()
        assert (evidence_path / "summary.md").exists()

    def test_with_custom_policy(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "new.py").write_text("a = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add"],
            cwd=str(repo), capture_output=True, check=True,
        )
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(textwrap.dedent("""\
            checks:
              syntax:
                severity: error
              ruff:
                severity: info
        """))
        output_dir = tmp_path / "output"
        pack, evidence_path = run_governance_check(
            repo, "HEAD~1..HEAD", policy_path=policy_file, output_dir=output_dir,
        )
        assert pack.policy_path == str(policy_file)

    def test_scope_violation(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "forbidden.secret").write_text("secret data\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add secret"],
            cwd=str(repo), capture_output=True, check=True,
        )
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(textwrap.dedent("""\
            scope:
              denied_paths: ["*.secret"]
        """))
        output_dir = tmp_path / "output"
        pack, evidence_path = run_governance_check(
            repo, "HEAD~1..HEAD", policy_path=policy_file, output_dir=output_dir,
        )
        assert pack.disposition == "FAIL"
        scope_checks = [c for c in pack.check_results if c.name == "scope"]
        assert len(scope_checks) == 1
        assert scope_checks[0].status == "FAIL"

    def test_diff_file_written(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "file.py").write_text("z = 3\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add"],
            cwd=str(repo), capture_output=True, check=True,
        )
        output_dir = tmp_path / "output"
        pack, evidence_path = run_governance_check(
            repo, "HEAD~1..HEAD", output_dir=output_dir,
        )
        diff_path = evidence_path / "diff-input.diff"
        assert diff_path.exists()
        assert len(diff_path.read_text()) > 0

    def test_empty_diff(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        # Create a second commit identical to the first
        (repo / "hello.py").write_text("print('hello')\n")  # same content
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "empty"],
            cwd=str(repo), capture_output=True, check=True,
        )
        output_dir = tmp_path / "output"
        pack, evidence_path = run_governance_check(
            repo, "HEAD~1..HEAD", output_dir=output_dir,
        )
        assert pack.disposition == "PASS"

    def test_staged_mode(self, tmp_path):
        repo = _init_git_repo(tmp_path)
        (repo / "staged.py").write_text("a = 1\n")
        subprocess.run(["git", "add", "staged.py"], cwd=str(repo), capture_output=True, check=True)
        output_dir = tmp_path / "output"
        pack, evidence_path = run_governance_check(
            repo, "", staged=True, output_dir=output_dir,
        )
        assert pack.diff_range == "--staged"
        assert evidence_path.exists()

    def test_run_data_recording_failure_is_silent(self, tmp_path):
        """run_data recording errors should not break governance."""
        repo = _init_git_repo(tmp_path)
        (repo / "f.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add"],
            cwd=str(repo), capture_output=True, check=True,
        )
        output_dir = tmp_path / "output"
        with patch("saturnday.run_data.get_db", side_effect=Exception("db fail")):
            pack, evidence_path = run_governance_check(
                repo, "HEAD~1..HEAD", output_dir=output_dir,
            )
        # Should still succeed despite db failure
        assert evidence_path.exists()
