"""Fix 35 — Bandit findings are remapped to the common finding schema.

Proves that:
1. Bandit findings rendered with non-empty ``file`` (was empty before fix).
2. Bandit findings rendered with non-empty ``detail`` (was ``(no detail)`` before fix).
3. ``_filter_bandit_findings()`` still receives raw Bandit dicts (B101/test-path still filtered).
4. Non-Bandit (ruff) tool behaviour is unchanged.
5. No stale encoding of the empty-render behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Helper: minimal run_shell_func stub
# ---------------------------------------------------------------------------

def _make_shell_stub(tool_outputs: dict) -> object:
    """Return a run_shell_func that serves canned JSON for named tools."""
    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        tool = cmd[0]
        stdout, returncode = tool_outputs.get(tool, ("", 0))
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
    return run_shell_stub


# ---------------------------------------------------------------------------
# Directly test _run_json_tool for Bandit
# ---------------------------------------------------------------------------

def test_bandit_finding_file_field_populated(tmp_path: Path) -> None:
    """Bandit findings have non-empty 'file' after remapping."""
    from saturnday.review import _run_json_tool

    raw_json = json.dumps({
        "results": [
            {
                "filename": "src/app.py",
                "line_number": 42,
                "issue_text": "subprocess call — check for execution of untrusted input.",
                "test_id": "B603",
                "issue_severity": "LOW",
                "issue_confidence": "HIGH",
            }
        ],
        "errors": [],
    })

    def stub(cmd, *, cwd, timeout_s, env=None):
        return {
            "kind": "shell", "argv": cmd, "cwd": str(cwd),
            "timeout_s": timeout_s, "returncode": 1,
            "stdout": raw_json, "stderr": "", "status": "RAN",
            "deny_reason": None, "error": None,
        }

    result, findings = _run_json_tool(
        "bandit",
        ["bandit", "-f", "json", "src/app.py"],
        tmp_path,
        run_shell_func=stub,
        timeout_s=30,
        strict=True,
        required=True,
        tool_runs=[],
    )

    assert len(findings) == 1
    assert findings[0]["file"] == "src/app.py", (
        "'file' must be populated from Bandit 'filename'"
    )
    assert findings[0]["file"] != "", "file must not be empty"


def test_bandit_finding_detail_field_populated(tmp_path: Path) -> None:
    """Bandit findings have non-empty 'detail' after remapping."""
    from saturnday.review import _run_json_tool

    raw_json = json.dumps({
        "results": [
            {
                "filename": "src/app.py",
                "line_number": 42,
                "issue_text": "subprocess call — check for execution of untrusted input.",
                "test_id": "B603",
                "issue_severity": "LOW",
            }
        ],
        "errors": [],
    })

    def stub(cmd, *, cwd, timeout_s, env=None):
        return {
            "kind": "shell", "argv": cmd, "cwd": str(cwd),
            "timeout_s": timeout_s, "returncode": 1,
            "stdout": raw_json, "stderr": "", "status": "RAN",
            "deny_reason": None, "error": None,
        }

    result, findings = _run_json_tool(
        "bandit",
        ["bandit", "-f", "json", "src/app.py"],
        tmp_path,
        run_shell_func=stub,
        timeout_s=30,
        strict=True,
        required=True,
        tool_runs=[],
    )

    assert len(findings) == 1
    assert findings[0]["detail"] != "", "detail must not be empty"
    assert "subprocess" in findings[0]["detail"], (
        "detail must contain the Bandit issue_text"
    )


def test_bandit_finding_line_field_populated(tmp_path: Path) -> None:
    """Bandit findings have non-None 'line' after remapping."""
    from saturnday.review import _run_json_tool

    raw_json = json.dumps({
        "results": [
            {
                "filename": "src/app.py",
                "line_number": 42,
                "issue_text": "Consider possible security implications.",
                "test_id": "B404",
                "issue_severity": "LOW",
            }
        ],
        "errors": [],
    })

    def stub(cmd, *, cwd, timeout_s, env=None):
        return {
            "kind": "shell", "argv": cmd, "cwd": str(cwd),
            "timeout_s": timeout_s, "returncode": 1,
            "stdout": raw_json, "stderr": "", "status": "RAN",
            "deny_reason": None, "error": None,
        }

    result, findings = _run_json_tool(
        "bandit",
        ["bandit", "-f", "json", "src/app.py"],
        tmp_path,
        run_shell_func=stub,
        timeout_s=30,
        strict=True,
        required=True,
        tool_runs=[],
    )

    assert findings[0]["line"] == 42, "line must be populated from Bandit line_number"


def test_bandit_filter_still_runs_on_raw_dicts(tmp_path: Path) -> None:
    """_filter_bandit_findings() still receives raw Bandit dicts — B101 in test file is dropped."""
    from saturnday.review import _run_json_tool

    # B101 in a test-path file must be filtered before remapping
    raw_json = json.dumps({
        "results": [
            {
                "filename": "./tests/test_example.py",
                "line_number": 5,
                "issue_text": "Use of assert detected.",
                "test_id": "B101",
                "issue_severity": "LOW",
            },
            {
                "filename": "src/app.py",
                "line_number": 42,
                "issue_text": "subprocess call — check for execution of untrusted input.",
                "test_id": "B603",
                "issue_severity": "LOW",
            },
        ],
        "errors": [],
    })

    def stub(cmd, *, cwd, timeout_s, env=None):
        return {
            "kind": "shell", "argv": cmd, "cwd": str(cwd),
            "timeout_s": timeout_s, "returncode": 1,
            "stdout": raw_json, "stderr": "", "status": "RAN",
            "deny_reason": None, "error": None,
        }

    result, findings = _run_json_tool(
        "bandit",
        ["bandit", "-f", "json", "src/app.py", "tests/test_example.py"],
        tmp_path,
        run_shell_func=stub,
        timeout_s=30,
        strict=True,
        required=True,
        tool_runs=[],
    )

    # B101 in test path must be filtered; only B603 in src survives
    assert len(findings) == 1, "B101 in test file must be filtered out"
    assert findings[0]["test_id"] == "B603"
    assert findings[0]["file"] == "src/app.py"


def test_ruff_findings_unchanged(tmp_path: Path) -> None:
    """Ruff finding dicts are not affected by the Bandit remapping change."""
    from saturnday.review import _run_json_tool

    ruff_json = json.dumps([
        {
            "filename": "/repo/src/app.py",
            "message": "Local variable `x` is assigned to but never used",
            "code": "F841",
            "location": {"row": 10, "column": 4},
        }
    ])

    def stub(cmd, *, cwd, timeout_s, env=None):
        return {
            "kind": "shell", "argv": cmd, "cwd": str(cwd),
            "timeout_s": timeout_s, "returncode": 1,
            "stdout": ruff_json, "stderr": "", "status": "RAN",
            "deny_reason": None, "error": None,
        }

    result, findings = _run_json_tool(
        "ruff",
        ["ruff", "check", "--output-format=json", "src/app.py"],
        tmp_path,
        run_shell_func=stub,
        timeout_s=30,
        strict=True,
        required=True,
        tool_runs=[],
    )

    # Ruff findings pass through raw — 'filename' and 'message' are the original keys
    assert len(findings) == 1
    assert findings[0].get("filename") == "/repo/src/app.py", (
        "Ruff findings must not be remapped"
    )
    assert findings[0].get("message") == "Local variable `x` is assigned to but never used"
