"""Tests for Phase 1 type-checking governance checks (mypy + tsc).

Covers T001 (_check_mypy), T002 (check_type_check_ts), and T003 wiring.
All subprocess calls are mocked — mypy and tsc are NOT required to be
installed for these tests to pass.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.review import _check_mypy, run_review
from saturnday.review_ts import (
    ALL_TS_CHECKS,
    _load_tsc_baseline,
    check_type_check_ts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a minimal repo directory with the given files."""
    for rel, content in files.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return tmp_path


def _fake_run_shell_func(cmd, *, cwd, timeout_s, env=None):
    """Minimal stub for run_review's run_shell_func parameter."""
    return {"status": "SKIPPED", "stdout": "", "stderr": "", "returncode": 0}


# ---------------------------------------------------------------------------
# T001: _check_mypy
# ---------------------------------------------------------------------------

class TestCheckMypy:
    def test_mypy_not_available_returns_skipped(self, tmp_path):
        """When mypy is not on PATH the check must return SKIPPED."""
        repo = _make_repo(tmp_path, {"foo.py": "x: int = 'oops'\n"})
        with patch("shutil.which", return_value=None):
            result = _check_mypy(repo, ["foo.py"])
        assert result["status"] == "SKIPPED"
        assert result["error"] == "mypy_not_found"
        assert result["findings"] == []
        assert result["severity"] == "warning"

    def test_mypy_no_python_files_returns_pass(self, tmp_path):
        """When no .py files in changed_files the check returns PASS immediately."""
        repo = _make_repo(tmp_path, {"README.md": "# hello\n"})
        result = _check_mypy(repo, ["README.md"])
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_mypy_clean_file_passes(self, tmp_path):
        """A run with no errors in mypy output yields PASS and no findings."""
        repo = _make_repo(tmp_path, {"good.py": "x: int = 1\n"})
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            result = _check_mypy(repo, ["good.py"])
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_mypy_type_error_detected(self, tmp_path):
        """mypy error lines are parsed into findings."""
        repo = _make_repo(tmp_path, {"bad.py": "x: int = 'oops'\n"})
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "bad.py:1: error: Incompatible types [assignment]\n"
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            result = _check_mypy(repo, ["bad.py"])
        assert result["status"] == "WARN"
        assert len(result["findings"]) == 1
        finding = result["findings"][0]
        assert finding["file"] == "bad.py"
        assert finding["line"] == 1
        assert finding["kind"] == "mypy_error"
        assert "Incompatible" in finding["detail"]

    def test_mypy_type_error_severity_is_warning(self, tmp_path):
        """Findings must carry warning severity and NOT be FAIL."""
        repo = _make_repo(tmp_path, {"bad.py": "x: int = 'oops'\n"})
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "bad.py:1: error: Incompatible types [assignment]\n"
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            result = _check_mypy(repo, ["bad.py"])
        assert result["status"] == "WARN"
        assert result["status"] != "FAIL"
        assert result["severity"] == "warning"

    def test_mypy_respects_config_no_extra_flag(self, tmp_path):
        """When a pyproject.toml [tool.mypy] section is present, the
        --ignore-missing-imports flag must NOT be added to the command."""
        repo = _make_repo(tmp_path, {
            "foo.py": "x: int = 1\n",
            "pyproject.toml": "[tool.mypy]\nstrict = true\n",
        })
        captured_cmd = []

        def _capture_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", side_effect=_capture_run):
            _check_mypy(repo, ["foo.py"])

        assert "--ignore-missing-imports" not in captured_cmd

    def test_mypy_no_config_adds_ignore_missing_imports(self, tmp_path):
        """Without any config, --ignore-missing-imports is included."""
        repo = _make_repo(tmp_path, {"foo.py": "x: int = 1\n"})
        captured_cmd = []

        def _capture_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", side_effect=_capture_run):
            _check_mypy(repo, ["foo.py"])

        assert "--ignore-missing-imports" in captured_cmd

    def test_mypy_baseline_ratchet_suppresses_existing_errors(self, tmp_path):
        """Pre-existing errors recorded in baseline must NOT appear in findings."""
        repo = _make_repo(tmp_path, {"bad.py": "x: int = 'oops'\n"})
        saturnday_dir = repo / ".saturnday"
        saturnday_dir.mkdir()
        # Baseline says bad.py already has 1 error
        (saturnday_dir / "mypy-baseline.json").write_text(
            json.dumps({"bad.py": 1}), encoding="utf-8"
        )

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "bad.py:1: error: Incompatible types [assignment]\n"
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            result = _check_mypy(repo, ["bad.py"])

        # 1 raw error == baseline count of 1  →  zero NEW findings
        assert result["findings"] == []
        assert result["status"] == "PASS"

    def test_mypy_baseline_ratchet_surfaces_new_errors(self, tmp_path):
        """Errors exceeding the baseline count must appear as findings."""
        repo = _make_repo(tmp_path, {"bad.py": "x = 1\n"})
        saturnday_dir = repo / ".saturnday"
        saturnday_dir.mkdir()
        # Baseline says bad.py had 1 error; now there are 2
        (saturnday_dir / "mypy-baseline.json").write_text(
            json.dumps({"bad.py": 1}), encoding="utf-8"
        )

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = (
            "bad.py:1: error: First error [assignment]\n"
            "bad.py:2: error: Second error [assignment]\n"
        )
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            result = _check_mypy(repo, ["bad.py"])

        # 2 raw errors, baseline is 1 → 1 new finding surfaced
        assert len(result["findings"]) == 1
        assert result["status"] == "WARN"

    def test_mypy_timeout_returns_skipped(self, tmp_path):
        """A subprocess timeout must result in SKIPPED, not a crash."""
        repo = _make_repo(tmp_path, {"bad.py": "x: int = 'oops'\n"})
        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="mypy", timeout=30)):
            result = _check_mypy(repo, ["bad.py"])
        assert result["status"] == "SKIPPED"
        assert result["error"] == "mypy_timeout"


# ---------------------------------------------------------------------------
# T002: check_type_check_ts
# ---------------------------------------------------------------------------

class TestCheckTypeCheckTs:
    def test_tsc_type_check_not_available_skipped(self, tmp_path):
        """SKIPPED when tsc binary is not on PATH."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
            "src/app.ts": "const x: number = 1;\n",
        })
        with patch("shutil.which", return_value=None):
            result = check_type_check_ts(repo, ["src/app.ts"])
        assert result["status"] == "SKIPPED"
        assert result["name"] == "type_check_ts"

    def test_tsc_type_check_no_tsconfig_skipped(self, tmp_path):
        """SKIPPED when tsconfig.json does not exist."""
        repo = _make_repo(tmp_path, {"src/app.ts": "const x: number = 1;\n"})
        with patch("shutil.which", return_value="/usr/bin/tsc"):
            result = check_type_check_ts(repo, ["src/app.ts"])
        assert result["status"] == "SKIPPED"

    def test_tsc_type_check_clean_passes(self, tmp_path):
        """No errors in tsc output → PASS."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
        })
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/tsc"), \
             patch("subprocess.run", return_value=mock_result):
            result = check_type_check_ts(repo, [])
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_tsc_type_check_error_detected(self, tmp_path):
        """tsc error output is parsed into findings."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
            "src/app.ts": "const x: number = 'oops';\n",
        })
        tsc_output = "src/app.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = tsc_output
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/tsc"), \
             patch("subprocess.run", return_value=mock_result):
            result = check_type_check_ts(repo, ["src/app.ts"])
        assert result["status"] == "WARN"
        assert result["severity"] == "warning"
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert "TS2322" in f["detail"]
        assert f["line"] == 1

    def test_tsc_type_check_severity_is_warning_not_fail(self, tmp_path):
        """type_check_ts must never return FAIL — always WARN or PASS."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
        })
        tsc_output = "app.ts(1,7): error TS2322: Something wrong.\n"
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = tsc_output
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/tsc"), \
             patch("subprocess.run", return_value=mock_result):
            result = check_type_check_ts(repo, ["app.ts"])
        assert result["status"] != "FAIL"

    def test_tsc_type_check_filters_to_changed_files(self, tmp_path):
        """Errors in files not in changed_files must be suppressed."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
        })
        tsc_output = (
            "other.ts(1,7): error TS2322: Type mismatch in other.\n"
            "changed.ts(5,3): error TS2304: Cannot find name.\n"
        )
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = tsc_output
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/tsc"), \
             patch("subprocess.run", return_value=mock_result):
            result = check_type_check_ts(repo, ["changed.ts"])
        # Only the changed.ts finding should survive the filter
        assert all("changed.ts" in f["file"] for f in result["findings"])
        assert all("other.ts" not in f["file"] for f in result["findings"])

    def test_tsc_type_check_baseline_ratchet(self, tmp_path):
        """Pre-existing errors in baseline are suppressed."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
        })
        saturnday_dir = repo / ".saturnday"
        saturnday_dir.mkdir()
        (saturnday_dir / "tsc-baseline.json").write_text(
            json.dumps({"src/app.ts": 1}), encoding="utf-8"
        )
        tsc_output = "src/app.ts(1,7): error TS2322: Type mismatch.\n"
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = tsc_output
        mock_result.stderr = ""
        with patch("shutil.which", return_value="/usr/bin/tsc"), \
             patch("subprocess.run", return_value=mock_result):
            result = check_type_check_ts(repo, ["src/app.ts"])
        # 1 raw error == baseline count → zero new findings
        assert result["findings"] == []
        assert result["status"] == "PASS"

    def test_tsc_type_check_registered_in_all_ts_checks(self):
        """check_type_check_ts must appear in ALL_TS_CHECKS."""
        names = [fn.__name__ for fn in ALL_TS_CHECKS]
        assert "check_type_check_ts" in names

    def test_tsc_type_check_timeout_returns_skipped(self, tmp_path):
        """Subprocess timeout degrades to SKIPPED gracefully."""
        repo = _make_repo(tmp_path, {
            "tsconfig.json": '{"compilerOptions": {}}',
        })
        with patch("shutil.which", return_value="/usr/bin/tsc"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="tsc", timeout=30)):
            result = check_type_check_ts(repo, [])
        assert result["status"] == "SKIPPED"
        assert result["error"] == "tsc_timeout"


# ---------------------------------------------------------------------------
# T003 wiring: run_review includes mypy
# ---------------------------------------------------------------------------

class TestRunReviewMypy:
    def test_run_review_includes_mypy_tool_entry(self, tmp_path):
        """run_review must include a 'mypy' entry in the returned tools dict."""
        repo = _make_repo(tmp_path, {"mod.py": "x: int = 1\n"})

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            review = run_review(
                repo,
                ["mod.py"],
                tmp_path,
                run_shell_func=_fake_run_shell_func,
                timeout_s=5,
            )

        assert "mypy" in review["tools"]

    def test_mypy_warning_does_not_fail_disposition(self, tmp_path):
        """WARNING findings from mypy must NOT flip governance disposition to FAIL."""
        repo = _make_repo(tmp_path, {"bad.py": "x: int = 'oops'\n"})

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "bad.py:1: error: Incompatible types [assignment]\n"
        mock_result.stderr = ""

        with patch("shutil.which", return_value="/usr/bin/mypy"), \
             patch("subprocess.run", return_value=mock_result):
            review = run_review(
                repo,
                ["bad.py"],
                tmp_path,
                run_shell_func=_fake_run_shell_func,
                timeout_s=5,
            )

        mypy_entry = review["tools"].get("mypy", {})
        # mypy fires WARN for the finding
        assert mypy_entry.get("status") in ("WARN", "PASS", "SKIPPED")
        # Overall disposition must NOT be FAIL solely due to mypy
        # (it may be FAIL for other reasons, so we check mypy specifically)
        assert mypy_entry.get("status") != "FAIL"

    def test_mypy_skipped_does_not_add_to_errors(self, tmp_path):
        """A SKIPPED mypy result must not appear in the errors list."""
        repo = _make_repo(tmp_path, {"mod.py": "x: int = 1\n"})

        with patch("shutil.which", return_value=None):
            review = run_review(
                repo,
                ["mod.py"],
                tmp_path,
                run_shell_func=_fake_run_shell_func,
                timeout_s=5,
            )

        assert all("mypy" not in e for e in review.get("errors", []))
