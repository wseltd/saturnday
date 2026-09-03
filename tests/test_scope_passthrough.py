"""T001: Verify that ticket.scope.allowed_globs flows correctly through the execution pipeline.

Passthrough chain confirmed by code inspection (2026-04-07):

1. write_context_file (CLI backends):
   ticket_runner.py _assemble_ticket_prompt line ~2459:
   write_context_file(state, _ctx_path, relevant_globs=ticket.scope.allowed_globs)

2. generate_context_summary (API backends):
   ticket_runner.py _assemble_ticket_prompt line ~2469-2472:
   generate_context_summary(state, relevant_globs=ticket.scope.allowed_globs, max_chars=8000)

3. apply_file_blocks (API backends, patch application):
   ticket_runner.py _execute_ticket line ~2546-2550:
   apply_file_blocks(repo_path, changes, ticket.scope.allowed_globs, ticket.scope.forbidden_globs)

4. build_ticket_prompt (both backends):
   context_assembler.py lines 268-274:
   Renders scope lines from ticket.scope.allowed_globs and ticket.scope.forbidden_globs
   when allowed_globs != ("**",).

Finding: The globs are NOT always ("**",). They are ticket-specific, set via TicketScope in
the plan JSON and propagated faithfully through every call site. The default ("**",) is only
used when the plan does not specify scope — the rendering in build_ticket_prompt even
suppresses the "Allowed files" line precisely when the default is used (line 268 guards
on != ("**",)), so the only way a caller would see the wildcard is if the plan omitted scope.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import TicketScope, TicketSpec
from saturnday.context_assembler import build_ticket_prompt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ticket(allowed_globs: tuple[str, ...], forbidden_globs: tuple[str, ...] = ()) -> TicketSpec:
    """Build a minimal TicketSpec with the given scope."""
    return TicketSpec(
        ticket_id="T-SCOPE-01",
        goal="Implement the widget factory",
        scope=TicketScope(
            allowed_globs=allowed_globs,
            forbidden_globs=forbidden_globs,
        ),
    )


# ---------------------------------------------------------------------------
# T001-A: write_context_file receives relevant_globs matching ticket scope
# ---------------------------------------------------------------------------


def test_write_context_file_receives_ticket_scope_globs(tmp_path: Path) -> None:
    """write_context_file must be called with relevant_globs=ticket.scope.allowed_globs.

    Verifies the CLI-backend path in _assemble_ticket_prompt (ticket_runner.py:~2459).
    Note: prompt assembly was refactored out of _execute_ticket into _assemble_ticket_prompt;
    write_context_file is now called there, before execution.
    """
    ticket = _make_ticket(allowed_globs=("src/saturnday/**/*.py", "tests/test_widget*.py"))

    # write_context_file is imported inside _assemble_ticket_prompt, so we patch
    # it at the source module (saturnday.project_state).
    captured: list[dict] = []

    def _spy_write_context_file(state: object, ctx_path: object, **kwargs: object) -> None:
        captured.append(kwargs)

    with patch(
        "saturnday.project_state.write_context_file",
        side_effect=_spy_write_context_file,
    ), patch(
        "saturnday.ticket_runner.is_cli_backend",
        return_value=True,
    ):
        from saturnday._types import CoderConfig
        from saturnday.project_state import ProjectState
        from saturnday.ticket_runner import _assemble_ticket_prompt

        coder_cfg = CoderConfig(backend="claude-cli")
        state = ProjectState(project_id="test-proj")

        _assemble_ticket_prompt(
            ticket=ticket,
            repo_path=tmp_path,
            coder_config=coder_cfg,
            system_prompt="",
            state=state,
            plan_notes="",
            repair_context=None,
        )

    assert captured, "write_context_file was never called"
    assert captured[0].get("relevant_globs") == ("src/saturnday/**/*.py", "tests/test_widget*.py"), (
        f"relevant_globs mismatch: got {captured[0].get('relevant_globs')}"
    )


# ---------------------------------------------------------------------------
# T001-B: build_ticket_prompt renders non-default allowed_globs
# ---------------------------------------------------------------------------


def test_build_ticket_prompt_renders_specific_allowed_globs() -> None:
    """build_ticket_prompt must include allowed file patterns in SCOPE section."""
    ticket = _make_ticket(allowed_globs=("src/saturnday/run/*.py",))
    prompt = build_ticket_prompt(ticket, project_state_summary="", plan_notes="", cli_mode=False)
    assert "src/saturnday/run/*.py" in prompt


def test_build_ticket_prompt_omits_scope_line_for_default_wildcard() -> None:
    """build_ticket_prompt must NOT render 'Allowed files' when scope is the default wildcard."""
    ticket = _make_ticket(allowed_globs=("**",))
    prompt = build_ticket_prompt(ticket, project_state_summary="", plan_notes="", cli_mode=False)
    assert "Allowed files" not in prompt


def test_build_ticket_prompt_renders_forbidden_globs() -> None:
    """build_ticket_prompt must include forbidden_globs in SCOPE section."""
    ticket = _make_ticket(
        allowed_globs=("src/**",),
        forbidden_globs=("src/saturnday/guard/**",),
    )
    prompt = build_ticket_prompt(ticket, project_state_summary="", plan_notes="", cli_mode=False)
    assert "src/saturnday/guard/**" in prompt


# ---------------------------------------------------------------------------
# T001-C: TicketScope default is ("**",) not empty
# ---------------------------------------------------------------------------


def test_ticket_scope_default_allowed_globs_is_wildcard() -> None:
    """TicketScope default allowed_globs must be ('**',), not empty."""
    scope = TicketScope()
    assert scope.allowed_globs == ("**",), (
        f"Default allowed_globs changed from ('**',): got {scope.allowed_globs}"
    )
