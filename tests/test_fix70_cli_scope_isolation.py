"""Fix 70 — CLI backend scope isolation.

Pre-Fix-70: API backends enforced scope inside ``apply_file_blocks``
(``_validate_path``) but CLI backends (``claude-cli`` / ``codex-cli`` —
the primary backends in production) wrote files directly via the agent and
the runner accepted whatever ``git status`` reported.  A coder could (and
did) edit files outside its declared ticket scope undetected.

After Fix 70 the runner validates every file ``_git_changed_files`` reports
on the CLI path.  Out-of-scope edits raise ``ScopeViolationError`` carrying
the offending paths, and the runner hard-resets to ``head_before`` so the
next attempt starts from a clean baseline (CLI agents may commit directly,
which a working-tree reset alone would not undo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday._exceptions import ScopeViolationError
from saturnday._types import CoderConfig, TicketScope, TicketSpec
from saturnday.ticket_runner import _detect_scope_violations, _execute_ticket


# ---------------------------------------------------------------------------
# Pure helper: _detect_scope_violations
# ---------------------------------------------------------------------------


def test_detect_no_violations_when_all_in_scope() -> None:
    assert _detect_scope_violations(
        ["src/app/module.py", "src/app/util.py"],
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
    ) == []


def test_detect_violations_when_outside_allowed() -> None:
    """A file outside allowed_globs is reported as a violation."""
    out = _detect_scope_violations(
        ["src/app/module.py", "tests/secret_test.py"],
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
    )
    assert out == ["tests/secret_test.py"]


def test_detect_violations_when_in_forbidden() -> None:
    """A file matching forbidden_globs is reported even if allowed_globs is empty."""
    out = _detect_scope_violations(
        ["src/app/module.py", "src/app/secrets.env"],
        allowed_globs=("**",),
        forbidden_globs=("**/*.env",),
    )
    assert out == ["src/app/secrets.env"]


def test_always_allowed_boilerplate_files_are_in_scope() -> None:
    """LICENSE / README / .gitignore etc. are always allowed regardless of scope.

    Reused via _validate_path so the CLI and API enforcement stay aligned."""
    out = _detect_scope_violations(
        ["LICENSE", "README.md", ".gitignore", "pyproject.toml"],
        allowed_globs=("src/**",),
        forbidden_globs=(),
    )
    assert out == []


def test_directory_traversal_is_a_violation() -> None:
    """Path traversal must be rejected even without explicit forbidden_globs."""
    out = _detect_scope_violations(
        ["../etc/passwd"],
        allowed_globs=("**",),
        forbidden_globs=(),
    )
    assert out == ["../etc/passwd"]


# ---------------------------------------------------------------------------
# End-to-end through _execute_ticket on a CLI backend
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return head


def _cli_config() -> CoderConfig:
    """A backend that ``is_cli_backend`` recognises as CLI."""
    return CoderConfig(backend="claude-cli", api_key="", model="cli")


def _ticket_with_scope(allowed: tuple[str, ...], forbidden: tuple[str, ...] = ()) -> TicketSpec:
    return TicketSpec(
        ticket_id="T070",
        goal="Add module under src/app/",
        scope=TicketScope(allowed_globs=allowed, forbidden_globs=forbidden),
    )


def test_cli_in_scope_edit_passes(tmp_path: Path) -> None:
    """CLI backend writing a file inside allowed scope: no violation, file returned."""
    _init_repo(tmp_path)
    (tmp_path / "src" / "app").mkdir(parents=True)

    def fake_call_coder(*a, **kw):
        # CLI agent writes a file inside scope and stages it (the "agent commits directly" case).
        (tmp_path / "src" / "app" / "module.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "agent commit"],
            cwd=tmp_path, check=True,
        )
        return "agent done"

    with patch("saturnday.ticket_runner.call_coder", side_effect=fake_call_coder):
        response, changed = _execute_ticket(
            ticket=_ticket_with_scope(("src/app/**",)),
            repo_path=tmp_path,
            coder_config=_cli_config(),
            messages=[{"role": "user", "content": "go"}],
        )
    assert response == "agent done"
    assert "src/app/module.py" in changed


def test_cli_out_of_scope_edit_raises_scope_violation(tmp_path: Path) -> None:
    """CLI backend writing OUTSIDE allowed scope must raise ScopeViolationError
    listing the offending paths and undo the agent's commit."""
    head_before = _init_repo(tmp_path)
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "other").mkdir(parents=True)

    def fake_call_coder(*a, **kw):
        # Agent writes ONE file in scope and TWO outside scope, then commits.
        (tmp_path / "src" / "app" / "in_scope.py").write_text("x = 1\n")
        (tmp_path / "src" / "other" / "leak.py").write_text("y = 2\n")
        (tmp_path / "tests" / "evil.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / "evil.py").write_text("z = 3\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "agent committed across scope"],
            cwd=tmp_path, check=True,
        )
        return "agent done"

    with patch("saturnday.ticket_runner.call_coder", side_effect=fake_call_coder):
        with pytest.raises(ScopeViolationError) as exc_info:
            _execute_ticket(
                ticket=_ticket_with_scope(("src/app/**",)),
                repo_path=tmp_path,
                coder_config=_cli_config(),
                messages=[{"role": "user", "content": "go"}],
            )

    err = exc_info.value
    # Both out-of-scope paths must appear in the structured attribute.
    assert "src/other/leak.py" in err.violating_paths
    assert "tests/evil.py" in err.violating_paths
    # In-scope file must NOT be in the violation list.
    assert "src/app/in_scope.py" not in err.violating_paths
    # Error message names the ticket and the paths.
    msg = str(err)
    assert "T070" in msg
    assert "src/other/leak.py" in msg

    # Hard-reset must have undone the agent's commit (HEAD back to head_before).
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head_after == head_before, (
        f"agent commit must have been reverted; HEAD={head_after} expected {head_before}"
    )

    # Working tree must also be clean — no uncommitted residue from the agent.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status == "", f"working tree must be clean after scope-violation reset; got: {status!r}"


def test_cli_forbidden_glob_violation_raises(tmp_path: Path) -> None:
    """A file matching forbidden_globs (even if inside allowed_globs) must raise."""
    _init_repo(tmp_path)
    (tmp_path / "src" / "app").mkdir(parents=True)

    def fake_call_coder(*a, **kw):
        (tmp_path / "src" / "app" / "secrets.env").write_text("API_KEY=...\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "agent wrote secret"],
            cwd=tmp_path, check=True,
        )
        return "ok"

    with patch("saturnday.ticket_runner.call_coder", side_effect=fake_call_coder):
        with pytest.raises(ScopeViolationError) as exc_info:
            _execute_ticket(
                ticket=_ticket_with_scope(
                    allowed=("src/**",),
                    forbidden=("**/*.env",),
                ),
                repo_path=tmp_path,
                coder_config=_cli_config(),
                messages=[{"role": "user", "content": "go"}],
            )
    assert "src/app/secrets.env" in exc_info.value.violating_paths


def test_api_backend_path_unchanged_no_post_hoc_check(tmp_path: Path) -> None:
    """Sanity: API backends still enforce scope inside apply_file_blocks via
    _validate_path; the new CLI post-hoc check must not affect that path.
    Confirms Fix 70 did not duplicate enforcement on the API side."""
    _init_repo(tmp_path)
    api_config = CoderConfig(backend="openai", api_key="k", model="m")

    def fake_call_coder(*a, **kw):
        # Return a FILE block inside scope (FILE/END FILE format).
        return (
            "FILE: src/app/module.py\n"
            "x = 1\n"
            "END FILE\n"
        )

    (tmp_path / "src" / "app").mkdir(parents=True)
    with patch("saturnday.ticket_runner.call_coder", side_effect=fake_call_coder):
        response, changed = _execute_ticket(
            ticket=_ticket_with_scope(("src/app/**",)),
            repo_path=tmp_path,
            coder_config=api_config,
            messages=[{"role": "user", "content": "go"}],
        )
    assert "src/app/module.py" in changed
