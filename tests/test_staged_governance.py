"""Tests for Fix 61 — staged governance and load_policy scoping.

Proves:
1. governance.py imports cleanly without scoping issues
2. Module-level load_policy is not shadowed by lazy imports
3. Staged governance with one staged file + policy does not crash
4. Staged governance with no staged files exits cleanly
5. Non-staged governance still works
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


class TestLoadPolicyScopingFix:
    def test_governance_module_imports_cleanly(self) -> None:
        """governance.py must import without any scoping issues."""
        import saturnday.governance
        assert hasattr(saturnday.governance, "run_governance_check")
        assert hasattr(saturnday.governance, "run_full_repo_review")

    def test_load_policy_not_shadowed(self) -> None:
        """Module-level load_policy must be accessible, not shadowed by lazy import."""
        from saturnday.governance import load_policy
        from saturnday.policy_manifest import load_policy as manifest_load
        assert load_policy is manifest_load


class TestStagedGovernanceRealRepo:
    """Test staged governance against real git repos."""

    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(path), capture_output=True, check=True)

    def test_staged_with_policy_no_crash(self, tmp_path: Path) -> None:
        """Staged governance with a policy file must not crash with UnboundLocalError."""
        self._init_repo(tmp_path)
        (tmp_path / "test.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "test.py"], cwd=str(tmp_path), capture_output=True, check=True)
        (tmp_path / ".saturnday-policy.yaml").write_text("expected_findings:\n  - license\n")

        from saturnday.governance import run_governance_check
        # Must not raise UnboundLocalError
        pack, _ = run_governance_check(
            repo_path=tmp_path,
            diff_range="HEAD",
            staged=True,
            policy_path=tmp_path / ".saturnday-policy.yaml",
        )
        assert pack.disposition in ("PASS", "FAIL", "WARN")

    def test_staged_no_files_no_crash(self, tmp_path: Path) -> None:
        """Staged governance with nothing staged must not crash."""
        self._init_repo(tmp_path)

        from saturnday.governance import run_governance_check
        pack, _ = run_governance_check(
            repo_path=tmp_path,
            diff_range="HEAD",
            staged=True,
        )
        assert pack.disposition in ("PASS", "FAIL", "WARN")

    def test_non_staged_still_works(self, tmp_path: Path) -> None:
        """Non-staged governance must still work normally."""
        self._init_repo(tmp_path)
        (tmp_path / "test.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "test.py"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add test"], cwd=str(tmp_path), capture_output=True, check=True)

        from saturnday.governance import run_governance_check
        pack, _ = run_governance_check(
            repo_path=tmp_path,
            diff_range="HEAD~1..HEAD",
            staged=False,
        )
        assert pack.disposition in ("PASS", "FAIL", "WARN")

    def test_precommit_hook_style_with_policy(self, tmp_path: Path) -> None:
        """Pre-commit hook style: staged + policy file, called via CLI entry point."""
        self._init_repo(tmp_path)
        (tmp_path / "app.py").write_text("def main(): pass\n")
        subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), capture_output=True, check=True)
        (tmp_path / ".saturnday-policy.yaml").write_text(
            "expected_findings:\n  - license\n  - readme\n"
        )

        # Simulate what the pre-commit hook does:
        # saturnday governance --repo . --staged --policy .saturnday-policy.yaml
        result = subprocess.run(
            ["python", "-m", "saturnday.cli", "governance",
             "--repo", str(tmp_path),
             "--staged",
             "--policy", str(tmp_path / ".saturnday-policy.yaml")],
            capture_output=True, text=True, timeout=60,
        )
        # Must not crash with UnboundLocalError
        assert "UnboundLocalError" not in result.stderr
        assert "load_policy" not in result.stderr or "UnboundLocalError" not in result.stderr
        # Must produce a governance result (exit 0 or 1, not a traceback crash)
        assert result.returncode in (0, 1)
        assert "Disposition:" in result.stdout or "Findings:" in result.stdout
