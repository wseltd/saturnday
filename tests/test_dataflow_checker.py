"""Tests for dataflow_checker.py — Phase 5 (T021).

All tests must pass without mypy, hypothesis, or tsc installed.
No subprocess calls to external tools — only internal logic tested.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday.run.dataflow_checker import (
    DataflowFinding,
    _MAX_FINDINGS,
    check_cross_function_flow,
    format_dataflow_findings,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """A minimal repo root used by tests that write actual files."""
    return tmp_path


# ---------------------------------------------------------------------------
# T021-1: test_optional_to_nonoptional_detected
# ---------------------------------------------------------------------------


def test_optional_to_nonoptional_detected(tmp_repo: Path) -> None:
    """Function returns Optional[int]; caller accesses .bit_length() without None check."""
    producer_src = textwrap.dedent("""\
        from typing import Optional

        def get_value() -> Optional[int]:
            return None
    """)
    caller_src = textwrap.dedent("""\
        from producer import get_value

        def use_it():
            result = get_value()
            length = result.bit_length()
    """)

    producer_path = tmp_repo / "producer.py"
    caller_path = tmp_repo / "caller.py"
    producer_path.write_text(producer_src, encoding="utf-8")
    caller_path.write_text(caller_src, encoding="utf-8")

    # Patch grep to return our caller
    def fake_grep(*args, capture_output=False, text=False, check=False, timeout=None):
        class Result:
            stdout = str(caller_path) + "\n"
            returncode = 0
        return Result()

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=fake_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    assert len(findings) >= 1
    kinds = {f["kind"] for f in findings}
    assert "optional_to_nonoptional" in kinds


# ---------------------------------------------------------------------------
# T021-2: test_empty_to_nonempty_detected
# ---------------------------------------------------------------------------


def test_empty_to_nonempty_detected(tmp_repo: Path) -> None:
    """Function returns list[str]; caller indexes result[0] without length check."""
    producer_src = textwrap.dedent("""\
        def get_items() -> list[str]:
            return []
    """)
    caller_src = textwrap.dedent("""\
        from producer import get_items

        def use_items():
            result = get_items()
            first = result[0]
    """)

    producer_path = tmp_repo / "producer.py"
    caller_path = tmp_repo / "caller.py"
    producer_path.write_text(producer_src, encoding="utf-8")
    caller_path.write_text(caller_src, encoding="utf-8")

    def fake_grep(*args, capture_output=False, text=False, check=False, timeout=None):
        class Result:
            stdout = str(caller_path) + "\n"
            returncode = 0
        return Result()

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=fake_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    assert len(findings) >= 1
    kinds = {f["kind"] for f in findings}
    assert "empty_to_nonempty" in kinds


# ---------------------------------------------------------------------------
# T021-3: test_type_mismatch_detected (safe usage produces no optional finding)
# ---------------------------------------------------------------------------


def test_type_mismatch_detected(tmp_repo: Path) -> None:
    """Optional return used safely with None check — no optional_to_nonoptional finding."""
    producer_src = textwrap.dedent("""\
        from typing import Optional

        def get_value() -> Optional[int]:
            return None
    """)
    # Caller checks None before use
    caller_src = textwrap.dedent("""\
        from producer import get_value

        def use_it():
            result = get_value()
            if result is not None:
                length = result.bit_length()
    """)

    producer_path = tmp_repo / "producer.py"
    caller_path = tmp_repo / "caller.py"
    producer_path.write_text(producer_src, encoding="utf-8")
    caller_path.write_text(caller_src, encoding="utf-8")

    def fake_grep(*args, capture_output=False, text=False, check=False, timeout=None):
        class Result:
            stdout = str(caller_path) + "\n"
            returncode = 0
        return Result()

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=fake_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    # The None check should suppress the finding
    optional_findings = [f for f in findings if f["kind"] == "optional_to_nonoptional"]
    assert len(optional_findings) == 0, (
        f"Expected no optional_to_nonoptional findings for safe usage, got: {optional_findings}"
    )


# ---------------------------------------------------------------------------
# T021-4: test_no_findings_for_safe_code
# ---------------------------------------------------------------------------


def test_no_findings_for_safe_code(tmp_repo: Path) -> None:
    """A function with a plain int return type generates no dataflow findings."""
    producer_src = textwrap.dedent("""\
        def compute(x: int, y: int) -> int:
            return x + y
    """)
    caller_src = textwrap.dedent("""\
        from producer import compute

        def use_it():
            result = compute(1, 2)
            return result + 1
    """)

    producer_path = tmp_repo / "producer.py"
    caller_path = tmp_repo / "caller.py"
    producer_path.write_text(producer_src, encoding="utf-8")
    caller_path.write_text(caller_src, encoding="utf-8")

    def fake_grep(*args, capture_output=False, text=False, check=False, timeout=None):
        class Result:
            stdout = str(caller_path) + "\n"
            returncode = 0
        return Result()

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=fake_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    # compute() returns int (not Optional, not list) — no findings expected
    assert findings == []


# ---------------------------------------------------------------------------
# T021-5: test_cap_at_20_findings
# ---------------------------------------------------------------------------


def test_cap_at_20_findings(tmp_repo: Path) -> None:
    """Results are capped at _MAX_FINDINGS regardless of how many are detected."""
    # Create a producer with 25 Optional-returning functions
    func_defs = "\n".join(
        f"def get_{i}() -> 'Optional[int]':\n    return None\n"
        for i in range(25)
    )
    producer_src = f"from typing import Optional\n\n{func_defs}"

    # Caller that does .bit_length() on each
    caller_lines = [f"    r{i} = get_{i}()\n    r{i}.bit_length()" for i in range(25)]
    caller_src = (
        "from producer import " + ", ".join(f"get_{i}" for i in range(25)) + "\n\n"
        "def use_all():\n" + "\n".join(caller_lines)
    )

    producer_path = tmp_repo / "producer.py"
    caller_path = tmp_repo / "caller.py"
    producer_path.write_text(producer_src, encoding="utf-8")
    caller_path.write_text(caller_src, encoding="utf-8")

    def fake_grep(*args, capture_output=False, text=False, check=False, timeout=None):
        class Result:
            stdout = str(caller_path) + "\n"
            returncode = 0
        return Result()

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=fake_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    assert len(findings) <= _MAX_FINDINGS


# ---------------------------------------------------------------------------
# T021-6: test_timeout_returns_empty
# ---------------------------------------------------------------------------


def test_timeout_returns_empty(tmp_repo: Path) -> None:
    """When grep times out, the function returns an empty list (no crash)."""
    import subprocess as sp

    producer_src = textwrap.dedent("""\
        from typing import Optional

        def get_value() -> Optional[int]:
            return None
    """)
    (tmp_repo / "producer.py").write_text(producer_src, encoding="utf-8")

    def timeout_grep(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="grep", timeout=5)

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=timeout_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    # Timeout in grep → no callers found → no findings (not a crash)
    assert isinstance(findings, list)
    assert len(findings) == 0


# ---------------------------------------------------------------------------
# T021-7: test_format_findings
# ---------------------------------------------------------------------------


def test_format_findings() -> None:
    """format_dataflow_findings renders the expected compact output."""
    findings = [
        {
            "file": "src/module.py",
            "line": 42,
            "kind": "optional_to_nonoptional",
            "producer": "module.parse_event",
            "consumer": "graph.process",
            "detail": "Optional[dict] used without None check",
            "severity": "warning",
        }
    ]
    output = format_dataflow_findings(findings)
    assert "[WARNING]" in output
    assert "src/module.py:42" in output
    assert "optional_to_nonoptional" in output
    assert "Optional[dict] used without None check" in output


# ---------------------------------------------------------------------------
# T021 extra: test_format_empty
# ---------------------------------------------------------------------------


def test_format_empty() -> None:
    """Empty findings list returns empty string."""
    assert format_dataflow_findings([]) == ""


# ---------------------------------------------------------------------------
# T021 extra: test_format_caps_at_1000_chars
# ---------------------------------------------------------------------------


def test_format_caps_at_1000_chars() -> None:
    """Output is bounded to 1000 characters regardless of finding count."""
    many_findings = [
        {
            "file": f"src/very/long/path/module_{i}.py",
            "line": i * 10,
            "kind": "optional_to_nonoptional",
            "producer": f"module_{i}.some_function_with_a_long_name",
            "consumer": f"other_module_{i}.another_function_with_a_long_name",
            "detail": "Optional[dict] returned but attribute accessed directly without None guard",
            "severity": "warning",
        }
        for i in range(50)
    ]
    output = format_dataflow_findings(many_findings)
    assert len(output) <= 1000


# ---------------------------------------------------------------------------
# T021 extra: test_skips_non_python_files
# ---------------------------------------------------------------------------


def test_skips_non_python_files(tmp_repo: Path) -> None:
    """Non-Python files in changed_files are ignored without error."""
    (tmp_repo / "README.md").write_text("# readme", encoding="utf-8")
    (tmp_repo / "config.yaml").write_text("key: value", encoding="utf-8")

    findings = check_cross_function_flow(
        ["README.md", "config.yaml", "package.json"],
        tmp_repo,
    )
    assert findings == []


# ---------------------------------------------------------------------------
# T021 extra: test_empty_changed_files
# ---------------------------------------------------------------------------


def test_empty_changed_files(tmp_repo: Path) -> None:
    """Empty changed_files list returns empty findings immediately."""
    findings = check_cross_function_flow([], tmp_repo)
    assert findings == []


# ---------------------------------------------------------------------------
# T021 extra: test_dataflow_finding_dataclass
# ---------------------------------------------------------------------------


def test_dataflow_finding_dataclass() -> None:
    """DataflowFinding dataclass can be instantiated with all required fields."""
    f = DataflowFinding(
        file="src/module.py",
        line=10,
        kind="optional_to_nonoptional",
        producer="module.parse",
        consumer="caller.process",
        detail="Optional used unsafely",
    )
    assert f.severity == "warning"
    assert f.kind == "optional_to_nonoptional"


# ---------------------------------------------------------------------------
# T021 extra: test_handles_missing_file_gracefully
# ---------------------------------------------------------------------------


def test_handles_missing_file_gracefully(tmp_repo: Path) -> None:
    """A changed file that doesn't exist is skipped silently."""
    findings = check_cross_function_flow(["nonexistent.py"], tmp_repo)
    assert isinstance(findings, list)
    assert len(findings) == 0


# ---------------------------------------------------------------------------
# T021 extra: test_handles_syntax_error_gracefully
# ---------------------------------------------------------------------------


def test_handles_syntax_error_gracefully(tmp_repo: Path) -> None:
    """A Python file with a syntax error is skipped and returns no findings."""
    bad_file = tmp_repo / "broken.py"
    bad_file.write_text("def foo(\n    # unclosed paren", encoding="utf-8")

    findings = check_cross_function_flow(["broken.py"], tmp_repo)
    assert isinstance(findings, list)
    assert len(findings) == 0


# ---------------------------------------------------------------------------
# T021 extra: test_union_none_optional_detected
# ---------------------------------------------------------------------------


def test_union_none_optional_detected(tmp_repo: Path) -> None:
    """X | None annotation (Python 3.10 syntax) is recognised as Optional."""
    producer_src = textwrap.dedent("""\
        def get_value() -> int | None:
            return None
    """)
    caller_src = textwrap.dedent("""\
        from producer import get_value

        def use_it():
            result = get_value()
            length = result.bit_length()
    """)

    producer_path = tmp_repo / "producer.py"
    caller_path = tmp_repo / "caller.py"
    producer_path.write_text(producer_src, encoding="utf-8")
    caller_path.write_text(caller_src, encoding="utf-8")

    def fake_grep(*args, capture_output=False, text=False, check=False, timeout=None):
        class Result:
            stdout = str(caller_path) + "\n"
            returncode = 0
        return Result()

    with patch("saturnday.run.dataflow_checker.subprocess.run", side_effect=fake_grep):
        findings = check_cross_function_flow(["producer.py"], tmp_repo)

    kinds = {f["kind"] for f in findings}
    assert "optional_to_nonoptional" in kinds
