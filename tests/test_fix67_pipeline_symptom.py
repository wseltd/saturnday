"""Fix 67 — pipeline symptom proof through the actual ticket runner.

Drives ``_run_ticket_with_retries`` end-to-end through the real
``_run_governance`` → ``run_governance_check`` → ``_apply_policy_filtering``
chain.  Only the coding backend (``_execute_ticket``) and the low-level review
checks (``run_review``) are stubbed — to remove backend dependency and
fixture noise — but every governance, policy, and runner decision is real.

Reproduction:

* The stub coder writes a deliverable file containing a read pattern that
  legitimately has no write counterpart in the changed-file corpus
  (``db.query(User).all()``).  This is the canonical
  ``read_without_write_surface`` false positive that originally motivated
  Fix 67.
* The stub ``run_review`` returns a single FAIL finding for that check.
  Held to one check so the disposition under test is unambiguous.

The three variants together prove what the policy + runner actually do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from saturnday._types import CoderConfig, TicketSpec
from saturnday.project_state import ProjectState
from saturnday.ticket_runner import _run_ticket_with_retries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


DELIVERABLE_CONTENT = (
    '"""Read-only data extractor for downstream reporting."""\n'
    "from __future__ import annotations\n"
    "class User: pass\n"
    "def list_users(db):\n"
    "    return db.query(User).all()\n"
)


def _init_repo(repo: Path) -> None:
    """Initialise a minimal git repo so the runner can stage and commit."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n")
    (repo / "LICENSE").write_text("MIT\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _make_stubbed_review(_repo_path: Path) -> dict:
    """Synthetic ``run_review`` output containing only one
    ``read_without_write_surface`` FAIL finding."""
    return {
        "tools": {
            "read_without_write_surface": {
                "name": "read_without_write_surface",
                "status": "FAIL",
                "severity": "warning",
                "findings": [
                    {
                        "file": "app.py",
                        "line": 0,
                        "kind": "read_without_write_path",
                        "detail": "Model 'User' is queried/read but has no write path.",
                    }
                ],
                "files_checked": ["app.py"],
                "exit_code": 1,
                "raw_output": "",
                "error": None,
            }
        },
        "tool_runs": [],
    }


def _make_stub_execute_ticket(deliverable: Path):
    """Stub coder that writes the deliverable on every attempt and returns ['app.py']."""
    call_count = {"n": 0}

    def _stub(*, ticket, repo_path, coder_config, messages):  # noqa: ARG001
        call_count["n"] += 1
        deliverable.write_text(DELIVERABLE_CONTENT)
        return ("simulated coder response", ["app.py"])

    return _stub, call_count


def _make_ticket() -> TicketSpec:
    return TicketSpec(
        ticket_id="T067",
        goal="Add list_users helper to app.py",
    )


def _make_state() -> ProjectState:
    return ProjectState(project_id="fix67-test")


def _make_coder_config() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="test-key", model="gpt-4")


def _run_ticket(tmp_path: Path, max_retries: int = 2):
    """Drive _run_ticket_with_retries with controlled review + stub coder."""
    deliverable = tmp_path / "app.py"
    stub_execute, call_count = _make_stub_execute_ticket(deliverable)

    with (
        patch("saturnday.ticket_runner._execute_ticket", side_effect=stub_execute),
        patch(
            "saturnday.governance.run_review",
            side_effect=lambda repo_path, changed_files, tmpdir, **kw: _make_stubbed_review(repo_path),
        ),
        patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
        patch("saturnday.ticket_runner._check_contracts", return_value=""),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
        patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
    ):
        result = _run_ticket_with_retries(
            ticket=_make_ticket(),
            repo_path=tmp_path,
            coder_config=_make_coder_config(),
            system_prompt="test system prompt",
            state=_make_state(),
            plan_notes="",
            output_dir=tmp_path / "out",
            max_retries=max_retries,
        )
    return result, call_count["n"], deliverable


# ---------------------------------------------------------------------------
# Variant A — no policy: the symptom
# ---------------------------------------------------------------------------


def test_no_policy_symptom_retries_exhaust_then_coded_ungoverned(tmp_path: Path) -> None:
    """Without policy, the false-positive finding cycles attempts and lands
    CODED_UNGOVERNED.

    ``read_without_write_surface`` is in SOFT_CHECKS — its default severity is
    ``warning``.  A FAIL status at warning severity yields disposition WARN.
    The runner has only one success gate (``effective_disposition == "PASS"``),
    so any non-PASS disposition — including WARN — falls through to the FAIL
    retry path.  After ``max_retries`` are exhausted the deliverable is
    committed under the [GOVERNANCE: review required] suffix instead of being
    accepted cleanly as PASS.
    """
    _init_repo(tmp_path)
    result, call_count, deliverable = _run_ticket(tmp_path, max_retries=2)

    assert call_count == 3, f"expected 3 coder calls (init + 2 retries), got {call_count}"
    assert result.disposition == "CODED_UNGOVERNED", (
        f"baseline symptom: disposition should be CODED_UNGOVERNED, got {result.disposition}"
    )
    assert result.governance_disposition == "WARN", (
        f"soft-check FAIL produces governance_disposition WARN, "
        f"got {result.governance_disposition}"
    )
    assert result.attempts == 3
    assert deliverable.is_file(), "deliverable should be on disk after CODED_UNGOVERNED commit"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert "GOVERNANCE: review required" in log, f"expected ungoverned commit, got: {log}"


# ---------------------------------------------------------------------------
# Variant B — exemptions resolve the symptom
# ---------------------------------------------------------------------------


def test_exemption_lets_ticket_complete_first_attempt(tmp_path: Path) -> None:
    """With ``exemptions`` matching the check, the finding is removed and the
    check status flips PASS.  Disposition becomes PASS, the runner accepts
    on the first attempt, and the deliverable is committed cleanly.

    This is the path Fix 65 made consistent across the full-review and
    diff-based governance entry points.
    """
    _init_repo(tmp_path)
    (tmp_path / ".saturnday-policy.yaml").write_text(
        "exemptions:\n"
        "  - check: read_without_write_surface\n"
        "    pattern: \"*\"\n"
        "    reason: read-only library — by design\n"
    )
    result, call_count, deliverable = _run_ticket(tmp_path, max_retries=2)

    assert call_count == 1, (
        f"with exemption, expected exactly 1 coder call (no retries), got {call_count}"
    )
    assert result.disposition == "PASS", (
        f"with exemption, expected PASS disposition, got {result.disposition}"
    )
    assert result.governance_disposition == "PASS", (
        f"exemption removes finding; governance disposition should be PASS, "
        f"got {result.governance_disposition}"
    )
    assert result.attempts == 1
    assert deliverable.is_file(), "deliverable should be committed cleanly"
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert "GOVERNANCE: review required" not in log, (
        f"clean PASS commit expected, found ungoverned suffix: {log}"
    )


# ---------------------------------------------------------------------------
# Variant C — Fix 67b: policy-cleared WARN must now succeed (Fix 78)
# ---------------------------------------------------------------------------


def test_expected_findings_with_error_severity_now_succeeds_under_fix78(tmp_path: Path) -> None:
    """Fix 67b closure — policy-cleared WARN is honoured by the runner.

    To exercise the Fix 65 follow-up downgrade we elevate the check's severity
    to ``error`` via the ``checks`` block.  The pre-policy disposition is then
    FAIL; ``expected_findings`` covers the only error finding; the policy
    layer downgrades FAIL → WARN and stamps reasons with the policy-cleared
    marker ``["all_findings_expected"]``.

    Fix 78 teaches the runner to honour exactly that marker.  After Fix 78:
    - call_count must be 1 (no retry exhaustion)
    - result.disposition must be PASS (clean ticket completion)
    - result.governance_disposition must remain WARN (the truth is preserved
      in evidence — the runner success is gated on the marker, not by
      overriding the disposition).

    If governance_disposition flips to PASS or call_count grows above 1, Fix
    78 has been smuggled into broader WARN-as-success.  If call_count stays
    at 3 / disposition is CODED_UNGOVERNED, Fix 78 is missing.
    """
    _init_repo(tmp_path)
    (tmp_path / ".saturnday-policy.yaml").write_text(
        "checks:\n"
        "  read_without_write_surface:\n"
        "    severity: error\n"
        "expected_findings:\n"
        "  - read_without_write_surface\n"
    )
    result, call_count, deliverable = _run_ticket(tmp_path, max_retries=2)

    assert call_count == 1, (
        f"policy-cleared WARN must complete in one attempt; got {call_count} coder calls"
    )
    assert result.disposition == "PASS", (
        f"expected PASS under Fix 78 policy-cleared marker; got {result.disposition}"
    )
    assert result.governance_disposition == "WARN", (
        f"governance disposition must remain WARN (truth preserved in evidence); "
        f"got {result.governance_disposition}"
    )
    assert result.attempts == 1
    assert deliverable.is_file(), "deliverable should be committed cleanly"


# ---------------------------------------------------------------------------
# Variant D — Fix 67c boundary: marker-shape WARN without elevation also succeeds
# (sanity check that the policy-cleared marker, not the check severity, is the
# discriminator) — this stays separate from Variant A which has no policy.
# ---------------------------------------------------------------------------


def test_expected_findings_without_severity_elevation_does_not_succeed(tmp_path: Path) -> None:
    """Boundary check: ``expected_findings`` on a soft-check WARN does NOT
    cross the Fix 78 gate.

    The soft check's pre-policy disposition is already WARN (severity
    ``warning``), so the FAIL → WARN downgrade in ``_apply_policy_filtering``
    never fires; ``reasons`` is the per-check list-of-dicts produced by
    ``compute_disposition``, NOT the policy-cleared marker.  Fix 78 (narrow)
    must therefore leave this case alone — no flip.

    This pins the boundary so a future widening of Fix 78 to "any WARN with
    expected_findings" is caught here, not silently shipped.
    """
    _init_repo(tmp_path)
    (tmp_path / ".saturnday-policy.yaml").write_text(
        "expected_findings:\n"
        "  - read_without_write_surface\n"
    )
    result, call_count, _ = _run_ticket(tmp_path, max_retries=2)

    assert call_count == 3, (
        f"soft-check WARN without policy-cleared marker must remain non-success; "
        f"got {call_count} coder calls"
    )
    assert result.disposition == "CODED_UNGOVERNED", (
        f"expected CODED_UNGOVERNED, got {result.disposition}"
    )
    assert result.governance_disposition == "WARN"
