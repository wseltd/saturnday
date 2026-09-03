"""Tests for the --auto-repair integration in ticket_runner.py and cli.py (U5).

Covers:
- --auto-repair flag exists in the CLI run subparser
- auto_repair=False: behaviour unchanged (no extra attempt after retry exhaustion)
- auto_repair=True: one repair attempt is made after retry exhaustion
- auto_repair=True: if repair succeeds, returns PASS
- auto_repair=True: if repair also fails, returns FAIL
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from saturnday._types import CoderConfig, TicketSpec
from saturnday.cli import build_parser
from saturnday.ticket_runner import _attempt_auto_repair


# ---------------------------------------------------------------------------
# CLI flag presence
# ---------------------------------------------------------------------------

class TestAutoRepairFlag:
    def test_auto_repair_flag_exists_in_run_subparser(self):
        """--auto-repair must be a recognised argument for the run subcommand."""
        parser = build_parser()
        # Should not raise
        args = parser.parse_args([
            "run",
            "--plan", "plan.json",
            "--repo", ".",
            "--backend", "codex-cli",
            "--auto-repair",
        ])
        assert args.auto_repair is True

    def test_auto_repair_defaults_false(self):
        parser = build_parser()
        args = parser.parse_args([
            "run",
            "--plan", "plan.json",
            "--repo", ".",
            "--backend", "codex-cli",
        ])
        assert args.auto_repair is False


# ---------------------------------------------------------------------------
# _attempt_auto_repair helper unit tests
# ---------------------------------------------------------------------------

def _make_ticket(**kwargs) -> TicketSpec:
    defaults = dict(
        ticket_id="T001",
        goal="Implement add function",
        acceptance_criteria=(),
    )
    defaults.update(kwargs)
    return TicketSpec(**defaults)


def _make_coder_config() -> CoderConfig:
    return CoderConfig(backend="anthropic", api_key="test-key")


class TestAttemptAutoRepair:
    """Unit tests for _attempt_auto_repair using mocks for coder and governance."""

    def _patch_targets(self):
        """Return a dict of patch targets for the ticket_runner internals."""
        base = "saturnday.ticket_runner"
        return {
            "execute": f"{base}._execute_ticket",
            "git_add": f"{base}._git_add",
            "governance": f"{base}._run_governance",
            "filter_findings": f"{base}._filter_findings_to_files",
            "post_checks": f"{base}.run_post_checks",
            "git_commit": f"{base}._git_commit",
            "git_reset": f"{base}._git_reset_changes",
            "write_evidence": f"{base}.write_ticket_evidence",
        }

    def test_repair_succeeds_returns_pass(self, tmp_path: Path):
        """When repair attempt passes governance, returns a PASS TicketResult."""
        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        t = self._patch_targets()

        with (
            patch(t["execute"], return_value=("repaired response", ["src/add.py"])),
            patch(t["git_add"]),
            patch(t["governance"], return_value=("PASS", [], "/path/to/evidence.json", [])),
            patch(t["filter_findings"], return_value=[]),
            patch(t["post_checks"], return_value=[]),
            patch(t["git_commit"]),
            patch(t["write_evidence"]),
        ):
            result = _attempt_auto_repair(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                repair_context="- src/add.py: missing function",
                attempt_number=4,
            )

        assert result is not None
        assert result.disposition == "PASS"
        assert result.ticket_id == "T001"
        assert result.attempts == 4

    def test_repair_governance_fails_returns_none(self, tmp_path: Path):
        """When repair attempt fails governance, returns None (caller returns FAIL)."""
        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        t = self._patch_targets()

        with (
            patch(t["execute"], return_value=("response", ["src/add.py"])),
            patch(t["git_add"]),
            patch(t["governance"], return_value=("FAIL", [{"message": "issue"}], "/ev.json", [])),
            patch(t["filter_findings"], return_value=[{"message": "issue"}]),
            patch(t["git_reset"]),
            patch(t["write_evidence"]),
        ):
            result = _attempt_auto_repair(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                repair_context="- src/add.py: missing function",
                attempt_number=4,
            )

        assert result is None

    def test_repair_coder_error_returns_none(self, tmp_path: Path):
        """When coder raises CloudCoreError, returns None."""
        from saturnday._exceptions import CloudCoreError

        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        t = self._patch_targets()

        with (
            patch(t["execute"], side_effect=CloudCoreError("coder failed")),
            patch(t["write_evidence"]),
        ):
            result = _attempt_auto_repair(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                repair_context="some context",
                attempt_number=4,
            )

        assert result is None

    def test_repair_post_checks_fail_returns_none(self, tmp_path: Path):
        """When repair passes governance but fails post-checks, returns None."""
        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        t = self._patch_targets()

        with (
            patch(t["execute"], return_value=("response", ["src/add.py"])),
            patch(t["git_add"]),
            patch(t["governance"], return_value=("PASS", [], "/ev.json", [])),
            patch(t["filter_findings"], return_value=[]),
            patch(t["post_checks"], return_value=[{"message": "post-check issue"}]),
            patch(t["git_reset"]),
            patch(t["write_evidence"]),
        ):
            result = _attempt_auto_repair(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                repair_context="some context",
                attempt_number=4,
            )

        assert result is None

    def test_repair_context_includes_auto_repair_framing(self, tmp_path: Path):
        """The repair context passed to coder should include explicit repair framing."""
        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        t = self._patch_targets()

        captured_context = {}

        def fake_execute(ticket, repo_path, coder_config, messages):
            captured_context["messages"] = messages
            return ("response", ["src/add.py"])

        with (
            patch(t["execute"], side_effect=fake_execute),
            patch(t["git_add"]),
            patch(t["governance"], return_value=("PASS", [], "/ev.json", [])),
            patch(t["filter_findings"], return_value=[]),
            patch(t["post_checks"], return_value=[]),
            patch(t["git_commit"]),
            patch(t["write_evidence"]),
        ):
            _attempt_auto_repair(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                repair_context="- src/add.py: missing add function",
                attempt_number=4,
            )

        msgs = captured_context.get("messages", [])
        rendered = "\n".join(
            m.get("content", "") for m in msgs if isinstance(m, dict)
        )
        assert "AUTO-REPAIR PASS" in rendered
        assert "missing add function" in rendered


# ---------------------------------------------------------------------------
# Integration: auto_repair=False leaves behaviour unchanged
# ---------------------------------------------------------------------------

class TestAutoRepairDisabled:
    """Verify that auto_repair=False does not call _attempt_auto_repair."""

    def test_no_auto_repair_call_when_disabled(self, tmp_path: Path):
        """_attempt_auto_repair must never be called when auto_repair=False."""
        from saturnday.ticket_runner import _run_ticket_with_retries
        from saturnday._exceptions import CloudCoreError

        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        base = "saturnday.ticket_runner"

        with (
            patch(f"{base}._execute_ticket",
                  side_effect=CloudCoreError("always fails")),
            patch(f"{base}.write_ticket_evidence"),
            patch(f"{base}._attempt_auto_repair") as mock_repair,
        ):
            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                max_retries=0,
                auto_repair=False,
            )

        mock_repair.assert_not_called()
        assert result.disposition == "FAIL"

    def test_auto_repair_called_when_enabled_and_retries_exhausted(self, tmp_path: Path):
        """_attempt_auto_repair is called exactly once when auto_repair=True and retries exhaust."""
        from saturnday.ticket_runner import _run_ticket_with_retries
        from saturnday._exceptions import CloudCoreError

        ticket = _make_ticket()
        config = _make_coder_config()
        state = MagicMock()
        base = "saturnday.ticket_runner"

        with (
            patch(f"{base}._execute_ticket",
                  side_effect=CloudCoreError("always fails")),
            patch(f"{base}.write_ticket_evidence"),
            patch(f"{base}._attempt_auto_repair", return_value=None) as mock_repair,
        ):
            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=config,
                system_prompt="sys",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                max_retries=0,
                auto_repair=True,
            )

        # Called exactly once
        mock_repair.assert_called_once()
        # Final result is still FAIL (repair returned None)
        assert result.disposition == "FAIL"
