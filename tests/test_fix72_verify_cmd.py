"""Fix 72 — verify_cmd gating + evidence integrity.

Two coupled defects in the runner around per-ticket verify_cmd:

72a (gating): pre-Fix-78, verify_cmd ran only inside the strict PASS gate;
non-PASS dispositions skipped it silently.  After Fix 78 the success branch
also fires for policy-cleared WARN, so verify_cmd now runs there too.  This
file proves the gating end-to-end through the real ticket runner.

72b (evidence integrity): pre-Fix-72b, ``verify_cmd_passed = None`` meant
both "no verify_cmd specified" and "specified but skipped".  TicketResult
now carries an explicit ``verify_cmd_specified: bool``.  The four states
must be cleanly distinguishable on the produced TicketResult.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from saturnday._types import CoderConfig, TicketSpec
from saturnday.project_state import ProjectState
from saturnday.ticket_runner import _run_ticket_with_retries


# ---------------------------------------------------------------------------
# Test fixtures (mirror tests/test_fix67_pipeline_symptom.py)
# ---------------------------------------------------------------------------


DELIVERABLE_CONTENT = (
    '"""Read-only data extractor for downstream reporting."""\n'
    "from __future__ import annotations\n"
    "class User: pass\n"
    "def list_users(db):\n"
    "    return db.query(User).all()\n"
)


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n")
    (repo / "LICENSE").write_text("MIT\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _stubbed_review_pass(_repo, _files, _tmp, **_kw) -> dict:
    """No findings — governance returns PASS."""
    return {"tools": {}, "tool_runs": []}


def _stubbed_review_fail(_repo, _files, _tmp, **_kw) -> dict:
    """One read_without_write_surface FAIL — disposition WARN (soft check)."""
    return {
        "tools": {
            "read_without_write_surface": {
                "name": "read_without_write_surface",
                "status": "FAIL",
                "severity": "warning",
                "findings": [{"file": "app.py", "line": 0,
                              "kind": "read_without_write_path",
                              "detail": "x"}],
                "files_checked": ["app.py"],
                "exit_code": 1, "raw_output": "", "error": None,
            }
        },
        "tool_runs": [],
    }


def _stub_execute(deliverable: Path):
    call_count = {"n": 0}

    def _stub(*, ticket, repo_path, coder_config, messages):  # noqa: ARG001
        call_count["n"] += 1
        deliverable.write_text(DELIVERABLE_CONTENT)
        return ("ok", ["app.py"])

    return _stub, call_count


def _make_state() -> ProjectState:
    return ProjectState(project_id="fix72-test")


def _make_coder_config() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="test", model="gpt-4")


def _run(tmp_path: Path, *, ticket: TicketSpec, review_stub, max_retries: int = 2):
    deliverable = tmp_path / "app.py"
    stub, call_count = _stub_execute(deliverable)
    with (
        patch("saturnday.ticket_runner._execute_ticket", side_effect=stub),
        patch("saturnday.governance.run_review", side_effect=review_stub),
        patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
        patch("saturnday.ticket_runner._check_contracts", return_value=""),
        patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
    ):
        result = _run_ticket_with_retries(
            ticket=ticket,
            repo_path=tmp_path,
            coder_config=_make_coder_config(),
            system_prompt="sys",
            state=_make_state(),
            plan_notes="",
            output_dir=tmp_path / "out",
            max_retries=max_retries,
        )
    return result, call_count["n"]


# ---------------------------------------------------------------------------
# 72b — evidence integrity on the produced TicketResult
# ---------------------------------------------------------------------------


def test_no_verify_cmd_specified_false_passed_none(tmp_path: Path) -> None:
    """Plan has no verify_cmd: TicketResult must report specified=False, passed=None."""
    _init_repo(tmp_path)
    ticket = TicketSpec(ticket_id="T", goal="g")  # no verify_cmd
    result, _ = _run(tmp_path, ticket=ticket, review_stub=_stubbed_review_pass)

    assert result.disposition == "PASS"
    assert result.verify_cmd_specified is False
    assert result.verify_cmd_passed is None


def test_verify_cmd_runs_and_passes_on_pass_path(tmp_path: Path) -> None:
    """Plan has verify_cmd; PASS path runs it; specified=True, passed=True."""
    _init_repo(tmp_path)
    ticket = TicketSpec(ticket_id="T", goal="g", verify_cmd="true")  # /usr/bin/true exits 0
    result, _ = _run(tmp_path, ticket=ticket, review_stub=_stubbed_review_pass)

    assert result.disposition == "PASS"
    assert result.verify_cmd_specified is True
    assert result.verify_cmd_passed is True


def test_verify_cmd_runs_and_fails_on_pass_path(tmp_path: Path) -> None:
    """Plan has verify_cmd that fails; PASS path runs it; specified=True, passed=False."""
    _init_repo(tmp_path)
    ticket = TicketSpec(ticket_id="T", goal="g",
                        verify_cmd="false")  # /usr/bin/false exits 1
    result, _ = _run(tmp_path, ticket=ticket, review_stub=_stubbed_review_pass)

    assert result.disposition == "CODED_UNGOVERNED"
    assert result.verify_cmd_specified is True
    assert result.verify_cmd_passed is False
    assert result.verify_cmd_failure  # non-empty


def test_verify_cmd_specified_but_skipped_when_governance_warn(tmp_path: Path) -> None:
    """Fix 72b core case — soft-check WARN with verify_cmd specified.

    The runner's success gate is not entered (WARN without policy-cleared
    marker → non-success → retry → eventually CODED_UNGOVERNED).  The verify_cmd
    therefore never executes, but the plan declared one.  The TicketResult must
    report specified=True, passed=None — the new explicit "specified but
    skipped" state.  Pre-Fix-72b this was indistinguishable from no-verify_cmd.
    """
    _init_repo(tmp_path)
    ticket = TicketSpec(ticket_id="T", goal="g", verify_cmd="true")
    result, call_count = _run(tmp_path, ticket=ticket, review_stub=_stubbed_review_fail)

    assert result.disposition == "CODED_UNGOVERNED"
    assert call_count == 3  # exhausted retries
    assert result.verify_cmd_specified is True, (
        "specified must be True even though verify_cmd never ran"
    )
    assert result.verify_cmd_passed is None, (
        "passed must be None — the proof never ran"
    )


# ---------------------------------------------------------------------------
# 72a — gating: verify_cmd runs on policy-cleared WARN (Fix 78 success branch)
# ---------------------------------------------------------------------------


def _stubbed_review_fail_error(_repo, _files, _tmp, **_kw) -> dict:
    """Same finding shape as _stubbed_review_fail; severity will be elevated
    to error via the policy block in the test."""
    return _stubbed_review_fail(_repo, _files, _tmp)


def test_verify_cmd_runs_on_policy_cleared_warn(tmp_path: Path) -> None:
    """Fix 72a: under policy-cleared WARN (post-Fix-78 success), verify_cmd
    must execute exactly as it does on PASS.  Specified=True, passed=True."""
    _init_repo(tmp_path)
    (tmp_path / ".saturnday-policy.yaml").write_text(
        "checks:\n"
        "  read_without_write_surface:\n"
        "    severity: error\n"
        "expected_findings:\n"
        "  - read_without_write_surface\n"
    )
    ticket = TicketSpec(ticket_id="T", goal="g", verify_cmd="true")
    result, call_count = _run(tmp_path, ticket=ticket, review_stub=_stubbed_review_fail_error)

    assert call_count == 1, "policy-cleared WARN must complete in one attempt"
    assert result.disposition == "PASS"
    assert result.governance_disposition == "WARN", (
        "governance disposition must remain WARN (Fix 78 preserves the truth)"
    )
    assert result.verify_cmd_specified is True
    assert result.verify_cmd_passed is True, (
        "verify_cmd must have run on the policy-cleared WARN success branch"
    )


def test_verify_cmd_failure_on_policy_cleared_warn_blocks_pass(tmp_path: Path) -> None:
    """Fix 72a + 78 interplay: even on policy-cleared WARN, a failing
    verify_cmd must block the PASS commit (the existing in-branch handler
    runs the same way it does for true PASS)."""
    _init_repo(tmp_path)
    (tmp_path / ".saturnday-policy.yaml").write_text(
        "checks:\n"
        "  read_without_write_surface:\n"
        "    severity: error\n"
        "expected_findings:\n"
        "  - read_without_write_surface\n"
    )
    ticket = TicketSpec(ticket_id="T", goal="g", verify_cmd="false")
    result, _ = _run(tmp_path, ticket=ticket, review_stub=_stubbed_review_fail_error)

    assert result.disposition == "CODED_UNGOVERNED"
    assert result.verify_cmd_specified is True
    assert result.verify_cmd_passed is False
