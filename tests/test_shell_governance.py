"""Tests for shell script governance: shellcheck + line continuation checks."""

import json
from pathlib import Path

import pytest

from saturnday.review import _check_shellcheck, _check_line_continuations, run_review
from saturnday.shell_policy import run_shell
from saturnday.policy_manifest import HARD_CHECKS, ALL_CHECKS, default_policy


# ---------------------------------------------------------------------------
# TestCheckShellcheck
# ---------------------------------------------------------------------------

class TestCheckShellcheck:
    def _make_run_func(self, stdout="[]", returncode=0, status="RAN", error=None):
        def run_shell_func(cmd, *, cwd, timeout_s, env=None):
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": "",
                "status": status,
                "deny_reason": None,
                "error": error,
            }
        return run_shell_func

    def test_shellcheck_finds_errors(self, tmp_path):
        findings_json = json.dumps([
            {"file": "test.sh", "line": 10, "column": 5, "level": "warning",
             "code": 2086, "message": "Double quote to prevent globbing"},
        ])
        result = _check_shellcheck(
            tmp_path, ["test.sh"],
            run_shell_func=self._make_run_func(stdout=findings_json, returncode=1),
            timeout_s=10, strict=False, tool_runs=[],
        )
        assert result["status"] == "FAIL"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["code"] == 2086

    def test_shellcheck_passes_clean(self, tmp_path):
        result = _check_shellcheck(
            tmp_path, ["test.sh"],
            run_shell_func=self._make_run_func(stdout="[]", returncode=0),
            timeout_s=10, strict=False, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_shellcheck_filters_sc1090(self, tmp_path):
        findings_json = json.dumps([
            {"file": "test.sh", "line": 1, "column": 1, "level": "warning",
             "code": 1090, "message": "ShellCheck can't follow non-constant source"},
        ])
        result = _check_shellcheck(
            tmp_path, ["test.sh"],
            run_shell_func=self._make_run_func(stdout=findings_json, returncode=1),
            timeout_s=10, strict=False, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_shellcheck_filters_style(self, tmp_path):
        findings_json = json.dumps([
            {"file": "test.sh", "line": 5, "column": 1, "level": "style",
             "code": 2148, "message": "Tips depend on target shell"},
        ])
        result = _check_shellcheck(
            tmp_path, ["test.sh"],
            run_shell_func=self._make_run_func(stdout=findings_json, returncode=1),
            timeout_s=10, strict=False, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_shellcheck_skips_no_sh_files(self, tmp_path):
        result = _check_shellcheck(
            tmp_path, ["main.py", "lib.py"],
            run_shell_func=self._make_run_func(),
            timeout_s=10, strict=False, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["raw_output"] == "no shell files"

    def test_shellcheck_not_installed(self, tmp_path):
        result = _check_shellcheck(
            tmp_path, ["test.sh"],
            run_shell_func=self._make_run_func(status="ERROR", error="command_not_found", returncode=None),
            timeout_s=10, strict=False, tool_runs=[],
        )
        assert result["status"] == "SKIPPED"
        assert result["error"] == "command_not_found"


# ---------------------------------------------------------------------------
# TestCheckLineContinuations
# ---------------------------------------------------------------------------

class TestCheckLineContinuations:
    def test_valid_continuation_chain(self, tmp_path):
        script = tmp_path / "good.sh"
        script.write_text(
            '#!/bin/bash\n'
            'VAR_ONE="hello" \\\n'
            'VAR_TWO="world" \\\n'
            'my-command --flag\n'
        )
        result = _check_line_continuations(tmp_path, ["good.sh"])
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_broken_chain_blank_line(self, tmp_path):
        script = tmp_path / "bad.sh"
        script.write_text(
            '#!/bin/bash\n'
            'VAR_ONE="hello" \\\n'
            '\n'
            'my-command --flag\n'
        )
        result = _check_line_continuations(tmp_path, ["bad.sh"])
        assert result["status"] == "FAIL"
        assert any("blank line" in f["message"] for f in result["findings"])

    def test_broken_chain_new_statement(self, tmp_path):
        """The exact run_plan.sh bug: env var continuation broken by inserted assignment."""
        script = tmp_path / "broken.sh"
        script.write_text(
            '#!/bin/bash\n'
            'ENV_ONE="value1" \\\n'
            'EXTRA_ARGS=""\n'
            'ENV_TWO="value2" \\\n'
            'my-command\n'
        )
        result = _check_line_continuations(tmp_path, ["broken.sh"])
        assert result["status"] == "FAIL"
        assert any("new statement" in f["message"] for f in result["findings"])

    def test_trailing_whitespace_after_backslash(self, tmp_path):
        script = tmp_path / "space.sh"
        script.write_text(
            '#!/bin/bash\n'
            'VAR="hello" \\ \n'
            'my-command\n'
        )
        result = _check_line_continuations(tmp_path, ["space.sh"])
        assert result["status"] == "FAIL"
        assert any("trailing whitespace" in f["message"] for f in result["findings"])

    def test_no_shell_files(self, tmp_path):
        result = _check_line_continuations(tmp_path, ["main.py", "lib.py"])
        assert result["status"] == "PASS"
        assert result["raw_output"] == "no shell files"

    def test_valid_script_no_continuations(self, tmp_path):
        script = tmp_path / "simple.sh"
        script.write_text(
            '#!/bin/bash\n'
            'echo "hello world"\n'
            'exit 0\n'
        )
        result = _check_line_continuations(tmp_path, ["simple.sh"])
        assert result["status"] == "PASS"

    def test_orphaned_backslash_eof(self, tmp_path):
        script = tmp_path / "eof.sh"
        script.write_text(
            '#!/bin/bash\n'
            'VAR="hello" \\'
        )
        result = _check_line_continuations(tmp_path, ["eof.sh"])
        assert result["status"] == "FAIL"
        assert any("orphaned" in f["message"] for f in result["findings"])

    def test_continuation_in_string_ignored(self, tmp_path):
        """Backslash inside an echo argument is fine — it's not a broken chain."""
        script = tmp_path / "string.sh"
        script.write_text(
            '#!/bin/bash\n'
            'echo "hello world"\n'
            'echo "next line"\n'
        )
        result = _check_line_continuations(tmp_path, ["string.sh"])
        assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# TestShellPolicyAllowsShellcheck
# ---------------------------------------------------------------------------

class TestShellPolicyAllowsShellcheck:
    def test_shellcheck_allowed(self, tmp_path):
        result = run_shell(
            ["shellcheck", "-f", "json", "test.sh"],
            cwd=tmp_path, timeout_s=10,
        )
        # Should not be DENIED — it might be ERROR (command not found) but not DENIED
        assert result["status"] != "DENIED"

    def test_shellcheck_string_denied(self, tmp_path):
        result = run_shell(
            "shellcheck -f json test.sh",
            cwd=tmp_path, timeout_s=10,
        )
        assert result["status"] == "DENIED"


# ---------------------------------------------------------------------------
# TestPolicyManifestShellChecks
# ---------------------------------------------------------------------------

class TestPolicyManifestShellChecks:
    def test_shellcheck_in_hard_checks(self):
        assert "shellcheck" in HARD_CHECKS

    def test_line_continuations_in_hard_checks(self):
        assert "line_continuations" in HARD_CHECKS

    def test_default_policy_includes_shell(self):
        policy = default_policy()
        assert "shellcheck" in policy.checks
        assert policy.checks["shellcheck"].severity == "error"
        assert "line_continuations" in policy.checks
        assert policy.checks["line_continuations"].severity == "error"


# ---------------------------------------------------------------------------
# TestReviewIntegration
# ---------------------------------------------------------------------------

class TestReviewIntegration:
    def _make_run_func(self, shellcheck_stdout="[]"):
        def run_shell_func(cmd, *, cwd, timeout_s, env=None):
            stdout = ""
            returncode = 0
            if cmd and cmd[0] == "shellcheck":
                stdout = shellcheck_stdout
            elif cmd and cmd[0] == "ruff":
                stdout = "[]"
            elif cmd and cmd[0] == "bandit":
                stdout = '{"results": []}'
            elif cmd and cmd[0] == "pip-audit":
                stdout = '{"dependencies": []}'
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        return run_shell_func

    def test_review_runs_shellcheck_on_sh_files(self, tmp_path):
        # Create a .sh file and a .py file
        sh_file = tmp_path / "deploy.sh"
        sh_file.write_text("#!/bin/bash\necho hello\n")
        py_file = tmp_path / "main.py"
        py_file.write_text("print('hello')\n")

        result = run_review(
            tmp_path, ["deploy.sh", "main.py"], tmp_path,
            run_shell_func=self._make_run_func(),
            timeout_s=10,
        )
        assert "shellcheck" in result["tools"]
        assert result["tools"]["shellcheck"]["status"] == "PASS"
        assert "line_continuations" in result["tools"]
        assert result["tools"]["line_continuations"]["status"] == "PASS"

    def test_review_skips_shellcheck_no_sh(self, tmp_path):
        py_file = tmp_path / "main.py"
        py_file.write_text("print('hello')\n")

        result = run_review(
            tmp_path, ["main.py"], tmp_path,
            run_shell_func=self._make_run_func(),
            timeout_s=10,
        )
        assert "shellcheck" in result["tools"]
        assert result["tools"]["shellcheck"]["status"] == "PASS"
        assert result["tools"]["shellcheck"]["raw_output"] == "no shell files"
        assert "line_continuations" in result["tools"]
        assert result["tools"]["line_continuations"]["raw_output"] == "no shell files"
