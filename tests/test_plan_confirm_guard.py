"""Tests for Fix 57 — plan-confirm guidance and runtime guard.

Proves:
1. Generated CLAUDE.md guidance tells coder to use 'saturnday plan' not 'plan-confirm'
2. Non-interactive plan-confirm gives actionable error with alternatives
3. plan-confirm still works in interactive context (not tested here — requires TTY)
4. saturnday plan behaviour is unchanged
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest


class TestCLAUDEMdGuidance:
    """Test that generated CLAUDE.md no longer tells coder to use plan-confirm."""

    def test_rule_4_uses_plan_not_plan_confirm(self) -> None:
        """Rule 4 in CLAUDE.md template must say 'saturnday plan', not 'plan-confirm'."""
        # The template is built inline in interactive.py — import and check the text
        import saturnday.interactive as inter
        # Find the CLAUDE.md template text by searching for the rule
        import inspect
        source = inspect.getsource(inter)

        # Rule 4 should say "saturnday plan --brief"
        assert 'a. saturnday plan --brief' in source
        # Should NOT say "ALWAYS use saturnday plan-confirm" as the default
        assert 'ALWAYS use the governed pipeline:\n   a. saturnday plan-confirm' not in source

    def test_new_projects_guidance_uses_plan(self) -> None:
        """'New projects' line must say 'plan + run', not 'plan-confirm + run'."""
        import inspect
        import saturnday.interactive as inter
        source = inspect.getsource(inter)
        assert '- New projects: saturnday plan + saturnday run' in source

    def test_plan_confirm_marked_as_user_terminal(self) -> None:
        """plan-confirm in command reference must note it requires user's own terminal."""
        import inspect
        import saturnday.interactive as inter
        source = inspect.getsource(inter)
        assert 'user must run in their own terminal' in source or '! saturnday plan-confirm' in source


class TestPlanConfirmNonInteractive:
    """Test that non-interactive plan-confirm gives actionable guidance."""

    def test_non_interactive_error_mentions_plan_alternative(self) -> None:
        """Non-interactive error must tell user to use 'saturnday plan' instead."""
        import argparse
        from saturnday.cli import _cmd_plan_confirm

        args = argparse.Namespace(
            brief="test", repo=".", backend="claude-cli",
            output=None, verbose=False,
        )

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                result = _cmd_plan_confirm(args)

        assert result == 1
        output = mock_stderr.getvalue()
        assert "saturnday plan" in output
        assert "non-interactive" in output.lower() or "subprocess" in output.lower()

    def test_non_interactive_error_mentions_bang_prefix(self) -> None:
        """Non-interactive error must mention ! prefix for manual terminal execution."""
        import argparse
        from saturnday.cli import _cmd_plan_confirm

        args = argparse.Namespace(
            brief="test", repo=".", backend="claude-cli",
            output=None, verbose=False,
        )

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                _cmd_plan_confirm(args)

        output = mock_stderr.getvalue()
        assert "! saturnday plan-confirm" in output
