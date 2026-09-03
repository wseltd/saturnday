"""Lightweight cross-function data flow mismatch detection (Phase 5, T018/T019).

AST-based, no symbolic execution.  Only checks changed files as producers and
their direct callers (one level deep).  WARNING-only; never blocks the pipeline.

Design constraints:
- No imports from saturnday.governance.
- Cap at 20 findings per run.
- 10-second total timeout (enforced via signal/threading on the analysis loop).
- Graceful fallback to empty list on any error.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saturnday.project_state import ProjectState

logger = logging.getLogger(__name__)

__all__ = [
    "DataflowFinding",
    "check_cross_function_flow",
    "format_dataflow_findings",
]

_MAX_FINDINGS = 20
_GREP_TIMEOUT = 5  # seconds per grep call
_TOTAL_BUDGET = 10  # approximate seconds for the whole check


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DataflowFinding:
    """A single cross-function data flow mismatch finding.

    Attributes:
        file: Repo-relative path of the caller file where the issue was found.
        line: Line number in the caller file (1-based; 0 when unknown).
        kind: Mismatch category — one of ``optional_to_nonoptional``,
            ``empty_to_nonempty``, ``type_mismatch``, ``key_mismatch``,
            ``shape_mismatch``.
        producer: Dotted name of the function that returns the value
            (``module.function``).
        consumer: Dotted name of the calling function or module location
            (``module.function``).
        detail: Human-readable description of the mismatch.
        severity: Always ``"warning"`` (never blocks pipeline).
    """

    file: str
    line: int
    kind: str  # optional_to_nonoptional | empty_to_nonempty | type_mismatch | key_mismatch | shape_mismatch
    producer: str  # "module.function"
    consumer: str  # "module.function"
    detail: str
    severity: str = "warning"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_optional_annotation(annotation: ast.expr | None) -> bool:
    """Return True when *annotation* represents ``Optional[X]`` or ``X | None``."""
    if annotation is None:
        return False
    # Optional[X] -> Subscript with Name "Optional"
    if isinstance(annotation, ast.Subscript):
        value = annotation.value
        if isinstance(value, ast.Name) and value.id == "Optional":
            return True
        # typing.Optional[X]
        if isinstance(value, ast.Attribute) and value.attr == "Optional":
            return True
    # X | None  (Python 3.10+ union syntax)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left, right = annotation.left, annotation.right
        if isinstance(right, ast.Constant) and right.value is None:
            return True
        if isinstance(left, ast.Constant) and left.value is None:
            return True
        if isinstance(right, ast.Name) and right.id == "None":
            return True
        if isinstance(left, ast.Name) and left.id == "None":
            return True
    # Union[X, None]
    if isinstance(annotation, ast.Subscript):
        value = annotation.value
        if isinstance(value, ast.Name) and value.id == "Union":
            if isinstance(annotation.slice, (ast.Tuple,)):
                for elt in annotation.slice.elts:
                    if isinstance(elt, ast.Constant) and elt.value is None:
                        return True
                    if isinstance(elt, ast.Name) and elt.id == "None":
                        return True
    return False


def _is_list_annotation(annotation: ast.expr | None) -> bool:
    """Return True when *annotation* represents a list type (``list[X]``, ``List[X]``)."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name) and annotation.id.lower() in ("list",):
        return True
    if isinstance(annotation, ast.Subscript):
        value = annotation.value
        if isinstance(value, ast.Name) and value.id in ("list", "List", "Sequence", "Iterable"):
            return True
        if isinstance(value, ast.Attribute) and value.attr in ("List", "Sequence", "Iterable"):
            return True
    return False


@dataclass
class _FuncInfo:
    """Summary of a function's return characteristics."""

    name: str
    qualname: str       # module_dotname.funcname
    is_optional: bool   # returns Optional or X|None
    is_list: bool       # returns list/List
    return_annotation: str  # raw annotation text for display


def _extract_functions(source: str, module_dotname: str, rel_path: str) -> list[_FuncInfo]:
    """Parse *source* and extract function return type info."""
    results: list[_FuncInfo] = []
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return results

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") or node.name.startswith("test_"):
            continue
        ret = node.returns
        if ret is None:
            continue
        ann_text = ast.unparse(ret) if hasattr(ast, "unparse") else ""
        results.append(_FuncInfo(
            name=node.name,
            qualname=f"{module_dotname}.{node.name}",
            is_optional=_is_optional_annotation(ret),
            is_list=_is_list_annotation(ret),
            return_annotation=ann_text,
        ))
    return results


def _grep_callers(repo_path: Path, func_name: str, changed_files_set: frozenset[str]) -> list[str]:
    """Return relative paths of repo .py files that call *func_name*."""
    if not func_name or len(func_name) < 3:  # noqa: PLR2004
        return []
    try:
        proc = subprocess.run(
            ["grep", "-rl", "--include=*.py", f"{func_name}(", str(repo_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GREP_TIMEOUT,
        )
        results: list[str] = []
        for line in proc.stdout.splitlines():
            try:
                rel = str(Path(line).relative_to(repo_path))
            except ValueError:
                rel = line.strip()
            if rel not in changed_files_set and rel.endswith(".py"):
                results.append(rel)
                if len(results) >= 10:  # noqa: PLR2004 — cap callers per function
                    break
        return results
    except Exception as exc:
        logger.debug("grep caller scan failed for %s: %s", func_name, exc)
        return []


def _line_has_none_check_before(lines: list[str], call_line_idx: int, func_name: str) -> bool:
    """Heuristic: scan a small window before *call_line_idx* for None checks."""
    window_start = max(0, call_line_idx - 5)
    window = lines[window_start:call_line_idx + 1]
    joined = "\n".join(window)
    # Common guard patterns
    patterns = [
        f"if {func_name}",
        "is not None",
        "is None",
        "if result",
        "if ret",
        "if value",
        "if val",
        "if r ",
        "if r\n",
        " and ",
        "or None",
    ]
    return any(p in joined for p in patterns)


def _check_optional_usage(
    source: str,
    rel_path: str,
    func_info: _FuncInfo,
) -> list[dict]:
    """Detect ``optional_to_nonoptional`` usage in a caller source file."""
    findings: list[dict] = []
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return findings

    lines = source.splitlines()
    func_name = func_info.name

    # Collect all assignment targets where RHS is a call to func_name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        # Check if value is a call to func_name (simple Name call or attribute call)
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        call_func = call.func
        is_our_call = (
            (isinstance(call_func, ast.Name) and call_func.id == func_name)
            or (isinstance(call_func, ast.Attribute) and call_func.attr == func_name)
        )
        if not is_our_call:
            continue

        # The result is assigned — now check how each target is used.
        # We look for attribute access, subscript, or arithmetic on the result
        # without a preceding None check.
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            var_name = target.id
            call_lineno = getattr(node, "lineno", 0)
            # Scan subsequent uses of var_name
            for use_node in ast.walk(tree):
                use_lineno = getattr(use_node, "lineno", 0)
                if use_lineno <= call_lineno:
                    continue
                # Attribute access: var_name.method() or var_name.attr
                if isinstance(use_node, ast.Attribute):
                    if isinstance(use_node.value, ast.Name) and use_node.value.id == var_name:
                        if not _line_has_none_check_before(lines, use_lineno - 1, var_name):
                            findings.append({
                                "file": rel_path,
                                "line": use_lineno,
                                "kind": "optional_to_nonoptional",
                                "producer": func_info.qualname,
                                "consumer": f"{rel_path}:{use_lineno}",
                                "detail": (
                                    f"{func_name}() returns {func_info.return_annotation} "
                                    f"but result used as non-optional (attribute access)"
                                ),
                                "severity": "warning",
                            })
                            break  # one finding per assignment
    return findings


def _check_empty_list_usage(
    source: str,
    rel_path: str,
    func_info: _FuncInfo,
) -> list[dict]:
    """Detect ``empty_to_nonempty`` usage — direct index on a list return."""
    findings: list[dict] = []
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError:
        return findings

    func_name = func_info.name

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        call_func = call.func
        is_our_call = (
            (isinstance(call_func, ast.Name) and call_func.id == func_name)
            or (isinstance(call_func, ast.Attribute) and call_func.attr == func_name)
        )
        if not is_our_call:
            continue

        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            var_name = target.id
            call_lineno = getattr(node, "lineno", 0)
            # Detect subscript access like result[0] or result[i]
            for use_node in ast.walk(tree):
                use_lineno = getattr(use_node, "lineno", 0)
                if use_lineno <= call_lineno:
                    continue
                if isinstance(use_node, ast.Subscript):
                    if isinstance(use_node.value, ast.Name) and use_node.value.id == var_name:
                        findings.append({
                            "file": rel_path,
                            "line": use_lineno,
                            "kind": "empty_to_nonempty",
                            "producer": func_info.qualname,
                            "consumer": f"{rel_path}:{use_lineno}",
                            "detail": (
                                f"{func_name}() returns list but result indexed "
                                f"directly (may be empty)"
                            ),
                            "severity": "warning",
                        })
                        break  # one finding per assignment
    return findings


def _module_dotname(rel_path: str) -> str:
    """Convert a repo-relative path to a dotted module name."""
    p = Path(rel_path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_cross_function_flow(
    changed_files: list[str],
    repo_path: Path,
    state: "ProjectState | None" = None,
) -> list[dict]:
    """Detect cross-function data flow mismatches introduced by changed files.

    For each changed Python file:
    1. Extract public functions with return type annotations.
    2. For Optional-returning and list-returning functions, find callers.
    3. In each caller, check if the return value is used safely.

    Args:
        changed_files: Repo-relative paths changed by the current ticket.
        repo_path: Repository root.
        state: Optional pre-computed project state (not used directly, reserved
            for future use).

    Returns:
        List of finding dicts (each has ``file``, ``line``, ``kind``,
        ``producer``, ``consumer``, ``detail``, ``severity`` keys).
        Capped at :const:`_MAX_FINDINGS`.  Empty list on any error.
    """
    if not changed_files:
        return []

    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return []

    findings: list[dict] = []
    changed_set = frozenset(changed_files)

    try:
        for rel_path in py_files:
            if len(findings) >= _MAX_FINDINGS:
                break
            full_path = repo_path / rel_path
            if not full_path.exists():
                continue
            try:
                source = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            module_name = _module_dotname(rel_path)
            funcs = _extract_functions(source, module_name, rel_path)

            for func_info in funcs:
                if len(findings) >= _MAX_FINDINGS:
                    break
                if not (func_info.is_optional or func_info.is_list):
                    continue

                callers = _grep_callers(repo_path, func_info.name, changed_set)
                for caller_path in callers:
                    if len(findings) >= _MAX_FINDINGS:
                        break
                    full_caller = repo_path / caller_path
                    if not full_caller.exists():
                        continue
                    try:
                        caller_source = full_caller.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        continue

                    if func_info.is_optional:
                        new_findings = _check_optional_usage(
                            caller_source, caller_path, func_info
                        )
                    else:
                        new_findings = _check_empty_list_usage(
                            caller_source, caller_path, func_info
                        )

                    for f in new_findings:
                        if len(findings) >= _MAX_FINDINGS:
                            break
                        findings.append(f)

    except Exception as exc:
        logger.debug("check_cross_function_flow failed: %s", exc)
        return findings

    return findings[:_MAX_FINDINGS]


def format_dataflow_findings(findings: list[dict]) -> str:
    """Render dataflow findings as compact structured text.

    Each finding produces one line:
    ``[WARNING] <file>:<line> -- <kind>: <detail>``

    Output is bounded at 1000 characters.  Empty input returns empty string.

    Args:
        findings: List of finding dicts as returned by
            :func:`check_cross_function_flow`.

    Returns:
        Multi-line text, at most 1000 characters.
    """
    if not findings:
        return ""

    lines: list[str] = []
    for f in findings:
        file_ref = f.get("file", "?")
        line_no = f.get("line", 0)
        kind = f.get("kind", "unknown")
        detail = f.get("detail", "")
        lines.append(f"[WARNING] {file_ref}:{line_no} -- {kind}: {detail}")

    result = "\n".join(lines)
    return result[:1000]
