"""Tests for src/saturnday/run/safe_shell.py (U6).

Covers:
- Allowed git commands pass through to subprocess.run
- Dangerous commands (rm, curl, etc.) raise ShellPolicyViolation
- Path traversal raises ShellPolicyViolation
- System path arguments raise ShellPolicyViolation
- Non-allowlisted commands raise ShellPolicyViolation
- String commands pass through unchanged (no policy applied)
- ALLOWED_GIT_SUBCOMMANDS now includes commit, checkout, clean
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.run.safe_shell import ShellPolicyViolation, _check_policy, safe_subprocess_run
from saturnday.shell_policy import ALLOWED_GIT_SUBCOMMANDS


# ---------------------------------------------------------------------------
# ALLOWED_GIT_SUBCOMMANDS expansion (runner git operations)
# ---------------------------------------------------------------------------

class TestAllowedGitSubcommands:
    def test_commit_in_allowed_subcommands(self):
        assert "commit" in ALLOWED_GIT_SUBCOMMANDS

    def test_checkout_in_allowed_subcommands(self):
        assert "checkout" in ALLOWED_GIT_SUBCOMMANDS

    def test_clean_in_allowed_subcommands(self):
        assert "clean" in ALLOWED_GIT_SUBCOMMANDS

    def test_pre_existing_subcommands_still_present(self):
        for sub in ("status", "add", "reset", "diff", "rev-parse", "ls-files"):
            assert sub in ALLOWED_GIT_SUBCOMMANDS, f"Expected {sub!r} in allowlist"


# ---------------------------------------------------------------------------
# _check_policy — direct unit tests
# ---------------------------------------------------------------------------

class TestCheckPolicy:
    # --- git allowlist ---

    def test_git_status_allowed(self):
        _check_policy(["git", "status", "--porcelain"])  # must not raise

    def test_git_add_allowed(self):
        _check_policy(["git", "add", "src/foo.py"])

    def test_git_commit_allowed(self):
        _check_policy(["git", "commit", "-m", "message"])

    def test_git_checkout_allowed(self):
        _check_policy(["git", "checkout", "--", "."])

    def test_git_clean_allowed(self):
        _check_policy(["git", "clean", "-fd"])

    def test_git_reset_allowed(self):
        _check_policy(["git", "reset", "HEAD"])

    def test_git_diff_allowed(self):
        _check_policy(["git", "diff", "--cached"])

    # --- git denials ---

    def test_git_push_blocked(self):
        with pytest.raises(ShellPolicyViolation) as exc_info:
            _check_policy(["git", "push", "origin", "main"])
        assert "push" in str(exc_info.value)

    def test_git_pull_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["git", "pull"])

    def test_bare_git_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["git"])

    # --- dangerous commands ---

    def test_rm_blocked(self):
        with pytest.raises(ShellPolicyViolation) as exc_info:
            _check_policy(["rm", "-rf", "/tmp/something"])
        assert exc_info.value.reason == "dangerous_command"

    def test_sudo_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["sudo", "apt-get", "install"])

    def test_dd_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["dd", "if=/dev/zero", "of=/tmp/f"])

    def test_mkfs_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["mkfs", "/dev/sdb1"])

    # --- network commands ---

    def test_curl_blocked(self):
        with pytest.raises(ShellPolicyViolation) as exc_info:
            _check_policy(["curl", "https://example.com"])
        assert exc_info.value.reason == "network_command"

    def test_wget_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["wget", "https://example.com"])

    def test_ssh_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["ssh", "user@host"])

    # --- path traversal ---

    def test_path_traversal_blocked(self):
        with pytest.raises(ShellPolicyViolation) as exc_info:
            _check_policy(["git", "add", "../../etc/passwd"])
        assert exc_info.value.reason == "path_traversal"

    def test_windows_path_traversal_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["git", "add", "..\\..\\Windows\\System32"])

    # --- system paths ---

    def test_system_path_in_args_blocked(self):
        with pytest.raises(ShellPolicyViolation) as exc_info:
            _check_policy(["python", "/etc/passwd"])
        assert exc_info.value.reason == "system_path"

    def test_system_path_prefix_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["python", "/usr/local/bin/something"])

    # --- non-allowlisted commands ---

    def test_bash_blocked(self):
        with pytest.raises(ShellPolicyViolation) as exc_info:
            _check_policy(["bash", "-c", "echo hello"])
        assert exc_info.value.reason == "not_in_allowlist"

    def test_cat_blocked(self):
        with pytest.raises(ShellPolicyViolation):
            _check_policy(["cat", "/etc/hostname"])

    # --- allowed tools ---

    def test_ruff_allowed(self):
        _check_policy(["ruff", "check", "src/"])

    def test_bandit_allowed(self):
        _check_policy(["bandit", "-r", "src/"])

    def test_python_allowed(self):
        _check_policy(["python", "-m", "pytest"])

    def test_python3_allowed(self):
        _check_policy(["python3", "script.py"])


# ---------------------------------------------------------------------------
# safe_subprocess_run — integration with actual subprocess
# ---------------------------------------------------------------------------

class TestSafeSubprocessRun:
    def test_allowed_command_executes(self, tmp_path: Path):
        """An allowed git command should actually execute via subprocess."""
        # Init a git repo so git status doesn't fail
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

        result = safe_subprocess_run(
            ["git", "status", "--porcelain"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0

    def test_dangerous_command_raises_before_execution(self):
        """Dangerous commands raise before subprocess.run is called."""
        with patch("saturnday.run.safe_shell.subprocess.run") as mock_run:
            with pytest.raises(ShellPolicyViolation):
                safe_subprocess_run(["rm", "-rf", "/tmp"])
            mock_run.assert_not_called()

    def test_string_command_passes_through(self):
        """String commands bypass policy and are passed to subprocess.run directly."""
        with patch("saturnday.run.safe_shell.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            safe_subprocess_run("echo hello", shell=True)
        mock_run.assert_called_once_with("echo hello", shell=True)

    def test_empty_list_passes_through(self):
        """An empty list command bypasses policy (no argv to check)."""
        with patch("saturnday.run.safe_shell.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            safe_subprocess_run([])
        mock_run.assert_called_once()

    def test_kwargs_forwarded(self, tmp_path: Path):
        """Keyword arguments are forwarded to subprocess.run."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)

        result = safe_subprocess_run(
            ["git", "status"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )
        # cwd was forwarded correctly
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# ShellPolicyViolation exception attributes
# ---------------------------------------------------------------------------

class TestShellPolicyViolation:
    def test_exception_carries_cmd(self):
        try:
            _check_policy(["rm", "-rf", "/"])
        except ShellPolicyViolation as exc:
            assert exc.cmd == ["rm", "-rf", "/"]
            assert exc.reason == "dangerous_command"
        else:
            pytest.fail("Expected ShellPolicyViolation")

    def test_exception_message_contains_command(self):
        try:
            _check_policy(["curl", "https://evil.example.com"])
        except ShellPolicyViolation as exc:
            assert "curl" in str(exc)
        else:
            pytest.fail("Expected ShellPolicyViolation")
