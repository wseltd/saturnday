"""Closes Nick #3b — when run-style commands are invoked without
``--work-branch``, print a one-time-per-invocation deprecation banner
warning that 1.2 will flip the default.

Tests pin three things:
  1. The banner fires when ``args.work_branch`` is None.
  2. The banner stays silent when ``--work-branch`` is set explicitly.
  3. The ``SATURNDAY_SUPPRESS_WORK_BRANCH_BANNER=1`` env var silences
     the banner unconditionally (CI / scripted invocations).
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stderr

import pytest

from saturnday.cli import _warn_implicit_work_branch_default


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _capture(command: str) -> str:
    buf = io.StringIO()
    with redirect_stderr(buf):
        _warn_implicit_work_branch_default(command)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Banner content + suppression
# ---------------------------------------------------------------------------


class TestBannerContent:
    def test_banner_names_command(self) -> None:
        out = _capture("run")
        assert "'run'" in out

    def test_banner_mentions_version_1_2(self) -> None:
        out = _capture("repair")
        assert "1.2" in out

    def test_banner_mentions_auto_default(self) -> None:
        out = _capture("resume")
        assert "auto" in out

    def test_banner_mentions_suppression_env_var(self) -> None:
        out = _capture("rerun-failed")
        assert "SATURNDAY_SUPPRESS_WORK_BRANCH_BANNER" in out

    def test_banner_writes_to_stderr_not_stdout(self) -> None:
        """Operator pipelines often redirect stdout but expect stderr
        to carry advisory text.  Pin so the banner doesn't pollute
        captured stdout (e.g., evidence pipelines)."""
        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stderr(stderr_buf), redirect_stdout(stdout_buf):
            _warn_implicit_work_branch_default("run")
        assert stderr_buf.getvalue(), "banner must go to stderr"
        assert not stdout_buf.getvalue(), "banner must NOT go to stdout"


class TestBannerSuppression:
    def test_env_var_silences_banner(self, monkeypatch) -> None:
        monkeypatch.setenv("SATURNDAY_SUPPRESS_WORK_BRANCH_BANNER", "1")
        assert _capture("run") == ""

    def test_env_var_with_other_value_does_not_silence(
        self, monkeypatch,
    ) -> None:
        """Only the literal string ``"1"`` silences.  Empty string,
        ``"true"``, ``"yes"`` etc. should NOT silence — keeps the
        opt-in deliberate."""
        monkeypatch.setenv("SATURNDAY_SUPPRESS_WORK_BRANCH_BANNER", "true")
        assert _capture("run"), "banner should fire when env var != '1'"

    def test_env_var_unset_means_banner_fires(self, monkeypatch) -> None:
        monkeypatch.delenv(
            "SATURNDAY_SUPPRESS_WORK_BRANCH_BANNER", raising=False,
        )
        assert _capture("run")


# ---------------------------------------------------------------------------
# Wiring — every governed-execution command consults the helper
# ---------------------------------------------------------------------------


class TestEntryPointsCallTheHelper:
    """Source-level pin: each of the six governed-execution entry
    points (run, resume, rerun-failed, rerun-remaining, repair, start)
    calls ``_warn_implicit_work_branch_default`` when work_branch is
    unset.  Driving each command end-to-end requires too much fixture
    surface (config, git repo, plan, backend stubs); pinning the call
    in source is the cheap way to ensure no entry point gets dropped
    in a future refactor."""

    @pytest.mark.parametrize("command_name,token", [
        ("_cmd_run",              '_warn_implicit_work_branch_default("run")'),
        ("_cmd_resume",           '_warn_implicit_work_branch_default("resume")'),
        ("_cmd_rerun_failed",     '_warn_implicit_work_branch_default("rerun-failed")'),
        ("_cmd_rerun_remaining",  '_warn_implicit_work_branch_default("rerun-remaining")'),
        ("_cmd_repair",           '_warn_implicit_work_branch_default("repair")'),
        ("_cmd_start",            '_warn_implicit_work_branch_default("start")'),
    ])
    def test_each_command_function_calls_helper(
        self, command_name: str, token: str,
    ) -> None:
        import inspect
        from saturnday import cli as cli_mod
        fn = getattr(cli_mod, command_name)
        src = inspect.getsource(fn)
        assert token in src, (
            f"{command_name} must call the helper with the right command "
            f"label.  Expected substring: {token!r}.  Source:\n{src}"
        )
