"""Tests for Fix 63 — Bandit false FAIL with 0 findings.

Exhaustive path coverage for the Bandit status-determination logic in
_run_json_tool.  Each test simulates a shell record and proves the
resulting status is correct.

Paths tested:
1. Clean run: returncode=0, valid JSON, 0 findings → PASS
2. Findings run: returncode=1, valid JSON, N findings → FAIL
3. Non-zero exit, zero findings: returncode=1, valid JSON, empty results → PASS (override)
4. JSON parse failure: stdout malformed → FAIL (error=parse_error)
5. Empty stdout: stdout="" → parsed as [] (not dict) → findings=[] → status depends on returncode
6. Command not found: status=ERROR → SKIPPED (non-strict) or FAIL (strict)
7. Command timeout: status=TIMEOUT → SKIPPED or FAIL
8. Stdout is valid JSON list (not dict): parsed is list → findings=[] → returncode-dependent
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from saturnday.review import _run_json_tool


def _make_shell_fn(record: dict):
    """Return a shell function that returns the given record."""
    def fn(cmd, *, cwd, timeout_s, env=None):
        return record
    return fn


class TestBanditCleanRun:
    def test_zero_exit_zero_findings_is_pass(self, tmp_path: Path) -> None:
        """Bandit: returncode=0, valid JSON, 0 findings → PASS."""
        bandit_json = json.dumps({"results": [], "metrics": {}})
        record = {"status": "RAN", "returncode": 0, "stdout": bandit_json, "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []
        assert result["error"] is None


class TestBanditWithFindings:
    def test_nonzero_exit_with_findings_is_fail(self, tmp_path: Path) -> None:
        """Bandit: returncode=1, valid JSON, findings present → FAIL."""
        bandit_json = json.dumps({
            "results": [
                {"filename": "app.py", "line_number": 5, "issue_text": "hardcoded password",
                 "test_id": "B105", "issue_severity": "LOW"}
            ]
        })
        record = {"status": "RAN", "returncode": 1, "stdout": bandit_json, "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "FAIL"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["test_id"] == "B105"


class TestBanditNonZeroExitZeroFindings:
    def test_nonzero_exit_empty_results_is_pass(self, tmp_path: Path) -> None:
        """Bandit: returncode=1, valid JSON dict, empty results → PASS (override at line 1589)."""
        bandit_json = json.dumps({"results": [], "metrics": {}})
        record = {"status": "RAN", "returncode": 1, "stdout": bandit_json, "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS", \
            f"Expected PASS from override but got {result['status']}"
        assert result["findings"] == []


class TestBanditJsonParseFailure:
    def test_malformed_stdout_is_fail(self, tmp_path: Path) -> None:
        """Bandit: stdout is malformed JSON → FAIL with error=parse_error."""
        record = {"status": "RAN", "returncode": 0, "stdout": "NOT JSON {{{", "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "FAIL"
        assert result["error"] == "parse_error"
        assert result["findings"] == []


class TestBanditEmptyStdout:
    def test_empty_stdout_returncode_zero_is_pass(self, tmp_path: Path) -> None:
        """Bandit: stdout empty, returncode=0 → parsed as [] (list, not dict) →
        findings=[] → returncode==0 → status stays PASS."""
        record = {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_empty_stdout_returncode_nonzero_is_pass_via_override(self, tmp_path: Path) -> None:
        """Bandit: stdout empty, returncode=1 → parsed as [] → findings=[] →
        line 1587 sets FAIL (returncode!=0) → line 1589 override sets PASS
        (bandit, no findings, no error)."""
        record = {"status": "RAN", "returncode": 1, "stdout": "", "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS", \
            f"Expected PASS via override but got {result['status']}"


class TestBanditNoneStdout:
    def test_none_stdout_returncode_zero_is_pass(self, tmp_path: Path) -> None:
        """Bandit: stdout=None → falls back to '[]' → parsed as list →
        findings=[] → returncode=0 → PASS."""
        record = {"status": "RAN", "returncode": 0, "stdout": None, "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS"


class TestBanditCommandFailure:
    def test_command_not_found_nonstrict_is_skipped(self, tmp_path: Path) -> None:
        """Bandit not installed, non-strict → SKIPPED."""
        record = {"status": "ERROR", "error": "command_not_found", "returncode": None}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "SKIPPED"

    def test_command_not_found_strict_is_fail(self, tmp_path: Path) -> None:
        """Bandit not installed, strict mode → FAIL."""
        record = {"status": "ERROR", "error": "command_not_found", "returncode": None}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=True, required=True, tool_runs=[],
        )
        assert result["status"] == "FAIL"

    def test_command_timeout_nonstrict_is_skipped(self, tmp_path: Path) -> None:
        """Bandit timeout, non-strict → SKIPPED."""
        record = {"status": "TIMEOUT", "returncode": None}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "SKIPPED"


class TestBanditParsedTypeMismatch:
    def test_parsed_is_list_not_dict_zero_exit(self, tmp_path: Path) -> None:
        """Bandit: stdout is valid JSON but a list not a dict → findings=[] →
        returncode=0 → PASS."""
        record = {"status": "RAN", "returncode": 0, "stdout": "[]", "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS"
        assert result["findings"] == []

    def test_parsed_is_list_not_dict_nonzero_exit(self, tmp_path: Path) -> None:
        """Bandit: stdout is valid JSON list, returncode=1 → findings=[] →
        override fires → PASS."""
        record = {"status": "RAN", "returncode": 1, "stdout": "[]", "stderr": ""}
        result, findings = _run_json_tool(
            "bandit", ["bandit"], tmp_path,
            run_shell_func=_make_shell_fn(record),
            timeout_s=60, strict=False, required=True, tool_runs=[],
        )
        assert result["status"] == "PASS", \
            f"Expected PASS via override but got {result['status']}"
