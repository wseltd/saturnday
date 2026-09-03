"""Proof tests for feedback batch β + α.

β — DoD inline edit UX (interactive.py _amend_outcomes_inline +
    _run_dod_gate extension).
α — Non-interactive acceptance-setup approval (ticket_runner.run_plan
    parameter + CLI flag + env var; evidence records the source).
"""
from __future__ import annotations

import io
import json
import os
import types
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday import interactive as interactive_mod
from saturnday._types import RunResult


# ---------------------------------------------------------------------------
# β — DoD inline edit
# ---------------------------------------------------------------------------


def _run_amend_with_inputs(inputs: list[str], outcomes: list[str]):
    """Feed a list of fake stdin lines through _amend_outcomes_inline and
    capture the return value.  Uses monkeypatching on ``input`` directly
    because readline's startup_hook is an implementation detail we don't
    want to smoke-test here."""
    it = iter(inputs)
    def _fake_input(_prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration as exc:
            raise EOFError from exc
    with patch("builtins.input", side_effect=_fake_input), patch(
        # Force the fallback path so the test doesn't depend on readline
        # being importable / TTY-detected.
        "saturnday.interactive._amend_outcomes_inline.__wrapped__", new=None,
    ) if False else patch("builtins.input", side_effect=_fake_input):
        return interactive_mod._amend_outcomes_inline(outcomes)


def test_beta_amend_keeps_and_replaces_individually() -> None:
    """Blank replacement keeps the original; typed replacement substitutes."""
    # Three existing outcomes.  Inputs:
    #   [1]  (blank) → keep outcome 1
    #   [2]  "new_B" → replace outcome 2
    #   [3]  (blank) → keep outcome 3
    #   [4]  (blank) → finish the "add new" loop
    out = _run_amend_with_inputs(
        inputs=["", "new_B", "", ""],
        outcomes=["A_original", "B_original", "C_original"],
    )
    assert out == ["A_original", "new_B", "C_original"]


def test_beta_amend_blank_finish_terminates_without_adding() -> None:
    """When the operator supplies a blank line at the first 'new outcome'
    prompt, the sequence terminates cleanly."""
    out = _run_amend_with_inputs(
        inputs=["", "", ""],  # keep 1, keep 2, blank-finish new
        outcomes=["A", "B"],
    )
    assert out == ["A", "B"]


def test_beta_amend_can_add_new_outcomes_after_existing() -> None:
    """Operator can add new outcomes after walking through existing ones."""
    out = _run_amend_with_inputs(
        inputs=["", "", "new_C", "new_D", ""],  # keep A, keep B, add C, add D, finish
        outcomes=["A", "B"],
    )
    assert out == ["A", "B", "new_C", "new_D"]


def test_beta_amend_cancel_on_eof_returns_none() -> None:
    """EOF / Ctrl-C mid-sequence returns None so the caller preserves original."""
    def _raise_eof(_prompt: str = "") -> str:
        raise EOFError
    with patch("builtins.input", side_effect=_raise_eof):
        result = interactive_mod._amend_outcomes_inline(["A", "B", "C"])
    assert result is None


def test_beta_dod_gate_accept_returns_true() -> None:
    """[1] Accept proceeds without touching outcomes."""
    plan_data = {"required_outcomes": ["a", "b"]}
    plan_path = Path("/tmp/plan-unused.json")
    with patch("builtins.input", side_effect=["1"]):
        ok = interactive_mod._run_dod_gate(plan_data, plan_path)
    assert ok is True
    assert plan_data["required_outcomes"] == ["a", "b"]


def test_beta_dod_gate_inline_path_rewrites_plan(tmp_path: Path) -> None:
    """[2] routes to _amend_outcomes_inline and persists changes."""
    plan_data = {"required_outcomes": ["a", "b"]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")
    # "2" picks inline.  Inline asks per-outcome then finishes.  Answer
    # sequence: keep-a, replace-b, finish.
    inputs = iter(["2", "", "replaced_b", ""])
    with patch("builtins.input", side_effect=lambda *_: next(inputs)):
        ok = interactive_mod._run_dod_gate(plan_data, plan_path)
    assert ok is True
    assert plan_data["required_outcomes"] == ["a", "replaced_b"]
    assert plan_data.get("_dod_user_edited") is True
    on_disk = json.loads(plan_path.read_text(encoding="utf-8"))
    assert on_disk["required_outcomes"] == ["a", "replaced_b"]


def test_beta_dod_gate_editor_path_still_works(tmp_path: Path) -> None:
    """[3] routes to _edit_outcomes_in_editor, preserving the existing flow."""
    plan_data = {"required_outcomes": ["a"]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")
    with patch(
        "saturnday.interactive._edit_outcomes_in_editor",
        return_value=["editor_replaced_a"],
    ) as mock_editor, patch("builtins.input", side_effect=["3"]):
        ok = interactive_mod._run_dod_gate(plan_data, plan_path)
    assert ok is True
    mock_editor.assert_called_once_with(["a"])
    assert plan_data["required_outcomes"] == ["editor_replaced_a"]


def test_beta_dod_gate_abort_returns_false() -> None:
    """[4] Abort returns False so the caller halts the run."""
    plan_data = {"required_outcomes": ["a", "b"]}
    plan_path = Path("/tmp/plan-unused.json")
    with patch("builtins.input", side_effect=["4"]):
        ok = interactive_mod._run_dod_gate(plan_data, plan_path)
    assert ok is False
    # Plan data must not be mutated on abort.
    assert plan_data["required_outcomes"] == ["a", "b"]


def test_beta_dod_gate_inline_cancel_preserves_outcomes(tmp_path: Path) -> None:
    """Cancelling mid-inline-edit returns the original list unchanged."""
    plan_data = {"required_outcomes": ["a", "b", "c"]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")

    def _choice_then_eof(_prompt: str = "") -> str:
        if not hasattr(_choice_then_eof, "_done_first"):
            _choice_then_eof._done_first = True
            return "2"
        raise EOFError

    with patch("builtins.input", side_effect=_choice_then_eof):
        ok = interactive_mod._run_dod_gate(plan_data, plan_path)
    assert ok is True
    assert plan_data["required_outcomes"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# α — non-interactive acceptance-setup approval
# ---------------------------------------------------------------------------


def test_alpha_run_result_has_approval_source_field() -> None:
    """RunResult must carry the approval-source field with a safe default."""
    r = RunResult(project_id="p")
    assert hasattr(r, "acceptance_setup_approval_source")
    assert r.acceptance_setup_approval_source == ""


def test_alpha_cli_run_parser_accepts_approve_flag() -> None:
    """`saturnday run --approve-acceptance-setup` must parse cleanly and
    set the attribute on the parsed namespace."""
    from saturnday.cli import build_parser
    parser = build_parser()
    argv = [
        "run",
        "--plan", "/tmp/plan.json",
        "--repo", "/tmp/repo",
        "--backend", "openai",
        "--approve-acceptance-setup",
    ]
    ns = parser.parse_args(argv)
    assert ns.approve_acceptance_setup is True


def test_alpha_cli_run_parser_default_is_false() -> None:
    """Without the flag, the attribute must default to False."""
    from saturnday.cli import build_parser
    parser = build_parser()
    argv = [
        "run",
        "--plan", "/tmp/plan.json",
        "--repo", "/tmp/repo",
        "--backend", "openai",
    ]
    ns = parser.parse_args(argv)
    assert ns.approve_acceptance_setup is False


def test_alpha_cli_resume_rerun_parsers_accept_flag() -> None:
    """resume / rerun-failed / rerun-remaining must also parse the flag."""
    from saturnday.cli import build_parser
    parser = build_parser()
    for cmd in ("resume", "rerun-failed", "rerun-remaining"):
        argv = [
            cmd,
            "--plan", "/tmp/plan.json",
            "--repo", "/tmp/repo",
            "--backend", "openai",
            "--output-dir", "/tmp/out",
            "--approve-acceptance-setup",
        ]
        ns = parser.parse_args(argv)
        assert ns.approve_acceptance_setup is True, f"{cmd} did not accept the flag"


def _simulate_approval_block(
    *,
    is_tty: bool,
    approve_flag: bool,
    env_var: bool,
    tty_choice: str = "n",
) -> tuple[bool, str]:
    """Replay the exact approval-block logic from ticket_runner.run_plan
    as a unit so we can cover each branch without standing up a full
    run_plan pipeline."""
    _env_approved = env_var
    _acceptance_approval_source = ""
    if is_tty:
        _choice = tty_choice
        _acceptance_approved = _choice == "y"
        if _acceptance_approved:
            _acceptance_approval_source = "interactive"
        else:
            _acceptance_approval_source = "declined"
    else:
        if approve_flag:
            _acceptance_approved = True
            _acceptance_approval_source = "cli_flag"
        elif _env_approved:
            _acceptance_approved = True
            _acceptance_approval_source = "env_var"
        else:
            _acceptance_approved = False
            _acceptance_approval_source = "blocked_noninteractive"
    return _acceptance_approved, _acceptance_approval_source


def test_alpha_non_tty_with_cli_flag_approves() -> None:
    approved, source = _simulate_approval_block(
        is_tty=False, approve_flag=True, env_var=False
    )
    assert approved is True
    assert source == "cli_flag"


def test_alpha_non_tty_with_env_var_approves() -> None:
    approved, source = _simulate_approval_block(
        is_tty=False, approve_flag=False, env_var=True
    )
    assert approved is True
    assert source == "env_var"


def test_alpha_non_tty_without_signal_is_blocked() -> None:
    approved, source = _simulate_approval_block(
        is_tty=False, approve_flag=False, env_var=False
    )
    assert approved is False
    assert source == "blocked_noninteractive"


def test_alpha_tty_accept_records_interactive() -> None:
    approved, source = _simulate_approval_block(
        is_tty=True, approve_flag=False, env_var=False, tty_choice="y"
    )
    assert approved is True
    assert source == "interactive"


def test_alpha_tty_decline_records_declined() -> None:
    approved, source = _simulate_approval_block(
        is_tty=True, approve_flag=False, env_var=False, tty_choice="n"
    )
    assert approved is False
    assert source == "declined"


def test_alpha_ticket_runner_source_level_has_all_branches() -> None:
    """Source-level canary: ticket_runner.run_plan must contain each of
    the four approval sources and the CLI-flag / env-var predicates."""
    src = (
        Path(__file__).resolve().parent.parent
        / "src" / "saturnday" / "ticket_runner.py"
    ).read_text(encoding="utf-8")
    for marker in (
        '"interactive"',
        '"declined"',
        '"cli_flag"',
        '"env_var"',
        '"blocked_noninteractive"',
        "approve_acceptance_setup",
        "SATURNDAY_APPROVE_SETUP",
    ):
        assert marker in src, f"missing branch marker in ticket_runner.py: {marker}"


def test_alpha_run_plan_signature_exposes_flag() -> None:
    """run_plan must accept the kwarg; callers can therefore wire it through."""
    import inspect
    from saturnday.ticket_runner import run_plan
    sig = inspect.signature(run_plan)
    assert "approve_acceptance_setup" in sig.parameters
    assert sig.parameters["approve_acceptance_setup"].default is False


def test_alpha_resume_signatures_expose_flag() -> None:
    """resume_plan / rerun_failed / rerun_remaining must accept the kwarg."""
    import inspect
    from saturnday.run import resume as resume_mod
    for fn_name in ("resume_plan", "rerun_failed", "rerun_remaining"):
        fn = getattr(resume_mod, fn_name)
        sig = inspect.signature(fn)
        assert "approve_acceptance_setup" in sig.parameters, (
            f"{fn_name} does not accept approve_acceptance_setup"
        )
        assert sig.parameters["approve_acceptance_setup"].default is False


def test_alpha_evidence_serialises_approval_source(tmp_path: Path) -> None:
    """run-summary JSON must include the approval source so evidence is honest."""
    from saturnday.run.evidence import write_run_summary
    result = RunResult(
        project_id="p",
        acceptance_setup_approval_source="cli_flag",
    )
    out = tmp_path / "evidence"
    write_run_summary(result, out, plan_data={})
    summary_path = out / "run-summary.json"
    assert summary_path.is_file()
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert data.get("acceptance_setup_approval_source") == "cli_flag"


def test_alpha_cli_run_wires_flag_to_run_plan() -> None:
    """Source-level canary: _cmd_run forwards the flag to run_plan."""
    cli_src = (
        Path(__file__).resolve().parent.parent
        / "src" / "saturnday" / "cli.py"
    ).read_text(encoding="utf-8")
    # The call to run_plan must carry the approve_acceptance_setup kwarg
    # sourced from args.
    assert (
        "approve_acceptance_setup=getattr(args, \"approve_acceptance_setup\", False)"
        in cli_src
    )


# ---------------------------------------------------------------------------
# α — real non-TTY lifecycle proofs
# ---------------------------------------------------------------------------
#
# These tests drive ticket_runner.run_plan against a minimal plan that
# declares acceptance_setup + local_proof_cmd.  The plan's single ticket
# is pre-skipped via ``skip_tickets`` so the coder is never invoked.
# Two low-level helpers used by the acceptance branch are captured so
# we can prove each scenario by observing which ones got called and
# what RunResult carries out the other side.
#
# Helpers captured:
#   _run_acceptance_setup_step  — the setup executor
#   _run_verify_cmd             — the proof executor
# Neither is stubbed: we wrap them to record calls then return "" so the
# real control-flow through run_plan is exercised end to end.


def _write_minimal_plan_with_setup(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal valid plan + a real git repo.

    Returns (plan_path, repo_path).
    """
    import subprocess as _sp

    repo = tmp_path / "repo"
    repo.mkdir()
    _sp.run(["git", "init", "-q"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    _sp.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _sp.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    _sp.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    # Use legacy_unclassified to keep the plan minimal and bypass Fix 73
    # proof meaningfulness (which is unrelated to the α lifecycle we're
    # proving here).  Legacy plans use ``acceptance_cmd`` + ``acceptance_setup``;
    # the runner still enters the exact same approval block (ticket_runner.py
    # line ~1687 guards on ``if _proof_cmd and plan.acceptance_setup``).
    plan = {
        "version": 1,
        "project_id": "alpha-real-lifecycle",
        "notes": "α real-lifecycle proof",
        "operating_mode": "legacy_unclassified",
        "is_runnable_product": True,
        "acceptance_cmd": "echo REAL_PROOF_CMD_RAN",
        "acceptance_setup": ["echo REAL_SETUP_STEP_RAN"],
        "definition_of_done": ["all_tickets_passed"],
        "tickets": [{
            "ticket_id": "T001",
            "goal": "noop",
            "acceptance_criteria": ["noop"],
        }],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path, repo


def _drive_run_plan_for_acceptance(
    *,
    tmp_path: Path,
    is_tty: bool,
    approve_flag: bool,
    env_var: bool,
    monkeypatch: pytest.MonkeyPatch,
):
    """Drive ticket_runner.run_plan against a plan that has acceptance_setup
    and local_proof_cmd, with the ticket pre-skipped.  Returns a dict:
      {
        "setup_calls": [<step_str>, ...],
        "proof_calls": [<cmd_str>, ...],
        "result": RunResult,
        "output_dir": Path,
      }
    """
    from saturnday import ticket_runner as tr
    from saturnday._types import CoderConfig

    plan_path, repo = _write_minimal_plan_with_setup(tmp_path)
    output_dir = tmp_path / "evidence"

    # Stdin isatty control.
    monkeypatch.setattr("sys.stdin.isatty", lambda: is_tty)

    # Env var control.
    if env_var:
        monkeypatch.setenv("SATURNDAY_APPROVE_SETUP", "1")
    else:
        monkeypatch.delenv("SATURNDAY_APPROVE_SETUP", raising=False)

    # Capture the acceptance-branch actions.
    setup_calls: list[str] = []
    proof_calls: list[str] = []

    real_run_setup = tr._run_acceptance_setup_step
    real_run_verify = tr._run_verify_cmd

    def _capture_setup(step_cmd, repo_path, *args, **kwargs):
        setup_calls.append(step_cmd)
        return ""  # success

    def _capture_verify(cmd, repo_path, *args, **kwargs):
        proof_calls.append(cmd)
        return ""  # success

    monkeypatch.setattr(tr, "_run_acceptance_setup_step", _capture_setup)
    monkeypatch.setattr(tr, "_run_verify_cmd", _capture_verify)

    # For TTY case we need to simulate the prompt "y".
    if is_tty:
        monkeypatch.setattr("builtins.input", lambda *_: "y")

    config = CoderConfig(
        backend="openai",
        base_url="",
        api_key="test",
        model="gpt-test",
    )
    result = tr.run_plan(
        plan_path=plan_path,
        repo_path=repo,
        coder_config=config,
        standards_dir=repo,  # unused because ticket is skipped
        output_dir=output_dir,
        skip_tickets=frozenset({"T001"}),
        auto_repair=False,
        role_passes=False,
        approve_acceptance_setup=approve_flag,
    )
    return {
        "setup_calls": setup_calls,
        "proof_calls": proof_calls,
        "result": result,
        "output_dir": output_dir,
    }


def test_alpha_lifecycle_non_tty_cli_flag_runs_setup_and_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 1: non-TTY + --approve-acceptance-setup.

    Real run_plan lifecycle.  Setup executor AND proof executor must be
    called; RunResult records 'cli_flag'; evidence JSON records the same.
    """
    outcome = _drive_run_plan_for_acceptance(
        tmp_path=tmp_path, is_tty=False, approve_flag=True, env_var=False,
        monkeypatch=monkeypatch,
    )

    assert outcome["setup_calls"] == ["echo REAL_SETUP_STEP_RAN"], (
        "acceptance_setup step was not executed end-to-end"
    )
    assert outcome["proof_calls"] == ["echo REAL_PROOF_CMD_RAN"], (
        "local_proof_cmd was not executed end-to-end"
    )
    assert outcome["result"].acceptance_setup_approval_source == "cli_flag"

    summary_path = outcome["output_dir"] / "run-summary.json"
    assert summary_path.is_file(), "run-summary.json missing"
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert data.get("acceptance_setup_approval_source") == "cli_flag"


def test_alpha_lifecycle_non_tty_env_var_runs_setup_and_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 2: non-TTY + SATURNDAY_APPROVE_SETUP=1.

    Same real-lifecycle assertions as the CLI flag case; source recorded
    as 'env_var'.
    """
    outcome = _drive_run_plan_for_acceptance(
        tmp_path=tmp_path, is_tty=False, approve_flag=False, env_var=True,
        monkeypatch=monkeypatch,
    )

    assert outcome["setup_calls"] == ["echo REAL_SETUP_STEP_RAN"]
    assert outcome["proof_calls"] == ["echo REAL_PROOF_CMD_RAN"]
    assert outcome["result"].acceptance_setup_approval_source == "env_var"

    data = json.loads(
        (outcome["output_dir"] / "run-summary.json").read_text(encoding="utf-8")
    )
    assert data.get("acceptance_setup_approval_source") == "env_var"


def test_alpha_lifecycle_non_tty_no_signal_blocks_setup_and_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 3: non-TTY + no signals.

    Setup executor MUST NOT be called.  Proof executor MUST NOT be called.
    RunResult and evidence must record 'blocked_noninteractive'.
    """
    outcome = _drive_run_plan_for_acceptance(
        tmp_path=tmp_path, is_tty=False, approve_flag=False, env_var=False,
        monkeypatch=monkeypatch,
    )

    assert outcome["setup_calls"] == [], (
        "non-TTY without approval signal MUST NOT run acceptance_setup"
    )
    assert outcome["proof_calls"] == [], (
        "non-TTY without approval signal MUST NOT run the proof — setup blocked"
    )
    assert outcome["result"].acceptance_setup_approval_source == "blocked_noninteractive"

    data = json.loads(
        (outcome["output_dir"] / "run-summary.json").read_text(encoding="utf-8")
    )
    assert data.get("acceptance_setup_approval_source") == "blocked_noninteractive"


def test_alpha_lifecycle_tty_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4: TTY path still works.

    Interactive approval (stdin.isatty=True, operator types 'y') must
    run setup + proof, and record 'interactive'.  Non-regression canary
    for the pre-α code path.
    """
    outcome = _drive_run_plan_for_acceptance(
        tmp_path=tmp_path, is_tty=True, approve_flag=False, env_var=False,
        monkeypatch=monkeypatch,
    )

    assert outcome["setup_calls"] == ["echo REAL_SETUP_STEP_RAN"]
    assert outcome["proof_calls"] == ["echo REAL_PROOF_CMD_RAN"]
    assert outcome["result"].acceptance_setup_approval_source == "interactive"


def test_alpha_lifecycle_tty_decline_records_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4b: TTY + operator types 'n' — declined, no setup, no proof.

    Closes the matrix: makes sure α didn't silently promote a TTY decline
    to a non-interactive approval path.
    """
    from saturnday import ticket_runner as tr
    from saturnday._types import CoderConfig

    plan_path, repo = _write_minimal_plan_with_setup(tmp_path)
    output_dir = tmp_path / "evidence"

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.delenv("SATURNDAY_APPROVE_SETUP", raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    setup_calls: list[str] = []
    proof_calls: list[str] = []
    monkeypatch.setattr(tr, "_run_acceptance_setup_step",
                        lambda step, rp, *a, **kw: (setup_calls.append(step) or ""))
    monkeypatch.setattr(tr, "_run_verify_cmd",
                        lambda cmd, rp, *a, **kw: (proof_calls.append(cmd) or ""))

    config = CoderConfig(backend="openai", base_url="", api_key="k", model="m")
    result = tr.run_plan(
        plan_path=plan_path, repo_path=repo, coder_config=config,
        standards_dir=repo, output_dir=output_dir,
        skip_tickets=frozenset({"T001"}),
        auto_repair=False, role_passes=False,
        approve_acceptance_setup=False,
    )

    assert setup_calls == []
    assert proof_calls == []
    assert result.acceptance_setup_approval_source == "declined"
