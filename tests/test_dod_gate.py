"""Tests for the new DoD gate UX in ``interactive._run_dod_gate``.

The new gate has two options:
  [1] Accept the Definition of Done and proceed
  [2] Edit the Definition of Done (opens $EDITOR pre-filled; saving = accept)

There is no [3] Abort and no separate ``y/n`` confirmation — saving the
editor IS the accept action.  Hard input interrupts (EOF / Ctrl-C) before
a choice return ``False`` so the caller can surface the cancellation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _write_plan(tmp_path: Path, required_outcomes: list[str] | None = None) -> Path:
    plan_data: dict = {
        "project_id": "test-proj",
        "tickets": [{
            "ticket_id": "T001", "goal": "do stuff",
            "acceptance_criteria": ["build ok"],
        }],
        "phases": [],
        "definition_of_done": ["all_tickets_passed"],
    }
    if required_outcomes is not None:
        plan_data["required_outcomes"] = required_outcomes
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan_data), encoding="utf-8")
    return plan_path


# ---------------------------------------------------------------------------
# Accept path ([1])
# ---------------------------------------------------------------------------


class TestDodAccept:
    def test_choice_1_accepts_and_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=["A", "B"])
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "1")
        proceed = _run_dod_gate(plan_data, plan_path)

        assert proceed is True
        # Not edited — flag set false
        assert plan_data.get("_dod_user_edited") is False

    def test_empty_choice_treated_as_accept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate
        plan_path = _write_plan(tmp_path, required_outcomes=["A"])
        plan_data = json.loads(plan_path.read_text())
        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _run_dod_gate(plan_data, plan_path) is True


# ---------------------------------------------------------------------------
# Edit path ([2])
# ---------------------------------------------------------------------------


class TestDodEdit:
    """β: menu renumbered — [2] is inline amend, [3] is $EDITOR.
    These tests target the $EDITOR path → choice '3'."""

    def test_edit_replaces_outcomes_and_persists_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=["Original"])
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "3")
        with patch(
            "saturnday.interactive._edit_outcomes_in_editor",
            return_value=["Edited A", "Edited B"],
        ):
            proceed = _run_dod_gate(plan_data, plan_path)

        assert proceed is True
        assert plan_data["required_outcomes"] == ["Edited A", "Edited B"]
        assert plan_data["_dod_user_edited"] is True
        # Plan file was rewritten.
        written = json.loads(plan_path.read_text())
        assert written["required_outcomes"] == ["Edited A", "Edited B"]

    def test_edit_no_changes_proceeds_without_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=["Same"])
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "3")
        with patch(
            "saturnday.interactive._edit_outcomes_in_editor",
            return_value=["Same"],
        ):
            proceed = _run_dod_gate(plan_data, plan_path)

        assert proceed is True
        assert plan_data["required_outcomes"] == ["Same"]
        assert plan_data["_dod_user_edited"] is False

    def test_edit_prints_updated_dod(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=["First"])
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "3")
        with patch(
            "saturnday.interactive._edit_outcomes_in_editor",
            return_value=["Updated outcome"],
        ):
            _run_dod_gate(plan_data, plan_path)

        out = capsys.readouterr().out
        assert "Updated Definition of Done" in out
        assert "Updated outcome" in out


# ---------------------------------------------------------------------------
# Ctrl-C / EOF on the menu
# ---------------------------------------------------------------------------


class TestDodInterrupt:
    def test_keyboard_interrupt_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=["A"])
        plan_data = json.loads(plan_path.read_text())

        def _raise_kbi(_prompt: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_kbi)
        assert _run_dod_gate(plan_data, plan_path) is False

    def test_eof_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=["A"])
        plan_data = json.loads(plan_path.read_text())

        def _raise_eof(_prompt: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        assert _run_dod_gate(plan_data, plan_path) is False


# ---------------------------------------------------------------------------
# Plan without required_outcomes — falls back to a single proceed prompt
# ---------------------------------------------------------------------------


class TestDodNoRequiredOutcomes:
    def test_proceed_y_returns_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=None)
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "y")
        assert _run_dod_gate(plan_data, plan_path) is True

    def test_proceed_n_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=None)
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "n")
        assert _run_dod_gate(plan_data, plan_path) is False

    def test_empty_proceed_defaults_to_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import _run_dod_gate

        plan_path = _write_plan(tmp_path, required_outcomes=None)
        plan_data = json.loads(plan_path.read_text())

        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _run_dod_gate(plan_data, plan_path) is True
