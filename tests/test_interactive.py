"""Tests for saturnday.interactive — the saturnday start guided workflow.

Covers detect_context, detect_backends, select_backend,
classify_intent, parse_slash, Session persistence, print_header,
repl_loop, show_plan_preview, print_ticket_progress, show_run_summary,
show_status, guided_guard, guided_run, guided_repair, and the CLI start
command handler in cli.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Lightweight stubs for types returned by internal subsystems
# ---------------------------------------------------------------------------

@dataclass
class _StubFinding:
    kind: str = "shell_danger"
    severity: str = "high"
    file: str = "skill.py"
    message: str = "found unsafe shell call"


@dataclass
class _StubScanResult:
    disposition: str = "PASS"
    findings: list = field(default_factory=list)

    @property
    def findings_count(self) -> int:
        return len(self.findings)


@dataclass
class _StubRunResult:
    project_id: str = "test-project"
    total_tickets: int = 2
    passed: int = 2
    failed: int = 0
    skipped: int = 0
    ticket_results: tuple = ()
    definition_of_done_met: bool = True
    stop_reason: str = ""


@dataclass
class _StubCheckResult:
    findings: list = field(default_factory=list)


@dataclass
class _StubPack:
    disposition: str = "PASS"
    check_results: list = field(default_factory=list)


@dataclass
class _StubRepairResult:
    fixed: int = 1
    partial: int = 0
    failed: int = 0
    stopped_early: bool = False
    stop_reason: str = ""


# ---------------------------------------------------------------------------
# detect_context
# ---------------------------------------------------------------------------

class TestDetectContext:
    def test_non_git_repo(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        ctx = detect_context(tmp_path)
        assert ctx["git_initialized"] is False
        assert ctx["git_branch"] == ""
        assert ctx["git_clean"] is True
        assert ctx["skill_md"] is False

    def test_skill_md_found(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        (tmp_path / "SKILL.md").write_text("# My Skill")
        ctx = detect_context(tmp_path)
        assert ctx["skill_md"] is True

    def test_git_initialized(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        (tmp_path / ".git").mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="main\n", returncode=0)
            ctx = detect_context(tmp_path)
        assert ctx["git_initialized"] is True

    def test_git_branch_and_clean(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        (tmp_path / ".git").mkdir()
        responses = [
            MagicMock(stdout="feature/foo\n", returncode=0),  # branch
            MagicMock(stdout="", returncode=0),               # status clean
        ]
        with patch("subprocess.run", side_effect=responses):
            ctx = detect_context(tmp_path)
        assert ctx["git_branch"] == "feature/foo"
        assert ctx["git_clean"] is True

    def test_git_dirty(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        (tmp_path / ".git").mkdir()
        responses = [
            MagicMock(stdout="main\n", returncode=0),
            MagicMock(stdout=" M some_file.py\n", returncode=0),
        ]
        with patch("subprocess.run", side_effect=responses):
            ctx = detect_context(tmp_path)
        assert ctx["git_clean"] is False

    def test_saturnday_version_present(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        ctx = detect_context(tmp_path)
        assert "saturnday_version" in ctx
        assert ctx["saturnday_version"] != ""

    def test_git_subprocess_error_is_swallowed(self, tmp_path: Path) -> None:
        from saturnday.interactive import detect_context
        import subprocess
        (tmp_path / ".git").mkdir()
        with patch("subprocess.run", side_effect=subprocess.SubprocessError("boom")):
            ctx = detect_context(tmp_path)
        # Should not raise; defaults remain
        assert ctx["git_branch"] == ""
        assert ctx["git_clean"] is True


# ---------------------------------------------------------------------------
# detect_backends
# ---------------------------------------------------------------------------

class TestDetectBackends:
    def test_no_backends_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import detect_backends
        monkeypatch.setattr("shutil.which", lambda _: None)
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        backends = detect_backends()
        assert all(not b["available"] for b in backends)

    def test_codex_cli_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import detect_backends
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        backends = detect_backends()
        codex = next(b for b in backends if b["name"] == "codex-cli")
        assert codex["available"] is True

    def test_openai_api_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import detect_backends
        monkeypatch.setattr("shutil.which", lambda _: None)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        backends = detect_backends()
        openai_b = next(b for b in backends if b["name"] == "openai")
        assert openai_b["available"] is True

    def test_anthropic_api_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import detect_backends
        monkeypatch.setattr("shutil.which", lambda _: None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        backends = detect_backends()
        ant = next(b for b in backends if b["name"] == "anthropic")
        assert ant["available"] is True

    def test_returns_six_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """detect_backends now returns 6 entries: codex-cli, claude-cli, openclaude, cursor-cli, openai, anthropic."""
        from saturnday.interactive import detect_backends
        monkeypatch.setattr("shutil.which", lambda _: None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        backends = detect_backends()
        assert len(backends) == 6

    def test_detect_backends_probes_version_responsive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI backends should probe --version; responsive binary yields 'installed and responsive'."""
        import subprocess
        from saturnday.interactive import detect_backends

        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        probe_result = MagicMock(returncode=0, stdout="1.0.0")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: probe_result)

        backends = detect_backends()
        codex = next(b for b in backends if b["name"] == "codex-cli")
        assert codex["available"] is True
        assert "responsive" in codex["reason"]

    def test_detect_backends_probes_version_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI backend that returns non-zero exit code yields an error reason but stays available."""
        from saturnday.interactive import detect_backends

        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        probe_result = MagicMock(returncode=1, stdout="", stderr="auth error")
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: probe_result)

        backends = detect_backends()
        codex = next(b for b in backends if b["name"] == "codex-cli")
        # Still marked available — the binary exists; login may or may not be the issue
        assert codex["available"] is True
        assert "error" in codex["reason"].lower()

    def test_detect_backends_probes_version_subprocess_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI backend where subprocess raises yields 'not responding' but stays available."""
        import subprocess
        from saturnday.interactive import detect_backends

        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        def _raise(*a: Any, **kw: Any) -> None:
            raise subprocess.SubprocessError("timeout")

        monkeypatch.setattr("subprocess.run", _raise)

        backends = detect_backends()
        codex = next(b for b in backends if b["name"] == "codex-cli")
        assert codex["available"] is True
        assert "not responding" in codex["reason"]


# ---------------------------------------------------------------------------
# classify_intent
# ---------------------------------------------------------------------------

class TestClassifyIntent:
    @pytest.mark.parametrize("line,expected", [
        # empty
        ("", "empty"),
        ("   ", "empty"),
        # slash
        ("/help", "slash"),
        ("/guard", "slash"),
        ("/quit", "slash"),
        # guard
        ("scan this skill", "guard"),
        ("check my changes", "guard"),
        ("run governance on the repo", "guard"),
        ("audit the findings", "guard"),
        # plan_run
        ("build a calculator skill", "plan_run"),
        ("create a new REST endpoint", "plan_run"),
        ("write a Slack summariser", "plan_run"),
        ("implement the OAuth flow", "plan_run"),
        # repair
        ("fix the shell findings", "repair"),
        ("repair the broken tests", "repair"),
        ("remediate the security issues", "repair"),
        # status
        ("what happened", "status"),
        ("show me the last run", "status"),
        ("show progress", "status"),
        # unknown
        ("hello world", "unknown"),
        ("42", "unknown"),
    ])
    def test_classify(self, line: str, expected: str) -> None:
        from saturnday.interactive import classify_intent
        assert classify_intent(line) == expected

    def test_case_insensitive(self) -> None:
        from saturnday.interactive import classify_intent
        assert classify_intent("SCAN the skill") == "guard"
        assert classify_intent("BUILD a thing") == "plan_run"

    @pytest.mark.parametrize("line", [
        "scanning the logs",       # "scanning" is not "scan"
        "the builder pattern",     # "builder" is not "build"
        "unfixable problem",       # "unfixable" is not "fix"
        "describe the building",   # "building" is not "build"
        "generator function",      # "generator" is not "generate"
    ])
    def test_word_boundary_no_false_positive(self, line: str) -> None:
        """Substrings of intent keywords must NOT trigger classification."""
        from saturnday.interactive import classify_intent
        assert classify_intent(line) == "unknown"

    def test_checklist_for_release_is_publish(self) -> None:
        """'checklist for release' contains 'release' so is now publish intent."""
        from saturnday.interactive import classify_intent
        # "release" is a publish keyword; this is the expected new behaviour
        assert classify_intent("checklist for release") == "publish"

    def test_slash_prefix_always_wins(self) -> None:
        from saturnday.interactive import classify_intent
        # Even if the content looks like guard, slash prefix wins
        assert classify_intent("/scan") == "slash"


# ---------------------------------------------------------------------------
# parse_slash
# ---------------------------------------------------------------------------

class TestParseSlash:
    @pytest.mark.parametrize("line,cmd,has_arg", [
        ("/help", "help", False),
        ("/quit", "quit", False),
        ("/exit", "exit", False),
        ("/guard", "guard", False),
        ("/plan build a thing", "plan", True),
        ("/status", "status", False),
        ("/resume", "resume", False),
        ("/run", "run", False),
    ])
    def test_known_commands(self, line: str, cmd: str, has_arg: bool) -> None:
        from saturnday.interactive import parse_slash
        result_cmd, result_arg = parse_slash(line)
        assert result_cmd == cmd
        if has_arg:
            assert result_arg != ""
        else:
            assert result_arg == ""

    def test_unknown_command_returns_unknown(self) -> None:
        from saturnday.interactive import parse_slash
        cmd, arg = parse_slash("/frobnicate")
        assert cmd == "unknown"

    def test_plan_captures_remainder(self) -> None:
        from saturnday.interactive import parse_slash
        cmd, arg = parse_slash("/plan build a slack summariser")
        assert cmd == "plan"
        assert arg == "build a slack summariser"

    def test_leading_whitespace_stripped(self) -> None:
        from saturnday.interactive import parse_slash
        cmd, _ = parse_slash("  /help  ")
        assert cmd == "help"


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

class TestSession:
    def test_save_creates_file(self, tmp_path: Path) -> None:
        from saturnday.interactive import Session, save_session
        s = Session(repo_path=str(tmp_path), started_at="2026-01-01T00:00:00Z")
        path = save_session(s, tmp_path)
        assert path.is_file()

    def test_save_creates_saturnday_dir(self, tmp_path: Path) -> None:
        from saturnday.interactive import Session, save_session
        s = Session(repo_path=str(tmp_path))
        save_session(s, tmp_path)
        assert (tmp_path / ".saturnday").is_dir()

    def test_round_trip(self, tmp_path: Path) -> None:
        from saturnday.interactive import Session, save_session, load_session
        s = Session(
            repo_path=str(tmp_path),
            backend="anthropic",
            last_plan_path="/tmp/plan.json",
            last_evidence_dir="/tmp/.saturnday-proj",
            last_disposition="PASS",
            started_at="2026-01-01T00:00:00Z",
        )
        save_session(s, tmp_path)
        loaded = load_session(tmp_path)
        assert loaded is not None
        assert loaded.backend == "anthropic"
        assert loaded.last_disposition == "PASS"
        assert loaded.started_at == "2026-01-01T00:00:00Z"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        from saturnday.interactive import load_session
        result = load_session(tmp_path)
        assert result is None

    def test_load_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        from saturnday.interactive import load_session
        sat_dir = tmp_path / ".saturnday"
        sat_dir.mkdir()
        (sat_dir / "session.json").write_text("not valid json", encoding="utf-8")
        result = load_session(tmp_path)
        assert result is None

    def test_load_ignores_unknown_fields(self, tmp_path: Path) -> None:
        from saturnday.interactive import load_session
        sat_dir = tmp_path / ".saturnday"
        sat_dir.mkdir()
        data = {"repo_path": "/some/path", "unknown_future_field": "value"}
        (sat_dir / "session.json").write_text(json.dumps(data), encoding="utf-8")
        result = load_session(tmp_path)
        assert result is not None
        assert result.repo_path == "/some/path"


# ---------------------------------------------------------------------------
# print_header
# ---------------------------------------------------------------------------

class TestPrintHeader:
    def _make_context(self, **overrides: Any) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "saturnday_version": "1.2.3",
            "repo_path": "/some/repo",
            "git_initialized": True,
            "git_branch": "main",
            "git_clean": True,
        }
        ctx.update(overrides)
        return ctx

    def test_shows_version_and_repo(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import print_header
        ctx = self._make_context()
        print_header(ctx)
        out = capsys.readouterr().out
        assert "1.2.3" in out
        assert "/some/repo" in out

    def test_git_none_when_not_initialized(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import print_header
        ctx = self._make_context(git_initialized=False)
        print_header(ctx)
        out = capsys.readouterr().out
        assert "Git: none" in out


# ---------------------------------------------------------------------------
# select_backend
# ---------------------------------------------------------------------------

class TestSelectBackend:
    def test_no_session_user_picks_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No session: shows full list, user picks '1' -> returns first supported backend."""
        from saturnday.interactive import select_backend
        from unittest.mock import MagicMock
        # Make codex installed and auth verified
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        monkeypatch.setattr("builtins.input", lambda _: "1")
        result = select_backend(session=None)
        assert result == "codex-cli"

    def test_no_session_invalid_selection_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No session: invalid numeric choice returns None."""
        from saturnday.interactive import select_backend
        monkeypatch.setattr("shutil.which", lambda _: None)
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("builtins.input", lambda _: "99")
        result = select_backend(session=None)
        assert result is None

    def test_session_with_backend_reuse_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session with backend set, user says 'Y' -> returns that backend (when ready)."""
        from saturnday.interactive import select_backend, Session
        session = Session(repo_path="/repo", backend="anthropic")
        monkeypatch.setattr("shutil.which", lambda _: None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("builtins.input", lambda _: "Y")
        result = select_backend(session=session)
        assert result == "anthropic"

    def test_session_with_backend_reuse_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Session with backend set, user presses Enter (empty) -> reuses backend."""
        from saturnday.interactive import select_backend, Session
        session = Session(repo_path="/repo", backend="anthropic")
        monkeypatch.setattr("shutil.which", lambda _: None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("builtins.input", lambda _: "")
        result = select_backend(session=session)
        assert result == "anthropic"

    def test_session_with_backend_reuse_no_shows_full_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session with backend set, user says 'n' -> falls through to full list, picks '1'."""
        from saturnday.interactive import select_backend, Session
        from unittest.mock import MagicMock
        session = Session(repo_path="/repo", backend="anthropic")
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/codex" if b == "codex" else None)
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # First input: 'n' to decline reuse; second input: '1' to pick from list
        inputs = iter(["n", "1"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        result = select_backend(session=session)
        assert result == "codex-cli"

    def test_eoferror_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """EOFError during selection returns None."""
        from saturnday.interactive import select_backend

        def _raise(_: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise)
        monkeypatch.setattr("shutil.which", lambda _: None)
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        result = select_backend(session=None)
        assert result is None

    def test_keyboard_interrupt_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KeyboardInterrupt during selection returns None."""
        from saturnday.interactive import select_backend

        def _raise(_: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise)
        monkeypatch.setattr("shutil.which", lambda _: None)
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        result = select_backend(session=None)
        assert result is None


# ---------------------------------------------------------------------------
# _check_cli_auth — honest auth verification
# ---------------------------------------------------------------------------

class TestCheckCliAuth:
    def test_not_installed_returns_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_cli_auth
        monkeypatch.setattr("shutil.which", lambda _: None)
        status, reason = _check_cli_auth("claude", ["claude", "auth", "status"])
        assert status == "not_installed"
        assert "'claude' not found" in reason

    def test_auth_verified_on_exit_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_cli_auth
        from unittest.mock import MagicMock
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        status, reason = _check_cli_auth("claude", ["claude", "auth", "status"])
        assert status == "auth_verified"
        assert "logged in" in reason

    def test_auth_unverified_on_exit_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_cli_auth
        from unittest.mock import MagicMock
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
        mock_run = MagicMock(return_value=MagicMock(returncode=1))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        status, reason = _check_cli_auth("codex", ["codex", "login", "status"])
        assert status == "installed_auth_unverified"
        assert "not logged in" in reason

    def test_auth_unverified_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_cli_auth
        import subprocess
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
        def _raise(*a, **k):
            raise subprocess.SubprocessError("timeout")
        monkeypatch.setattr("saturnday.interactive.subprocess.run", _raise)
        status, reason = _check_cli_auth("claude", ["claude", "auth", "status"])
        assert status == "installed_auth_unverified"
        assert "unknown" in reason


# ---------------------------------------------------------------------------
# _check_backend_ready — honest status model
# ---------------------------------------------------------------------------

class TestCheckBackendReady:
    def test_codex_not_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_backend_ready
        monkeypatch.setattr("shutil.which", lambda _: None)
        ready, reason = _check_backend_ready("codex-cli")
        assert not ready
        assert "not found" in reason

    def test_codex_installed_not_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_backend_ready
        from unittest.mock import MagicMock
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
        mock_run = MagicMock(return_value=MagicMock(returncode=1))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        ready, reason = _check_backend_ready("codex-cli")
        assert not ready
        assert "not logged in" in reason

    def test_codex_installed_and_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_backend_ready
        from unittest.mock import MagicMock
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        ready, reason = _check_backend_ready("codex-cli")
        assert ready
        assert "logged in" in reason

    def test_claude_installed_and_logged_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_backend_ready
        from unittest.mock import MagicMock
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr("saturnday.interactive.subprocess.run", mock_run)
        ready, reason = _check_backend_ready("claude-cli")
        assert ready
        assert "logged in" in reason

    def test_openai_api_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_backend_ready
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        ready, reason = _check_backend_ready("openai")
        assert ready
        assert "API key" in reason

    def test_anthropic_api_key_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.interactive import _check_backend_ready
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        ready, reason = _check_backend_ready("anthropic")
        assert not ready


# ---------------------------------------------------------------------------
# _scan_for_repair — uses right scanner based on repo type
# ---------------------------------------------------------------------------

class TestScanForRepair:
    def test_uses_skill_scanner_when_skill_md_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When SKILL.md exists, uses OpenClaw skill scan."""
        from saturnday.interactive import _scan_for_repair
        from unittest.mock import MagicMock, patch
        (tmp_path / "SKILL.md").write_text("# My Skill\nA test skill\n")
        mock_result = MagicMock()
        mock_result.findings = []
        with patch("saturnday.guard.cloud_scanner.scan_skill", return_value=mock_result):
            findings, mode = _scan_for_repair(tmp_path)
        assert mode == "skill"

    def test_uses_repo_review_when_git_and_no_skill_md(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When .git exists but no SKILL.md, uses full repo review."""
        from saturnday.interactive import _scan_for_repair
        from unittest.mock import MagicMock, patch
        (tmp_path / ".git").mkdir()
        mock_pack = MagicMock()
        mock_pack.check_results = []
        with patch("saturnday.governance.run_full_repo_review", return_value=(mock_pack, "/tmp/ev")):
            findings, mode = _scan_for_repair(tmp_path)
        assert mode == "repo"
        assert findings == []

    def test_repo_review_converts_findings_to_finding_objects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Governance CheckResult findings are converted to Finding dataclass instances."""
        from saturnday.interactive import _scan_for_repair
        from saturnday.openclaw_scanner import Finding
        from unittest.mock import MagicMock, patch
        (tmp_path / ".git").mkdir()
        mock_cr = MagicMock()
        mock_cr.name = "secrets"
        mock_cr.severity = "error"
        mock_cr.findings = [
            {"file": "config.py", "line": 10, "pattern": "AWS_SECRET_KEY = 'AKIA...'"},
        ]
        mock_pack = MagicMock()
        mock_pack.check_results = [mock_cr]
        with patch("saturnday.governance.run_full_repo_review", return_value=(mock_pack, "/tmp/ev")):
            findings, mode = _scan_for_repair(tmp_path)
        assert mode == "repo"
        assert len(findings) == 1
        assert isinstance(findings[0], Finding)
        assert findings[0].file == "config.py"
        assert findings[0].line == 10
        assert findings[0].kind == "secrets"

    def test_no_triage_when_coder_config_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When coder_config is None (default), security_triage is never called."""
        from saturnday.interactive import _scan_for_repair
        from saturnday import capability_registry
        from unittest.mock import MagicMock, patch
        (tmp_path / ".git").mkdir()
        mock_pack = MagicMock()
        mock_pack.check_results = []
        mock_triage_handler = MagicMock()
        with patch("saturnday.governance.run_full_repo_review", return_value=(mock_pack, "/tmp/ev")):
            with patch.object(capability_registry, "get", return_value=mock_triage_handler):
                findings, mode = _scan_for_repair(tmp_path)
        mock_triage_handler.filter_findings.assert_not_called()
        assert mode == "repo"

    def test_triage_called_when_coder_config_provided(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When coder_config is provided, security_triage.filter_findings filters findings."""
        from saturnday.interactive import _scan_for_repair
        from saturnday.openclaw_scanner import Finding
        from saturnday._types import CoderConfig
        from saturnday import capability_registry
        from unittest.mock import MagicMock, patch
        (tmp_path / ".git").mkdir()
        fd_keep = {"file": "src/app.py", "line": 5, "pattern": "SELECT * FROM users"}
        fd_drop = {"file": "src/app.py", "line": 6, "pattern": "SELECT * FROM logs"}
        mock_cr = MagicMock()
        mock_cr.name = "sql_injection"
        mock_cr.severity = "error"
        mock_cr.findings = [fd_keep, fd_drop]
        mock_pack = MagicMock()
        mock_pack.check_results = [mock_cr]

        # Triage handler keeps only fd_keep
        mock_triage_handler = MagicMock()
        mock_triage_handler.filter_findings.side_effect = (
            lambda findings, repo_path, coder_config: [findings[0]]
        )

        cfg = CoderConfig(backend="claude-cli")
        with patch("saturnday.governance.run_full_repo_review", return_value=(mock_pack, "/tmp/ev")):
            with patch.object(capability_registry, "get", return_value=mock_triage_handler):
                findings, mode = _scan_for_repair(tmp_path, coder_config=cfg)
        assert mode == "repo"
        assert len(findings) == 1
        assert isinstance(findings[0], Finding)
        assert findings[0].file == "src/app.py"
        assert findings[0].line == 5

    def test_triage_failure_is_nonfatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When triage raises, scan completes normally with untriaged findings."""
        from saturnday.interactive import _scan_for_repair
        from saturnday.openclaw_scanner import Finding
        from saturnday._types import CoderConfig
        from saturnday import capability_registry
        from unittest.mock import MagicMock, patch
        (tmp_path / ".git").mkdir()
        mock_cr = MagicMock()
        mock_cr.name = "secrets"
        mock_cr.severity = "error"
        mock_cr.findings = [{"file": "main.py", "line": 1, "pattern": "SECRET=abc"}]
        mock_pack = MagicMock()
        mock_pack.check_results = [mock_cr]

        mock_triage_handler = MagicMock()
        mock_triage_handler.filter_findings.side_effect = RuntimeError("LLM unavailable")

        cfg = CoderConfig(backend="claude-cli")
        with patch("saturnday.governance.run_full_repo_review", return_value=(mock_pack, "/tmp/ev")):
            with patch.object(capability_registry, "get", return_value=mock_triage_handler):
                findings, mode = _scan_for_repair(tmp_path, coder_config=cfg)
        # Triage failure must not suppress findings — all originals returned
        assert mode == "repo"
        assert len(findings) == 1
        assert isinstance(findings[0], Finding)


# ---------------------------------------------------------------------------
# repl_loop
# ---------------------------------------------------------------------------

def _make_context(**overrides: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "saturnday_version": "1.0.0",
        "repo_path": "/repo",
        "git_initialized": False,
        "git_branch": "",
        "git_clean": True,
        "skill_md": False,
    }
    ctx.update(overrides)
    return ctx


class TestReplLoop:
    """Tests for repl_loop using monkeypatched detect_context, detect_backends, and input."""

    def _patch_detect(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            "saturnday.interactive.detect_context",
            lambda _: _make_context(repo_path=str(tmp_path)),
        )
        # Return None from select_backend so repl_loop falls through
        # to the built-in REPL (not the coder launcher)
        monkeypatch.setattr(
            "saturnday.interactive.select_backend",
            lambda session=None: None,
        )

    def test_quit_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        rc = repl_loop(tmp_path)
        assert rc == 0

    def test_exit_command_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["/exit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        rc = repl_loop(tmp_path)
        assert rc == 0

    def test_eoferror_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)

        def _raise(_: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise)
        rc = repl_loop(tmp_path)
        assert rc == 0

    def test_help_command_prints_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["/help", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        repl_loop(tmp_path)
        out = capsys.readouterr().out
        assert "/guard" in out
        assert "/quit" in out

    def test_empty_input_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty lines should not crash and loop should continue."""
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["", "   ", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        rc = repl_loop(tmp_path)
        assert rc == 0

    def test_unknown_intent_forwards_to_coder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Unknown intent forwards to coder for conversational response."""
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["hello world", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        called = []
        monkeypatch.setattr(
            "saturnday.interactive._handle_conversation",
            lambda msg, rp, sess: called.append(msg),
        )
        repl_loop(tmp_path)
        assert called
        assert called[0] == "hello world"

    def test_guard_intent_calls_guided_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["scan this skill", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        called = []
        monkeypatch.setattr(
            "saturnday.interactive.guided_guard",
            lambda rp, ctx, hint="": called.append(True) or 0,
        )
        repl_loop(tmp_path)
        assert called

    def test_plan_run_intent_calls_guided_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["build a calculator", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        called_with: list[str | None] = []
        monkeypatch.setattr(
            "saturnday.interactive.guided_run",
            lambda rp, goal=None, session=None: called_with.append(goal) or 0,
        )
        repl_loop(tmp_path)
        assert called_with
        assert called_with[0] is not None
        assert "calculator" in called_with[0]

    def test_repair_intent_calls_guided_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["fix the findings", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        called = []
        monkeypatch.setattr(
            "saturnday.interactive.guided_repair",
            lambda rp, session=None, **kwargs: called.append(True) or 0,
        )
        repl_loop(tmp_path)
        assert called

    def test_session_saved_on_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import repl_loop
        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        repl_loop(tmp_path)
        assert (tmp_path / ".saturnday" / "session.json").is_file()

    def test_prior_run_shown_if_evidence_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When session.last_evidence_dir points to a real directory, the REPL shows prior run."""
        from saturnday.interactive import Session, save_session, repl_loop
        evidence_dir = tmp_path / ".saturnday-proj-xyz"
        evidence_dir.mkdir()
        s = Session(
            repo_path=str(tmp_path),
            last_evidence_dir=str(evidence_dir),
            last_disposition="PASS",
        )
        save_session(s, tmp_path)

        self._patch_detect(monkeypatch, tmp_path)
        inputs = iter(["/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        repl_loop(tmp_path)
        out = capsys.readouterr().out
        assert "Prior run" in out
        assert ".saturnday-proj-xyz" in out


# ---------------------------------------------------------------------------
# show_plan_preview
# ---------------------------------------------------------------------------

class TestShowPlanPreview:
    def test_basic_output(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_plan_preview
        plan: dict[str, Any] = {
            "project_id": "my-proj",
            "tickets": [
                {"ticket_id": "T001", "goal": "Build the thing"},
                {"ticket_id": "T002", "goal": "Test the thing"},
            ],
            "phases": [{"phase_id": "P1", "name": "build"}],
            "definition_of_done": ["all_tickets_passed"],
        }
        show_plan_preview(plan)
        out = capsys.readouterr().out
        assert "my-proj" in out
        assert "T001" in out
        assert "T002" in out

    def test_more_than_ten_tickets(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_plan_preview
        tickets = [{"ticket_id": f"T{i:03d}", "goal": f"goal {i}"} for i in range(15)]
        plan: dict[str, Any] = {
            "project_id": "big-proj",
            "tickets": tickets,
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        show_plan_preview(plan)
        out = capsys.readouterr().out
        assert "and 5 more" in out


# ---------------------------------------------------------------------------
# print_ticket_progress
# ---------------------------------------------------------------------------

class TestPrintTicketProgress:
    @pytest.mark.parametrize("status,icon", [
        ("PASS", "\u2713"), ("FAIL", "\u2717"), ("SKIP", "\u2298"), ("RUNNING", "\u2192"), ("RETRY", "\u21bb"),
    ])
    def test_icons(self, capsys: pytest.CaptureFixture, status: str, icon: str) -> None:
        from saturnday.interactive import print_ticket_progress
        print_ticket_progress("T001", "build", 1, status)
        out = capsys.readouterr().out
        assert icon in out
        assert "T001" in out

    def test_attempt_zero_no_attempt_string(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import print_ticket_progress
        print_ticket_progress("T001", "", 0, "RUNNING")
        out = capsys.readouterr().out
        assert "attempt" not in out

    def test_attempt_shown_when_nonzero(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import print_ticket_progress
        print_ticket_progress("T002", "test", 2, "PASS")
        out = capsys.readouterr().out
        assert "attempt 2" in out


# ---------------------------------------------------------------------------
# show_run_summary
# ---------------------------------------------------------------------------

class TestShowRunSummary:
    def test_pass_result(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_run_summary
        from saturnday._types import RunResult
        real = RunResult(
            project_id="p",
            total_tickets=4,
            passed=3,
            failed=0,
            skipped=1,
            definition_of_done_met=True,
        )
        show_run_summary(real)
        out = capsys.readouterr().out
        assert "Passed: 3" in out
        assert "MET" in out

    def test_fail_result_shows_not_met(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_run_summary
        from saturnday._types import RunResult
        real = RunResult(
            project_id="p",
            total_tickets=2,
            passed=0,
            failed=2,
            skipped=0,
            definition_of_done_met=False,
            stop_reason="consecutive_failures",
        )
        show_run_summary(real)
        out = capsys.readouterr().out
        assert "NOT MET" in out
        assert "consecutive_failures" in out

    def test_non_runresult_is_noop(self, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_run_summary
        show_run_summary({"not": "a RunResult"})
        out = capsys.readouterr().out
        assert out == ""


# ---------------------------------------------------------------------------
# show_status
# ---------------------------------------------------------------------------

class TestShowStatus:
    def test_no_runs(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_status
        rc = show_status(tmp_path)
        assert rc == 0
        assert "No previous runs" in capsys.readouterr().out

    def test_with_summary(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_status
        evidence_dir = tmp_path / ".saturnday-my-proj"
        evidence_dir.mkdir()
        summary = {
            "ticket_results": [
                {"disposition": "PASS"},
                {"disposition": "FAIL"},
            ],
            "definition_of_done_met": False,
        }
        (evidence_dir / "run-summary.json").write_text(json.dumps(summary))
        rc = show_status(tmp_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Passed: 1" in out
        assert "Failed: 1" in out
        assert "NOT MET" in out

    def test_no_summary_file(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import show_status
        evidence_dir = tmp_path / ".saturnday-proj"
        evidence_dir.mkdir()
        # No run-summary.json
        rc = show_status(tmp_path)
        assert rc == 0
        assert "No run-summary.json" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# guided_guard
# ---------------------------------------------------------------------------

class TestGuidedGuard:
    def test_scan_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_guard
        ctx: dict[str, Any] = {"git_initialized": True}
        # Option 2 is Scan — needs SKILL.md to use skill scanner
        (tmp_path / "SKILL.md").write_text("# Test Skill\nA test.\n")
        monkeypatch.setattr("builtins.input", lambda _: "2")
        scan_result = _StubScanResult(disposition="PASS", findings=[])
        with patch("saturnday.guard.cloud_scanner.scan_skill", return_value=scan_result):
            rc = guided_guard(tmp_path, ctx)
        assert rc == 0
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_scan_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_guard
        ctx: dict[str, Any] = {"git_initialized": True}
        # Option 2 is Scan — needs SKILL.md to use skill scanner
        (tmp_path / "SKILL.md").write_text("# Test Skill\nA test.\n")
        monkeypatch.setattr("builtins.input", lambda _: "2")
        finding = _StubFinding(kind="shell_danger", message="unsafe call")
        scan_result = _StubScanResult(disposition="FAIL", findings=[finding])
        with patch("saturnday.guard.cloud_scanner.scan_skill", return_value=scan_result):
            rc = guided_guard(tmp_path, ctx)
        assert rc == 1

    def test_check_no_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_guard
        ctx: dict[str, Any] = {"git_initialized": False}
        # Option 3 is now Check
        monkeypatch.setattr("builtins.input", lambda _: "3")
        rc = guided_guard(tmp_path, ctx)
        assert rc == 1
        assert "not a git repo" in capsys.readouterr().out

    def test_eoferror_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import guided_guard

        def _raise(_: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise)
        ctx: dict[str, Any] = {"git_initialized": False}
        rc = guided_guard(tmp_path, ctx)
        assert rc == 0

    def test_check_with_git_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_guard
        ctx: dict[str, Any] = {"git_initialized": True}
        # Option 3 is Check; then diff range prompt
        inputs = iter(["3", "HEAD~1..HEAD"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        pack = _StubPack(disposition="PASS", check_results=[])
        evidence_path = tmp_path / ".saturnday-gov"
        with patch("saturnday.governance.run_governance_check", return_value=(pack, str(evidence_path))):
            rc = guided_guard(tmp_path, ctx)
        assert rc == 0
        assert "PASS" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# guided_run
# ---------------------------------------------------------------------------

class TestGuidedRun:
    def _make_git_repo(self, tmp_path: Path) -> None:
        """Create a minimal .git directory so guided_run skips the git-init prompt."""
        (tmp_path / ".git").mkdir()

    def test_no_goal_interactive_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no goal is pre-supplied and user enters empty string, returns 1."""
        from saturnday.interactive import guided_run
        self._make_git_repo(tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: "")
        with patch("saturnday.interactive.select_backend", return_value="codex-cli"):
            rc = guided_run(tmp_path)
        assert rc == 1

    def test_no_goal_preseeded_empty_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When goal='' is pre-supplied, returns 1 immediately without prompting."""
        from saturnday.interactive import guided_run
        self._make_git_repo(tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: "")
        with patch("saturnday.interactive.select_backend", return_value="codex-cli"):
            rc = guided_run(tmp_path, goal="")
        assert rc == 1

    def test_goal_parameter_skips_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When goal is pre-supplied, the goal-input prompt must not appear."""
        from saturnday.interactive import guided_run
        self._make_git_repo(tmp_path)
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "test-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        inputs = iter(["n"])  # only the confirm prompt
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path):
            rc = guided_run(tmp_path, goal="build a thing")
        assert rc == 0
        out = capsys.readouterr().out
        assert "Aborted" in out

    def test_abort_at_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_run
        self._make_git_repo(tmp_path)
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "test-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        inputs = iter(["my goal", "n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path):
            rc = guided_run(tmp_path)
        assert rc == 0
        assert "Aborted" in capsys.readouterr().out

    def test_successful_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult
        self._make_git_repo(tmp_path)
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "run-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(project_id="run-proj", total_tickets=1, passed=1)
        inputs = iter(["my goal", "y"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            rc = guided_run(tmp_path)
        assert rc == 0

    def test_successful_run_with_goal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """guided_run with goal= skips the goal prompt."""
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult
        self._make_git_repo(tmp_path)
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "run-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(project_id="run-proj", total_tickets=1, passed=1)
        inputs = iter(["y"])  # only the confirm prompt
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            rc = guided_run(tmp_path, goal="build the thing")
        assert rc == 0

    def test_failed_run_resume_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult
        self._make_git_repo(tmp_path)
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "run-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        fail_result = RunResult(project_id="run-proj", total_tickets=1, failed=1)
        pass_result = RunResult(project_id="run-proj", total_tickets=1, passed=1)
        inputs = iter(["my goal", "y", "y"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=fail_result), \
             patch("saturnday.run.resume.rerun_failed", return_value=pass_result):
            rc = guided_run(tmp_path)
        assert rc == 0

    def test_plan_generation_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_run
        self._make_git_repo(tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: "my goal")
        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", side_effect=ValueError("bad plan")):
            rc = guided_run(tmp_path)
        assert rc == 1
        assert "Plan generation failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# guided_repair
# ---------------------------------------------------------------------------

class TestGuidedRepair:
    def test_no_findings_returns_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_repair
        scan_result = _StubScanResult(disposition="PASS", findings=[])
        with patch("saturnday.guard.cloud_scanner.scan_skill", return_value=scan_result):
            rc = guided_repair(tmp_path, session=None)
        assert rc == 0
        assert "No findings" in capsys.readouterr().out

    def test_abort_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_repair
        findings = [_StubFinding()]
        scan_result = _StubScanResult(disposition="FAIL", findings=findings)
        # generate_repair_tickets is called before the confirm prompt now
        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"
        inputs = iter(["n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        with patch("saturnday.guard.cloud_scanner.scan_skill", return_value=scan_result), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"):
            rc = guided_repair(tmp_path, session=None)
        assert rc == 0
        assert "Aborted" in capsys.readouterr().out

    def test_repair_runs_and_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import guided_repair
        findings = [_StubFinding()]
        pre_scan = _StubScanResult(disposition="FAIL", findings=findings)
        post_scan = _StubScanResult(disposition="PASS", findings=[])
        repair_result = _StubRepairResult(fixed=1, failed=0)
        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"
        scan_calls: list[Any] = []

        def _scan(rp: Any) -> Any:
            scan_calls.append(rp)
            return pre_scan if len(scan_calls) == 1 else post_scan

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("saturnday.guard.cloud_scanner.scan_skill", side_effect=_scan), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.repair.repair_runner.run_repair_batch", return_value=repair_result), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.coder_adapter.call_coder", return_value="fixed code"):
            rc = guided_repair(tmp_path, session=None)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Repaired:" in out

    def test_repair_with_failures_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.interactive import guided_repair
        findings = [_StubFinding()]
        pre_scan = _StubScanResult(disposition="FAIL", findings=findings)
        post_scan = _StubScanResult(disposition="FAIL", findings=findings)
        repair_result = _StubRepairResult(fixed=0, failed=1)
        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"
        scan_calls: list[Any] = []

        def _scan(rp: Any) -> Any:
            scan_calls.append(rp)
            return pre_scan if len(scan_calls) == 1 else post_scan

        monkeypatch.setattr("builtins.input", lambda _: "y")
        with patch("saturnday.guard.cloud_scanner.scan_skill", side_effect=_scan), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.repair.repair_runner.run_repair_batch", return_value=repair_result), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.coder_adapter.call_coder", return_value=""):
            rc = guided_repair(tmp_path, session=None)
        assert rc == 1


# ---------------------------------------------------------------------------
# ticket_runner.run_plan — progress_callback integration
# ---------------------------------------------------------------------------

class TestRunPlanProgressCallback:
    def test_progress_callback_called(self, tmp_path: Path) -> None:
        """Verify the progress_callback parameter is accepted without error."""
        from saturnday.ticket_runner import run_plan
        import inspect
        sig = inspect.signature(run_plan)
        assert "progress_callback" in sig.parameters

    def test_progress_callback_default_is_none(self) -> None:
        from saturnday.ticket_runner import run_plan
        import inspect
        sig = inspect.signature(run_plan)
        assert sig.parameters["progress_callback"].default is None


# ---------------------------------------------------------------------------
# CLI start command
# ---------------------------------------------------------------------------

class TestCliStart:
    def test_start_help_exits_zero(self) -> None:
        from saturnday.cli import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["start", "--help"])
        assert exc_info.value.code == 0

    def test_start_in_parser(self) -> None:
        from saturnday.cli import build_parser
        parser = build_parser()
        args = parser.parse_args(["start", "--repo", "."])
        assert args.command == "start"

    def test_start_non_tty_returns_one(self) -> None:
        from saturnday.cli import _cmd_start
        args = MagicMock()
        args.repo = "."
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            rc = _cmd_start(args)
        assert rc == 1

    def test_start_dispatched_via_main(self, tmp_path: Path) -> None:
        """main() routes 'start' to _cmd_start without error."""
        from saturnday.cli import main
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            rc = main(["start", "--repo", str(tmp_path)])
        assert rc == 1  # non-tty returns 1 — verifies routing

    def test_start_tty_keyboard_interrupt(self, tmp_path: Path) -> None:
        from saturnday.cli import _cmd_start
        args = MagicMock()
        args.repo = str(tmp_path)

        def _raise_ki(*a: Any, **kw: Any) -> None:
            raise KeyboardInterrupt

        with patch("sys.stdin") as mock_stdin, \
             patch("saturnday.interactive.repl_loop", side_effect=_raise_ki):
            mock_stdin.isatty.return_value = True
            rc = _cmd_start(args)
        assert rc == 130


# ---------------------------------------------------------------------------
# FU-06: First-user flow hardening tests
# ---------------------------------------------------------------------------

class TestFU05SessionParam:
    """FU-05: guided_run session parameter."""

    def test_guided_run_receives_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """guided_run accepts the session keyword argument without error."""
        import inspect
        from saturnday.interactive import guided_run, Session
        sig = inspect.signature(guided_run)
        assert "session" in sig.parameters
        assert sig.parameters["session"].default is None

    def test_guided_run_updates_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a successful run, session fields are updated and persisted."""
        from saturnday.interactive import guided_run, Session, load_session
        from saturnday._types import RunResult

        (tmp_path / ".git").mkdir()
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "sess-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(
            project_id="sess-proj",
            total_tickets=1,
            passed=1,
            definition_of_done_met=True,
        )
        session = Session(repo_path=str(tmp_path))
        inputs = iter(["y"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            guided_run(tmp_path, goal="build the thing", session=session)

        # Session should be updated in memory
        assert session.last_plan_path is not None
        assert session.last_evidence_dir is not None
        assert session.last_disposition == "MET"

        # And also persisted to disk
        loaded = load_session(tmp_path)
        assert loaded is not None
        assert loaded.last_disposition == "MET"


class TestFU03HelpIntent:
    """FU-03: Bare 'help' intent classification."""

    @pytest.mark.parametrize("line", ["help", "?", "h"])
    def test_classify_intent_help(self, line: str) -> None:
        """'help', '?', and 'h' all classify as 'help' intent."""
        from saturnday.interactive import classify_intent
        assert classify_intent(line) == "help"

    def test_help_intent_calls_print_help(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Typing 'help' in the REPL triggers _print_help output."""
        from saturnday.interactive import repl_loop

        monkeypatch.setattr(
            "saturnday.interactive.detect_context",
            lambda _: {
                "saturnday_version": "1.0.0",
                "repo_path": str(tmp_path),
                "git_initialized": False,
                "git_branch": "",
                "git_clean": True,
                "skill_md": False,
            },
        )
        monkeypatch.setattr("saturnday.interactive.select_backend", lambda session=None: None)
        inputs = iter(["help", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        repl_loop(tmp_path)
        out = capsys.readouterr().out
        assert "/guard" in out


class TestFU01GitAutoInit:
    """FU-01: Git auto-init in guided_run."""

    def test_git_auto_init_prompt_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When user accepts git init, subprocess is called and run continues."""
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult

        assert not (tmp_path / ".git").is_dir()

        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "init-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(project_id="init-proj", total_tickets=1, passed=1)
        sp_calls: list[list[str]] = []

        def _fake_sp_run(cmd: list[str], **kw: Any) -> MagicMock:
            sp_calls.append(cmd)
            # After git init, create .git so the rest of the code sees it
            if "init" in cmd and str(tmp_path) in cmd:
                (tmp_path / ".git").mkdir(exist_ok=True)
            return MagicMock(returncode=0)

        # Inputs: git init prompt "y" then proceed "y"
        inputs = iter(["y", "y"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("subprocess.run", side_effect=_fake_sp_run), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            rc = guided_run(tmp_path, goal="build the thing")

        assert any("init" in " ".join(cmd) for cmd in sp_calls), (
            "Expected git init to be called"
        )
        out = capsys.readouterr().out
        assert "Git initialized" in out

    def test_git_auto_init_declined(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When user declines git init, guided_run returns 0 without running git."""
        from saturnday.interactive import guided_run

        assert not (tmp_path / ".git").is_dir()

        sp_calls: list[list[str]] = []

        def _fake_sp_run(cmd: list[str], **kw: Any) -> MagicMock:
            sp_calls.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr("builtins.input", lambda _: "n")

        with patch("subprocess.run", side_effect=_fake_sp_run):
            rc = guided_run(tmp_path, goal="build the thing")

        assert rc == 0
        assert not any("init" in " ".join(cmd) for cmd in sp_calls), (
            "git init must NOT run when user declines"
        )
        out = capsys.readouterr().out
        assert "Aborted" in out


class TestFU02PostRunNextSteps:
    """FU-02: Post-run next steps shown after guided_run."""

    def test_post_run_next_steps_shown_on_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """After a fully-passing run, 'What to do next' is printed."""
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult

        (tmp_path / ".git").mkdir()
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "next-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(
            project_id="next-proj",
            total_tickets=1,
            passed=1,
            definition_of_done_met=True,
        )
        monkeypatch.setattr("builtins.input", lambda _: "y")

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            guided_run(tmp_path, goal="build the thing")

        out = capsys.readouterr().out
        assert "What to do next" in out
        assert "scan this skill" in out

    def test_post_run_next_steps_shown_on_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """After a run with failures, 'fix the findings' appears in next steps."""
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult

        (tmp_path / ".git").mkdir()
        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "fail-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(
            project_id="fail-proj",
            total_tickets=1,
            failed=1,
            definition_of_done_met=False,
        )
        # "y" for proceed, "n" for resume prompt
        inputs = iter(["y", "n"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            guided_run(tmp_path, goal="build the thing")

        out = capsys.readouterr().out
        assert "What to do next" in out
        assert "fix the findings" in out


class TestFU04Resume:
    """FU-04: /resume wires to rerun_failed using session state."""

    def _patch_detect(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            "saturnday.interactive.detect_context",
            lambda _: {
                "saturnday_version": "1.0.0",
                "repo_path": str(tmp_path),
                "git_initialized": True,
                "git_branch": "main",
                "git_clean": True,
                "skill_md": False,
            },
        )
        # Note: do NOT mock select_backend here — the resume test
        # patches it to "codex-cli" in its own with-block.

    def test_resume_with_session_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When session has last_evidence_dir + last_plan_path, /resume calls rerun_failed."""
        from saturnday.interactive import Session, save_session, repl_loop
        from saturnday._types import RunResult

        # Create on-disk evidence dir and plan file so path checks pass
        evidence_dir = tmp_path / ".saturnday-proj"
        evidence_dir.mkdir()
        plan_path = tmp_path / "plan.json"
        plan_path.write_text('{"project_id": "proj", "tickets": [], "phases": [], "definition_of_done": []}')

        session = Session(
            repo_path=str(tmp_path),
            last_evidence_dir=str(evidence_dir),
            last_plan_path=str(plan_path),
            last_disposition="NOT MET",
        )
        save_session(session, tmp_path)

        self._patch_detect(monkeypatch, tmp_path)

        resume_result = RunResult(project_id="proj", total_tickets=1, passed=1)
        rerun_called: list[bool] = []

        def _fake_rerun_failed(**kwargs: Any) -> RunResult:
            rerun_called.append(True)
            return resume_result

        inputs = iter(["/resume", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))

        # First call (repl_loop top-level) returns None -> fallback REPL
        # Second call (/resume handler) returns "codex-cli"
        backend_calls = iter([None, "codex-cli"])
        with patch("saturnday.interactive.select_backend", side_effect=lambda session=None: next(backend_calls)), \
             patch("saturnday.run.resume.rerun_failed", side_effect=_fake_rerun_failed):
            repl_loop(tmp_path)

        assert rerun_called, "rerun_failed should have been called by /resume"
        out = capsys.readouterr().out
        assert "Resuming from" in out

    def test_resume_no_session_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When session has no prior run data, /resume prints a helpful message."""
        from saturnday.interactive import repl_loop

        self._patch_detect(monkeypatch, tmp_path)
        monkeypatch.setattr("saturnday.interactive.select_backend", lambda session=None: None)
        inputs = iter(["/resume", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        repl_loop(tmp_path)
        out = capsys.readouterr().out
        assert "No prior run" in out


# ---------------------------------------------------------------------------
# Fix 1c: Intent classifier — review/inspect keywords and publish intent
# ---------------------------------------------------------------------------

class TestClassifyIntentReview:
    """review/inspect/analyse/analyze/examine all map to guard."""

    @pytest.mark.parametrize("phrase", [
        "review this repo",
        "inspect the repo",
        "analyse the code",
        "analyze this skill",
        "examine the changes",
    ])
    def test_classify_intent_review_guard(self, phrase: str) -> None:
        from saturnday.interactive import classify_intent
        assert classify_intent(phrase) == "guard"

    def test_classify_intent_inspect(self) -> None:
        from saturnday.interactive import classify_intent
        assert classify_intent("inspect my project") == "guard"


class TestClassifyIntentPublish:
    """publish/release/ship/deploy map to publish."""

    @pytest.mark.parametrize("phrase", [
        "publish this",
        "release the skill",
        "ship it",
        "deploy to production",
    ])
    def test_classify_intent_publish(self, phrase: str) -> None:
        from saturnday.interactive import classify_intent
        assert classify_intent(phrase) == "publish"

    def test_publish_intent_triggers_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """'publish this' in the REPL triggers Guard preflight output."""
        from saturnday.interactive import repl_loop

        monkeypatch.setattr(
            "saturnday.interactive.detect_context",
            lambda _: {
                "saturnday_version": "1.0.0",
                "repo_path": str(tmp_path),
                "git_initialized": False,
                "git_branch": "",
                "git_clean": True,
                "skill_md": False,
            },
        )

        guard_calls: list[bool] = []

        def _fake_guided_guard(rp: Any, ctx: Any, hint: str = "") -> int:
            guard_calls.append(True)
            return 0

        monkeypatch.setattr("saturnday.interactive.guided_guard", _fake_guided_guard)
        monkeypatch.setattr("saturnday.interactive.select_backend", lambda session=None: None)

        inputs = iter(["publish this skill", "/quit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        repl_loop(tmp_path)
        assert guard_calls, "guided_guard should be called for publish intent"
        out = capsys.readouterr().out
        assert "preflight" in out.lower() or "Publish" in out


# ---------------------------------------------------------------------------
# Fix 1b: guided_guard three-option menu
# ---------------------------------------------------------------------------

class TestGuidedGuardThreeOptions:
    """guided_guard now shows Review, Scan, Check options."""

    def test_guided_guard_has_review_option(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """guided_guard prints all three options including Review."""
        from saturnday.interactive import guided_guard

        # Return without selecting to just see the menu
        def _eof(_: str) -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        ctx: dict[str, Any] = {"git_initialized": False}
        guided_guard(tmp_path, ctx)
        out = capsys.readouterr().out
        assert "[1]" in out
        assert "[2]" in out
        assert "[3]" in out
        assert "Review" in out
        assert "Scan" in out
        assert "Check" in out

    def test_guided_guard_option2_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Option 2 in the new menu triggers a skill scan when SKILL.md exists."""
        from saturnday.interactive import guided_guard

        (tmp_path / "SKILL.md").write_text("# Test Skill\nA test.\n")
        monkeypatch.setattr("builtins.input", lambda _: "2")
        ctx: dict[str, Any] = {"git_initialized": True}
        scan_result = _StubScanResult(disposition="PASS", findings=[])
        with patch("saturnday.guard.cloud_scanner.scan_skill", return_value=scan_result):
            rc = guided_guard(tmp_path, ctx)
        assert rc == 0
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_guided_guard_option3_check_no_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Option 3 without git repo returns error."""
        from saturnday.interactive import guided_guard

        monkeypatch.setattr("builtins.input", lambda _: "3")
        ctx: dict[str, Any] = {"git_initialized": False}
        rc = guided_guard(tmp_path, ctx)
        assert rc == 1
        assert "not a git repo" in capsys.readouterr().out

    def test_guided_guard_option1_review_no_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Option 1 (review) without git returns error."""
        from saturnday.interactive import guided_guard

        monkeypatch.setattr("builtins.input", lambda _: "1")
        ctx: dict[str, Any] = {"git_initialized": False}
        rc = guided_guard(tmp_path, ctx)
        assert rc == 1
        assert "not a git repo" in capsys.readouterr().out

    def test_guided_guard_option1_review_with_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Option 1 (review) with git calls run_full_repo_review."""
        from saturnday.interactive import guided_guard

        monkeypatch.setattr("builtins.input", lambda _: "1")
        ctx: dict[str, Any] = {"git_initialized": True}

        stub_pack = _StubPack(disposition="PASS", check_results=[])
        evidence_path = tmp_path / ".saturnday-review"
        with patch("saturnday.governance.run_full_repo_review", return_value=(stub_pack, str(evidence_path))):
            rc = guided_guard(tmp_path, ctx)
        assert rc == 0
        out = capsys.readouterr().out
        assert "PASS" in out


# ---------------------------------------------------------------------------
# Fix 2: guided_repair plan-first, re-scan, next-steps
# ---------------------------------------------------------------------------

class TestGuidedRepairPlanFirst:
    """New guided_repair shows plan before executing and re-scans after."""

    def test_guided_repair_shows_plan_before_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Repair plan (ticket IDs / kinds) is shown before the confirm prompt."""
        from saturnday.interactive import guided_repair

        finding = _StubFinding(kind="shell_danger", message="unsafe call", file="skill.py")
        scan_result = _StubScanResult(disposition="FAIL", findings=[finding])

        # Ticket stub with expected attributes
        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"

        repair_result = _StubRepairResult(fixed=1, failed=0)

        # First scan returns findings, second (post-repair) scan returns clean
        post_scan = _StubScanResult(disposition="PASS", findings=[])
        scan_calls: list[Any] = []

        def _scan(rp: Any) -> Any:
            scan_calls.append(rp)
            if len(scan_calls) == 1:
                return scan_result
            return post_scan

        inputs_iter = iter(["y"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs_iter))

        with patch("saturnday.guard.cloud_scanner.scan_skill", side_effect=_scan), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.repair.repair_runner.run_repair_batch", return_value=repair_result), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.coder_adapter.call_coder", return_value="fixed code"):
            rc = guided_repair(tmp_path, session=None)

        assert rc == 0
        out = capsys.readouterr().out
        # Plan must appear before summary (ticket ID visible in plan section)
        assert "R001" in out

    def test_guided_repair_rescans_after_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """After repairs, a re-scan is performed and before/after counts shown."""
        from saturnday.interactive import guided_repair

        finding = _StubFinding()
        pre_scan = _StubScanResult(disposition="FAIL", findings=[finding])
        post_scan = _StubScanResult(disposition="PASS", findings=[])

        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"

        repair_result = _StubRepairResult(fixed=1, failed=0)
        scan_calls: list[Any] = []

        def _scan(rp: Any) -> Any:
            scan_calls.append(rp)
            return pre_scan if len(scan_calls) == 1 else post_scan

        monkeypatch.setattr("builtins.input", lambda _: "y")

        with patch("saturnday.guard.cloud_scanner.scan_skill", side_effect=_scan), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.repair.repair_runner.run_repair_batch", return_value=repair_result), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.coder_adapter.call_coder", return_value="fixed code"):
            rc = guided_repair(tmp_path, session=None)

        assert rc == 0
        # Two scans must have been issued
        assert len(scan_calls) == 2
        out = capsys.readouterr().out
        assert "Re-scanning" in out
        assert "Before:" in out
        assert "After:" in out
        assert "All findings resolved" in out

    def test_guided_repair_next_steps_remaining_findings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When findings remain after repair, 'fix the findings' appears in next steps."""
        from saturnday.interactive import guided_repair

        finding = _StubFinding()
        pre_scan = _StubScanResult(disposition="FAIL", findings=[finding, finding])
        post_scan = _StubScanResult(disposition="FAIL", findings=[finding])

        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"

        repair_result = _StubRepairResult(fixed=1, partial=0, failed=0)
        scan_calls: list[Any] = []

        def _scan(rp: Any) -> Any:
            scan_calls.append(rp)
            return pre_scan if len(scan_calls) == 1 else post_scan

        monkeypatch.setattr("builtins.input", lambda _: "y")

        with patch("saturnday.guard.cloud_scanner.scan_skill", side_effect=_scan), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.repair.repair_runner.run_repair_batch", return_value=repair_result), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.coder_adapter.call_coder", return_value="fixed"):
            guided_repair(tmp_path, session=None)

        out = capsys.readouterr().out
        assert "fix the findings" in out

    def test_guided_repair_next_steps_all_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When all findings resolved, 'verify clean state' guidance shown."""
        from saturnday.interactive import guided_repair

        finding = _StubFinding()
        pre_scan = _StubScanResult(disposition="FAIL", findings=[finding])
        post_scan = _StubScanResult(disposition="PASS", findings=[])

        fake_ticket = MagicMock()
        fake_ticket.ticket_id = "R001"
        fake_ticket.kind = "shell_danger"
        fake_ticket.file_path = "skill.py"

        repair_result = _StubRepairResult(fixed=1, failed=0)
        scan_calls: list[Any] = []

        def _scan(rp: Any) -> Any:
            scan_calls.append(rp)
            return pre_scan if len(scan_calls) == 1 else post_scan

        monkeypatch.setattr("builtins.input", lambda _: "y")

        with patch("saturnday.guard.cloud_scanner.scan_skill", side_effect=_scan), \
             patch("saturnday.repair.repair_tickets.generate_repair_tickets", return_value=[fake_ticket]), \
             patch("saturnday.repair.repair_runner.run_repair_batch", return_value=repair_result), \
             patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.coder_adapter.call_coder", return_value="fixed"):
            guided_repair(tmp_path, session=None)

        out = capsys.readouterr().out
        assert "verify clean state" in out or "scan this skill" in out


# ---------------------------------------------------------------------------
# Fix 4a: _humanize_finding_kind
# ---------------------------------------------------------------------------

class TestHumanizeFindingKind:
    """_humanize_finding_kind returns readable labels."""

    @pytest.mark.parametrize("kind,expected", [
        ("shell_danger", "Dangerous shell execution"),
        ("credential_leak", "Exposed credentials"),
        ("command_interpolation", "Command injection risk"),
        ("broad_filesystem", "Risky filesystem operations"),
        ("missing_approval_gate", "Missing confirmation for destructive actions"),
    ])
    def test_known_kinds(self, kind: str, expected: str) -> None:
        from saturnday.interactive import _humanize_finding_kind
        assert _humanize_finding_kind(kind) == expected

    def test_unknown_kind_falls_back_to_title_case(self) -> None:
        from saturnday.interactive import _humanize_finding_kind
        result = _humanize_finding_kind("some_new_issue")
        assert "Some" in result or "some" in result.lower()
        assert "_" not in result  # underscores must be removed

    def test_empty_string_does_not_raise(self) -> None:
        from saturnday.interactive import _humanize_finding_kind
        result = _humanize_finding_kind("")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Fix 3a: Auth retry prompt in guided_run
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason="validate_auth retry flow was removed from guided_run; "
           "these tests require a future re-implementation"
)
class TestAuthRetryPrompt:
    """guided_run gives user a retry prompt when auth is invalid.

    NOTE: The validate_auth retry flow is no longer present in guided_run.
    Backend readiness is now handled inside select_backend via _check_backend_ready.
    These tests are skipped until the auth retry feature is re-added.
    """

    def test_auth_retry_prompt_then_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """User is shown a retry prompt; on second check auth becomes valid."""
        from saturnday.interactive import guided_run
        from saturnday._types import RunResult

        (tmp_path / ".git").mkdir()

        plan_path = tmp_path / "plan.json"
        plan_data = {
            "project_id": "auth-proj",
            "tickets": [{"ticket_id": "T001", "goal": "do stuff", "acceptance_criteria": ["done"]}],
            "phases": [],
            "definition_of_done": ["all_tickets_passed"],
        }
        plan_path.write_text(json.dumps(plan_data))

        run_result = RunResult(project_id="auth-proj", total_tickets=1, passed=1)

        auth_invalid = MagicMock()
        auth_invalid.valid = False
        auth_invalid.reason = "not logged in"

        auth_valid = MagicMock()
        auth_valid.valid = True
        auth_valid.reason = "ready"

        auth_calls: list[Any] = []

        def _validate(cfg: Any) -> Any:
            auth_calls.append(cfg)
            if len(auth_calls) == 1:
                return auth_invalid
            return auth_valid

        # Inputs: Enter for retry prompt, "y" for plan confirm
        inputs_iter = iter(["", "y"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs_iter))

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.shared.backend_auth.validate_auth", side_effect=_validate), \
             patch("saturnday.run.planner.generate_plan", return_value=plan_path), \
             patch("saturnday.ticket_runner.run_plan", return_value=run_result):
            rc = guided_run(tmp_path, goal="build the thing")

        assert rc == 0
        assert len(auth_calls) == 2
        out = capsys.readouterr().out
        assert "not ready" in out
        assert "Backend ready" in out

    def test_auth_retry_prompt_still_invalid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """When auth is still invalid after retry, guided_run returns 1."""
        from saturnday.interactive import guided_run

        (tmp_path / ".git").mkdir()

        auth_invalid = MagicMock()
        auth_invalid.valid = False
        auth_invalid.reason = "still not logged in"

        # Enter for retry then still fails
        monkeypatch.setattr("builtins.input", lambda _: "")

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.shared.backend_auth.validate_auth", return_value=auth_invalid):
            rc = guided_run(tmp_path, goal="build the thing")

        assert rc == 1
        out = capsys.readouterr().out
        assert "Still not ready" in out

    def test_auth_retry_keyboard_interrupt_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl+C at the retry prompt returns 1 cleanly."""
        from saturnday.interactive import guided_run

        (tmp_path / ".git").mkdir()

        auth_invalid = MagicMock()
        auth_invalid.valid = False
        auth_invalid.reason = "not logged in"

        call_count = 0

        def _input(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:  # first real prompt after backend selection is retry
                raise KeyboardInterrupt
            return ""

        monkeypatch.setattr("builtins.input", _input)

        with patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
             patch("saturnday.shared.backend_auth.validate_auth", return_value=auth_invalid):
            rc = guided_run(tmp_path, goal="build the thing")

        assert rc == 1


# ---------------------------------------------------------------------------
# Fix 4a: _show_role_pass_results — jargon-free labels
# ---------------------------------------------------------------------------

class TestShowRolePassResults:
    """_show_role_pass_results uses clean labels instead of internal jargon."""

    def _write_role_pass(self, evidence_dir: Path, filename: str, classification: str) -> None:
        data = {"success": True, "output": classification}
        (evidence_dir / filename).write_text(json.dumps(data), encoding="utf-8")

    def test_dod_met_label(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import _show_role_pass_results
        with patch("saturnday.role_modes.extract_classification", return_value="DOD_MET"):
            self._write_role_pass(tmp_path, "role-pass-dod.json", "DOD_MET")
            _show_role_pass_results(tmp_path)
        out = capsys.readouterr().out
        assert "completion check" in out.lower()
        assert "all acceptance criteria met" in out.lower()
        assert "definition of done" not in out.lower()

    def test_evidence_sufficient_label(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import _show_role_pass_results
        with patch("saturnday.role_modes.extract_classification", return_value="SUFFICIENT"):
            self._write_role_pass(tmp_path, "role-pass-evidence-gate.json", "SUFFICIENT")
            _show_role_pass_results(tmp_path)
        out = capsys.readouterr().out
        assert "evidence review" in out.lower()
        assert "complete" in out.lower()
        assert "evidence gate" not in out.lower()

    def test_dod_not_met_label(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import _show_role_pass_results
        with patch("saturnday.role_modes.extract_classification", return_value="DOD_NOT_MET"):
            self._write_role_pass(tmp_path, "role-pass-dod.json", "DOD_NOT_MET")
            _show_role_pass_results(tmp_path)
        out = capsys.readouterr().out
        assert "criteria not met" in out.lower()

    def test_evidence_insufficient_label(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import _show_role_pass_results
        with patch("saturnday.role_modes.extract_classification", return_value="INSUFFICIENT"):
            self._write_role_pass(tmp_path, "role-pass-evidence-gate.json", "INSUFFICIENT")
            _show_role_pass_results(tmp_path)
        out = capsys.readouterr().out
        assert "incomplete" in out.lower()


# ---------------------------------------------------------------------------
# _install_codex_wrappers
# ---------------------------------------------------------------------------

class TestInstallCodexWrappers:
    """Unit tests for _install_codex_wrappers."""

    def test_creates_bin_dir(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        assert (tmp_path / "bin").is_dir()

    def test_creates_all_seven_wrappers(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        expected = {
            "sat-scan", "sat-scan-staged", "sat-repair",
            "sat-repair-skill", "sat-plan", "sat-run", "sat-commit",
        }
        created = {p.name for p in (tmp_path / "bin").iterdir()}
        assert expected == created

    def test_wrappers_are_executable(self, tmp_path: Path) -> None:
        import stat
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        for script in (tmp_path / "bin").iterdir():
            mode = script.stat().st_mode
            assert mode & stat.S_IXUSR, f"{script.name} not user-executable"

    def test_wrappers_have_shebang(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        for script in (tmp_path / "bin").iterdir():
            content = script.read_text(encoding="utf-8")
            assert content.startswith("#!/bin/sh"), f"{script.name} missing shebang"

    def test_sat_scan_runs_full_governance(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        content = (tmp_path / "bin" / "sat-scan").read_text(encoding="utf-8")
        assert "saturnday governance --repo . --full" in content

    def test_sat_scan_staged_runs_staged_governance(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        content = (tmp_path / "bin" / "sat-scan-staged").read_text(encoding="utf-8")
        assert "saturnday governance --repo . --staged" in content

    def test_sat_repair_uses_codex_backend(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        content = (tmp_path / "bin" / "sat-repair").read_text(encoding="utf-8")
        assert "--backend codex-cli" in content

    def test_sat_commit_scans_before_committing(self, tmp_path: Path) -> None:
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        content = (tmp_path / "bin" / "sat-commit").read_text(encoding="utf-8")
        assert "saturnday governance --repo . --staged" in content
        assert "git commit" in content

    def test_does_not_overwrite_existing_wrapper(self, tmp_path: Path) -> None:
        """Pre-existing wrappers must be preserved (idempotency)."""
        from saturnday.interactive import _install_codex_wrappers
        (tmp_path / "bin").mkdir()
        custom = "#!/bin/sh\necho custom\n"
        (tmp_path / "bin" / "sat-scan").write_text(custom, encoding="utf-8")
        _install_codex_wrappers(tmp_path)
        assert (tmp_path / "bin" / "sat-scan").read_text(encoding="utf-8") == custom

    def test_idempotent_second_call(self, tmp_path: Path) -> None:
        """Calling twice must not raise and must leave scripts intact."""
        from saturnday.interactive import _install_codex_wrappers
        _install_codex_wrappers(tmp_path)
        first_content = (tmp_path / "bin" / "sat-scan").read_text(encoding="utf-8")
        _install_codex_wrappers(tmp_path)
        assert (tmp_path / "bin" / "sat-scan").read_text(encoding="utf-8") == first_content


# ---------------------------------------------------------------------------
# _launch_coder: codex-cli wrapper installation and AGENTS.md content
# ---------------------------------------------------------------------------

class TestLaunchCoderCodexWrappers:
    """Integration tests verifying _launch_coder installs wrappers and updates AGENTS.md."""

    _CONTEXT: dict = {
        "git_initialized": False,
        "git_clean": True,
        "git_branch": "",
        "saturnday_version": "test",
    }

    def test_codex_installs_wrappers(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        from saturnday.interactive import _launch_coder
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _launch_coder("codex-cli", tmp_path, self._CONTEXT)
        assert (tmp_path / "bin").is_dir()
        assert (tmp_path / "bin" / "sat-scan").is_file()

    def test_codex_prints_wrappers_installed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import _launch_coder
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _launch_coder("codex-cli", tmp_path, self._CONTEXT)
        out = capsys.readouterr().out
        assert "Command wrappers installed in bin/" in out

    def test_claude_cli_does_not_install_wrappers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.interactive import _launch_coder
        with patch("subprocess.run") as mock_run, \
             patch("saturnday.interactive._install_codex_wrappers") as mock_install:
            mock_run.return_value = MagicMock(returncode=0)
            _launch_coder("claude-cli", tmp_path, self._CONTEXT)
        mock_install.assert_not_called()

    def test_codex_agents_md_contains_wrapper_section(
        self, tmp_path: Path
    ) -> None:
        from saturnday.interactive import _launch_coder
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _launch_coder("codex-cli", tmp_path, self._CONTEXT)
        agents_md = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "Governed command wrappers" in agents_md
        assert "./bin/sat-scan" in agents_md
        assert "./bin/sat-commit" in agents_md
        assert "Prefer these wrappers" in agents_md

    def test_claude_cli_claude_md_no_wrapper_section(
        self, tmp_path: Path
    ) -> None:
        from saturnday.interactive import _launch_coder
        with patch("subprocess.run") as mock_run, \
             patch("saturnday.hooks.install_claude_hooks", return_value=None):
            mock_run.return_value = MagicMock(returncode=0)
            _launch_coder("claude-cli", tmp_path, self._CONTEXT)
        claude_md = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Governed command wrappers" not in claude_md

    def test_wrappers_install_exception_is_swallowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """If wrapper installation fails, _launch_coder must not raise."""
        from saturnday.interactive import _launch_coder
        with patch("subprocess.run") as mock_run, \
             patch("saturnday.interactive._install_codex_wrappers", side_effect=OSError("disk full")):
            mock_run.return_value = MagicMock(returncode=0)
            rc = _launch_coder("codex-cli", tmp_path, self._CONTEXT)
        # Should still launch (returncode from subprocess mock)
        assert rc == 0
