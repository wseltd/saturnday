"""Property-based test generation for eligible pure functions (Phase 3).

T010: identify_eligible_targets — AST scan of changed Python files.
T011: generate_property_tests — Hypothesis-based test file generation.
T012: run_property_tests — temp-file execution with cleanup.

Design constraints:
- No imports from saturnday.governance (separation of concerns).
- Hypothesis is optional; returns empty string / SKIPPED result if absent.
- Never raises — all errors captured in result dicts or empty returns.
- WARNING-only: nothing here blocks the pipeline.
- Cap: 10 eligible targets per call, 60-second total timeout.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "identify_eligible_targets",
    "generate_property_tests",
    "run_property_tests",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TARGETS = 10

# Call names that indicate I/O or side effects — disqualify a function.
_IO_CALL_NAMES: frozenset[str] = frozenset({
    "open",
    "print",
    "input",
    "exit",
    "quit",
})

# Attribute access prefixes indicating I/O (first segment of a dotted call).
_IO_ATTR_PREFIXES: frozenset[str] = frozenset({
    "os",
    "sys",
    "subprocess",
    "requests",
    "socket",
    "urllib",
    "httpx",
    "aiohttp",
    "logging",
    "logger",
    "db",
    "session",
    "cursor",
    "conn",
    "connection",
    "redis",
    "boto",
    "gcs",
})

# Name patterns that suggest a pure-transform function.
_TRANSFORM_PREFIXES: tuple[str, ...] = (
    "parse",
    "convert",
    "validate",
    "normalize",
    "format",
    "encode",
    "decode",
    "sort",
    "filter",
    "map",
    "serialize",
    "deserialize",
    "transform",
    "sanitize",
    "clean",
    "process",
    "extract",
    "build",
    "make",
    "compute",
    "calculate",
    "render",
    "flatten",
    "merge",
)

# Hypothesis strategy mapping keyed by annotation string.
_STRATEGY_MAP: dict[str, str] = {
    "str": "st.text(max_size=50)",
    "int": "st.integers(min_value=-1000, max_value=1000)",
    "float": "st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6)",
    "bool": "st.booleans()",
    "bytes": "st.binary(max_size=50)",
}

# TypeScript I/O indicators.
_TS_IO_PATTERNS = re.compile(
    r"\b(?:fetch|axios|http|https|fs\.|process\.|console\.|require\s*\(|import\s+\*)"
)


# ---------------------------------------------------------------------------
# T010: Target identification
# ---------------------------------------------------------------------------


def identify_eligible_targets(
    changed_files: list[str],
    repo_path: Path,
) -> list[dict]:
    """Scan changed files for pure-ish functions suitable for property testing.

    For Python files, uses the AST.  For TypeScript/JavaScript files, uses
    regex-based heuristics (no TS AST available from Python).

    Args:
        changed_files: File paths relative to *repo_path* (or absolute).
        repo_path: Repository root.

    Returns:
        Up to ``_MAX_TARGETS`` eligible function dicts.  Each dict has keys:
            ``name``               — function name
            ``file``               — file path as given in *changed_files*
            ``params``             — list of ``{"name": str, "type": str}``
            ``return_annotation``  — return type string (may be empty)
            ``is_pure``            — True (always True for returned targets)
            ``line``               — line number in file
            ``language``           — "python" | "typescript"
    """
    targets: list[dict] = []

    for file_path in changed_files:
        if len(targets) >= _MAX_TARGETS:
            break
        abs_path = _resolve(file_path, repo_path)
        if not abs_path.is_file():
            continue

        suffix = abs_path.suffix.lower()
        try:
            if suffix == ".py":
                found = _scan_python_file(abs_path, file_path)
            elif suffix in (".ts", ".js", ".tsx", ".jsx"):
                found = _scan_ts_file(abs_path, file_path)
            else:
                continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("identify_eligible_targets: error scanning %s: %s", file_path, exc)
            continue

        for t in found:
            if len(targets) >= _MAX_TARGETS:
                break
            targets.append(t)

    return targets


def _resolve(file_path: str, repo_path: Path) -> Path:
    p = Path(file_path)
    if p.is_absolute():
        return p
    return repo_path / p


# ---- Python AST scanning ------------------------------------------------


def _scan_python_file(abs_path: Path, rel_path: str) -> list[dict]:
    """Return eligible function dicts from a Python source file."""
    source = abs_path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(source, filename=str(abs_path))
    except SyntaxError:
        return []

    results: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        info = _check_python_function(node, rel_path)
        if info is not None:
            results.append(info)
    return results


def _check_python_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    rel_path: str,
) -> dict | None:
    """Return an eligible function dict or None if the function is ineligible."""
    name = node.name

    # Skip test functions and dunder methods.
    if name.startswith("test_") or (name.startswith("__") and name.endswith("__")):
        return None

    # Must have a return annotation.
    if node.returns is None:
        return None

    return_annotation = ast.unparse(node.returns)

    # Skip None-returning functions.
    if return_annotation in ("None", "NoReturn"):
        return None

    # Must have at least one parameter beyond self/cls.
    params = _extract_params(node)
    if not params:
        return None

    # Body must have at least 2 meaningful lines.
    body_lines = sum(
        1 for n in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        if isinstance(n, ast.stmt) and not isinstance(n, (ast.Pass, ast.Expr))
    )
    if body_lines < 2:
        return None

    # Scan body for I/O calls — disqualify if found.
    if _has_io_calls(node):
        return None

    # Must either match transform prefix OR have at least one typed parameter.
    name_lower = name.lower()
    has_transform_prefix = any(name_lower.startswith(p) for p in _TRANSFORM_PREFIXES)
    has_typed_param = any(p.get("type") for p in params)

    if not (has_transform_prefix or has_typed_param):
        return None

    return {
        "name": name,
        "file": rel_path,
        "params": params,
        "return_annotation": return_annotation,
        "is_pure": True,
        "line": node.lineno,
        "language": "python",
    }


def _extract_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    """Extract typed parameters, skipping self/cls."""
    result: list[dict] = []
    args = node.args
    all_args = args.args + args.posonlyargs + args.kwonlyargs
    for arg in all_args:
        if arg.arg in ("self", "cls"):
            continue
        type_str = ast.unparse(arg.annotation) if arg.annotation else ""
        result.append({"name": arg.arg, "type": type_str})
    return result


def _has_io_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if the function body contains I/O or side-effect calls."""
    for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
        if isinstance(child, ast.Call):
            # Direct call: open(...), print(...)
            if isinstance(child.func, ast.Name):
                if child.func.id in _IO_CALL_NAMES:
                    return True
            # Attribute call: os.path, subprocess.run, requests.get, ...
            elif isinstance(child.func, ast.Attribute):
                if isinstance(child.func.value, ast.Name):
                    if child.func.value.id in _IO_ATTR_PREFIXES:
                        return True
        # Global assignment (mutation of module-level state).
        elif isinstance(child, ast.Global):
            return True
    return False


# ---- TypeScript/JavaScript regex scanning --------------------------------


def _scan_ts_file(abs_path: Path, rel_path: str) -> list[dict]:
    """Return eligible function dicts from a TS/JS file using regex."""
    source = abs_path.read_text(encoding="utf-8", errors="ignore")
    results: list[dict] = []

    # Pattern: function Name(params): ReturnType { ... }
    # or: const Name = (params): ReturnType => { ... }
    func_re = re.compile(
        r"(?:(?:export\s+)?(?:async\s+)?function\s+([\w$]+)\s*\(([^)]*)\)\s*:\s*([\w<>\[\]|?. ]+))"
        r"|"
        r"(?:(?:export\s+)?const\s+([\w$]+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*:\s*([\w<>\[\]|?. ]+)\s*=>)",
        re.MULTILINE,
    )

    for m in func_re.finditer(source):
        if m.group(1):
            name = m.group(1)
            params_raw = m.group(2)
            return_type = m.group(3).strip()
        else:
            name = m.group(4)
            params_raw = m.group(5)
            return_type = m.group(6).strip()

        if not name or not return_type:
            continue

        # Skip test functions and void-returning functions.
        if name.startswith("test") or return_type in ("void", "never", "Promise<void>"):
            continue

        # Estimate function body: 200 chars after match position.
        body_snippet = source[m.start():m.start() + 300]
        if _TS_IO_PATTERNS.search(body_snippet):
            continue

        params = _parse_ts_params(params_raw)
        if not params:
            continue

        name_lower = name.lower()
        has_transform_prefix = any(name_lower.startswith(p) for p in _TRANSFORM_PREFIXES)
        if not has_transform_prefix:
            continue

        results.append({
            "name": name,
            "file": rel_path,
            "params": params,
            "return_annotation": return_type,
            "is_pure": True,
            "line": source[: m.start()].count("\n") + 1,
            "language": "typescript",
        })

    return results


def _parse_ts_params(params_raw: str) -> list[dict]:
    """Parse TypeScript parameter list string into param dicts."""
    if not params_raw.strip():
        return []
    result: list[dict] = []
    for part in params_raw.split(","):
        part = part.strip()
        if not part:
            continue
        # param: Type or param?: Type
        m = re.match(r"([\w$]+)\??:\s*([\w<>\[\]|?. ]+)", part)
        if m:
            result.append({"name": m.group(1), "type": m.group(2).strip()})
        else:
            result.append({"name": part, "type": ""})
    return result


# ---------------------------------------------------------------------------
# T011: Property test generation
# ---------------------------------------------------------------------------


def generate_property_tests(targets: list[dict], repo_path: Path) -> str:
    """Generate a Hypothesis-based property test file for Python targets.

    For TypeScript targets, generation is deferred (returns empty string for
    that portion since fast-check detection is out of scope here).

    Args:
        targets: Output of :func:`identify_eligible_targets`.
        repo_path: Repository root (used to resolve import paths).

    Returns:
        A complete Python test file string, or empty string if no Python
        targets are eligible after strategy mapping.
    """
    py_targets = [t for t in targets if t.get("language") == "python"]
    if not py_targets:
        return ""

    # Collect modules to import.
    module_imports: dict[str, str] = {}  # rel_path -> module_name
    for t in py_targets:
        file_path = t.get("file", "")
        if file_path and file_path.endswith(".py") and file_path not in module_imports:
            module_name = _path_to_module(file_path)
            if module_name:
                module_imports[file_path] = module_name

    test_functions: list[str] = []
    for target in py_targets:
        generated = _generate_tests_for_target(target, module_imports)
        test_functions.extend(generated)

    if not test_functions:
        return ""

    lines: list[str] = [
        "# Auto-generated property tests — do not edit",
        "# Generated by saturnday property_tests module",
        "from hypothesis import given, settings, strategies as st",
        "import pytest",
        "",
    ]

    # Import modules.
    for file_path, module_name in module_imports.items():
        lines.append(f"try:")
        lines.append(f"    from {module_name} import *  # noqa: F401,F403")
        lines.append(f"except ImportError:")
        lines.append(f"    pass")
        lines.append("")

    lines.extend(test_functions)

    return "\n".join(lines)


def _path_to_module(file_path: str) -> str:
    """Convert rel path 'src/foo/bar.py' -> 'foo.bar'."""
    try:
        p = Path(file_path)
        parts = list(p.with_suffix("").parts)
        if parts and parts[0] in ("src", "lib"):
            parts = parts[1:]
        return ".".join(parts) if parts else ""
    except Exception:  # noqa: BLE001
        return ""


def _generate_tests_for_target(target: dict, module_imports: dict[str, str]) -> list[str]:
    """Generate property test functions for a single target.

    Returns list of function-definition strings (including blank lines).
    Skips target entirely if any parameter has an unknown type annotation.
    """
    name = target["name"]
    params = target.get("params", [])
    return_annotation = target.get("return_annotation", "")

    # Map each param to a Hypothesis strategy.
    strategies: list[str] = []
    for p in params:
        type_str = p.get("type", "")
        strat = _map_type_to_strategy(type_str)
        if strat is None:
            # Unknown type — skip this target entirely per plan spec.
            logger.debug(
                "generate_property_tests: skipping %s — unknown type %r", name, type_str
            )
            return []
        strategies.append(strat)

    if not strategies:
        return []

    given_decorator = f"@given({', '.join(strategies)})"
    param_names = [p["name"] for p in params]
    call_args = ", ".join(param_names)
    func_sig = f"def test_prop_{name}_{{prop}}({', '.join(param_names)}):"

    lines: list[str] = []

    # Property 1: No exception for valid input shape.
    lines += [
        "@settings(max_examples=50, deadline=5000)",
        given_decorator,
        func_sig.format(prop="no_exception"),
        f"    try:",
        f"        {name}({call_args})",
        f"    except Exception as exc:",
        f"        pytest.fail(f'Unexpected exception from {name}: {{exc}}')",
        "",
    ]

    # Property 2: Type invariant (only for known return types).
    return_type_class = _annotation_to_type_class(return_annotation)
    if return_type_class:
        lines += [
            "@settings(max_examples=50, deadline=5000)",
            given_decorator,
            func_sig.format(prop="return_type"),
            f"    result = {name}({call_args})",
            f"    assert isinstance(result, {return_type_class}), (",
            f"        f'Expected {return_type_class}, got {{type(result).__name__}}'",
            f"    )",
            "",
        ]

    # Property 3: Determinism — same input yields same output.
    lines += [
        "@settings(max_examples=30, deadline=5000)",
        given_decorator,
        func_sig.format(prop="determinism"),
        f"    result_a = {name}({call_args})",
        f"    result_b = {name}({call_args})",
        f"    assert result_a == result_b, 'Non-deterministic output from {name}'",
        "",
    ]

    # Property 4: Idempotence — for normalize/validate prefix functions where T -> T.
    name_lower = name.lower()
    if name_lower.startswith(("normalize", "validate", "sanitize", "clean")):
        if len(params) == 1:
            lines += [
                "@settings(max_examples=30, deadline=5000)",
                given_decorator,
                func_sig.format(prop="idempotence"),
                f"    try:",
                f"        first = {name}({call_args})",
                f"        second = {name}(first)",
                f"        assert first == second, 'Idempotence violated for {name}'",
                f"    except TypeError:",
                f"        pass  # return type not re-passable — skip idempotence",
                "",
            ]

    return lines


def _map_type_to_strategy(type_str: str) -> str | None:
    """Map a type annotation string to a Hypothesis strategy string.

    Returns None if the type is not supported (caller should skip the target).
    """
    if not type_str:
        return None

    clean = type_str.strip()

    # Direct mapping.
    if clean in _STRATEGY_MAP:
        return _STRATEGY_MAP[clean]

    # Optional[X] or X | None -> treat as the inner type.
    optional_m = re.match(r"^Optional\[(.*)\]$", clean)
    if optional_m:
        inner = optional_m.group(1).strip()
        inner_strat = _map_type_to_strategy(inner)
        if inner_strat:
            return f"st.none() | {inner_strat}"
        return None

    union_none_m = re.match(r"^(.*)\s*\|\s*None$", clean)
    if union_none_m:
        inner = union_none_m.group(1).strip()
        inner_strat = _map_type_to_strategy(inner)
        if inner_strat:
            return f"st.none() | {inner_strat}"
        return None

    # list[X] -> st.lists(strategy_for_X, max_size=10)
    list_m = re.match(r"^[Ll]ist\[(.*)\]$", clean)
    if list_m:
        inner_strat = _map_type_to_strategy(list_m.group(1).strip())
        if inner_strat:
            return f"st.lists({inner_strat}, max_size=10)"
        return None

    # dict[K, V] -> st.dictionaries(...)
    dict_m = re.match(r"^[Dd]ict\[(.+),\s*(.+)\]$", clean)
    if dict_m:
        k_strat = _map_type_to_strategy(dict_m.group(1).strip())
        v_strat = _map_type_to_strategy(dict_m.group(2).strip())
        if k_strat and v_strat:
            return f"st.dictionaries({k_strat}, {v_strat}, max_size=5)"
        return None

    # Unknown — return None to signal skip.
    return None


def _annotation_to_type_class(annotation: str) -> str | None:
    """Map a return annotation to a Python type class name for isinstance check."""
    mapping = {
        "str": "str",
        "int": "int",
        "float": "float",
        "bool": "bool",
        "bytes": "bytes",
        "dict": "dict",
        "list": "list",
        "tuple": "tuple",
        "set": "set",
    }
    clean = annotation.strip()
    # Exact match.
    if clean in mapping:
        return mapping[clean]
    # dict[...] -> dict
    if clean.lower().startswith("dict[") or clean.lower().startswith("dict "):
        return "dict"
    if clean.lower().startswith("list["):
        return "list"
    if clean.lower().startswith("tuple["):
        return "tuple"
    if clean.lower().startswith("set["):
        return "set"
    return None


# ---------------------------------------------------------------------------
# T012: Property test execution
# ---------------------------------------------------------------------------


def run_property_tests(
    test_content: str,
    repo_path: Path,
    language: str = "python",
    timeout_s: float = 60.0,
) -> list[dict]:
    """Write a temp property test file, run it, parse results, clean up.

    Args:
        test_content: Complete test file content string.
        repo_path: Repository root (temp file written here).
        language: "python" (TypeScript support not yet implemented).
        timeout_s: Total subprocess timeout in seconds.

    Returns:
        List of result dicts, each with keys:
            ``test``    — test function name
            ``passed``  — bool
            ``detail``  — output or error detail string
        On hypothesis-not-installed: ``[{"test": "SKIPPED", "passed": False, "detail": "..."}]``.
        Never raises.
    """
    if not test_content.strip():
        return []

    if language != "python":
        return [{"test": "SKIPPED", "passed": False, "detail": f"Language {language!r} not supported"}]

    # Check if Hypothesis is importable.
    if not _hypothesis_available():
        return [{"test": "SKIPPED", "passed": False, "detail": "hypothesis not installed"}]

    tmp_path = repo_path / "_saturnday_prop_test.py"
    try:
        tmp_path.write_text(test_content, encoding="utf-8")
        return _execute_property_tests(tmp_path, repo_path, timeout_s)
    except Exception as exc:  # noqa: BLE001
        logger.debug("run_property_tests: error: %s", exc)
        return [{"test": "ERROR", "passed": False, "detail": str(exc)}]
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:  # noqa: BLE001
            pass


def _hypothesis_available() -> bool:
    """Return True if hypothesis is importable in the current Python env."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import hypothesis"],
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _execute_property_tests(
    tmp_path: Path,
    repo_path: Path,
    timeout_s: float,
) -> list[dict]:
    """Run pytest on the temp property test file and parse results."""
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                str(tmp_path),
                "-x", "-q", "--tb=short",
                "--timeout=30",
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=timeout_s,
        )
        raw = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return [{"test": "TIMEOUT", "passed": False, "detail": f"Property tests timed out after {timeout_s}s"}]
    except Exception as exc:  # noqa: BLE001
        return [{"test": "ERROR", "passed": False, "detail": str(exc)}]

    return _parse_property_test_output(raw)


def _parse_property_test_output(output: str) -> list[dict]:
    """Parse pytest -q output into per-test result dicts."""
    results: list[dict] = []

    # Match lines like: "PASSED _saturnday_prop_test.py::test_prop_foo_no_exception"
    # or: "FAILED _saturnday_prop_test.py::test_prop_foo_no_exception - ..."
    for line in output.splitlines():
        m = re.search(
            r"(PASSED|FAILED|ERROR)\s+.*?::(test_\w+)",
            line,
            re.IGNORECASE,
        )
        if m:
            status = m.group(1).upper()
            test_name = m.group(2)
            passed = status == "PASSED"
            detail = line.strip() if not passed else ""
            results.append({"test": test_name, "passed": passed, "detail": detail})
            continue

        # Alternative pytest -q format: "test_name PASSED"
        m2 = re.search(r"(test_\w+)\s+(PASSED|FAILED|ERROR)", line, re.IGNORECASE)
        if m2:
            test_name = m2.group(1)
            status = m2.group(2).upper()
            passed = status == "PASSED"
            results.append({"test": test_name, "passed": passed, "detail": "" if passed else line.strip()})

    if not results:
        # Fallback: overall outcome.
        overall = "passed" in output.lower() and "failed" not in output.lower()
        results.append({"test": "overall", "passed": overall, "detail": output[:300]})

    return results
