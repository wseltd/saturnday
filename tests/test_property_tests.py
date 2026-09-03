"""Tests for property_tests.py (Phase 3: T010–T012, T014).

Covers:
- test_identify_eligible_pure_function
- test_identify_eligible_parser_function
- test_identify_skips_test_functions
- test_identify_skips_io_functions
- test_identify_skips_trivial_functions
- test_generate_property_tests_python
- test_generate_property_tests_empty_targets
- test_generate_includes_hypothesis_import
- test_generate_determinism_property
- test_run_property_tests_cleanup
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.run.property_tests import (
    generate_property_tests,
    identify_eligible_targets,
    run_property_tests,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(tmp_path: Path, filename: str, source: str) -> str:
    """Write source to tmp_path/filename and return the relative path string."""
    p = tmp_path / filename
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    return filename


# ---------------------------------------------------------------------------
# T010 tests: identify_eligible_targets
# ---------------------------------------------------------------------------


def test_identify_eligible_pure_function(tmp_path: Path) -> None:
    """A typed function with no I/O and multiple body lines is eligible."""
    source = """\
        def add_numbers(x: int, y: int) -> int:
            result = x + y
            return result
    """
    rel = _write_py(tmp_path, "math_utils.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert len(targets) == 1
    t = targets[0]
    assert t["name"] == "add_numbers"
    assert t["is_pure"] is True
    assert t["return_annotation"] == "int"
    assert t["language"] == "python"
    assert any(p["name"] == "x" for p in t["params"])


def test_identify_eligible_parser_function(tmp_path: Path) -> None:
    """A function with a 'parse' prefix is eligible even without transform heuristic params."""
    source = """\
        def parse_event(raw: str) -> dict:
            parts = raw.split(",")
            return {"key": parts[0]}
    """
    rel = _write_py(tmp_path, "parser.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    names = [t["name"] for t in targets]
    assert "parse_event" in names


def test_identify_skips_test_functions(tmp_path: Path) -> None:
    """Functions with test_ prefix are excluded."""
    source = """\
        def test_something(x: int) -> int:
            value = x + 1
            return value
    """
    rel = _write_py(tmp_path, "test_code.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert all(t["name"] != "test_something" for t in targets)


def test_identify_skips_io_functions(tmp_path: Path) -> None:
    """Functions calling open() or os.path are excluded."""
    source = """\
        def read_config(path: str) -> dict:
            with open(path) as f:
                return {"data": f.read()}
    """
    rel = _write_py(tmp_path, "config_reader.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert all(t["name"] != "read_config" for t in targets)


def test_identify_skips_trivial_functions(tmp_path: Path) -> None:
    """Functions with fewer than 2 non-trivial body lines are excluded."""
    source = """\
        def trivial(x: int) -> int:
            return x
    """
    rel = _write_py(tmp_path, "trivial.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert all(t["name"] != "trivial" for t in targets)


def test_identify_skips_dunder_methods(tmp_path: Path) -> None:
    """Dunder methods like __init__ are excluded."""
    source = """\
        class Foo:
            def __init__(self, x: int) -> None:
                self.x = x
                self.y = x * 2

            def normalize_value(self, val: int) -> int:
                result = val + self.x
                return result
    """
    rel = _write_py(tmp_path, "model.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    names = [t["name"] for t in targets]
    assert "__init__" not in names
    # normalize_value should be found (self param stripped)
    assert "normalize_value" in names


def test_identify_caps_at_ten(tmp_path: Path) -> None:
    """At most 10 targets are returned even when more are eligible."""
    lines = []
    for i in range(15):
        lines.append(f"def parse_item_{i}(x: str) -> str:")
        lines.append(f"    result = x.strip()")
        lines.append(f"    return result")
        lines.append("")
    source = "\n".join(lines)
    rel = _write_py(tmp_path, "many_funcs.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert len(targets) <= 10


def test_identify_skips_functions_without_return_annotation(tmp_path: Path) -> None:
    """Functions lacking a return annotation are excluded."""
    source = """\
        def no_annotation(x, y):
            result = x + y
            return result
    """
    rel = _write_py(tmp_path, "no_ann.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert all(t["name"] != "no_annotation" for t in targets)


def test_identify_handles_syntax_error(tmp_path: Path) -> None:
    """Files with syntax errors produce empty list (no crash)."""
    bad_file = tmp_path / "broken.py"
    bad_file.write_text("def (: broken syntax ::::", encoding="utf-8")
    targets = identify_eligible_targets(["broken.py"], tmp_path)
    assert isinstance(targets, list)
    assert targets == []


def test_identify_handles_missing_file(tmp_path: Path) -> None:
    """Non-existent files are silently skipped."""
    targets = identify_eligible_targets(["does_not_exist.py"], tmp_path)
    assert targets == []


def test_identify_skips_io_subprocess_calls(tmp_path: Path) -> None:
    """Functions using subprocess are excluded."""
    source = """\
        def run_process(cmd: str) -> str:
            import subprocess
            result = subprocess.run(cmd, capture_output=True)
            return result.stdout.decode()
    """
    rel = _write_py(tmp_path, "proc.py", source)
    targets = identify_eligible_targets([rel], tmp_path)
    assert all(t["name"] != "run_process" for t in targets)


# ---------------------------------------------------------------------------
# T011 tests: generate_property_tests
# ---------------------------------------------------------------------------


def test_generate_property_tests_python(tmp_path: Path) -> None:
    """Known eligible targets produce a non-empty Python test file."""
    targets = [
        {
            "name": "parse_event",
            "file": "src/saturnday/parser.py",
            "params": [{"name": "raw", "type": "str"}],
            "return_annotation": "dict",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert isinstance(result, str)
    assert len(result) > 0
    assert "parse_event" in result


def test_generate_property_tests_empty_targets(tmp_path: Path) -> None:
    """Empty target list produces empty string."""
    result = generate_property_tests([], tmp_path)
    assert result == ""


def test_generate_includes_hypothesis_import(tmp_path: Path) -> None:
    """Generated file starts with hypothesis import."""
    targets = [
        {
            "name": "normalize_text",
            "file": "src/app/text.py",
            "params": [{"name": "s", "type": "str"}],
            "return_annotation": "str",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert "from hypothesis import given" in result
    assert "strategies as st" in result


def test_generate_determinism_property(tmp_path: Path) -> None:
    """Generated file includes a determinism property test."""
    targets = [
        {
            "name": "convert_value",
            "file": "src/converter.py",
            "params": [{"name": "x", "type": "int"}],
            "return_annotation": "int",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert "determinism" in result
    assert "result_a" in result
    assert "result_b" in result


def test_generate_no_exception_property(tmp_path: Path) -> None:
    """Generated file includes a no-exception property test."""
    targets = [
        {
            "name": "format_label",
            "file": "src/labels.py",
            "params": [{"name": "value", "type": "str"}],
            "return_annotation": "str",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert "no_exception" in result


def test_generate_skips_unknown_type_annotation(tmp_path: Path) -> None:
    """Targets with unknown type annotations are skipped silently."""
    targets = [
        {
            "name": "parse_blob",
            "file": "src/blob.py",
            "params": [{"name": "data", "type": "MyCustomClass"}],
            "return_annotation": "dict",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    # Should produce empty string (no test for unknown type).
    assert result == ""


def test_generate_idempotence_for_normalize(tmp_path: Path) -> None:
    """Normalize functions with single str param get idempotence property."""
    targets = [
        {
            "name": "normalize_name",
            "file": "src/names.py",
            "params": [{"name": "name", "type": "str"}],
            "return_annotation": "str",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert "idempotence" in result


def test_generate_type_invariant_for_dict_return(tmp_path: Path) -> None:
    """Dict return annotation generates isinstance check."""
    targets = [
        {
            "name": "parse_record",
            "file": "src/records.py",
            "params": [{"name": "raw", "type": "str"}],
            "return_annotation": "dict",
            "is_pure": True,
            "line": 1,
            "language": "python",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert "isinstance" in result
    assert "return_type" in result


def test_generate_only_py_targets_for_python_file(tmp_path: Path) -> None:
    """TypeScript targets are excluded from Python test generation."""
    targets = [
        {
            "name": "parseFoo",
            "file": "src/app.ts",
            "params": [{"name": "x", "type": "string"}],
            "return_annotation": "object",
            "is_pure": True,
            "line": 1,
            "language": "typescript",
        }
    ]
    result = generate_property_tests(targets, tmp_path)
    assert result == ""


# ---------------------------------------------------------------------------
# T012 tests: run_property_tests
# ---------------------------------------------------------------------------


def test_run_property_tests_cleanup(tmp_path: Path) -> None:
    """Temp file is removed after run_property_tests completes."""
    # Use a test content that will be written then cleaned up.
    content = "# property test\n"

    with patch(
        "saturnday.run.property_tests._hypothesis_available", return_value=False
    ):
        run_property_tests(content, tmp_path)

    # The temp file must not exist after the call.
    tmp_file = tmp_path / "_saturnday_prop_test.py"
    assert not tmp_file.exists()


def test_run_property_tests_returns_skipped_when_hypothesis_missing(tmp_path: Path) -> None:
    """Returns SKIPPED result when hypothesis is not installed."""
    content = "from hypothesis import given\n"

    with patch(
        "saturnday.run.property_tests._hypothesis_available", return_value=False
    ):
        results = run_property_tests(content, tmp_path)

    assert len(results) == 1
    assert results[0]["test"] == "SKIPPED"
    assert results[0]["passed"] is False
    assert "hypothesis" in results[0]["detail"].lower()


def test_run_property_tests_empty_content(tmp_path: Path) -> None:
    """Empty content returns empty list without running subprocess."""
    results = run_property_tests("", tmp_path)
    assert results == []


def test_run_property_tests_unsupported_language(tmp_path: Path) -> None:
    """Non-python language returns SKIPPED."""
    results = run_property_tests("// ts content", tmp_path, language="typescript")
    assert len(results) == 1
    assert results[0]["test"] == "SKIPPED"


def test_run_property_tests_parse_passed_output(tmp_path: Path) -> None:
    """Parses pytest output and marks tests as passed."""
    fake_output = (
        "PASSED _saturnday_prop_test.py::test_prop_parse_event_no_exception\n"
        "PASSED _saturnday_prop_test.py::test_prop_parse_event_determinism\n"
        "2 passed in 0.5s\n"
    )

    mock_proc = MagicMock()
    mock_proc.stdout = fake_output
    mock_proc.stderr = ""
    mock_proc.returncode = 0

    content = "# some test content\n"

    with patch("saturnday.run.property_tests._hypothesis_available", return_value=True), \
         patch("subprocess.run", return_value=mock_proc):
        results = run_property_tests(content, tmp_path)

    passed = [r for r in results if r["passed"]]
    assert len(passed) >= 1


def test_run_property_tests_parse_failed_output(tmp_path: Path) -> None:
    """Parses pytest output and marks failed tests."""
    fake_output = (
        "FAILED _saturnday_prop_test.py::test_prop_foo_no_exception - AssertionError\n"
        "1 failed in 0.3s\n"
    )

    mock_proc = MagicMock()
    mock_proc.stdout = fake_output
    mock_proc.stderr = ""
    mock_proc.returncode = 1

    content = "# failing test\n"

    with patch("saturnday.run.property_tests._hypothesis_available", return_value=True), \
         patch("subprocess.run", return_value=mock_proc):
        results = run_property_tests(content, tmp_path)

    failed = [r for r in results if not r["passed"]]
    assert len(failed) >= 1


def test_run_property_tests_timeout(tmp_path: Path) -> None:
    """Timeout is handled gracefully and returns TIMEOUT result."""
    import subprocess

    content = "# slow test\n"

    with patch("saturnday.run.property_tests._hypothesis_available", return_value=True), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=60)):
        results = run_property_tests(content, tmp_path)

    assert len(results) == 1
    assert results[0]["test"] == "TIMEOUT"
    assert results[0]["passed"] is False


# ---------------------------------------------------------------------------
# Wiring smoke test
# ---------------------------------------------------------------------------


def test_ticket_runner_imports_property_tests() -> None:
    """ticket_runner.py must be importable and reference property_tests."""
    import saturnday.ticket_runner as tr
    src = Path(tr.__file__).read_text()
    assert "property_tests" in src


def test_ticket_runner_wiring_is_try_except_wrapped() -> None:
    """The property_tests wiring in ticket_runner must be inside try/except."""
    import saturnday.ticket_runner as tr
    src = Path(tr.__file__).read_text()
    # Verify the import is lazy (inside the function body).
    assert "from saturnday.run.property_tests import" in src
    # Verify it's wrapped in try/except.
    assert "_prop_exc" in src
