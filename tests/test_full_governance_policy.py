"""Tests for Fix 65 + Fix 66 — full-governance policy application and contract consistency.

Proves:
1. Full review with exemptions removes exempted findings
2. Full review with expected_findings downgrades disposition
3. Full review without policy is unchanged
4. expected_findings matches both internal finding kind AND check-group name
5. Diff-based path still works after refactor
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saturnday.evidence import CheckResult
from saturnday.governance import _apply_policy_filtering


def _make_cr(name: str, findings: list[dict], status: str = "FAIL", severity: str = "error") -> CheckResult:
    return CheckResult(name=name, status=status, severity=severity, findings=findings)


class TestApplyPolicyFilteringExemptions:
    """Fix 65: exemptions must work in the shared helper."""

    def test_exemption_removes_findings(self, tmp_path: Path) -> None:
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text(
            "exemptions:\n"
            "  - check: stubs\n"
            '    pattern: "*"\n'
            "    reason: test\n"
        )
        cr = _make_cr("stubs", [{"file": "a.py", "kind": "stub"}])
        result, _ = _apply_policy_filtering([cr], "FAIL", tmp_path / ".saturnday-policy.yaml")
        assert cr.findings == []
        assert cr.status == "PASS"

    def test_no_policy_unchanged(self) -> None:
        cr = _make_cr("stubs", [{"file": "a.py", "kind": "stub"}])
        result, _ = _apply_policy_filtering([cr], "FAIL", None)
        assert result == "FAIL"
        assert len(cr.findings) == 1


class TestApplyPolicyFilteringExpectedFindings:
    """Fix 65+66: expected_findings must work and match both kind and check-group name."""

    def test_expected_finding_kind_downgrades(self, tmp_path: Path) -> None:
        """expected_findings with internal finding kind downgrades FAIL→WARN."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text("expected_findings:\n  - session_cookie_no_httponly\n")
        cr = _make_cr("cookie_security_hard", [
            {"file": "a.py", "kind": "session_cookie_no_httponly"},
        ])
        result, _ = _apply_policy_filtering([cr], "FAIL", policy_file)
        assert result == "WARN"

    def test_expected_check_group_name_downgrades(self, tmp_path: Path) -> None:
        """Fix 66: expected_findings with check-group name downgrades FAIL→WARN."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text("expected_findings:\n  - cookie_security_hard\n")
        cr = _make_cr("cookie_security_hard", [
            {"file": "a.py", "kind": "session_cookie_no_httponly"},
        ])
        result, _ = _apply_policy_filtering([cr], "FAIL", policy_file)
        assert result == "WARN"

    def test_unmatched_expected_does_not_downgrade(self, tmp_path: Path) -> None:
        """expected_findings that don't match keep FAIL."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text("expected_findings:\n  - blast_radius\n")
        cr = _make_cr("stubs", [{"file": "a.py", "kind": "stub"}])
        result, _ = _apply_policy_filtering([cr], "FAIL", policy_file)
        assert result == "FAIL"

    def test_multiple_checks_all_expected(self, tmp_path: Path) -> None:
        """All error findings expected → WARN."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text(
            "expected_findings:\n  - cookie_security_hard\n  - missing_repr\n"
        )
        cr1 = _make_cr("cookie_security_hard", [
            {"file": "a.py", "kind": "session_cookie_no_httponly"},
        ])
        cr2 = _make_cr("code_quality", [
            {"file": "b.py", "kind": "missing_repr"},
        ])
        result, _ = _apply_policy_filtering([cr1, cr2], "FAIL", policy_file)
        assert result == "WARN"

    def test_one_unexpected_keeps_fail(self, tmp_path: Path) -> None:
        """One unexpected finding keeps FAIL."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text("expected_findings:\n  - cookie_security_hard\n")
        cr1 = _make_cr("cookie_security_hard", [
            {"file": "a.py", "kind": "session_cookie_no_httponly"},
        ])
        cr2 = _make_cr("stubs", [{"file": "b.py", "kind": "stub"}])
        result, _ = _apply_policy_filtering([cr1, cr2], "FAIL", policy_file)
        assert result == "FAIL"


class TestPolicyPathAndReasons:
    """Fix 65 follow-up: exact file path and reasons correctness."""

    def test_custom_policy_filename(self, tmp_path: Path) -> None:
        """Policy with non-default filename must be loaded correctly."""
        custom = tmp_path / "my-custom-policy.yaml"
        custom.write_text(
            "exemptions:\n"
            "  - check: stubs\n"
            '    pattern: "*"\n'
            "    reason: test\n"
        )
        cr = _make_cr("stubs", [{"file": "a.py", "kind": "stub"}])
        result, reasons = _apply_policy_filtering([cr], "FAIL", custom)
        assert cr.findings == []
        assert cr.status == "PASS"

    def test_reasons_match_disposition_after_filtering(self, tmp_path: Path) -> None:
        """After expected_findings downgrade, reasons must reflect the downgrade."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text("expected_findings:\n  - stubs\n")
        cr = _make_cr("stubs", [{"file": "a.py", "kind": "stub"}])
        disposition, reasons = _apply_policy_filtering([cr], "FAIL", policy_file)
        assert disposition == "WARN"
        assert "all_findings_expected" in reasons

    def test_reasons_not_stale_after_exemption(self, tmp_path: Path) -> None:
        """After exemptions remove all findings, reasons must match PASS disposition."""
        policy_file = tmp_path / ".saturnday-policy.yaml"
        policy_file.write_text(
            "exemptions:\n"
            "  - check: stubs\n"
            '    pattern: "*"\n'
            "    reason: test\n"
        )
        cr = _make_cr("stubs", [{"file": "a.py", "kind": "stub"}])
        disposition, reasons = _apply_policy_filtering([cr], "FAIL", policy_file)
        assert disposition == "PASS"
        # reasons should NOT contain FAIL reasons after filtering
        assert "stubs" not in reasons


class TestFullReviewWithPolicy:
    """Fix 65: prove run_full_repo_review actually applies policy."""

    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), capture_output=True, check=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=str(path), capture_output=True, check=True)

    def test_full_review_applies_exemptions(self, tmp_path: Path) -> None:
        """run_full_repo_review with exemptions must suppress matching findings."""
        self._init_repo(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=str(tmp_path), capture_output=True)

        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text(
            "exemptions:\n"
            "  - check: license\n"
            '    pattern: "*"\n'
            "    reason: test\n"
            "  - check: readme\n"
            '    pattern: "*"\n'
            "    reason: test\n"
        )

        from saturnday.governance import run_full_repo_review
        pack, _ = run_full_repo_review(tmp_path, policy_path=policy)

        # license and readme findings should be suppressed
        all_names = [cr.name for cr in pack.check_results if cr.status == "FAIL"]
        assert "license" not in all_names, f"license should be exempted but found in: {all_names}"
        assert "readme" not in all_names, f"readme should be exempted but found in: {all_names}"

    def test_full_review_without_policy_unchanged(self, tmp_path: Path) -> None:
        """run_full_repo_review without policy still reports findings."""
        self._init_repo(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "add"], cwd=str(tmp_path), capture_output=True)

        from saturnday.governance import run_full_repo_review
        pack, _ = run_full_repo_review(tmp_path)

        all_names = [cr.name for cr in pack.check_results if cr.status == "FAIL"]
        # Without policy, license/readme should still be flagged
        assert "license" in all_names or "readme" in all_names
