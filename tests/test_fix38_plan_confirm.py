"""Fix 38 — DoD approval gate enforced on launcher start path via plan-confirm.

Proves that:
1. plan_confirm() generates a plan and shows the DoD gate.
2. Accept leaves a confirmed .saturnday/plan.json and returns 0.
3. Edit rewrites required_outcomes in the persisted plan and returns 0.
4. Abort exits cleanly (returns 1) without proceeding.
5. Launcher governance prompt references plan-confirm, not plain plan.
6. guided_run() DoD behaviour is unchanged (delegates to _run_dod_gate).
7. _run_dod_gate correctly handles the no-required-outcomes simple Proceed path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plan_with_outcomes(tmp_path: Path, outcomes: list[str] | None = None) -> Path:
    """Write a minimal plan.json, optionally with required_outcomes."""
    plan_data: dict[str, Any] = {
        "project_id": "fix38-test",
        "tickets": [{"ticket_id": "T001", "goal": "do something", "acceptance_criteria": ["done"]}],
        "phases": [],
        "definition_of_done": ["all_tickets_passed"],
    }
    if outcomes is not None:
        plan_data["required_outcomes"] = outcomes
    plan_path = tmp_path / ".saturnday" / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")
    return plan_path


def _fake_generate_plan(tmp_path: Path, outcomes: list[str] | None = None):
    """Return a side-effect function that writes and returns a plan path."""
    plan_path = _plan_with_outcomes(tmp_path, outcomes)

    def _gen(brief, repo_path, coder_config, output_path=None, **kw):
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(plan_path.read_text(encoding="utf-8"), encoding="utf-8")
            return out
        return plan_path

    return _gen


# ---------------------------------------------------------------------------
# Test 1 & 2: plan_confirm accept → confirmed plan.json exists, returns 0
# ---------------------------------------------------------------------------

def test_plan_confirm_accept_returns_0_and_writes_plan(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Accepting the DoD gate leaves plan.json confirmed and returns 0."""
    from saturnday.interactive import plan_confirm

    gen = _fake_generate_plan(tmp_path, outcomes=["App runs", "Tests pass"])

    monkeypatch.setattr("builtins.input", lambda _: "1")  # Accept

    with patch("saturnday.run.planner.generate_plan", side_effect=gen):
        rc = plan_confirm(
            brief="build an app",
            repo_path=tmp_path,
            backend="claude-cli",
        )

    assert rc == 0
    plan_path = tmp_path / ".saturnday" / "plan.json"
    assert plan_path.exists(), "plan.json must exist after accept"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data["project_id"] == "fix38-test"
    out = capsys.readouterr().out
    assert "Plan confirmed" in out


# ---------------------------------------------------------------------------
# Test 3: edit → required_outcomes rewritten in persisted plan
# ---------------------------------------------------------------------------

def test_plan_confirm_edit_rewrites_outcomes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Editing outcomes via the $EDITOR-backed gate persists the new
    outcomes to plan.json.

    The new UX opens $EDITOR pre-filled with the proposed outcomes; the
    operator's save replaces the required_outcomes list.  Saving IS the
    accept action — no separate y/n confirmation.
    """
    from saturnday.interactive import plan_confirm

    gen = _fake_generate_plan(tmp_path, outcomes=["Original outcome"])

    # Pick [2] — edit — then the editor returns the new outcomes.
    monkeypatch.setattr("builtins.input", lambda _: "2")

    with (
        patch("saturnday.run.planner.generate_plan", side_effect=gen),
        patch(
            "saturnday.interactive._edit_outcomes_in_editor",
            return_value=["New outcome A", "New outcome B"],
        ),
    ):
        rc = plan_confirm(
            brief="build an app",
            repo_path=tmp_path,
            backend="claude-cli",
        )

    assert rc == 0
    plan_path = tmp_path / ".saturnday" / "plan.json"
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data["required_outcomes"] == ["New outcome A", "New outcome B"], (
        "required_outcomes must be updated to the editor output"
    )
    assert data.get("_dod_user_edited") is True


# ---------------------------------------------------------------------------
# Test 4: abort → returns 1, plan.json still written by generate_plan but
#          the function signals abort (caller should not proceed to run)
# ---------------------------------------------------------------------------

def test_plan_confirm_ctrl_c_returns_1(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The new UX has no explicit Abort option — the only way to cancel
    is a hard interrupt at the menu (Ctrl-C / EOF).  That must return 1
    so callers don't proceed to run."""
    from saturnday.interactive import plan_confirm

    gen = _fake_generate_plan(tmp_path, outcomes=["Some outcome"])

    def _raise_kbi(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise_kbi)

    with patch("saturnday.run.planner.generate_plan", side_effect=gen):
        rc = plan_confirm(
            brief="build an app",
            repo_path=tmp_path,
            backend="claude-cli",
        )

    assert rc == 1
    out = capsys.readouterr().out
    assert "Aborted" in out


# ---------------------------------------------------------------------------
# Test 5: launcher governance prompt uses plan-confirm, not plain plan
# ---------------------------------------------------------------------------

def test_governance_prompt_references_plan_confirm() -> None:
    """_GOVERNANCE_PROMPT_NEW must surface plan-confirm as an operator-gated
    alternative for builds requiring manual DoD approval.

    The main pipeline rule-4 recipe uses ``saturnday plan`` because the
    automatic DoD gate is mechanical, but the prompt must also direct the
    operator to run ``saturnday plan-confirm`` in their own terminal when
    they want to review and approve the DoD before running.  Both
    references are mandatory.
    """
    from saturnday.interactive import _GOVERNANCE_PROMPT_NEW

    assert "plan-confirm" in _GOVERNANCE_PROMPT_NEW, (
        "Launcher governance prompt must reference plan-confirm so the "
        "operator has a manual-DoD path"
    )
    lines = _GOVERNANCE_PROMPT_NEW.splitlines()
    rule4_lines = []
    in_rule4 = False
    for line in lines:
        if line.strip().startswith("4."):
            in_rule4 = True
        elif line.strip().startswith(("5.", "6.", "7.", "8.", "9.")):
            in_rule4 = False
        if in_rule4:
            rule4_lines.append(line)

    rule4_text = "\n".join(rule4_lines)
    assert "plan-confirm" in rule4_text, (
        "Rule 4 must surface plan-confirm as the operator-gated option"
    )


def test_command_reference_lists_plan_confirm() -> None:
    """_COMMAND_REFERENCE written to CLAUDE.md must list plan-confirm."""
    from saturnday.interactive import _COMMAND_REFERENCE

    assert "plan-confirm" in _COMMAND_REFERENCE


# ---------------------------------------------------------------------------
# Test 6: guided_run() DoD gate unchanged — delegates to _run_dod_gate
# ---------------------------------------------------------------------------

def test_guided_run_dod_accept_still_calls_run_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """guided_run() with required_outcomes: accept still reaches run_plan."""
    from saturnday.interactive import guided_run
    from saturnday._types import RunResult

    (tmp_path / ".git").mkdir()
    plan_path = _plan_with_outcomes(tmp_path, outcomes=["Feature works"])
    run_result = RunResult(project_id="fix38-test", total_tickets=1, passed=1)

    inputs = iter(["1"])  # Accept
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))

    with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
         patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
         patch("saturnday.ticket_runner.run_plan", return_value=run_result) as mock_run:
        rc = guided_run(tmp_path, goal="build something")

    assert mock_run.called, "run_plan must be called after DoD accepted in guided_run"


def test_guided_run_dod_ctrl_c_does_not_call_run_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """guided_run() with required_outcomes: Ctrl-C at the DoD gate must
    not reach run_plan.  Under the new UX the only way to cancel is a
    hard interrupt at the menu."""
    from saturnday.interactive import guided_run

    (tmp_path / ".git").mkdir()
    plan_path = _plan_with_outcomes(tmp_path, outcomes=["Feature works"])

    def _raise_kbi(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise_kbi)

    with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
         patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
         patch("saturnday.ticket_runner.run_plan") as mock_run:
        rc = guided_run(tmp_path, goal="build something")

    assert not mock_run.called, "run_plan must NOT be called when DoD gate interrupted"
    assert rc == 0


# ---------------------------------------------------------------------------
# Test 7: _run_dod_gate simple proceed path (no required_outcomes)
# ---------------------------------------------------------------------------

def test_run_dod_gate_no_outcomes_proceed(tmp_path: Path, monkeypatch) -> None:
    """When no required_outcomes: simple Y/n gate — Y returns True."""
    from saturnday.interactive import _run_dod_gate

    plan_path = _plan_with_outcomes(tmp_path, outcomes=None)
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))

    monkeypatch.setattr("builtins.input", lambda _: "")  # Enter = proceed

    result = _run_dod_gate(plan_data, plan_path)
    assert result is True


def test_run_dod_gate_no_outcomes_decline(tmp_path: Path, monkeypatch) -> None:
    """When no required_outcomes: simple Y/n gate — n returns False."""
    from saturnday.interactive import _run_dod_gate

    plan_path = _plan_with_outcomes(tmp_path, outcomes=None)
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))

    monkeypatch.setattr("builtins.input", lambda _: "n")

    result = _run_dod_gate(plan_data, plan_path)
    assert result is False
