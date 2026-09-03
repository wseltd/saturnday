"""Fix 40 — hard cross-ticket / cross-file consistency gate.

Proves that:
1. Two files written in separate ticket-like phases can individually PASS yet
   be caught by the new repo-wide checker when they share a constant name with
   conflicting literal values.
2. Same-name constants with identical literal values do not produce a false positive.
3. A conflicting same-name constant produces the
   ``cross_file_shared_default_divergence`` finding category.
4. The final run summary surfaces the hard consistency failure (DoD NOT MET).
5. CLI exit becomes non-success (return 1) when this gate fires.
6. Unrelated repo-wide advisory warnings remain non-binding (no false blocking).
7. The cross_ticket_consistency_failures field is written to run-summary.json.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**kw: Any) -> Any:
    from saturnday._types import RunResult
    defaults: dict[str, Any] = {
        "project_id": "fix40-test",
        "total_tickets": 2,
        "passed": 2,
        "failed": 0,
    }
    defaults.update(kw)
    return RunResult(**defaults)


def _capture_cli_summary(result: Any) -> str:
    from saturnday.cli import _print_run_summary
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_run_summary(result)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Test 1: conflicting constants across two files → finding fires
# ---------------------------------------------------------------------------

def test_conflicting_constants_across_two_files_is_caught(tmp_path: Path) -> None:
    """Two files individually OK; same-name constant with different values → FAIL."""
    from saturnday.post_checks import check_cross_file_shared_default_divergence

    src = tmp_path / "src"
    src.mkdir()

    (src / "module_a.py").write_text(
        'DEFAULT_OUTPUT = "/data/output"\n',
        encoding="utf-8",
    )
    (src / "module_b.py").write_text(
        'DEFAULT_OUTPUT = "/data/results"\n',
        encoding="utf-8",
    )

    findings = check_cross_file_shared_default_divergence(tmp_path)

    assert findings, "Expected at least one finding for conflicting DEFAULT_OUTPUT"
    assert any(f["constant"] == "DEFAULT_OUTPUT" for f in findings)
    assert any(f["category"] == "cross_file_shared_default_divergence" for f in findings)


# ---------------------------------------------------------------------------
# Test 2: identical values across two files → no false positive
# ---------------------------------------------------------------------------

def test_identical_constants_across_two_files_no_finding(tmp_path: Path) -> None:
    """Same constant name, same value → checker must remain silent."""
    from saturnday.post_checks import check_cross_file_shared_default_divergence

    src = tmp_path / "src"
    src.mkdir()

    (src / "module_a.py").write_text(
        'DEFAULT_OUTPUT = "/data/output"\n',
        encoding="utf-8",
    )
    (src / "module_b.py").write_text(
        'DEFAULT_OUTPUT = "/data/output"\n',
        encoding="utf-8",
    )

    findings = check_cross_file_shared_default_divergence(tmp_path)
    conflicting = [f for f in findings if f["constant"] == "DEFAULT_OUTPUT"]
    assert not conflicting, (
        f"No finding expected for identical DEFAULT_OUTPUT values; got {conflicting}"
    )


# ---------------------------------------------------------------------------
# Test 3: finding has correct category and structure
# ---------------------------------------------------------------------------

def test_finding_structure_is_correct(tmp_path: Path) -> None:
    """check_cross_file_shared_default_divergence returns well-formed findings."""
    from saturnday.post_checks import check_cross_file_shared_default_divergence

    src = tmp_path / "src"
    src.mkdir()

    (src / "alpha.py").write_text('BASE_URL = "https://api.example.com"\n', encoding="utf-8")
    (src / "beta.py").write_text('BASE_URL = "https://api.staging.com"\n', encoding="utf-8")

    findings = check_cross_file_shared_default_divergence(tmp_path)
    assert findings, "Expected a finding"

    f = findings[0]
    assert f["category"] == "cross_file_shared_default_divergence"
    assert "constant" in f
    assert "file_a" in f and "file_b" in f
    assert "value_a" in f and "value_b" in f
    assert "message" in f
    assert "BASE_URL" in f["message"]


# ---------------------------------------------------------------------------
# Test 4: run summary surfaces the hard consistency failure (DoD NOT MET)
# ---------------------------------------------------------------------------

def test_print_run_summary_shows_consistency_failure() -> None:
    """_print_run_summary must show Cross-ticket consistency FAILED."""
    result = _make_result(
        definition_of_done_met=False,
        cross_ticket_consistency_failures=(
            {
                "category": "cross_file_shared_default_divergence",
                "constant": "DEFAULT_OUTPUT",
                "file_a": "src/module_a.py",
                "value_a": "/data/output",
                "file_b": "src/module_b.py",
                "value_b": "/data/results",
                "message": "Shared constant 'DEFAULT_OUTPUT' has conflicting values.",
            },
        ),
    )
    out = _capture_cli_summary(result)
    assert "Cross-ticket consistency FAILED" in out
    assert "DEFAULT_OUTPUT" in out
    assert "Definition of done: NOT MET" in out


# ---------------------------------------------------------------------------
# Test 5: CLI exits non-success when consistency gate fires
# ---------------------------------------------------------------------------

def test_cmd_run_exit_nonzero_when_consistency_gate_fails(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """CLI returns 1 when cross_ticket_consistency_failures is non-empty."""
    import argparse
    from saturnday._types import RunResult
    from saturnday.cli import _cmd_run

    plan_path = tmp_path / ".saturnday" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({
        "project_id": "fix40-cli",
        "tickets": [],
        "phases": [],
        "definition_of_done": [],
    }), encoding="utf-8")

    ctc_result = RunResult(
        project_id="fix40-cli",
        total_tickets=2,
        passed=2,
        failed=0,
        definition_of_done_met=False,
        cross_ticket_consistency_failures=(
            {
                "category": "cross_file_shared_default_divergence",
                "constant": "DEFAULT_OUTPUT",
                "file_a": "src/a.py",
                "value_a": "/data/output",
                "file_b": "src/b.py",
                "value_b": "/data/results",
                "message": "Shared constant 'DEFAULT_OUTPUT' has conflicting values.",
            },
        ),
    )

    args = argparse.Namespace(
        plan=str(plan_path),
        repo=str(tmp_path),
        backend="claude-cli",
        standards_dir=str(tmp_path),
        output_dir=None,
        auto_repair=False,
        lessons=None,
        no_role_passes=False,
        strict_dod=False,
        verbose=False,
        api_key="",
        base_url="",
        model="",
        temperature=0.0,
        max_tokens=0,
        timeout=120,
    )

    with patch("saturnday.ticket_runner.run_plan", return_value=ctc_result), \
         patch("saturnday.cli._require_git_repo", return_value=True):
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            rc = _cmd_run(args)

    assert rc == 1, "CLI must return 1 when cross_ticket_consistency_failures is non-empty"
    assert "cross-ticket consistency" in stderr_buf.getvalue().lower()


# ---------------------------------------------------------------------------
# Test 6: CLI exits 0 when no consistency failures (happy path)
# ---------------------------------------------------------------------------

def test_cmd_run_exit_zero_when_consistency_gate_passes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """CLI returns 0 when all tickets pass and no consistency failures."""
    import argparse
    from saturnday._types import RunResult, TicketResult
    from saturnday.cli import _cmd_run

    plan_path = tmp_path / ".saturnday" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps({
        "project_id": "fix40-happy",
        "tickets": [],
        "phases": [],
        "definition_of_done": [],
    }), encoding="utf-8")

    happy_result = RunResult(
        project_id="fix40-happy",
        total_tickets=1,
        passed=1,
        failed=0,
        definition_of_done_met=True,
        plan_governance_met=True,
        plan_governance_reason="PLAN_GOVERNANCE_MET",
        cross_ticket_consistency_failures=(),
        ticket_results=(TicketResult(
            ticket_id="T001",
            disposition="PASS",
            verify_cmd_passed=None,
        ),),
    )

    args = argparse.Namespace(
        plan=str(plan_path),
        repo=str(tmp_path),
        backend="claude-cli",
        standards_dir=str(tmp_path),
        output_dir=None,
        auto_repair=False,
        lessons=None,
        no_role_passes=False,
        strict_dod=False,
        verbose=False,
        api_key="",
        base_url="",
        model="",
        temperature=0.0,
        max_tokens=0,
        timeout=120,
    )

    with patch("saturnday.ticket_runner.run_plan", return_value=happy_result), \
         patch("saturnday.cli._require_git_repo", return_value=True):
        rc = _cmd_run(args)

    assert rc == 0, "CLI must return 0 when no consistency failures"


# ---------------------------------------------------------------------------
# Test 7: unrelated warning-only advisory findings do not block
# ---------------------------------------------------------------------------

def test_unrelated_warnings_do_not_trigger_consistency_gate(tmp_path: Path) -> None:
    """Advisory warnings from other checks are not treated as hard consistency failures."""
    from saturnday.post_checks import check_cross_file_shared_default_divergence

    src = tmp_path / "src"
    src.mkdir()

    # Files that differ in lower-case vars (not UPPER_CASE) should not fire
    (src / "module_a.py").write_text(
        'timeout = 30\nsome_var = "foo"\n',
        encoding="utf-8",
    )
    (src / "module_b.py").write_text(
        'timeout = 60\nsome_var = "bar"\n',
        encoding="utf-8",
    )

    findings = check_cross_file_shared_default_divergence(tmp_path)
    assert not findings, (
        f"Lower-case variables must not trigger the consistency gate; got {findings}"
    )


# ---------------------------------------------------------------------------
# Test 8: consistency failures written to run-summary.json
# ---------------------------------------------------------------------------

def test_consistency_failures_written_to_run_summary(tmp_path: Path) -> None:
    """cross_ticket_consistency_failures appears in run-summary.json."""
    from saturnday._types import RunResult
    from saturnday.run.evidence import write_run_summary

    failure = {
        "category": "cross_file_shared_default_divergence",
        "constant": "DEFAULT_OUTPUT",
        "file_a": "src/a.py",
        "value_a": "/data/output",
        "file_b": "src/b.py",
        "value_b": "/data/results",
        "message": "Shared constant DEFAULT_OUTPUT has conflicting values.",
    }
    result = RunResult(
        project_id="fix40-evidence",
        total_tickets=2,
        passed=2,
        definition_of_done_met=False,
        cross_ticket_consistency_failures=(failure,),
    )
    write_run_summary(result, tmp_path)
    data = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))

    assert "cross_ticket_consistency_failures" in data
    failures = data["cross_ticket_consistency_failures"]
    assert len(failures) == 1
    assert failures[0]["constant"] == "DEFAULT_OUTPUT"
    assert failures[0]["category"] == "cross_file_shared_default_divergence"


# ---------------------------------------------------------------------------
# Test 9: per-ticket check still only sees its own files (no regression)
# ---------------------------------------------------------------------------

def test_per_ticket_domain_duplicates_does_not_see_cross_ticket_files(
    tmp_path: Path,
) -> None:
    """check_domain_duplicates still returns empty when only one file per ticket."""
    from saturnday.post_checks import check_domain_duplicates

    src = tmp_path / "src"
    src.mkdir()

    (src / "module_a.py").write_text(
        'DOMAINS = frozenset({"api", "search"})\n',
        encoding="utf-8",
    )
    (src / "module_b.py").write_text(
        'DOMAINS = frozenset({"api", "billing"})\n',
        encoding="utf-8",
    )

    # Per-ticket call sees only one file — must return empty (as before Fix 40)
    findings_a = check_domain_duplicates(tmp_path, ["src/module_a.py"])
    findings_b = check_domain_duplicates(tmp_path, ["src/module_b.py"])
    assert not findings_a, "Single-file per-ticket call must not fire"
    assert not findings_b, "Single-file per-ticket call must not fire"
