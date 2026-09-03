"""Specification-based verification for Saturnday (Phase 2 + Phase 4).

T005: generate_spec_assertions — deterministic pattern matching on acceptance
criteria strings to produce executable assertion dicts.

T006: run_spec_assertions — executes Python assertion dicts in a temp pytest
file and returns structured pass/fail results.

T008: assertions_to_verify_cmd — converts assertion dicts to a verify_cmd
string suitable for storage in a plan.

T015: infer_spec_assertions — LLM fallback for when deterministic generation
yields no assertions.  Calls invoke_role("code_reviewer", ...) and parses
the response for assert lines.  Confidence is always 0.3.

Design constraints (from execution plan Phase 2 + Phase 4):
- No imports from saturnday.governance — separation of concerns.
- LLM calls only in infer_spec_assertions (Phase 4 fallback).
- All subprocess calls have timeouts and graceful fallback.
- Never raises: all errors are captured in result dicts.
- WARNING-only: nothing here blocks the pipeline.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from saturnday._types import TicketSpec

logger = logging.getLogger(__name__)

# invoke_role is imported at module level so it can be patched in tests.
# The try/except guard avoids circular-import issues if this module is
# loaded before role_modes is fully initialised.
try:
    from saturnday.role_modes import invoke_role  # noqa: F401
except ImportError:  # pragma: no cover
    invoke_role = None  # type: ignore[assignment]

__all__ = [
    "generate_spec_assertions",
    "infer_spec_assertions",
    "run_spec_assertions",
    "assertions_to_verify_cmd",
]

# ---------------------------------------------------------------------------
# Criterion pattern matching
# ---------------------------------------------------------------------------

# Patterns we recognise and the assertion template strings they produce.
# Key: compiled regex, Value: handler function name (dispatched below).

# "returns non-empty" or "return non-empty"
_RE_RETURNS_NON_EMPTY = re.compile(
    r"\breturns?\s+non[- ]?empty\b",
    re.IGNORECASE,
)

# "returns dict with key X" or "return dict with key X"
_RE_RETURNS_DICT_KEY = re.compile(
    r"\breturns?\s+dict\s+with\s+key\s+([\w\"\']+)",
    re.IGNORECASE,
)

# "does not raise" or "does not raise an exception"
_RE_DOES_NOT_RAISE = re.compile(
    r"\bdoes\s+not\s+raise\b",
    re.IGNORECASE,
)

# "output type is X" or "returns type X"
_RE_OUTPUT_TYPE = re.compile(
    r"\b(?:output\s+type\s+is|returns?\s+type)\s+([\w]+)",
    re.IGNORECASE,
)

# "preserves ordering" or "preserve order"
_RE_PRESERVES_ORDERING = re.compile(
    r"\bpreserves?\s+order(?:ing)?\b",
    re.IGNORECASE,
)

# Structural patterns already handled by contract_checker — skip these.
# "function X exists" / "class X exists" / "file X exists" / "test X exists"
_RE_STRUCTURAL = re.compile(
    r"\b(?:function|class|file|test)\s+\S+\s+exists\b",
    re.IGNORECASE,
)


def _is_structural(criterion: str) -> bool:
    """Return True if criterion is already handled by contract_checker."""
    return bool(_RE_STRUCTURAL.search(criterion))


def _extract_function_name(criterion: str) -> str | None:
    """Try to extract a function name from a criterion string.

    Looks for patterns like "function foo", "foo() returns", "foo returns".
    Returns the function name string or None if not found.
    """
    # "function <name>" pattern
    m = re.search(r"\bfunction\s+([\w]+)", criterion, re.IGNORECASE)
    if m:
        return m.group(1)
    # "<name>() returns" or "<name> returns" — leading word before "returns"
    m = re.search(r"\b([\w]+)\s*\(\s*\)\s+returns?", criterion, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b([\w]+)\s+returns?", criterion, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def generate_spec_assertions(
    ticket: "TicketSpec",
    changed_files: list[str],
    repo_path: Path,
) -> list[dict]:
    """Generate executable assertion dicts from ticket acceptance criteria.

    Performs deterministic pattern matching only — no I/O beyond reading
    acceptance_criteria strings, no LLM calls.

    Args:
        ticket: The ticket whose acceptance_criteria to parse.
        changed_files: Files changed by the ticket (used to infer import paths).
        repo_path: Absolute path to the repository root (unused currently,
            reserved for future file-content based heuristics).

    Returns:
        List of assertion dicts. Each dict has keys:
            ``criterion``      — original criterion string
            ``assertion``      — the assert statement string
            ``file``           — target file (may be empty string if unknown)
            ``language``       — ``"python"`` or ``"typescript"``
            ``confidence``     — float in [0.0, 1.0]; 1.0 = exact match
        Returns empty list when no patterns match or on any error.
    """
    results: list[dict] = []
    try:
        for criterion in ticket.acceptance_criteria:
            if not isinstance(criterion, str) or not criterion.strip():
                continue
            _parse_one_criterion(criterion, changed_files, results)
    except Exception as exc:  # noqa: BLE001
        logger.debug("generate_spec_assertions: unexpected error: %s", exc)
    return results


def _parse_one_criterion(
    criterion: str,
    changed_files: list[str],
    out: list[dict],
) -> None:
    """Parse a single criterion string and append assertion dicts to *out*."""
    # Skip structural patterns — contract_checker owns these.
    if _is_structural(criterion):
        return

    # Pick the first Python file from changed_files as import target.
    py_file = next(
        (f for f in changed_files if f.endswith(".py")),
        "",
    )

    func_name = _extract_function_name(criterion)

    # Pattern 1: "returns non-empty"
    if _RE_RETURNS_NON_EMPTY.search(criterion) and func_name:
        out.append({
            "criterion": criterion,
            "assertion": f"assert len({func_name}(None)) > 0",
            "file": py_file,
            "language": "python",
            "confidence": 1.0,
        })
        return

    # Pattern 2: "returns dict with key X"
    m2 = _RE_RETURNS_DICT_KEY.search(criterion)
    if m2 and func_name:
        key = m2.group(1).strip("\"'")
        out.append({
            "criterion": criterion,
            "assertion": f'assert "{key}" in {func_name}(None)',
            "file": py_file,
            "language": "python",
            "confidence": 1.0,
        })
        return

    # Pattern 3: "does not raise"
    if _RE_DOES_NOT_RAISE.search(criterion) and func_name:
        out.append({
            "criterion": criterion,
            "assertion": (
                f"try:\n    {func_name}(None)\nexcept Exception:\n    assert False, 'unexpected exception'"
            ),
            "file": py_file,
            "language": "python",
            "confidence": 1.0,
        })
        return

    # Pattern 4: "output type is X" / "returns type X"
    m4 = _RE_OUTPUT_TYPE.search(criterion)
    if m4 and func_name:
        type_name = m4.group(1)
        out.append({
            "criterion": criterion,
            "assertion": f"assert isinstance({func_name}(None), {type_name})",
            "file": py_file,
            "language": "python",
            "confidence": 1.0,
        })
        return

    # Pattern 5: "preserves ordering"
    if _RE_PRESERVES_ORDERING.search(criterion) and func_name:
        out.append({
            "criterion": criterion,
            "assertion": (
                f"_in = [3, 1, 2]; _out = {func_name}(_in); "
                f"assert _out == sorted(_out)"
            ),
            "file": py_file,
            "language": "python",
            "confidence": 0.5,
        })
        return

    # No pattern matched — skip (don't force an assertion).


# ---------------------------------------------------------------------------
# Assertion execution
# ---------------------------------------------------------------------------

def run_spec_assertions(
    assertions: list[dict],
    repo_path: Path,
    timeout_s: float = 15.0,
) -> list[dict]:
    """Execute assertion dicts and return pass/fail results.

    Writes a temporary pytest file under *repo_path*, runs it, parses the
    output, and cleans up.  Never raises.

    Args:
        assertions: Output of :func:`generate_spec_assertions`.
        repo_path: Repository root (temp file written here).
        timeout_s: Per-run subprocess timeout in seconds.

    Returns:
        List of result dicts with keys:
            ``criterion``  — original criterion string
            ``assertion``  — the assertion code
            ``passed``     — bool
            ``output``     — combined stdout/stderr from the run
            ``error``      — error message string or ``None``
    """
    if not assertions:
        return []

    py_assertions = [a for a in assertions if a.get("language") == "python"]
    results: list[dict] = []

    if py_assertions:
        _py_results = _run_python_assertions(py_assertions, repo_path, timeout_s)
        results.extend(_py_results)

    # TypeScript assertions: currently no TS execution support — mark skipped.
    ts_assertions = [a for a in assertions if a.get("language") in ("typescript", "ts")]
    for a in ts_assertions:
        results.append({
            "criterion": a.get("criterion", ""),
            "assertion": a.get("assertion", ""),
            "passed": False,
            "output": "",
            "error": "TypeScript assertion execution not yet supported",
        })

    return results


def _run_python_assertions(
    assertions: list[dict],
    repo_path: Path,
    timeout_s: float,
) -> list[dict]:
    """Write a temp pytest file, run it, parse results, clean up."""
    tmp_path = repo_path / "_saturnday_spec_test.py"
    results: list[dict] = []

    try:
        content = _build_pytest_file(assertions)
        tmp_path.write_text(content, encoding="utf-8")

        try:
            proc = subprocess.run(
                ["python", "-m", "pytest", str(tmp_path), "-v", "--tb=short", "--no-header", "-q"],
                capture_output=True,
                text=True,
                cwd=str(repo_path),
                timeout=timeout_s,
            )
            raw_output = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            raw_output = ""
            for a in assertions:
                results.append({
                    "criterion": a.get("criterion", ""),
                    "assertion": a.get("assertion", ""),
                    "passed": False,
                    "output": "",
                    "error": f"Assertion execution timed out after {timeout_s}s",
                })
            return results
        except Exception as exc:  # noqa: BLE001
            for a in assertions:
                results.append({
                    "criterion": a.get("criterion", ""),
                    "assertion": a.get("assertion", ""),
                    "passed": False,
                    "output": "",
                    "error": f"Subprocess error: {exc}",
                })
            return results

        results = _parse_pytest_output(assertions, raw_output)

    except Exception as exc:  # noqa: BLE001
        logger.debug("_run_python_assertions: error: %s", exc)
        for a in assertions:
            results.append({
                "criterion": a.get("criterion", ""),
                "assertion": a.get("assertion", ""),
                "passed": False,
                "output": "",
                "error": str(exc),
            })
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:  # noqa: BLE001
            pass

    return results


def _build_pytest_file(assertions: list[dict]) -> str:
    """Build a pytest file string from assertion dicts.

    Each assertion becomes a test function ``test_spec_N``.
    Import lines are generated from the ``file`` field of each assertion.
    """
    lines: list[str] = ["# Auto-generated by spec_verifier — do not edit", "import pytest", ""]

    # Collect unique Python files to import
    imported_modules: set[str] = set()
    for a in assertions:
        file_path = a.get("file", "")
        if file_path and file_path.endswith(".py"):
            module = _path_to_module(file_path)
            if module and module not in imported_modules:
                lines.append(f"try:")
                lines.append(f"    import {module}")
                lines.append(f"    from {module} import *  # noqa: F401,F403")
                lines.append(f"except ImportError:")
                lines.append(f"    pass")
                lines.append("")
                imported_modules.add(module)

    for i, a in enumerate(assertions):
        assertion_code = a.get("assertion", "assert True")
        # Indent assertion body for function
        indented = "\n".join(f"    {line}" for line in assertion_code.splitlines())
        lines.append(f"def test_spec_{i}():")
        lines.append(indented)
        lines.append("")

    return "\n".join(lines)


def _path_to_module(file_path: str) -> str:
    """Convert a file path like 'src/foo/bar.py' to a module string 'foo.bar'.

    Strips common source prefixes (src/, lib/) and the .py suffix.
    Returns empty string if conversion fails.
    """
    try:
        p = Path(file_path)
        parts = p.with_suffix("").parts
        # Strip leading 'src' or 'lib' component
        if parts and parts[0] in ("src", "lib"):
            parts = parts[1:]
        return ".".join(parts) if parts else ""
    except Exception:  # noqa: BLE001
        return ""


def _parse_pytest_output(assertions: list[dict], output: str) -> list[dict]:
    """Parse pytest -v output to determine pass/fail per test.

    Maps ``test_spec_N`` results back to the N-th assertion.
    Falls back to overall exit-code interpretation if per-test parsing fails.
    """
    results: list[dict] = []

    # Build a map: test index → PASSED/FAILED
    test_results: dict[int, bool] = {}
    for line in output.splitlines():
        # Lines like: "test_spec_0 PASSED" or "PASSED _saturnday_spec_test.py::test_spec_0"
        m = re.search(r"test_spec_(\d+)\s+(PASSED|FAILED|ERROR)", line, re.IGNORECASE)
        if m:
            idx = int(m.group(1))
            test_results[idx] = m.group(2).upper() == "PASSED"

    overall_pass = "failed" not in output.lower() and "error" not in output.lower()

    for i, a in enumerate(assertions):
        if i in test_results:
            passed = test_results[i]
        else:
            # Fallback: if overall run succeeded with no parse, assume pass
            passed = overall_pass
        results.append({
            "criterion": a.get("criterion", ""),
            "assertion": a.get("assertion", ""),
            "passed": passed,
            "output": output[:500] if not passed else "",
            "error": None,
        })

    return results


# ---------------------------------------------------------------------------
# T015: LLM-assisted assertion inference (Phase 4 fallback)
# ---------------------------------------------------------------------------

_MIN_GOAL_LENGTH = 20  # goals shorter than this are too vague to infer from
_LLM_CONFIDENCE = 0.3  # lower than deterministic (1.0 / 0.5)

# Read at most this many characters per changed file when building the prompt.
_FILE_READ_LIMIT = 2000


def infer_spec_assertions(
    ticket: "TicketSpec",
    changed_files: list[str],
    repo_path: Path,
    coder_config: Any,
) -> list[dict]:
    """Infer executable assertion dicts from ticket goal + code via LLM.

    This is a FALLBACK — only meaningful when ``generate_spec_assertions``
    returns an empty list.  The caller is responsible for checking that
    condition before calling this function.

    Calls ``invoke_role("code_reviewer", ...)`` and parses lines that start
    with ``assert `` from the response.  All returned assertions have
    ``confidence=0.3``.

    Args:
        ticket: The ticket whose goal and acceptance_criteria to include in
            the prompt.
        changed_files: Files changed by the ticket (first 2 000 chars each
            are included in the prompt context).
        repo_path: Absolute path to the repository root.
        coder_config: Backend configuration forwarded to ``invoke_role``.

    Returns:
        List of assertion dicts (same schema as ``generate_spec_assertions``)
        with ``confidence=0.3``.  Returns empty list on any error or when
        the ticket goal is too short to be useful.
    """
    try:
        # Guard: skip if goal is too vague.
        goal = getattr(ticket, "goal", "") or ""
        if len(goal.strip()) <= _MIN_GOAL_LENGTH:
            logger.debug(
                "infer_spec_assertions: goal too short (%d chars) — skipping",
                len(goal.strip()),
            )
            return []

        # Build file context snippets.
        file_snippets: list[str] = []
        for fpath in changed_files[:5]:  # cap at 5 files to bound prompt size
            try:
                full_path = Path(fpath) if Path(fpath).is_absolute() else repo_path / fpath
                content = full_path.read_text(encoding="utf-8", errors="replace")
                snippet = content[:_FILE_READ_LIMIT]
                file_snippets.append(f"### {fpath}\n```python\n{snippet}\n```")
            except Exception as _read_exc:  # noqa: BLE001
                logger.debug("infer_spec_assertions: could not read %s: %s", fpath, _read_exc)

        criteria_text = "\n".join(
            f"- {c}" for c in (getattr(ticket, "acceptance_criteria", ()) or ())
        )

        task = (
            "Given the following ticket goal and code, generate 3-5 EXECUTABLE Python "
            "assert statements that verify the code does what the goal says.\n"
            "Output ONLY assert statements, one per line. "
            "Each must be self-contained and import what it needs. "
            "No explanation. No prose. No markdown fences.\n\n"
            f"## Ticket goal\n{goal}\n\n"
            + (f"## Acceptance criteria\n{criteria_text}\n\n" if criteria_text else "")
            + ("## Changed files\n" + "\n\n".join(file_snippets) if file_snippets else "")
        )

        if invoke_role is None:
            logger.debug("infer_spec_assertions: role_modes unavailable — skipping")
            return []

        result = invoke_role(
            "code_reviewer",
            task,
            coder_config=coder_config,
            repo_path=repo_path,
        )

        if not result.success or not result.output:
            logger.debug("infer_spec_assertions: LLM call unsuccessful or empty output")
            return []

        return _parse_llm_assertions(result.output, changed_files)

    except Exception as exc:  # noqa: BLE001
        logger.debug("infer_spec_assertions: error — %s", exc)
        return []


def _parse_llm_assertions(llm_output: str, changed_files: list[str]) -> list[dict]:
    """Extract assert lines from LLM output and wrap in assertion dicts.

    Accepts lines starting with ``assert `` (with optional leading whitespace
    and optional ``from ... import ...`` prefix lines).  Import lines
    immediately preceding an assert are collected and prepended to the
    assertion code so the assertion can be self-contained.

    Args:
        llm_output: Raw text response from the LLM.
        changed_files: Used to populate the ``file`` field.

    Returns:
        List of assertion dicts with ``confidence=0.3``.
    """
    py_file = next((f for f in changed_files if f.endswith(".py")), "")
    results: list[dict] = []
    pending_imports: list[str] = []

    for raw_line in llm_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            pending_imports = []  # reset import accumulator on blank/comment
            continue

        # Accumulate import lines that precede an assert.
        if line.startswith("from ") or line.startswith("import "):
            pending_imports.append(line)
            continue

        if line.startswith("assert "):
            # Combine any accumulated imports with the assert line.
            if pending_imports:
                assertion_code = "\n".join(pending_imports) + "\n" + line
                pending_imports = []
            else:
                assertion_code = line
            results.append({
                "criterion": "(LLM inferred)",
                "assertion": assertion_code,
                "file": py_file,
                "language": "python",
                "confidence": _LLM_CONFIDENCE,
            })
            continue

        # Non-assert, non-import line — reset import accumulator.
        pending_imports = []

    return results


# ---------------------------------------------------------------------------
# T008: Convert assertions to verify_cmd
# ---------------------------------------------------------------------------

def assertions_to_verify_cmd(assertions: list[dict]) -> str:
    """Convert a list of assertion dicts into a verify_cmd shell string.

    Produces a single ``python -c "..."`` command that runs all Python
    assertions inline.  Suitable for storage in a plan.json ``verify_cmd``
    field.

    Args:
        assertions: Output of :func:`generate_spec_assertions`.

    Returns:
        A shell command string, or empty string if no Python assertions exist.
    """
    py_assertions = [
        a for a in assertions
        if a.get("language") == "python" and a.get("assertion")
    ]
    if not py_assertions:
        return ""

    # Build a single-line Python snippet that runs all assertions
    parts: list[str] = []
    for a in py_assertions:
        # Collapse multi-line assertions to semicolons for inline use
        inline = "; ".join(
            line.strip() for line in a["assertion"].splitlines() if line.strip()
        )
        parts.append(inline)

    combined = "; ".join(parts)
    # Escape for shell single-quote context
    escaped = combined.replace("'", r"'\''")
    return f"python -c '{escaped}'"
