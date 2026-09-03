"""Fix 37 — git auto-init restored on launcher-backend start path.

Proves that:
1. claude-cli start in a non-git folder prompts for git init.
2. Accepting the prompt calls git init + baseline commit before _launch_coder.
3. Declining aborts cleanly: _launch_coder not called, no launcher side effects.
4. Existing git repo skips the prompt and launches normally.
5. openclaude and cursor-cli launcher paths also get the check.
6. codex-cli path is unchanged (still routes via _fallback_repl).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(git_initialized: bool = False, **overrides: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "repo_path": "/tmp/test",
        "git_initialized": git_initialized,
        "git_branch": "",
        "git_clean": True,
        "skill_md": False,
        "saturnday_version": "test",
    }
    ctx.update(overrides)
    return ctx


# ---------------------------------------------------------------------------
# Test 1 & 2: non-git folder, accept → git init runs, _launch_coder called
# ---------------------------------------------------------------------------

def test_launcher_git_init_prompt_accept_calls_git_init(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """claude-cli + no .git: accepting the prompt runs git init before launch."""
    from saturnday.interactive import repl_loop

    context = _make_context(git_initialized=False, repo_path=str(tmp_path))
    sp_calls: list[list[str]] = []

    def fake_sp_run(cmd, **kw):
        sp_calls.append(list(cmd))
        if "init" in cmd:
            (tmp_path / ".git").mkdir(exist_ok=True)
        return MagicMock(returncode=0)

    monkeypatch.setattr("builtins.input", lambda _: "y")

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="claude-cli"), \
         patch("saturnday.interactive._offer_project_mode"), \
         patch("saturnday.interactive._offer_agent_governance"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._launch_coder", return_value=0) as mock_launch, \
         patch("subprocess.run", side_effect=fake_sp_run):
        rc = repl_loop(tmp_path)

    assert rc == 0
    assert mock_launch.called, "_launch_coder must be called after accepted git init"
    git_init_calls = [c for c in sp_calls if "init" in c]
    assert git_init_calls, "git init must be called"
    out = capsys.readouterr().out
    assert "Git initialized" in out


# ---------------------------------------------------------------------------
# Test 2b: context["git_initialized"] is True when _launch_coder receives it
# ---------------------------------------------------------------------------

def test_launcher_context_updated_after_git_init(
    tmp_path: Path, monkeypatch
) -> None:
    """After git init, context['git_initialized'] must be True before _launch_coder."""
    from saturnday.interactive import repl_loop

    context = _make_context(git_initialized=False, repo_path=str(tmp_path))
    captured_ctx: list[dict] = []

    def fake_launch(backend, rp, ctx, **kw):
        captured_ctx.append(dict(ctx))
        return 0

    monkeypatch.setattr("builtins.input", lambda _: "y")

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="claude-cli"), \
         patch("saturnday.interactive._offer_project_mode"), \
         patch("saturnday.interactive._offer_agent_governance"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._launch_coder", side_effect=fake_launch), \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        repl_loop(tmp_path)

    assert captured_ctx, "_launch_coder must be called"
    assert captured_ctx[0]["git_initialized"] is True, (
        "context must be updated to git_initialized=True before _launch_coder"
    )


# ---------------------------------------------------------------------------
# Test 3: decline → abort before _launch_coder and before side effects
# ---------------------------------------------------------------------------

def test_launcher_git_init_prompt_decline_aborts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Declining git init: returns 0, _launch_coder not called, no policy file written."""
    from saturnday.interactive import repl_loop

    context = _make_context(git_initialized=False, repo_path=str(tmp_path))
    offer_project_called: list[bool] = []

    def fake_offer_project(rp):
        offer_project_called.append(True)

    monkeypatch.setattr("builtins.input", lambda _: "n")

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="claude-cli"), \
         patch("saturnday.interactive._offer_project_mode", side_effect=fake_offer_project), \
         patch("saturnday.interactive._offer_agent_governance"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._launch_coder", return_value=0) as mock_launch, \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        rc = repl_loop(tmp_path)

    assert rc == 0
    assert not mock_launch.called, "_launch_coder must NOT be called when git init declined"
    assert not offer_project_called, "_offer_project_mode must NOT run when git init declined"
    out = capsys.readouterr().out
    assert "Aborted" in out


# ---------------------------------------------------------------------------
# Test 4: existing git repo → no prompt, _launch_coder called normally
# ---------------------------------------------------------------------------

def test_launcher_existing_git_repo_no_init_prompt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Existing git repo: no prompt, launch proceeds immediately."""
    from saturnday.interactive import repl_loop

    (tmp_path / ".git").mkdir()
    context = _make_context(git_initialized=True, repo_path=str(tmp_path))
    input_called: list[str] = []

    def fake_input(prompt: str) -> str:
        input_called.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", fake_input)

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="claude-cli"), \
         patch("saturnday.interactive._offer_project_mode"), \
         patch("saturnday.interactive._offer_agent_governance"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._launch_coder", return_value=0) as mock_launch, \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        rc = repl_loop(tmp_path)

    assert rc == 0
    assert mock_launch.called
    # No git-init prompts should appear
    git_init_prompts = [p for p in input_called if "Initialize git" in p or "project mode" in p.lower()]
    assert not git_init_prompts, f"Must not prompt for git init in existing repo, got: {input_called}"
    out = capsys.readouterr().out
    assert "Git initialized" not in out


# ---------------------------------------------------------------------------
# Test 5: openclaude and cursor-cli also get the check
# ---------------------------------------------------------------------------

def test_openclaude_launcher_git_init_check(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """openclaude launcher path also gets git-init check."""
    from saturnday.interactive import repl_loop

    context = _make_context(git_initialized=False, repo_path=str(tmp_path))

    monkeypatch.setattr("builtins.input", lambda _: "n")

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="openclaude"), \
         patch("saturnday.interactive._offer_project_mode"), \
         patch("saturnday.interactive._offer_agent_governance"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._launch_coder", return_value=0) as mock_launch, \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        rc = repl_loop(tmp_path)

    assert rc == 0
    assert not mock_launch.called, "openclaude must not launch when git init declined"


def test_cursor_cli_launcher_git_init_check(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """cursor-cli launcher path also gets git-init check."""
    from saturnday.interactive import repl_loop

    context = _make_context(git_initialized=False, repo_path=str(tmp_path))

    monkeypatch.setattr("builtins.input", lambda _: "n")

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="cursor-cli"), \
         patch("saturnday.interactive._offer_project_mode"), \
         patch("saturnday.interactive._offer_agent_governance"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._launch_coder", return_value=0) as mock_launch, \
         patch("subprocess.run", return_value=MagicMock(returncode=0)):
        rc = repl_loop(tmp_path)

    assert rc == 0
    assert not mock_launch.called, "cursor-cli must not launch when git init declined"


# ---------------------------------------------------------------------------
# Test 6: codex-cli path unchanged — goes to _fallback_repl, not _launch_coder
# ---------------------------------------------------------------------------

def test_codex_cli_path_unchanged(tmp_path: Path, monkeypatch) -> None:
    """codex-cli routes via _fallback_repl — new check must not fire on this path."""
    from saturnday.interactive import repl_loop

    context = _make_context(git_initialized=False, repo_path=str(tmp_path))
    fallback_called: list[bool] = []
    launch_called: list[bool] = []

    def fake_fallback(rp, ctx, sess):
        fallback_called.append(True)
        return 0

    def fake_launch(backend, rp, ctx):
        launch_called.append(True)
        return 0

    with patch("saturnday.interactive.detect_context", return_value=context), \
         patch("saturnday.interactive.select_backend", return_value="codex-cli"), \
         patch("saturnday.interactive._offer_project_mode"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._fallback_repl", side_effect=fake_fallback), \
         patch("saturnday.interactive._launch_coder", side_effect=fake_launch):
        rc = repl_loop(tmp_path)

    assert rc == 0
    assert fallback_called, "_fallback_repl must be called for codex-cli"
    assert not launch_called, "_launch_coder must NOT be called for codex-cli"
