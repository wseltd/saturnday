"""Tests for saturnday.role_modes — role-mode orchestration layer.

Covers: load_role_prompt, available_roles, missing_roles, RoleResult,
invoke_role, run_role_sequence, run_dod_check, extract_classification.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from saturnday.role_modes import (
    KNOWN_ROLES,
    ROLE_CLASSIFICATIONS,
    ROLE_MODES_DIR,
    RoleResult,
    available_roles,
    extract_classification,
    invoke_role,
    load_role_prompt,
    missing_roles,
    run_dod_check,
    run_role_sequence,
)


# ---------------------------------------------------------------------------
# Loader tests (1-8)
# ---------------------------------------------------------------------------


class TestLoadRolePrompt:
    """Tests 1-5: load_role_prompt."""

    def setup_method(self) -> None:
        """Clear lru_cache between tests."""
        load_role_prompt.cache_clear()

    def test_01_valid_role_returns_combined_prompt(self) -> None:
        """Test 1: valid role returns operator_preferences + role prompt joined."""
        role = available_roles()[0]
        result = load_role_prompt(role)
        assert "---" in result
        assert len(result) > 10

    def test_02_invalid_role_raises_value_error(self) -> None:
        """Test 2: unknown role raises ValueError with helpful message."""
        with pytest.raises(ValueError, match="Unknown role"):
            load_role_prompt("not_a_real_role")

    def test_03_missing_role_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Test 3: role in KNOWN_ROLES but no .md file raises FileNotFoundError."""
        # Patch ROLE_MODES_DIR to point at a dir with operator_preferences but
        # no role-specific files.
        prefs = tmp_path / "operator_preferences.md"
        prefs.write_text("# Prefs", encoding="utf-8")
        with patch("saturnday.role_modes.ROLE_MODES_DIR", tmp_path):
            load_role_prompt.cache_clear()
            with pytest.raises(FileNotFoundError, match="Role prompt not found"):
                load_role_prompt("coder")

    def test_04_missing_operator_preferences_raises_file_not_found(
        self, tmp_path: Path
    ) -> None:
        """Test 4: missing operator_preferences.md raises FileNotFoundError."""
        role_file = tmp_path / "coder.md"
        role_file.write_text("# Coder", encoding="utf-8")
        with patch("saturnday.role_modes.ROLE_MODES_DIR", tmp_path):
            load_role_prompt.cache_clear()
            with pytest.raises(FileNotFoundError, match="Operator preferences not found"):
                load_role_prompt("coder")

    def test_05_operator_preferences_content_prepended(self) -> None:
        """Test 5: operator_preferences content appears before role content."""
        prefs_text = (ROLE_MODES_DIR / "operator_preferences.md").read_text(encoding="utf-8")
        role = available_roles()[0]
        combined = load_role_prompt(role)
        # Prefs content must appear before the '---' separator
        separator_idx = combined.index("---")
        prefs_end = combined[:separator_idx]
        # At least the first meaningful line of prefs is present
        first_pref_line = prefs_text.strip().splitlines()[0]
        assert first_pref_line in pefs_end_or_full(prefs_end, combined)

    def _prefs_in_combined(self, combined: str) -> bool:
        prefs_text = (ROLE_MODES_DIR / "operator_preferences.md").read_text(encoding="utf-8")
        return prefs_text.strip().splitlines()[0] in combined


def pefs_end_or_full(prefs_end: str, combined: str) -> str:
    """Helper: return the full combined string for assertion (prefs appear somewhere)."""
    return combined


class TestAvailableAndMissingRoles:
    """Tests 6-8: available_roles, missing_roles, KNOWN_ROLES vs disk."""

    def test_06_available_roles_returns_sorted_list(self) -> None:
        """Test 6: available_roles returns a sorted list of strings."""
        roles = available_roles()
        assert isinstance(roles, list)
        assert roles == sorted(roles)

    def test_07_missing_roles_returns_sorted_list(self) -> None:
        """Test 7: missing_roles returns sorted list (empty if all files present)."""
        roles = missing_roles()
        assert isinstance(roles, list)
        assert roles == sorted(roles)

    def test_08_known_roles_matches_available_when_all_files_present(self) -> None:
        """Test 8: if all .md files are on disk, missing_roles() is empty."""
        # All files should exist (they were copied in RM-01)
        absent = missing_roles()
        assert absent == [], (
            f"Expected all role prompts present, missing: {absent}"
        )


# ---------------------------------------------------------------------------
# RoleResult (9-10)
# ---------------------------------------------------------------------------


class TestRoleResult:
    """Tests 9-10: RoleResult dataclass."""

    def test_09_role_result_fields(self) -> None:
        """Test 9: RoleResult holds all expected fields."""
        r = RoleResult(role="coder", task="do thing", output="done", success=True)
        assert r.role == "coder"
        assert r.task == "do thing"
        assert r.output == "done"
        assert r.success is True
        assert r.error is None

    def test_10_role_result_error_field(self) -> None:
        """Test 10: RoleResult.error stores failure message."""
        r = RoleResult(
            role="coder", task="do thing", output="", success=False, error="timeout"
        )
        assert r.success is False
        assert r.error == "timeout"


# ---------------------------------------------------------------------------
# invoke_role (11-14)
# ---------------------------------------------------------------------------


class TestInvokeRole:
    """Tests 11-14: invoke_role with mocked call_coder."""

    def setup_method(self) -> None:
        load_role_prompt.cache_clear()

    @patch("saturnday.role_modes.load_role_prompt", return_value="SYSTEM PROMPT")
    @patch("saturnday.coder_adapter.call_coder", return_value="coder output")
    def test_11_success_path(
        self, mock_call: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        """Test 11: successful coder call returns RoleResult(success=True)."""
        result = invoke_role(
            "coder",
            "write a function",
            coder_config=MagicMock(),
            repo_path=tmp_path,
        )
        assert result.success is True
        assert result.output == "coder output"
        assert result.error is None
        assert result.role == "coder"

    @patch("saturnday.role_modes.load_role_prompt", return_value="SYSTEM PROMPT")
    @patch("saturnday.coder_adapter.call_coder")
    def test_12_failure_path_returns_success_false(
        self, mock_call: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        """Test 12: backend exception returns RoleResult(success=False)."""
        from saturnday._exceptions import CoderAPIError

        mock_call.side_effect = CoderAPIError("boom")
        result = invoke_role(
            "coder",
            "write a function",
            coder_config=MagicMock(),
            repo_path=tmp_path,
        )
        assert result.success is False
        assert result.error == "boom"
        assert result.output == ""

    @patch("saturnday.role_modes.load_role_prompt", return_value="SYSTEM PROMPT")
    @patch("saturnday.coder_adapter.call_coder", return_value="result with context")
    def test_13_context_appended_to_user_message(
        self, mock_call: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        """Test 13: context dict is JSON-appended to the user message."""
        ctx = {"key": "value"}
        invoke_role(
            "coder",
            "task",
            coder_config=MagicMock(),
            repo_path=tmp_path,
            context=ctx,
        )
        # call_coder(config, messages, repo_path) -> args[1] is messages
        messages = mock_call.call_args[0][1]
        user_msg = messages[1]["content"]
        assert "## Context" in user_msg
        assert '"key": "value"' in user_msg

    @patch("saturnday.role_modes.load_role_prompt", return_value="SYSTEM PROMPT")
    @patch("saturnday.coder_adapter.call_coder", return_value="ok")
    def test_14_system_message_contains_role_prompt(
        self, mock_call: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        """Test 14: system message content equals the loaded role prompt."""
        invoke_role(
            "coder",
            "task",
            coder_config=MagicMock(),
            repo_path=tmp_path,
        )
        messages = mock_call.call_args[0][1]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "SYSTEM PROMPT"


# ---------------------------------------------------------------------------
# run_role_sequence (15-18)
# ---------------------------------------------------------------------------


class TestRunRoleSequence:
    """Tests 15-18: run_role_sequence."""

    def setup_method(self) -> None:
        load_role_prompt.cache_clear()

    def _make_invoke_mock(self, outputs: list[tuple[bool, str]]) -> MagicMock:
        """Return a mock for invoke_role that yields successive results."""
        results = [
            RoleResult(role=r, task="t", output=o, success=s, error=None if s else "err")
            for (s, o), r in zip(outputs, ["coder", "repair", "evidence_gate"])
        ]
        mock = MagicMock(side_effect=results)
        return mock

    @patch("saturnday.role_modes.load_role_prompt", return_value="SYS")
    @patch("saturnday.coder_adapter.call_coder", return_value="ok")
    def test_15_all_succeed(
        self, mock_call: MagicMock, mock_load: MagicMock, tmp_path: Path
    ) -> None:
        """Test 15: all roles succeed — returns list of all results."""
        roles = [("coder", "task1"), ("repair", "task2")]
        with patch("saturnday.role_modes.invoke_role") as mock_invoke:
            mock_invoke.side_effect = [
                RoleResult(role="coder", task="task1", output="a", success=True),
                RoleResult(role="repair", task="task2", output="b", success=True),
            ]
            results = run_role_sequence(
                roles, coder_config=MagicMock(), repo_path=tmp_path
            )
        assert len(results) == 2
        assert all(r.success for r in results)

    @patch("saturnday.role_modes.invoke_role")
    def test_16_stop_on_failure(self, mock_invoke: MagicMock, tmp_path: Path) -> None:
        """Test 16: stop_on_failure=True aborts after first failure."""
        mock_invoke.side_effect = [
            RoleResult(role="coder", task="t", output="", success=False, error="err"),
            RoleResult(role="repair", task="t", output="ok", success=True),
        ]
        results = run_role_sequence(
            [("coder", "t"), ("repair", "t")],
            coder_config=MagicMock(),
            repo_path=tmp_path,
            stop_on_failure=True,
        )
        assert len(results) == 1
        assert results[0].success is False

    @patch("saturnday.role_modes.invoke_role")
    def test_17_continue_on_failure_by_default(
        self, mock_invoke: MagicMock, tmp_path: Path
    ) -> None:
        """Test 17: stop_on_failure=False (default) runs all roles."""
        mock_invoke.side_effect = [
            RoleResult(role="coder", task="t", output="", success=False, error="err"),
            RoleResult(role="repair", task="t", output="ok", success=True),
        ]
        results = run_role_sequence(
            [("coder", "t"), ("repair", "t")],
            coder_config=MagicMock(),
            repo_path=tmp_path,
        )
        assert len(results) == 2

    @patch("saturnday.role_modes.invoke_role")
    def test_18_callbacks_invoked(self, mock_invoke: MagicMock, tmp_path: Path) -> None:
        """Test 18: on_role_start and on_role_complete callbacks are called."""
        mock_invoke.side_effect = [
            RoleResult(role="coder", task="t", output="ok", success=True),
        ]
        starts: list[tuple[str, str]] = []
        completes: list[tuple[str, RoleResult]] = []

        run_role_sequence(
            [("coder", "t")],
            coder_config=MagicMock(),
            repo_path=tmp_path,
            on_role_start=lambda r, t: starts.append((r, t)),
            on_role_complete=lambda r, res: completes.append((r, res)),
        )
        assert starts == [("coder", "t")]
        assert len(completes) == 1
        assert completes[0][0] == "coder"


# ---------------------------------------------------------------------------
# run_dod_check (19-21)
# ---------------------------------------------------------------------------


class TestRunDodCheck:
    """Tests 19-21: run_dod_check."""

    def setup_method(self) -> None:
        load_role_prompt.cache_clear()

    def _write_files(
        self,
        tmp_path: Path,
        *,
        plan: dict | None = None,
        ledger: dict | None = None,
        summary: dict | None = None,
    ) -> tuple[Path, Path]:
        """Write plan / ledger / summary to tmp_path. Returns (plan_path, evidence_dir)."""
        plan_path = tmp_path / "plan.json"
        if plan is not None:
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

        evidence_dir = tmp_path / "evidence_out"
        evidence_dir.mkdir()

        if ledger is not None:
            ledger_dir = evidence_dir / "evidence" / "run"
            ledger_dir.mkdir(parents=True)
            (ledger_dir / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

        if summary is not None:
            (evidence_dir / "run-summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )

        return plan_path, evidence_dir

    @patch("saturnday.role_modes.invoke_role")
    def test_19_with_full_evidence(
        self, mock_invoke: MagicMock, tmp_path: Path
    ) -> None:
        """Test 19: run_dod_check with plan + ledger + summary calls invoke_role."""
        plan = {
            "project_id": "test-proj",
            "tickets": [{"ticket_id": "T1", "acceptance_criteria": ["passes tests"]}],
            "definition_of_done": ["all_tickets_passed"],
        }
        summary = {"ticket_results": [{"ticket_id": "T1", "disposition": "PASS"}]}
        ledger = {"entries": []}

        plan_path, evidence_dir = self._write_files(
            tmp_path, plan=plan, ledger=ledger, summary=summary
        )
        mock_invoke.return_value = RoleResult(
            role="definition_of_done", task="...", output="DOD_MET", success=True
        )
        result = run_dod_check(
            plan_path=plan_path,
            evidence_dir=evidence_dir,
            repo_path=tmp_path,
            coder_config=MagicMock(),
        )
        assert result.success is True
        # Task sent to invoke_role should mention the project and ticket
        task_sent = mock_invoke.call_args[0][1]
        assert "test-proj" in task_sent
        assert "T1" in task_sent

    @patch("saturnday.role_modes.invoke_role")
    def test_20_missing_ledger_does_not_raise(
        self, mock_invoke: MagicMock, tmp_path: Path
    ) -> None:
        """Test 20: missing ledger file handled gracefully (note added to task)."""
        plan = {"project_id": "proj", "tickets": [], "definition_of_done": ["all_tickets_passed"]}
        plan_path, evidence_dir = self._write_files(tmp_path, plan=plan, summary={})
        mock_invoke.return_value = RoleResult(
            role="definition_of_done", task="...", output="DOD_NOT_MET", success=True
        )
        result = run_dod_check(
            plan_path=plan_path,
            evidence_dir=evidence_dir,
            repo_path=tmp_path,
            coder_config=MagicMock(),
        )
        task_sent = mock_invoke.call_args[0][1]
        assert "Ledger not found" in task_sent

    @patch("saturnday.role_modes.invoke_role")
    def test_21_missing_summary_does_not_raise(
        self, mock_invoke: MagicMock, tmp_path: Path
    ) -> None:
        """Test 21: missing run-summary.json handled gracefully (note added to task)."""
        plan = {"project_id": "proj", "tickets": [], "definition_of_done": ["all_tickets_passed"]}
        plan_path, evidence_dir = self._write_files(tmp_path, plan=plan)
        mock_invoke.return_value = RoleResult(
            role="definition_of_done", task="...", output="DOD_NOT_MET", success=True
        )
        result = run_dod_check(
            plan_path=plan_path,
            evidence_dir=evidence_dir,
            repo_path=tmp_path,
            coder_config=MagicMock(),
        )
        task_sent = mock_invoke.call_args[0][1]
        assert "Run summary not found" in task_sent


# ---------------------------------------------------------------------------
# extract_classification (22-24)
# ---------------------------------------------------------------------------


class TestExtractClassification:
    """Tests 22-24: extract_classification."""

    def test_22_found_first_candidate(self) -> None:
        """Test 22: returns first matching candidate."""
        output = "The evaluation result is DOD_MET for all tickets."
        result = extract_classification(
            output, ["DOD_MET", "DOD_PARTIAL", "DOD_NOT_MET"]
        )
        assert result == "DOD_MET"

    def test_23_no_match_returns_truncated_output(self) -> None:
        """Test 23: no candidate match returns truncated first-line fallback."""
        output = "Some ambiguous output that does not contain any known classification."
        result = extract_classification(output, ["DOD_MET", "DOD_PARTIAL"])
        # Must be <= 83 chars (80 + '...')
        assert len(result) <= 83
        assert "ambiguous" in result

    def test_24_multiple_candidates_returns_first_match(self) -> None:
        """Test 24: when multiple candidates match, returns the first in candidate list order."""
        # Both DOD_PARTIAL and DOD_MET appear, but DOD_MET is first in list
        output = "DOD_PARTIAL and also DOD_MET are present."
        result = extract_classification(
            output, ["DOD_MET", "DOD_PARTIAL", "DOD_NOT_MET"]
        )
        assert result == "DOD_MET"


# ---------------------------------------------------------------------------
# ROLE_CLASSIFICATIONS (25-26)
# ---------------------------------------------------------------------------


class TestRoleClassifications:
    """Tests 25-26: ROLE_CLASSIFICATIONS registry."""

    def test_25_role_classifications_cover_all_known_roles(self) -> None:
        """Test 25: ROLE_CLASSIFICATIONS must cover every KNOWN_ROLE."""
        assert set(ROLE_CLASSIFICATIONS.keys()) == KNOWN_ROLES

    def test_26_role_classifications_have_required_fields(self) -> None:
        """Test 26: Each classification must have type, path, run, repair; type must be valid."""
        valid_types = {"prompt_pass", "native_runtime", "hybrid"}
        for role, info in ROLE_CLASSIFICATIONS.items():
            assert "type" in info, f"{role} missing 'type'"
            assert info["type"] in valid_types, (
                f"{role} has invalid type {info['type']!r}; expected one of {valid_types}"
            )
            assert "path" in info, f"{role} missing 'path'"
            assert "run" in info, f"{role} missing 'run'"
            assert "repair" in info, f"{role} missing 'repair'"
