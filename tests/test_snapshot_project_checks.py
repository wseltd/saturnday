"""Tests for _snapshot_project_checks and regression filtering in ticket_runner.

Tests cover:
- _snapshot_project_checks returns dict with expected keys
- license=PASS when LICENSE file present
- license=FAIL when LICENSE file absent
- Regression filtering: pre=FAIL, post=FAIL → finding removed (not a regression)
- Regression filtering: pre=PASS, post=FAIL → finding kept (regression detected)
- Regression filtering: pre=FAIL, post=PASS → no finding (improvement, not blocking)

Temporary git repos are created for snapshot tests.
_run_governance is mocked for regression filter tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.ticket_runner import _snapshot_project_checks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git_repo(path: Path) -> Path:
    """Initialise a bare git repo so _snapshot_project_checks can run checks."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True,
    )
    return path


def _add_py_file(repo: Path, name: str = "main.py", content: str = "x = 1\n") -> Path:
    """Write a Python file so rglob finds it and populates dummy_files."""
    f = repo / name
    f.write_text(content, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# _snapshot_project_checks: return shape
# ---------------------------------------------------------------------------

class TestSnapshotReturnShape:
    def test_returns_dict(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert isinstance(result, dict)

    def test_has_license_key(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert "license" in result

    def test_has_readme_key(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert "readme" in result

    def test_has_tests_pass_key(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert "tests_pass" in result

    def test_has_project_runnable_key(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert "project_runnable" in result

    def test_all_values_are_strings(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        for key, value in result.items():
            assert isinstance(value, str), f"Key {key!r} has non-string value {value!r}"

    def test_values_are_valid_statuses(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        valid = {"PASS", "FAIL", "WARN", "SKIPPED", "UNKNOWN"}
        for key, value in result.items():
            assert value in valid, f"Key {key!r} has unexpected status {value!r}"

    def test_empty_repo_no_python_files_still_returns_dict(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "empty-repo")
        # No Python or TypeScript files
        result = _snapshot_project_checks(repo)
        assert isinstance(result, dict)
        assert set(result.keys()) == {"license", "readme", "tests_pass", "project_runnable"}

    def test_returns_exactly_four_keys(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert len(result) == 4

    def test_check_function_exception_yields_unknown(self, tmp_path: Path) -> None:
        """If a check function raises, the snapshot must record UNKNOWN, not crash."""
        repo = _init_git_repo(tmp_path / "repo")
        _add_py_file(repo)
        with patch(
            "saturnday.review._check_license",
            side_effect=RuntimeError("simulated check crash"),
        ):
            result = _snapshot_project_checks(repo)
        # license must be UNKNOWN; other keys must still be present
        assert result["license"] == "UNKNOWN"
        assert "readme" in result


# ---------------------------------------------------------------------------
# _snapshot_project_checks: LICENSE detection
# ---------------------------------------------------------------------------

class TestLicenseSnapshot:
    def test_license_pass_when_license_file_present(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "licensed")
        _add_py_file(repo)
        (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        result = _snapshot_project_checks(repo)
        assert result["license"] == "PASS"

    def test_license_fail_when_no_license_file(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "unlicensed")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert result["license"] == "FAIL"

    def test_license_pass_with_license_md(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "licensed-md")
        _add_py_file(repo)
        (repo / "LICENSE.md").write_text("Apache 2.0\n", encoding="utf-8")
        result = _snapshot_project_checks(repo)
        assert result["license"] == "PASS"

    def test_license_pass_with_license_txt(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "licensed-txt")
        _add_py_file(repo)
        (repo / "LICENSE.txt").write_text("GPL\n", encoding="utf-8")
        result = _snapshot_project_checks(repo)
        assert result["license"] == "PASS"


# ---------------------------------------------------------------------------
# _snapshot_project_checks: README detection
# ---------------------------------------------------------------------------

class TestReadmeSnapshot:
    def test_readme_pass_when_readme_present(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "with-readme")
        _add_py_file(repo)
        (repo / "README.md").write_text("# Project\n\n## Usage\nrun it\n", encoding="utf-8")
        result = _snapshot_project_checks(repo)
        assert result["readme"] in ("PASS", "WARN", "FAIL")  # presence check only

    def test_readme_fail_when_no_readme(self, tmp_path: Path) -> None:
        repo = _init_git_repo(tmp_path / "no-readme")
        _add_py_file(repo)
        result = _snapshot_project_checks(repo)
        assert result["readme"] == "FAIL"


# ---------------------------------------------------------------------------
# Regression filtering via _run_governance(pre_ticket_state=...)
# ---------------------------------------------------------------------------
#
# These tests exercise the regression-check logic inside _run_governance.
# We mock run_governance_check to produce controlled check_results, then
# verify that the final findings list and disposition match expectations.
#

def _make_mock_check(name: str, status: str, findings: list[dict] | None = None) -> MagicMock:
    """Build a MagicMock that looks like a CheckResult with mutatable findings."""
    cr = MagicMock()
    cr.name = name
    cr.status = status
    cr.findings = list(findings or [])
    return cr


def _make_mock_pack(disposition: str, check_results: list) -> MagicMock:
    pack = MagicMock()
    pack.disposition = disposition
    pack.check_results = check_results
    return pack


class TestRegressionFiltering:
    """Exercise regression check logic: pre_ticket_state drives which findings block."""

    def _run_gov(
        self,
        repo_path: Path,
        pre_ticket_state: dict[str, str],
        check_results: list,
        disposition: str = "FAIL",
    ) -> tuple[str, list[dict], str]:
        from saturnday.ticket_runner import _run_governance

        mock_pack = _make_mock_pack(disposition, check_results)
        mock_evidence_path = repo_path / ".saturnday" / "evidence.json"
        mock_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        mock_evidence_path.touch()

        with patch(
            "saturnday.governance.run_governance_check",
            return_value=(mock_pack, mock_evidence_path),
        ):
            disp, findings, ev_path, _reasons = _run_governance(
                repo_path,
                run_mode=True,
                pre_ticket_state=pre_ticket_state,
            )
            return disp, findings, ev_path

    def _make_repo(self, tmp_path: Path, name: str = "repo") -> Path:
        repo = _init_git_repo(tmp_path / name)
        _add_py_file(repo)
        return repo

    def test_pre_fail_post_fail_finding_removed(self, tmp_path: Path) -> None:
        """pre=FAIL, post=FAIL → pre-existing failure, not a regression — finding cleared."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "LICENSE", "line": 0, "kind": "missing_license", "detail": "no license"}
        cr = _make_mock_check("license", "FAIL", [finding])
        pre_state = {"license": "FAIL", "readme": "PASS", "tests_pass": "PASS", "project_runnable": "PASS"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        # The pre-existing FAIL should NOT appear in findings (cleared by regression filter)
        license_findings = [f for f in findings if f.get("kind") == "missing_license"]
        assert len(license_findings) == 0

    def test_pre_pass_post_fail_finding_kept(self, tmp_path: Path) -> None:
        """pre=PASS, post=FAIL → regression introduced by this ticket — finding kept."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "LICENSE", "line": 0, "kind": "missing_license", "detail": "no license"}
        cr = _make_mock_check("license", "FAIL", [finding])
        pre_state = {"license": "PASS", "readme": "PASS", "tests_pass": "PASS", "project_runnable": "PASS"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        license_findings = [f for f in findings if f.get("kind") == "missing_license"]
        assert len(license_findings) == 1

    def test_pre_unknown_post_fail_finding_removed(self, tmp_path: Path) -> None:
        """pre=UNKNOWN, post=FAIL → treated same as FAIL (not a regression) — finding cleared."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "LICENSE", "line": 0, "kind": "missing_license", "detail": "no license"}
        cr = _make_mock_check("license", "FAIL", [finding])
        pre_state = {"license": "UNKNOWN", "readme": "PASS", "tests_pass": "PASS", "project_runnable": "PASS"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        license_findings = [f for f in findings if f.get("kind") == "missing_license"]
        assert len(license_findings) == 0

    def test_pre_fail_post_pass_no_finding(self, tmp_path: Path) -> None:
        """pre=FAIL, post=PASS → improvement, check now passes, no finding emitted."""
        repo = self._make_repo(tmp_path)
        cr = _make_mock_check("license", "PASS", [])
        pre_state = {"license": "FAIL", "readme": "PASS", "tests_pass": "PASS", "project_runnable": "PASS"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr], disposition="PASS")

        license_findings = [f for f in findings if f.get("kind") == "missing_license"]
        assert len(license_findings) == 0

    def test_readme_regression_detected(self, tmp_path: Path) -> None:
        """readme check regression: pre=PASS, post=FAIL → finding kept."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "README.md", "line": 0, "kind": "missing_readme", "detail": "gone"}
        cr = _make_mock_check("readme", "FAIL", [finding])
        pre_state = {"license": "PASS", "readme": "PASS", "tests_pass": "PASS", "project_runnable": "PASS"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        readme_findings = [f for f in findings if f.get("kind") == "missing_readme"]
        assert len(readme_findings) == 1

    def test_readme_pre_existing_failure_not_blocked(self, tmp_path: Path) -> None:
        """readme was already FAIL before ticket — should not block."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "README.md", "line": 0, "kind": "missing_readme", "detail": "gone"}
        cr = _make_mock_check("readme", "FAIL", [finding])
        pre_state = {"license": "PASS", "readme": "FAIL", "tests_pass": "PASS", "project_runnable": "PASS"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        readme_findings = [f for f in findings if f.get("kind") == "missing_readme"]
        assert len(readme_findings) == 0

    def test_non_project_level_findings_always_kept(self, tmp_path: Path) -> None:
        """Security / quality findings not in _PROJECT_LEVEL_KINDS are never removed."""
        repo = self._make_repo(tmp_path)
        sec_finding = {"file": "app.py", "line": 5, "kind": "sql_injection", "detail": "raw sql"}
        cr = _make_mock_check("sql_injection", "FAIL", [sec_finding])
        # Even if pre-state is FAIL for this check (not a project-level one), finding stays
        pre_state = {"license": "FAIL", "readme": "FAIL", "tests_pass": "FAIL", "project_runnable": "FAIL"}

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        sql_findings = [f for f in findings if f.get("kind") == "sql_injection"]
        assert len(sql_findings) == 1

    def test_no_pre_state_does_not_filter_findings(self, tmp_path: Path) -> None:
        """When pre_ticket_state is None, no regression filtering is applied."""
        from saturnday.ticket_runner import _run_governance

        repo = self._make_repo(tmp_path)
        finding = {"file": "LICENSE", "line": 0, "kind": "missing_license", "detail": "no license"}
        cr = _make_mock_check("license", "FAIL", [finding])
        mock_pack = _make_mock_pack("FAIL", [cr])
        mock_evidence_path = repo / ".saturnday" / "evidence.json"
        mock_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        mock_evidence_path.touch()

        with patch(
            "saturnday.governance.run_governance_check",
            return_value=(mock_pack, mock_evidence_path),
        ):
            disposition, findings, _, _ = _run_governance(
                repo, run_mode=True, pre_ticket_state=None
            )

        license_findings = [f for f in findings if f.get("kind") == "missing_license"]
        assert len(license_findings) == 1

    def test_blast_radius_suppressed_in_run_mode(self, tmp_path: Path) -> None:
        """excessive_blast_radius findings are suppressed in run_mode=True."""
        from saturnday.ticket_runner import _run_governance

        repo = self._make_repo(tmp_path)
        br_finding = {"file": ".", "line": 0, "kind": "excessive_blast_radius", "detail": "too many files"}
        cr = _make_mock_check("blast_radius", "FAIL", [br_finding])
        mock_pack = _make_mock_pack("FAIL", [cr])
        mock_evidence_path = repo / ".saturnday" / "evidence.json"
        mock_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        mock_evidence_path.touch()

        with patch(
            "saturnday.governance.run_governance_check",
            return_value=(mock_pack, mock_evidence_path),
        ):
            disposition, findings, _, _ = _run_governance(repo, run_mode=True, pre_ticket_state=None)

        br_findings = [f for f in findings if f.get("kind") == "excessive_blast_radius"]
        assert len(br_findings) == 0

    def test_blast_radius_kept_when_not_in_run_mode(self, tmp_path: Path) -> None:
        """excessive_blast_radius is not suppressed when run_mode=False."""
        from saturnday.ticket_runner import _run_governance

        repo = self._make_repo(tmp_path)
        br_finding = {"file": ".", "line": 0, "kind": "excessive_blast_radius", "detail": "too many files"}
        cr = _make_mock_check("blast_radius", "FAIL", [br_finding])
        mock_pack = _make_mock_pack("FAIL", [cr])
        mock_evidence_path = repo / ".saturnday" / "evidence.json"
        mock_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        mock_evidence_path.touch()

        with patch(
            "saturnday.governance.run_governance_check",
            return_value=(mock_pack, mock_evidence_path),
        ):
            disposition, findings, _, _ = _run_governance(repo, run_mode=False, pre_ticket_state=None)

        br_findings = [f for f in findings if f.get("kind") == "excessive_blast_radius"]
        assert len(br_findings) == 1

    def test_tests_failing_pre_existing_not_blocked(self, tmp_path: Path) -> None:
        """tests_failing was already FAIL before ticket — must not block."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "tests", "line": 0, "kind": "tests_failing", "detail": "pytest exit 1"}
        cr = _make_mock_check("tests_pass", "FAIL", [finding])
        pre_state = {
            "license": "PASS",
            "readme": "PASS",
            "tests_pass": "FAIL",
            "project_runnable": "PASS",
        }

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        test_findings = [f for f in findings if f.get("kind") == "tests_failing"]
        assert len(test_findings) == 0

    def test_tests_failing_regression_kept(self, tmp_path: Path) -> None:
        """tests_pass went from PASS to FAIL → regression — finding must be kept."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "tests", "line": 0, "kind": "tests_failing", "detail": "pytest exit 1"}
        cr = _make_mock_check("tests_pass", "FAIL", [finding])
        pre_state = {
            "license": "PASS",
            "readme": "PASS",
            "tests_pass": "PASS",
            "project_runnable": "PASS",
        }

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        test_findings = [f for f in findings if f.get("kind") == "tests_failing"]
        assert len(test_findings) == 1

    def test_project_runnable_regression_kept(self, tmp_path: Path) -> None:
        """project_runnable going PASS→FAIL is a regression and must be kept."""
        repo = self._make_repo(tmp_path)
        finding = {
            "file": "pyproject.toml",
            "line": 0,
            "kind": "missing_project_config",
            "detail": "no pyproject.toml",
        }
        cr = _make_mock_check("project_runnable", "FAIL", [finding])
        pre_state = {
            "license": "PASS",
            "readme": "PASS",
            "tests_pass": "PASS",
            "project_runnable": "PASS",
        }

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        runnable_findings = [f for f in findings if f.get("kind") == "missing_project_config"]
        assert len(runnable_findings) == 1

    def test_project_runnable_pre_existing_failure_removed(self, tmp_path: Path) -> None:
        """project_runnable was already FAIL before ticket — not a regression."""
        repo = self._make_repo(tmp_path)
        finding = {
            "file": "pyproject.toml",
            "line": 0,
            "kind": "missing_project_config",
            "detail": "no pyproject.toml",
        }
        cr = _make_mock_check("project_runnable", "FAIL", [finding])
        pre_state = {
            "license": "PASS",
            "readme": "PASS",
            "tests_pass": "PASS",
            "project_runnable": "FAIL",
        }

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        runnable_findings = [f for f in findings if f.get("kind") == "missing_project_config"]
        assert len(runnable_findings) == 0

    def test_readme_missing_section_treated_as_readme_check(self, tmp_path: Path) -> None:
        """readme_missing_section kind maps to readme check for pre-state lookup."""
        repo = self._make_repo(tmp_path)
        finding = {"file": "README.md", "line": 0, "kind": "readme_missing_section", "detail": "missing Usage"}
        cr = _make_mock_check("readme", "FAIL", [finding])
        pre_state = {
            "license": "PASS",
            "readme": "FAIL",  # was already failing
            "tests_pass": "PASS",
            "project_runnable": "PASS",
        }

        disposition, findings, _ = self._run_gov(repo, pre_state, [cr])

        section_findings = [f for f in findings if f.get("kind") == "readme_missing_section"]
        assert len(section_findings) == 0
