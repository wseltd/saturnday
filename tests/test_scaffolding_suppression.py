"""Tests for Fix 62 — scaffolding-phase hygiene check suppression.

Proves the regression suppression in _run_governance correctly handles:
1. Pre-existing missing_readme/missing_license/missing_project_config are suppressed
2. Genuine regressions (was PASS, now FAIL) are NOT suppressed
3. Final full governance scan is unaffected (no suppression)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


class TestScaffoldingSuppressionBounded:
    """Bounded reproduction: per-ticket governance suppresses pre-existing hygiene failures."""

    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(path), capture_output=True, check=True)

    def test_preexisting_hygiene_suppressed_in_ticket_mode(self, tmp_path: Path) -> None:
        """In per-ticket mode (run_mode=True), pre-existing missing README/LICENSE
        are suppressed by the regression check. Only real code findings survive."""
        self._init_repo(tmp_path)
        # Create a .py file and stage it — simulates a ticket producing code
        (tmp_path / "app.py").write_text("def hello(): pass\n")
        subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), capture_output=True, check=True)

        from saturnday.ticket_runner import _run_governance, _snapshot_project_checks

        # Snapshot BEFORE — fresh repo has no README, no LICENSE, no pyproject.toml
        pre = _snapshot_project_checks(tmp_path)
        assert pre.get("license") == "FAIL", "Precondition: license must be FAIL on fresh repo"
        assert pre.get("readme") == "FAIL", "Precondition: readme must be FAIL on fresh repo"

        # Run governance in per-ticket mode with the pre-snapshot
        disposition, findings, _, _ = _run_governance(
            tmp_path, run_mode=True, pre_ticket_state=pre,
        )

        # Extract finding kinds
        kinds = [f.get("kind", "") for f in findings]

        # missing_readme and missing_license must NOT appear — they are pre-existing
        assert "missing_readme" not in kinds, \
            f"missing_readme should be suppressed but found in: {kinds}"
        assert "missing_license" not in kinds, \
            f"missing_license should be suppressed but found in: {kinds}"
        assert "missing_project_config" not in kinds, \
            f"missing_project_config should be suppressed but found in: {kinds}"

    def test_full_scan_still_catches_missing_hygiene(self, tmp_path: Path) -> None:
        """Full governance scan (--full) must still report missing README/LICENSE."""
        self._init_repo(tmp_path)
        (tmp_path / "app.py").write_text("def hello(): pass\n")
        subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add app"], cwd=str(tmp_path), capture_output=True, check=True)

        from saturnday.governance import run_full_repo_review

        pack, _ = run_full_repo_review(repo_path=tmp_path)

        all_findings = []
        for cr in pack.check_results:
            all_findings.extend(cr.findings)
        kinds = [f.get("kind", "") for f in all_findings]

        # Full scan must still report these — no suppression in full mode
        assert "missing_license" in kinds or "missing_readme" in kinds, \
            f"Full scan should catch hygiene issues but found: {kinds}"

    def test_regression_not_suppressed(self, tmp_path: Path) -> None:
        """If README existed before but coder removed it, that is a regression
        and must NOT be suppressed."""
        self._init_repo(tmp_path)
        # Create README with required sections — establishes PASS state
        (tmp_path / "README.md").write_text(
            "# My Project\n\n## Trade-offs\nNone.\n\n## Limitations\nNone.\n\n## Non-goals\nNone.\n"
        )
        subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add readme"], cwd=str(tmp_path), capture_output=True, check=True)

        from saturnday.ticket_runner import _snapshot_project_checks

        # Snapshot shows readme: PASS
        pre = _snapshot_project_checks(tmp_path)
        assert pre.get("readme") == "PASS", "Precondition: readme must be PASS after adding it"

        # Now delete README and stage the deletion — simulates coder breaking it
        (tmp_path / "README.md").unlink()
        subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), capture_output=True, check=True)

        from saturnday.ticket_runner import _run_governance

        disposition, findings, _, _ = _run_governance(
            tmp_path, run_mode=True, pre_ticket_state=pre,
        )

        kinds = [f.get("kind", "") for f in findings]

        # This IS a regression (was PASS, now FAIL) — must NOT be suppressed
        assert "missing_readme" in kinds, \
            f"Regression (README removed) should NOT be suppressed but missing_readme not in: {kinds}"
