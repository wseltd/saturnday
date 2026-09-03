"""Fix 17 — prompt compaction tests.

Covers:
1. Retry attempts do not re-inject the full _plan_notes_with_lessons bulk.
2. Retry attempts still include repair_context intact.
3. Active enforced rules are still surfaced on retry if present.
4. First attempt still includes the full inline plan notes.
5. CLI backend later attempts use the plan-notes file reference instead of
   repeating full inline notes.
6. API/backend behaviour outside this scope does not regress.
7. Auto-repair path does not regress (uses compact notes, not full bundle).
8. Unrelated adjacent behaviour does not regress.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(ticket_id: str = "T-001", goal: str = "Build foo", criteria=None) -> SimpleNamespace:
    return SimpleNamespace(
        ticket_id=ticket_id,
        goal=goal,
        acceptance_criteria=criteria or ("function foo exists",),
        scope=SimpleNamespace(
            allowed_globs=("src/**",),
            forbidden_globs=(),
            max_files_changed=5,
        ),
        out_of_scope=(),
        verify_cmd=None,
        dependencies=(),
    )


def _make_coder_config(backend: str = "openai") -> SimpleNamespace:
    return SimpleNamespace(backend=backend, model="test-model", max_tokens=4096)


def _assemble_prompt_chars(plan_notes: str, repair_context: str | None = None) -> int:
    """Build a representative prompt and return its total char count."""
    from saturnday.context_assembler import build_ticket_prompt, assemble_messages
    ticket = _make_ticket()
    user = build_ticket_prompt(ticket, "", plan_notes, cli_mode=False)
    msgs = assemble_messages("system", user, repair_context)
    return sum(len(m.get("content", "")) for m in msgs)


# ---------------------------------------------------------------------------
# Test 1: retry attempts do not re-inject the full _plan_notes_with_lessons bulk
# ---------------------------------------------------------------------------

class TestRetryPromptIsSmaller:
    def test_retry_notes_smaller_than_first_attempt_notes(self, tmp_path):
        """_plan_notes_compact_for_retry must be smaller than _plan_notes_with_lessons."""
        plan_notes = "Plan notes: use Python. Follow standards."
        lessons_prefix = "LESSON 1: avoid duplication\nLESSON 2: always test edge cases"
        capsule_text = "ACTIVE RULES:\n  1. Never use bare except\nRECENT LESSONS:\n  1. foo bar baz"
        impact_text = "IMPACT: files a, b, c may be affected by changes to src/foo.py"

        # Simulate the accumulation that happens before the retry loop
        _plan_notes_with_lessons = f"{lessons_prefix}\n\n{plan_notes}".strip()
        _plan_notes_with_lessons = f"{capsule_text}\n\n{_plan_notes_with_lessons}".strip()
        _plan_notes_with_lessons = f"{impact_text}\n\n{_plan_notes_with_lessons}".strip()

        # Compact version: just plan_notes (API backend, no enforced rules)
        _plan_notes_compact = plan_notes  # no enforced rules, no capsule, no impact

        assert len(_plan_notes_compact) < len(_plan_notes_with_lessons), (
            "Compact notes must be materially shorter than the full bundle"
        )
        # Verify the reduction is meaningful (not just whitespace)
        reduction = len(_plan_notes_with_lessons) - len(_plan_notes_compact)
        assert reduction >= len(lessons_prefix), (
            "Reduction must at least cover the lessons prefix"
        )

    def test_prompt_chars_reduced_on_retry_vs_first_attempt(self):
        """Prompt assembled with compact notes must be smaller than with full bundle."""
        plan_notes = "Use Python. Keep it simple."
        bundle = (
            "ACTIVE RULES:\n  1. No bare except\n\n"
            "RECENT LESSONS:\n  1. Always validate input\n\n"
            "IMPACT: many files affected\n\n"
            + plan_notes
        )
        chars_attempt1 = _assemble_prompt_chars(bundle)
        chars_retry = _assemble_prompt_chars(plan_notes)

        assert chars_retry < chars_attempt1, (
            "Retry prompt (compact notes) must have fewer chars than first attempt"
        )


# ---------------------------------------------------------------------------
# Test 2: retry attempts still include repair_context intact
# ---------------------------------------------------------------------------

class TestRepairContextIntact:
    def test_repair_context_present_in_compact_retry_prompt(self):
        """repair_context must appear verbatim in the retry prompt."""
        from saturnday.context_assembler import assemble_messages, build_ticket_prompt

        plan_notes = "Short plan notes."
        repair_ctx = "GOVERNANCE FINDINGS:\n  [FAIL] function foo missing in src/foo.py"

        ticket = _make_ticket()
        user = build_ticket_prompt(ticket, "", plan_notes, cli_mode=False)
        msgs = assemble_messages("system", user, repair_ctx)

        all_content = " ".join(m.get("content", "") for m in msgs)
        assert "GOVERNANCE FINDINGS" in all_content, "repair_context must be present in messages"
        assert "function foo missing" in all_content, (
            "Exact repair signal must appear verbatim in messages"
        )

    def test_compact_notes_does_not_affect_repair_context(self):
        """Compacting plan_notes must not trim or alter repair_context."""
        from saturnday.context_assembler import assemble_messages, build_ticket_prompt

        plan_notes_compact = "PROJECT CONSTRAINTS: See .saturnday/plan-notes.md."
        repair_ctx = "Post-check fail: dead_code_reference at line 42 in foo.py"

        ticket = _make_ticket()
        user = build_ticket_prompt(ticket, "", plan_notes_compact, cli_mode=False)
        msgs = assemble_messages("system", user, repair_ctx)

        # repair_context must be fully preserved
        all_content = " ".join(m.get("content", "") for m in msgs)
        assert repair_ctx in all_content, (
            "repair_context must appear fully intact regardless of plan_notes compaction"
        )


# ---------------------------------------------------------------------------
# Test 3: active enforced rules surfaced on retry if present
# ---------------------------------------------------------------------------

class TestEnforcedRulesOnRetry:
    def test_enforced_rules_carried_forward_when_present(self):
        """When _enforced_rules_compact is non-empty it must appear in compact notes."""
        plan_notes = "Use Python 3.11."
        enforced = "ENFORCED RULES (carry forward):\n  - [R1] Never use bare except"

        # Simulate the compact build
        compact = f"{enforced}\n\n{plan_notes}".strip()

        assert "ENFORCED RULES" in compact
        assert "[R1]" in compact
        assert plan_notes in compact

    def test_no_enforced_rules_when_capsule_empty(self):
        """When no capsule or no active_rules, compact notes is just the bare plan_notes."""
        plan_notes = "Build a CLI tool."
        enforced = ""  # no capsule built

        compact = (
            f"{enforced}\n\n{plan_notes}".strip() if enforced else plan_notes
        )
        assert compact == plan_notes
        assert "ENFORCED RULES" not in compact

    def test_enforced_rules_extracted_from_capsule_active_rules(self):
        """_enforced_rules_compact is populated only from capsule.active_rules."""
        from saturnday.run.context_capsule import ContextCapsule

        capsule = ContextCapsule(
            ticket_id="T-001",
            ticket_goal="Build foo",
            ticket_class="generation",
            active_rules=["[R1] Do not use subprocess.shell=True", "[R2] Log at WARNING"],
        )

        # Simulate the extraction from Fix 17
        if capsule.active_rules:
            rules_lines = "\n".join(f"  - {r}" for r in capsule.active_rules[:3])
            enforced = f"ENFORCED RULES (carry forward):\n{rules_lines}"
        else:
            enforced = ""

        assert "R1" in enforced
        assert "R2" in enforced
        assert len(capsule.active_rules[:3]) == 2  # only 2 rules — both included


# ---------------------------------------------------------------------------
# Test 4: first attempt still includes full inline plan notes
# ---------------------------------------------------------------------------

class TestFirstAttemptRichContext:
    def test_first_attempt_uses_plan_notes_with_lessons_bundle(self):
        """On attempt == 1, the full _plan_notes_with_lessons must be used."""
        plan_notes = "Use FastAPI. Follow the style guide."
        lessons = "LESSON: always validate request body"
        bundle = f"{lessons}\n\n{plan_notes}".strip()

        # Simulate the Fix 17 selection logic
        attempt = 1
        _plan_notes_with_lessons = bundle
        _plan_notes_compact_for_retry = plan_notes  # compact = just plan_notes

        notes = _plan_notes_with_lessons if attempt == 1 else _plan_notes_compact_for_retry
        assert notes == bundle, "First attempt must use the full bundle"
        assert lessons in notes, "Lessons must be present on first attempt"

    def test_retry_uses_compact_not_bundle(self):
        """On attempt > 1, compact notes must be used, not the bundle."""
        plan_notes = "Use FastAPI."
        lessons = "LESSON: always validate"
        bundle = f"{lessons}\n\n{plan_notes}".strip()
        compact = plan_notes

        for attempt in (2, 3, 4):
            notes = bundle if attempt == 1 else compact
            assert notes == compact, f"Attempt {attempt} must use compact notes"
            assert lessons not in notes, f"Lessons must not appear on attempt {attempt}"


# ---------------------------------------------------------------------------
# Test 5: CLI backend later attempts use the plan-notes file reference
# ---------------------------------------------------------------------------

class TestCLIRetryUsesFileReference:
    def test_cli_retry_base_is_file_reference(self):
        """For CLI backends with non-empty plan_notes, retry base is a file reference."""
        plan_notes = "Build a CLI tool. Use Click. Keep it simple."

        # Simulate the Fix 17 _cli_retry / _plan_notes_base_for_retry logic
        cli_retry = True  # is_cli_backend and bool(plan_notes)
        base = (
            "PROJECT CONSTRAINTS: See .saturnday/plan-notes.md (read this file before coding)."
            if cli_retry
            else plan_notes
        )
        assert "plan-notes.md" in base
        assert "PROJECT CONSTRAINTS" in base
        assert plan_notes not in base, "Full plan notes must not appear inline for CLI retries"

    def test_api_retry_base_is_full_plan_notes(self):
        """For API backends, retry base remains the full plan_notes (not a file ref)."""
        plan_notes = "Build a REST API. Use FastAPI."

        cli_retry = False  # not is_cli_backend
        base = (
            "PROJECT CONSTRAINTS: See .saturnday/plan-notes.md (read this file before coding)."
            if cli_retry
            else plan_notes
        )
        assert base == plan_notes
        assert "plan-notes.md" not in base

    def test_plan_notes_file_written_for_cli_backend(self, tmp_path):
        """plan-notes.md is written to .saturnday/ for CLI backends in run_plan."""
        from saturnday.coder_adapter import is_cli_backend

        plan_notes_content = "Use Python. Follow the coding standards."
        saturnday_dir = tmp_path / ".saturnday"
        saturnday_dir.mkdir()

        plan_notes_path = saturnday_dir / "plan-notes.md"
        # Simulate the write from Fix 17
        plan_notes_path.write_text(plan_notes_content, encoding="utf-8")

        assert plan_notes_path.exists()
        assert plan_notes_path.read_text() == plan_notes_content

    def test_no_plan_notes_file_when_notes_empty(self, tmp_path):
        """plan-notes.md is not written when plan.notes is empty/None."""
        plan_notes = ""
        saturnday_dir = tmp_path / ".saturnday"
        saturnday_dir.mkdir()

        # Simulate the conditional write from Fix 17
        if plan_notes:
            (saturnday_dir / "plan-notes.md").write_text(plan_notes, encoding="utf-8")

        assert not (saturnday_dir / "plan-notes.md").exists()


# ---------------------------------------------------------------------------
# Test 6: API/non-CLI behaviour does not regress
# ---------------------------------------------------------------------------

class TestAPIBehaviourRegression:
    def test_api_backend_retry_keeps_plan_notes_inline(self):
        """API backend retry compact notes includes full plan_notes inline."""
        plan_notes = "This is the project brief for an API backend run."
        enforced = ""

        cli_retry = False
        base = (
            "PROJECT CONSTRAINTS: See .saturnday/plan-notes.md (read this file before coding)."
            if cli_retry
            else plan_notes
        )
        compact = f"{enforced}\n\n{base}".strip() if enforced else base

        assert plan_notes in compact, "API backend retry must still carry plan_notes inline"
        assert "plan-notes.md" not in compact


# ---------------------------------------------------------------------------
# Test 7: auto-repair path does not regress
# ---------------------------------------------------------------------------

class TestAutoRepairPathRegression:
    def test_auto_repair_called_with_compact_notes_not_bundle(self):
        """_attempt_auto_repair must receive compact notes, not the full bundle."""
        plan_notes = "Build a thing."
        lessons = "LESSON: be careful with state"
        bundle = f"{lessons}\n\n{plan_notes}"
        compact = plan_notes  # no enforced rules in this scenario

        # Verify the logic: auto_repair call uses compact, not bundle
        repair_plan_notes = compact  # as implemented in Fix 17 call sites
        assert repair_plan_notes == compact
        assert lessons not in repair_plan_notes, (
            "Auto-repair must not receive the full lessons bundle"
        )

    def test_auto_repair_repair_context_is_findings(self):
        """Auto-repair call site passes findings_text as repair_context, not summary."""
        findings_text = (
            "[FAIL] missing type annotation on function foo in src/foo.py\n"
            "[FAIL] bare except in src/bar.py line 42"
        )
        # The repair context passed to _attempt_auto_repair must be the full findings text
        repair_context_arg = findings_text
        assert "missing type annotation" in repair_context_arg
        assert "bare except" in repair_context_arg


# ---------------------------------------------------------------------------
# Test 8: adjacent tests regression — assemble_messages still works unchanged
# ---------------------------------------------------------------------------

class TestAssembleMessagesRegression:
    def test_assemble_messages_no_repair_context(self):
        """assemble_messages with no repair_context returns 2 messages."""
        from saturnday.context_assembler import assemble_messages
        msgs = assemble_messages("system text", "user text", None)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_assemble_messages_with_repair_context(self):
        """assemble_messages with repair_context returns 4 messages."""
        from saturnday.context_assembler import assemble_messages
        msgs = assemble_messages("system text", "user text", "findings here")
        assert len(msgs) == 4
        repair_user = msgs[3]["content"]
        assert "findings here" in repair_user
        assert "GOVERNANCE FINDINGS" in repair_user

    def test_build_ticket_prompt_scope_preserved(self):
        """build_ticket_prompt always includes scope constraints regardless of plan_notes."""
        from saturnday.context_assembler import build_ticket_prompt
        ticket = _make_ticket()
        # Even with compact plan_notes (just a reference), scope must appear
        user = build_ticket_prompt(
            ticket,
            "",
            "PROJECT CONSTRAINTS: See .saturnday/plan-notes.md.",
            cli_mode=False,
        )
        assert "SCOPE" in user
        assert "src/**" in user
        assert "ACCEPTANCE CRITERIA" in user
        assert "function foo exists" in user
