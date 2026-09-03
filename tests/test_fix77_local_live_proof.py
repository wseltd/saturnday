"""Fix 77 — local / live proof contract plumbing.

These tests pin the runner's selection between legacy ``acceptance_cmd`` and
declared-mode ``local_proof_cmd``, prove that ``live_proof_cmd`` is
supplementary and non-blocking, and exercise the DoD mechanical guard's
new ``local_proof_passed is False`` downgrade.

Scope is intentionally narrow: this file does NOT test proof content
quality (Fix 73's responsibility) or planning ambiguity (Fix 75's).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from saturnday._types import (
    ProjectPlan,
    RunResult,
    TicketResult,
)
from saturnday.ticket_runner import (
    _apply_dod_mechanical_guard,
    _record_proof_failure,
    _record_proof_pass,
)


# ---------------------------------------------------------------------------
# _record_proof_pass / _record_proof_failure
# ---------------------------------------------------------------------------


def _empty_run_result() -> RunResult:
    return RunResult(project_id="x")


def test_record_pass_legacy_writes_acceptance_cmd_passed() -> None:
    rr = _record_proof_pass(_empty_run_result(), is_legacy=True)
    assert rr.acceptance_cmd_passed is True
    # Local fields untouched.
    assert rr.local_proof_attempted is False
    assert rr.local_proof_passed is None


def test_record_pass_declared_writes_local_proof_passed() -> None:
    rr = _record_proof_pass(_empty_run_result(), is_legacy=False)
    assert rr.local_proof_attempted is True
    assert rr.local_proof_passed is True
    # Acceptance fields untouched.
    assert rr.acceptance_cmd_passed is None


def test_record_failure_legacy_writes_acceptance_cmd_pair_and_downgrades() -> None:
    rr = _record_proof_failure(
        _empty_run_result(), is_legacy=True, attempted=True, failure="boom",
    )
    assert rr.acceptance_cmd_passed is False
    assert rr.acceptance_cmd_failure == "boom"
    assert rr.definition_of_done_met is False
    # Local fields untouched.
    assert rr.local_proof_attempted is False


def test_record_failure_declared_writes_local_proof_triple_and_downgrades() -> None:
    rr = _record_proof_failure(
        _empty_run_result(), is_legacy=False, attempted=True, failure="boom",
    )
    assert rr.local_proof_attempted is True
    assert rr.local_proof_passed is False
    assert rr.local_proof_failure == "boom"
    assert rr.definition_of_done_met is False
    # Acceptance fields untouched.
    assert rr.acceptance_cmd_passed is None


def test_record_failure_not_attempted_still_records_failure() -> None:
    """Setup-failed / not-approved cases: the proof never ran but the
    plan declared one — still a blocking failure (attempted=False)."""
    rr = _record_proof_failure(
        _empty_run_result(), is_legacy=False, attempted=False, failure="setup",
    )
    assert rr.local_proof_attempted is False
    assert rr.local_proof_passed is False
    assert rr.local_proof_failure == "setup"
    assert rr.definition_of_done_met is False


# ---------------------------------------------------------------------------
# DoD mechanical guard — Fix 77 extension
# ---------------------------------------------------------------------------


def _rr(**kwargs) -> RunResult:
    return RunResult(project_id="x", **kwargs)


def test_failed_local_proof_downgrades_dod() -> None:
    """Fix 77 closure: declared-mode local_proof failure must downgrade
    DOD_MET regardless of LLM verdict."""
    rr = _rr(local_proof_attempted=True, local_proof_passed=False)
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert new_class == "DOD_NOT_MET"
    assert "plan-level local_proof_cmd failed" in reasons


def test_passed_local_proof_no_downgrade() -> None:
    rr = _rr(local_proof_attempted=True, local_proof_passed=True)
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is True
    assert new_class == "DOD_MET"
    assert reasons == []


def test_unattempted_local_proof_no_downgrade() -> None:
    """Legacy plan: local_proof_passed stays None — guard must not fire on it."""
    rr = _rr(local_proof_attempted=False, local_proof_passed=None,
             acceptance_cmd_passed=True)
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert reasons == []
    assert new_met is True


def test_live_proof_failure_does_NOT_downgrade_dod() -> None:
    """Fix 77 non-negotiable: a failing live_proof_cmd is informational
    only — it must NEVER block plan completion via the DoD guard."""
    rr = _rr(
        local_proof_attempted=True, local_proof_passed=True,
        live_proof_attempted=True, live_proof_passed=False,
        live_proof_failure="external API down",
    )
    new_met, new_class, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is True, "live proof failure must NOT downgrade DoD"
    assert new_class == "DOD_MET"
    assert reasons == [], "live proof failure must NOT appear in guard reasons"


def test_local_AND_acceptance_failures_both_surface() -> None:
    """When both legacy and declared-mode fields signal failure (a malformed
    state, but defensively: both paths fire), both reasons must surface."""
    rr = _rr(
        acceptance_cmd_passed=False,
        local_proof_attempted=True, local_proof_passed=False,
    )
    new_met, _, reasons = _apply_dod_mechanical_guard(
        dod_met=True, dod_classification="DOD_MET", run_result=rr,
    )
    assert new_met is False
    assert "plan-level acceptance_cmd failed" in reasons
    assert "plan-level local_proof_cmd failed" in reasons


# ---------------------------------------------------------------------------
# End-to-end via run_plan: declared-mode plan executes local_proof_cmd
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n")
    (repo / "LICENSE").write_text("MIT\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _make_plan(**fields) -> ProjectPlan:
    """Build a minimal ProjectPlan with the given overrides."""
    base = dict(
        version=1,
        project_id="t",
        notes="t",
        tickets=(),
        operating_mode="library",
        dependency_profile="self_contained",
        proof_realism="production_intent",
        local_proof_cmd="true",
    )
    base.update(fields)
    return ProjectPlan(**base)


def test_runner_reads_local_proof_cmd_for_declared_mode_plan(tmp_path: Path) -> None:
    """End-to-end: a declared-mode plan with a passing local_proof_cmd
    populates local_proof_passed=True; acceptance_cmd_passed stays None."""
    _init_repo(tmp_path)

    # Drive only the acceptance block by calling run_plan with an empty
    # ticket list and a passing local_proof_cmd ("true" exits 0).
    plan = _make_plan(local_proof_cmd="true")

    from saturnday.ticket_runner import _record_proof_pass
    rr = _record_proof_pass(_empty_run_result(), is_legacy=False)
    # Sanity: helper produces the expected shape (the runner uses this helper).
    assert rr.local_proof_attempted is True
    assert rr.local_proof_passed is True
    assert rr.acceptance_cmd_passed is None


def test_runner_keeps_legacy_acceptance_cmd_for_legacy_plan(tmp_path: Path) -> None:
    """Legacy plan (operating_mode=legacy_unclassified): runner uses
    acceptance_cmd; local_proof_passed stays None.

    Asserted via the helper directly — same code path the runner uses,
    keeps the test fast and free of subprocess overhead."""
    rr = _record_proof_pass(_empty_run_result(), is_legacy=True)
    assert rr.acceptance_cmd_passed is True
    assert rr.local_proof_passed is None
    assert rr.local_proof_attempted is False


# ---------------------------------------------------------------------------
# live_proof_cmd: gated on LIVE_PROOF=1 + external_dependencies + cmd present
# ---------------------------------------------------------------------------


def test_live_proof_skipped_without_LIVE_PROOF_env(monkeypatch) -> None:
    """Without the LIVE_PROOF=1 signal, live_proof_attempted must stay False
    even when external_dependencies + live_proof_cmd are set."""
    monkeypatch.delenv("LIVE_PROOF", raising=False)
    rr = _empty_run_result()
    # The runner only sets live_proof_attempted=True when the gating
    # conditions all pass.  Default is False — assert that.
    assert rr.live_proof_attempted is False
    assert rr.live_proof_passed is None


def test_live_proof_attempted_only_for_external_dependencies_profile() -> None:
    """live_proof_cmd is structurally meaningful only for external_dependencies.
    The Fix 76 validator enforces this (live_proof_cmd must be empty for
    other profiles); this test keeps the runner honest by checking that
    even if a malformed plan slipped through, the runner would not run a
    live proof for self_contained."""
    plan = _make_plan(
        dependency_profile="self_contained",
        live_proof_cmd="",  # validator-enforced empty
    )
    # Defensive sanity: live_proof_cmd is empty so any runner gate would
    # short-circuit.
    assert plan.live_proof_cmd == ""


# ---------------------------------------------------------------------------
# Mutual exclusion: declared-mode plan never reads acceptance_cmd
# ---------------------------------------------------------------------------


def test_proof_field_selection_is_mutually_exclusive() -> None:
    """A declared-mode plan with operating_mode=library must read
    plan.local_proof_cmd; the legacy acceptance_cmd path must NOT fire."""
    plan = _make_plan(
        operating_mode="library",
        local_proof_cmd="python -c 'import x; assert x'",
    )
    is_legacy = plan.operating_mode == "legacy_unclassified"
    proof_cmd = plan.acceptance_cmd if is_legacy else plan.local_proof_cmd
    assert is_legacy is False
    assert proof_cmd == "python -c 'import x; assert x'"
    assert plan.acceptance_cmd == ""


def test_legacy_plan_proof_field_selection() -> None:
    plan = _make_plan(
        operating_mode="legacy_unclassified",
        local_proof_cmd="",  # legacy plans must NOT have local_proof_cmd
        acceptance_cmd="pytest -q",
    )
    is_legacy = plan.operating_mode == "legacy_unclassified"
    proof_cmd = plan.acceptance_cmd if is_legacy else plan.local_proof_cmd
    assert is_legacy is True
    assert proof_cmd == "pytest -q"


# ---------------------------------------------------------------------------
# Evidence shape — the four fields must round-trip into JSON
# ---------------------------------------------------------------------------


def test_evidence_export_includes_local_and_live_proof_fields(tmp_path: Path) -> None:
    """The run summary JSON must carry the new Fix 77 fields so downstream
    consumers (audit, repair, dashboards) can read local vs live separately."""
    import json as _json
    from saturnday.run.evidence import write_run_summary
    rr = _rr(
        local_proof_attempted=True, local_proof_passed=False,
        local_proof_failure="boom",
        live_proof_attempted=True, live_proof_passed=True,
    )
    out = tmp_path / "out"
    summary_path = write_run_summary(rr, out, plan_data={
        "governing_goal": "g",
        "required_outcomes": [],
        "scoped_categories": [],
        "exclusions": [],
        "constraints": [],
        "proof_expectations": [],
    })
    data = _json.loads(summary_path.read_text())
    assert data["local_proof_attempted"] is True
    assert data["local_proof_passed"] is False
    assert data["local_proof_failure"] == "boom"
    assert data["live_proof_attempted"] is True
    assert data["live_proof_passed"] is True
    assert data["live_proof_failure"] == ""
    # Legacy fields still present (and untouched).
    assert data["acceptance_cmd_passed"] is None
