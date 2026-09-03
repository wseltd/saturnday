"""Phase 6 — run-time proof negotiation.

When a declared-mode plan's proof fails at runtime AND we're interactive,
the operator can:
  [1] Accept failure
  [2] Let the coder derive a different proof and retry
  [3] Edit the proof in $EDITOR and retry
  [4] Exit (same as 1)

Non-interactive (no TTY): no negotiation, return original failure.
Never silently promote a failed proof to success.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday._types import CoderConfig, ProjectPlan, TicketSpec
from saturnday.ticket_runner import _edit_proof_in_editor, _negotiate_proof_retry


def _plan(mode: str = "library") -> ProjectPlan:
    return ProjectPlan(
        version=1,
        project_id="p",
        tickets=(
            TicketSpec(
                ticket_id="T001", goal="g",
                acceptance_criteria=("function f exists in src/m.py",),
            ),
        ),
        operating_mode=mode,
        dependency_profile="self_contained",
        proof_realism="production_intent",
        testing_strategy="unspecified",
        local_proof_cmd="python -c \"from src.m import f; assert f(1) == 1\"",
    )


def _cfg() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="k", model="m")


# ---------------------------------------------------------------------------
# Non-interactive path: never negotiate
# ---------------------------------------------------------------------------


def test_non_interactive_returns_original_failure(tmp_path: Path) -> None:
    """When stdin is not a TTY, no dialogue — return the original cmd
    and failure untouched."""
    with patch("sys.stdin.isatty", return_value=False):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'x'",
            original_failure="boom",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert cmd == "python -c 'x'"
    assert fail == "boom"


# ---------------------------------------------------------------------------
# Interactive: choice [1] Accept failure
# ---------------------------------------------------------------------------


def test_accept_failure_returns_original(tmp_path: Path) -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="1"),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'x'",
            original_failure="boom",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert cmd == "python -c 'x'"
    assert fail == "boom"


def test_empty_choice_treated_as_accept(tmp_path: Path) -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value=""),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="x", original_failure="boom", plan=_plan(),
            repo_path=tmp_path, coder_config=_cfg(),
        )
    assert fail == "boom"


def test_choice_4_exit_returns_failure(tmp_path: Path) -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="4"),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="x", original_failure="boom", plan=_plan(),
            repo_path=tmp_path, coder_config=_cfg(),
        )
    assert fail == "boom"


# ---------------------------------------------------------------------------
# Interactive: choice [2] Coder retry
# ---------------------------------------------------------------------------


def test_choice_2_coder_derives_new_proof_and_passes(tmp_path: Path) -> None:
    """Operator picks [2], coder derives a valid proof, retry succeeds."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f(x): return x + 1\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            '```bash\n'
            'python -c "from src.m import f; assert f(1) == 2"\n'
            '```'
        )

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="2"),
        patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder),
        patch("saturnday.ticket_runner._git_changed_files", return_value=["src/m.py"]),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'old'",
            original_failure="assertion failed",
            plan=_plan("library"),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert fail == ""
    assert "f(1) == 2" in cmd


def test_choice_2_coder_derives_but_retry_still_fails(tmp_path: Path) -> None:
    """Coder produces a valid-shape proof but running it fails — caps at
    max_retries and returns the last failure."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f(x): raise ValueError('nope')\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        return '```bash\npython -c "from src.m import f; assert f(1) == 1"\n```'

    # After 2 retries of [2], dialogue must end.  We feed [2] both times,
    # then a final EOF to avoid looping forever — not needed because
    # _max_retries caps at 2.
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["2", "2"]),
        patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder),
        patch("saturnday.ticket_runner._git_changed_files", return_value=["src/m.py"]),
        patch("saturnday.ticket_runner._run_verify_cmd",
              side_effect=["first retry fail", "second retry fail"]),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'old'",
            original_failure="initial boom",
            plan=_plan("library"),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    # Derived cmd replaced original; failure reflects the last run.
    assert "f(1) == 1" in cmd
    assert fail  # non-empty


def test_choice_2_coder_cannot_derive(tmp_path: Path) -> None:
    """Coder returns INSUFFICIENT_CONTEXT → resolver says unresolved_gap
    → negotiator keeps the original command and failure."""
    def fake_coder(cfg, messages, repo_path, **kw):
        return "INSUFFICIENT_CONTEXT"

    # Next turn: operator accepts.
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["2", "1"]),
        patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder),
        patch("saturnday.ticket_runner._git_changed_files", return_value=[]),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'original'",
            original_failure="boom",
            plan=_plan("library"),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert cmd == "python -c 'original'"
    assert fail == "boom"


# ---------------------------------------------------------------------------
# Interactive: choice [3] Edit proof
# ---------------------------------------------------------------------------


def test_choice_3_edit_proof_passes(tmp_path: Path) -> None:
    def fake_edit(cmd: str) -> str:
        return "python -c 'assert 1==1'"  # edited version

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="3"),
        patch("saturnday.ticket_runner._edit_proof_in_editor",
              side_effect=fake_edit),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'broken'",
            original_failure="boom",
            plan=_plan("library"),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert fail == ""
    assert "assert 1==1" in cmd


def test_choice_3_edit_unchanged_continues_loop(tmp_path: Path) -> None:
    """If the operator saves the editor without changing anything, the
    negotiator returns to the menu rather than pretending success."""
    def fake_edit(cmd: str) -> str:
        return cmd  # no change

    # Menu twice: [3] (no change), then [1] (accept).
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["3", "1"]),
        patch("saturnday.ticket_runner._edit_proof_in_editor",
              side_effect=fake_edit),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'x'",
            original_failure="boom",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert cmd == "python -c 'x'"
    assert fail == "boom"


def test_choice_3_edited_proof_still_fails(tmp_path: Path) -> None:
    def fake_edit(cmd: str) -> str:
        return "python -c 'also broken'"

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["3", "1"]),
        patch("saturnday.ticket_runner._edit_proof_in_editor",
              side_effect=fake_edit),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value="still boom"),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="python -c 'x'",
            original_failure="boom",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
        )
    assert cmd == "python -c 'also broken'"
    assert fail == "still boom"


# ---------------------------------------------------------------------------
# Ctrl-C / EOF handling
# ---------------------------------------------------------------------------


def test_ctrl_c_at_menu_returns_failure(tmp_path: Path) -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=KeyboardInterrupt),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="x", original_failure="boom", plan=_plan(),
            repo_path=tmp_path, coder_config=_cfg(),
        )
    assert fail == "boom"


def test_eof_at_menu_returns_failure(tmp_path: Path) -> None:
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=EOFError),
    ):
        cmd, fail = _negotiate_proof_retry(
            original_cmd="x", original_failure="boom", plan=_plan(),
            repo_path=tmp_path, coder_config=_cfg(),
        )
    assert fail == "boom"


# ---------------------------------------------------------------------------
# _edit_proof_in_editor
# ---------------------------------------------------------------------------


def test_edit_proof_saves_new_content(tmp_path: Path, monkeypatch) -> None:
    """Use an inline script-based 'editor' that modifies the file to
    simulate an operator save."""
    import textwrap
    editor_script = tmp_path / "fake_editor.sh"
    editor_script.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo 'python -c "assert 2==2"' > "$1"
    """))
    editor_script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor_script))

    out = _edit_proof_in_editor("python -c 'original'")
    assert out == 'python -c "assert 2==2"'


def test_edit_proof_empty_save_keeps_original(tmp_path: Path, monkeypatch) -> None:
    editor_script = tmp_path / "empty_editor.sh"
    editor_script.write_text("#!/bin/sh\n> \"$1\"\n")
    editor_script.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor_script))

    out = _edit_proof_in_editor("python -c 'keepme'")
    assert out == "python -c 'keepme'"


def test_edit_proof_no_editor_keeps_original(monkeypatch) -> None:
    """If no editor can be found, return the original cmd."""
    monkeypatch.setenv("EDITOR", "definitely_not_a_real_editor_binary_12345")
    monkeypatch.setenv("VISUAL", "definitely_not_a_real_editor_binary_12345")
    # Patch shutil.which to return None so the fallbacks also fail.
    with patch("shutil.which", return_value=None):
        out = _edit_proof_in_editor("python -c 'keepme'")
    assert out == "python -c 'keepme'"
