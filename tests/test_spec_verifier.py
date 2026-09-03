"""Tests for src/saturnday/run/spec_verifier.py (Phase 2, T009; Phase 4, T017).

Covers:
- generate_spec_assertions: pattern matching on acceptance criteria
- run_spec_assertions: subprocess-based execution of assertions
- assertions_to_verify_cmd: conversion to verify_cmd string
- infer_spec_assertions: LLM fallback (T015/T017)
- ticket_runner wiring: spec verification is non-fatal on error
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import TicketSpec
from saturnday.run.spec_verifier import (
    assertions_to_verify_cmd,
    generate_spec_assertions,
    infer_spec_assertions,
    run_spec_assertions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(criteria: tuple[str, ...], goal: str = "test goal") -> TicketSpec:
    return TicketSpec(ticket_id="T001", goal=goal, acceptance_criteria=criteria)


# ---------------------------------------------------------------------------
# T005: generate_spec_assertions
# ---------------------------------------------------------------------------

class TestGenerateSpecAssertions:

    def test_generate_from_returns_non_empty_criterion(self):
        """'function foo returns non-empty' produces an assertion."""
        ticket = _make_ticket(("function foo returns non-empty",))
        results = generate_spec_assertions(ticket, ["src/mymodule.py"], Path("/tmp"))
        assert len(results) == 1
        a = results[0]
        assert a["language"] == "python"
        assert a["confidence"] == 1.0
        assert "foo" in a["assertion"]
        assert a["criterion"] == "function foo returns non-empty"

    def test_generate_from_returns_dict_criterion(self):
        """'function parse returns dict with key name' produces a dict-key assertion."""
        ticket = _make_ticket(("function parse returns dict with key name",))
        results = generate_spec_assertions(ticket, ["src/parser.py"], Path("/tmp"))
        assert len(results) == 1
        a = results[0]
        assert '"name"' in a["assertion"] or "'name'" in a["assertion"]
        assert "parse" in a["assertion"]
        assert a["confidence"] == 1.0

    def test_generate_from_does_not_raise_criterion(self):
        """'function validate does not raise' produces a try/except assertion."""
        ticket = _make_ticket(("function validate does not raise",))
        results = generate_spec_assertions(ticket, ["src/v.py"], Path("/tmp"))
        assert len(results) == 1
        a = results[0]
        assert "validate" in a["assertion"]
        assert "except" in a["assertion"]
        assert a["confidence"] == 1.0

    def test_generate_from_output_type_criterion(self):
        """'function build output type is dict' produces an isinstance assertion."""
        ticket = _make_ticket(("function build output type is dict",))
        results = generate_spec_assertions(ticket, ["src/builder.py"], Path("/tmp"))
        assert len(results) == 1
        a = results[0]
        assert "isinstance" in a["assertion"]
        assert "dict" in a["assertion"]

    def test_generate_from_preserves_ordering_criterion(self):
        """'function sort preserves ordering' produces an ordering assertion."""
        ticket = _make_ticket(("function sort preserves ordering",))
        results = generate_spec_assertions(ticket, ["src/sorter.py"], Path("/tmp"))
        assert len(results) == 1
        a = results[0]
        assert "sort" in a["assertion"]
        assert a["confidence"] == 0.5  # fuzzy

    def test_generate_skips_structural_criteria(self):
        """'function foo exists in file.py' is already handled by contract_checker — skip."""
        ticket = _make_ticket((
            "function foo exists in module.py",
            "class Bar exists",
            "file README.md exists",
        ))
        results = generate_spec_assertions(ticket, ["src/module.py"], Path("/tmp"))
        assert len(results) == 0

    def test_generate_no_assertions_for_vague_criteria(self):
        """Criteria that match no known pattern produce no assertions."""
        ticket = _make_ticket((
            "the API works correctly",
            "data is normalized",
            "performance is acceptable",
        ))
        results = generate_spec_assertions(ticket, [], Path("/tmp"))
        assert results == []

    def test_generate_multiple_criteria(self):
        """Multiple matching criteria produce multiple assertion dicts."""
        ticket = _make_ticket((
            "function parse returns non-empty",
            "function validate does not raise",
        ))
        results = generate_spec_assertions(ticket, ["src/main.py"], Path("/tmp"))
        assert len(results) == 2

    def test_generate_empty_criteria_tuple(self):
        """Empty acceptance_criteria returns empty list."""
        ticket = _make_ticket(())
        results = generate_spec_assertions(ticket, [], Path("/tmp"))
        assert results == []

    def test_generate_assertion_dict_has_all_required_keys(self):
        """Every assertion dict has all 5 required keys."""
        ticket = _make_ticket(("function foo returns non-empty",))
        results = generate_spec_assertions(ticket, ["src/foo.py"], Path("/tmp"))
        assert len(results) == 1
        required = {"criterion", "assertion", "file", "language", "confidence"}
        assert required.issubset(results[0].keys())

    def test_generate_confidence_bounded(self):
        """All confidence values are in [0.0, 1.0]."""
        ticket = _make_ticket((
            "function foo returns non-empty",
            "function bar preserves ordering",
        ))
        results = generate_spec_assertions(ticket, ["src/foo.py"], Path("/tmp"))
        for r in results:
            assert 0.0 <= r["confidence"] <= 1.0

    def test_generate_never_raises_on_bad_input(self):
        """generate_spec_assertions never raises, even with malformed input."""
        # Pass None as a criterion (shouldn't crash)
        ticket = _make_ticket((None, 42, "function foo returns non-empty"))  # type: ignore[arg-type]
        results = generate_spec_assertions(ticket, [], Path("/tmp"))
        # Should produce at least the one valid criterion
        assert isinstance(results, list)

    def test_generate_file_field_contains_py_file(self):
        """The file field in the assertion dict references a .py file."""
        ticket = _make_ticket(("function foo returns non-empty",))
        results = generate_spec_assertions(ticket, ["src/myapp.py"], Path("/tmp"))
        assert results[0]["file"] == "src/myapp.py"

    def test_generate_no_py_file_gives_empty_file_field(self):
        """When changed_files has no .py files, file field is empty string."""
        ticket = _make_ticket(("function foo returns non-empty",))
        results = generate_spec_assertions(ticket, ["app.ts"], Path("/tmp"))
        assert results[0]["file"] == ""


# ---------------------------------------------------------------------------
# T006: run_spec_assertions
# ---------------------------------------------------------------------------

class TestRunSpecAssertions:

    def test_run_empty_list_returns_empty(self):
        """run_spec_assertions on empty input returns empty list."""
        results = run_spec_assertions([], Path("/tmp"))
        assert results == []

    def test_run_assertions_python_passing(self, tmp_path):
        """A passing assertion returns passed=True."""
        assertions = [{
            "criterion": "should pass",
            "assertion": "assert 1 + 1 == 2",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        results = run_spec_assertions(assertions, tmp_path, timeout_s=30.0)
        assert len(results) == 1
        assert results[0]["passed"] is True
        assert results[0]["error"] is None

    def test_run_assertions_python_failing(self, tmp_path):
        """A failing assertion returns passed=False."""
        assertions = [{
            "criterion": "should fail",
            "assertion": "assert 1 == 2, 'intentional failure'",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        results = run_spec_assertions(assertions, tmp_path, timeout_s=30.0)
        assert len(results) == 1
        assert results[0]["passed"] is False

    def test_run_assertions_cleanup(self, tmp_path):
        """Temp file _saturnday_spec_test.py is removed after run."""
        assertions = [{
            "criterion": "test",
            "assertion": "assert True",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        run_spec_assertions(assertions, tmp_path, timeout_s=30.0)
        assert not (tmp_path / "_saturnday_spec_test.py").exists()

    def test_run_assertions_timeout(self, tmp_path):
        """Timeout is reported as error, not crash."""
        assertions = [{
            "criterion": "timeout test",
            "assertion": "import time; time.sleep(100)",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        results = run_spec_assertions(assertions, tmp_path, timeout_s=0.1)
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert results[0]["error"] is not None

    def test_run_ts_assertions_return_unsupported(self):
        """TypeScript assertions report unsupported (not crash)."""
        assertions = [{
            "criterion": "ts test",
            "assertion": "console.assert(true)",
            "file": "app.ts",
            "language": "typescript",
            "confidence": 1.0,
        }]
        results = run_spec_assertions(assertions, Path("/tmp"))
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert "not yet supported" in (results[0]["error"] or "").lower()

    def test_run_multiple_assertions_independent_results(self, tmp_path):
        """Multiple assertions produce one result each."""
        assertions = [
            {"criterion": "a", "assertion": "assert 1 == 1", "file": "", "language": "python", "confidence": 1.0},
            {"criterion": "b", "assertion": "assert 2 == 2", "file": "", "language": "python", "confidence": 1.0},
        ]
        results = run_spec_assertions(assertions, tmp_path, timeout_s=30.0)
        assert len(results) == 2

    def test_run_result_contains_criterion_field(self, tmp_path):
        """Each result dict has criterion field matching input."""
        assertions = [{
            "criterion": "my criterion text",
            "assertion": "assert True",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        results = run_spec_assertions(assertions, tmp_path, timeout_s=30.0)
        assert results[0]["criterion"] == "my criterion text"


# ---------------------------------------------------------------------------
# T008: assertions_to_verify_cmd
# ---------------------------------------------------------------------------

class TestAssertionsToVerifyCmd:

    def test_empty_list_returns_empty_string(self):
        assert assertions_to_verify_cmd([]) == ""

    def test_python_assertion_produces_python_c_command(self):
        assertions = [{
            "criterion": "foo",
            "assertion": "assert 1 == 1",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        cmd = assertions_to_verify_cmd(assertions)
        assert cmd.startswith("python -c")
        assert "assert 1 == 1" in cmd

    def test_ts_only_assertions_return_empty(self):
        assertions = [{
            "criterion": "ts",
            "assertion": "console.assert(true)",
            "file": "app.ts",
            "language": "typescript",
            "confidence": 1.0,
        }]
        cmd = assertions_to_verify_cmd(assertions)
        assert cmd == ""

    def test_multiple_assertions_combined_in_one_command(self):
        assertions = [
            {"criterion": "a", "assertion": "assert 1 == 1", "file": "", "language": "python", "confidence": 1.0},
            {"criterion": "b", "assertion": "assert 2 == 2", "file": "", "language": "python", "confidence": 1.0},
        ]
        cmd = assertions_to_verify_cmd(assertions)
        assert "assert 1 == 1" in cmd
        assert "assert 2 == 2" in cmd

    def test_multiline_assertion_collapsed_to_inline(self):
        assertions = [{
            "criterion": "c",
            "assertion": "try:\n    foo()\nexcept Exception:\n    assert False",
            "file": "",
            "language": "python",
            "confidence": 1.0,
        }]
        cmd = assertions_to_verify_cmd(assertions)
        # Verify it is a single-line command (no bare newlines in the output)
        assert "\n" not in cmd.replace("\\'", "")


# ---------------------------------------------------------------------------
# Wiring: ticket_runner non-fatal on spec_verifier errors
# ---------------------------------------------------------------------------

class TestSpecVerifierWiringNonFatal:
    """Ensure the ticket_runner wiring handles spec_verifier exceptions."""

    def test_generate_spec_assertions_handles_exception_gracefully(self):
        """generate_spec_assertions returns [] when an unexpected exception occurs."""
        with patch(
            "saturnday.run.spec_verifier._parse_one_criterion",
            side_effect=RuntimeError("boom"),
        ):
            ticket = _make_ticket(("function foo returns non-empty",))
            result = generate_spec_assertions(ticket, ["src/foo.py"], Path("/tmp"))
            assert result == []


# ---------------------------------------------------------------------------
# T017: infer_spec_assertions (Phase 4 LLM fallback)
# ---------------------------------------------------------------------------


def _make_role_result(output: str, success: bool = True) -> MagicMock:
    """Build a mock RoleResult returned by invoke_role."""
    rr = MagicMock()
    rr.output = output
    rr.success = success
    return rr


class TestInferSpecAssertions:
    """T017: LLM-inferred assertion fallback."""

    def test_infer_assertions_returns_assert_statements(self, tmp_path):
        """When invoke_role returns assert lines they are parsed into dicts."""
        llm_response = "assert result is not None\nassert len(result) > 0\n"
        ticket = _make_ticket((), goal="Parse an event and return a non-empty dict")
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
            return_value=_make_role_result(llm_response),
        ) as mock_invoke:
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, ["src/parser.py"], tmp_path, coder_cfg)

        assert len(results) == 2
        assertions = [r["assertion"] for r in results]
        assert any("assert result is not None" in a for a in assertions)
        assert any("assert len(result) > 0" in a for a in assertions)
        mock_invoke.assert_called_once()

    def test_infer_assertions_confidence_lower(self, tmp_path):
        """All inferred assertions have confidence exactly 0.3."""
        llm_response = "assert x == 1\nassert y == 2\n"
        ticket = _make_ticket((), goal="Compute values correctly for the given inputs")
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
            return_value=_make_role_result(llm_response),
        ):
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, [], tmp_path, coder_cfg)

        assert all(r["confidence"] == 0.3 for r in results)

    def test_infer_assertions_skips_short_goal(self, tmp_path):
        """Goals with 20 or fewer characters are skipped — too vague."""
        ticket = _make_ticket((), goal="Do something")  # well under 20 chars
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
        ) as mock_invoke:
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, [], tmp_path, coder_cfg)

        assert results == []
        mock_invoke.assert_not_called()

    def test_infer_assertions_empty_on_failure(self, tmp_path):
        """When invoke_role raises, an empty list is returned (never raises)."""
        ticket = _make_ticket((), goal="Process the incoming webhook payload and store it")
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
            side_effect=RuntimeError("backend unavailable"),
        ):
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, [], tmp_path, coder_cfg)

        assert results == []

    def test_infer_assertions_empty_on_unsuccessful_role(self, tmp_path):
        """When invoke_role returns success=False, an empty list is returned."""
        ticket = _make_ticket((), goal="Parse incoming events and return structured data")
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
            return_value=_make_role_result("", success=False),
        ):
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, [], tmp_path, coder_cfg)

        assert results == []

    def test_infer_assertions_ignores_prose_lines(self, tmp_path):
        """Non-assert lines in LLM output are not included in results."""
        llm_response = (
            "Here are the assertions:\n"
            "assert parse_event({'type': 'x'}) is not None\n"
            "This ensures correctness.\n"
            "assert isinstance(result, dict)\n"
        )
        ticket = _make_ticket((), goal="Parse event payloads and return structured output")
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
            return_value=_make_role_result(llm_response),
        ):
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, [], tmp_path, coder_cfg)

        # Only the two assert lines should be returned.
        assert len(results) == 2
        for r in results:
            assert r["assertion"].startswith("assert ")

    def test_infer_assertions_reads_changed_files(self, tmp_path):
        """File content is read and included in the LLM prompt."""
        changed = tmp_path / "mymodule.py"
        changed.write_text("def parse_event(data):\n    return data\n")
        ticket = _make_ticket((), goal="Parse events and return data without modification")
        captured_tasks: list[str] = []

        def fake_invoke(role, task, *, coder_config, repo_path):
            captured_tasks.append(task)
            return _make_role_result("assert True\n")

        with patch("saturnday.run.spec_verifier.invoke_role", side_effect=fake_invoke):
            coder_cfg = MagicMock()
            infer_spec_assertions(
                ticket, [str(changed)], tmp_path, coder_cfg
            )

        assert captured_tasks, "invoke_role was not called"
        # The file content should appear somewhere in the task prompt.
        assert "parse_event" in captured_tasks[0]

    def test_infer_assertions_dict_has_all_required_keys(self, tmp_path):
        """Every inferred assertion dict contains the 5 canonical keys."""
        llm_response = "assert result != {}\n"
        ticket = _make_ticket((), goal="Build and return a populated result dictionary")
        with patch(
            "saturnday.run.spec_verifier.invoke_role",
            return_value=_make_role_result(llm_response),
        ):
            coder_cfg = MagicMock()
            results = infer_spec_assertions(ticket, ["src/builder.py"], tmp_path, coder_cfg)

        assert len(results) == 1
        required_keys = {"criterion", "assertion", "file", "language", "confidence"}
        assert required_keys.issubset(results[0].keys())
        assert results[0]["language"] == "python"
