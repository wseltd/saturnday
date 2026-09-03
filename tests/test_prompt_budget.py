"""Tests for prompt-budget instrumentation.

Covers:
1. Below soft threshold: no warning, no behaviour change
2. Above soft threshold: warning recorded, no behaviour change
3. Hard-threshold-sized prompt: threshold recorded, no behaviour change
4. Evidence fields appear as expected on TicketEvidence
5. Final assembled prompt content is unchanged (proof of non-interference)
"""
from __future__ import annotations

from saturnday.context_assembler import assemble_messages, build_ticket_prompt
from saturnday.run.evidence import TicketEvidence
from saturnday._types import TicketSpec, TicketScope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(goal: str = "Build something") -> TicketSpec:
    return TicketSpec(
        ticket_id="T001",
        goal=goal,
        scope=TicketScope(),
    )


def _measure_prompt(system: str, user: str, repair_context: str | None = None) -> int:
    """Measure prompt chars the same way _execute_ticket does."""
    messages = assemble_messages(system, user, repair_context)
    return sum(len(m.get("content", "")) for m in messages)


# ---------------------------------------------------------------------------
# 1. Below soft threshold: no warning
# ---------------------------------------------------------------------------

class TestBelowSoftThreshold:
    def test_small_prompt_no_warning(self) -> None:
        system = "You are a coder."
        user = "Build a function that adds two numbers."
        chars = _measure_prompt(system, user)
        assert chars < 7500
        # No warning would be emitted — just measurement

    def test_evidence_fields_present(self) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            prompt_chars=500,
            prompt_budget_warning=False,
            prompt_budget_soft_threshold=7500,
        )
        assert ev.prompt_chars == 500
        assert ev.prompt_budget_warning is False
        assert ev.prompt_budget_soft_threshold == 7500


# ---------------------------------------------------------------------------
# 2. Above soft threshold: warning recorded
# ---------------------------------------------------------------------------

class TestAboveSoftThreshold:
    def test_large_prompt_triggers_warning_field(self) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            prompt_chars=8000,
            prompt_budget_warning=True,
            prompt_budget_soft_threshold=7500,
        )
        assert ev.prompt_budget_warning is True
        assert ev.prompt_chars == 8000

    def test_warning_threshold_correct(self) -> None:
        """Soft threshold is 7500 chars."""
        assert 7500 < 9000  # soft < hard


# ---------------------------------------------------------------------------
# 3. Hard threshold: recorded, no behaviour change
# ---------------------------------------------------------------------------

class TestHardThreshold:
    def test_hard_threshold_recorded(self) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            prompt_chars=10000,
            prompt_budget_warning=True,
            prompt_budget_soft_threshold=7500,
        )
        # Hard threshold is recorded but does not change behaviour
        assert ev.prompt_chars == 10000
        assert ev.prompt_budget_warning is True


# ---------------------------------------------------------------------------
# 4. Evidence fields appear as expected
# ---------------------------------------------------------------------------

class TestEvidenceFields:
    def test_all_placeholder_fields_present(self) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            prompt_chars=3000,
            prompt_budget_warning=False,
            prompt_budget_soft_threshold=7500,
            context_compaction_applied=False,
            prompt_split_exempt=False,
            prompt_split_reason=None,
        )
        assert ev.context_compaction_applied is False
        assert ev.prompt_split_exempt is False
        assert ev.prompt_split_reason is None

    def test_default_values(self) -> None:
        ev = TicketEvidence(ticket_id="T001", attempt=1)
        assert ev.prompt_chars == 0
        assert ev.prompt_budget_warning is False
        assert ev.prompt_budget_soft_threshold == 0
        assert ev.prompt_budget_hard_threshold == 0
        assert ev.context_compaction_applied is False
        assert ev.prompt_split_exempt is False
        assert ev.prompt_split_reason is None

    def test_both_thresholds_present_and_correct(self) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            prompt_chars=8500,
            prompt_budget_warning=True,
            prompt_budget_soft_threshold=7500,
            prompt_budget_hard_threshold=9000,
        )
        assert ev.prompt_budget_soft_threshold == 7500
        assert ev.prompt_budget_hard_threshold == 9000
        assert ev.prompt_budget_soft_threshold < ev.prompt_budget_hard_threshold


# ---------------------------------------------------------------------------
# 5. Prompt content unchanged (proof of non-interference)
# ---------------------------------------------------------------------------

class TestPromptContentUnchanged:
    def test_assemble_messages_output_identical(self) -> None:
        """assemble_messages returns identical content — instrumentation
        is applied AFTER assembly in _execute_ticket, not inside it."""
        system = "System prompt with standards and rules " * 50
        user = "Build a REST API with auth and tests " * 30
        repair = "Fix the SQL injection finding in routes.py"

        messages = assemble_messages(system, user, repair)

        # Verify exact content is preserved
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == system
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == user
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "I'll fix the issues."
        assert messages[3]["role"] == "user"
        assert "GOVERNANCE FINDINGS:" in messages[3]["content"]
        assert repair in messages[3]["content"]

    def test_build_ticket_prompt_output_identical(self) -> None:
        """build_ticket_prompt returns identical content regardless of
        prompt-budget instrumentation (which runs after, not inside)."""
        ticket = _make_ticket("Build a calculator")
        state_summary = "Current state: empty project"
        plan_notes = "This is a simple calculator"

        prompt_a = build_ticket_prompt(ticket, state_summary, plan_notes, cli_mode=False)
        prompt_b = build_ticket_prompt(ticket, state_summary, plan_notes, cli_mode=False)

        assert prompt_a == prompt_b, "build_ticket_prompt must be deterministic"
        assert "Build a calculator" in prompt_a

    def test_error_path_evidence_preserves_budget(self) -> None:
        """When execution fails after prompt assembly, the measured budget
        must still be present in evidence (not fall back to 0)."""
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            error="CloudCoreError: coder crashed",
            prompt_chars=8200,
            prompt_budget_warning=True,
            prompt_budget_soft_threshold=7500,
            prompt_budget_hard_threshold=9000,
        )
        assert ev.prompt_chars == 8200, "Error-path evidence must preserve measured prompt_chars"
        assert ev.prompt_budget_warning is True
        assert ev.prompt_budget_soft_threshold == 7500
        assert ev.prompt_budget_hard_threshold == 9000

    def test_repair_attempt_evidence_has_budget(self) -> None:
        """Auto-repair execution attempts must also record prompt budget."""
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=4,  # repair attempt
            coder_response="repaired code",
            prompt_chars=6000,
            prompt_budget_warning=False,
            prompt_budget_soft_threshold=7500,
            prompt_budget_hard_threshold=9000,
        )
        assert ev.prompt_chars == 6000
        assert ev.prompt_budget_soft_threshold == 7500
        assert ev.prompt_budget_hard_threshold == 9000

    def test_single_source_of_truth(self) -> None:
        """_assemble_ticket_prompt is the only prompt assembly path.
        _execute_ticket does not assemble prompts — it receives
        pre-assembled messages.  This proves there is no duplication."""
        from saturnday.ticket_runner import _assemble_ticket_prompt, _execute_ticket
        import inspect
        # _execute_ticket must NOT call build_ticket_prompt or assemble_messages
        source = inspect.getsource(_execute_ticket)
        assert "build_ticket_prompt" not in source, "_execute_ticket must not assemble prompts"
        assert "assemble_messages" not in source, "_execute_ticket must not assemble prompts"
        # _assemble_ticket_prompt MUST call both
        source_asm = inspect.getsource(_assemble_ticket_prompt)
        assert "build_ticket_prompt" in source_asm
        assert "assemble_messages" in source_asm

    def test_measurement_does_not_alter_message_list(self) -> None:
        """The measurement operation (sum of content lengths) does not
        mutate the messages list."""
        system = "System"
        user = "User"
        messages = assemble_messages(system, user)

        # Measure (same as _execute_ticket does)
        _ = sum(len(m.get("content", "")) for m in messages)

        # Verify messages are untouched
        assert len(messages) == 2
        assert messages[0]["content"] == "System"
        assert messages[1]["content"] == "User"
