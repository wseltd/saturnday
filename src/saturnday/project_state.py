"""Track what has been built so far, so subsequent tickets get full context.

Simplified from saturnday-v3's ``project_state.py``.  Maintains a JSON
state file (``.saturnday/state.json``) in the repo.  After each
ticket completes, the changed files are AST-scanned and the state is
updated.  ``generate_context_summary()`` produces the text block that is
injected into the coder prompt for the next ticket.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "saturnday-state.json"  # legacy — kept for migration
STATE_DIR_FILENAME = "state.json"  # new location under .saturnday/
SCHEMA_VERSION = "1.0.0"

_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", "runs",
    "dist", "build", ".saturnday", ".pytest_cache",
})


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FunctionState:
    """Tracked state of a single public function."""

    name: str
    params: list[str] = field(default_factory=list)
    return_type: str = ""
    line: int = 0


@dataclass
class ClassState:
    """Tracked state of a single public class."""

    name: str
    methods: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    line: int = 0


@dataclass
class ModuleState:
    """Tracked state of a single Python module."""

    created_by: str = ""
    functions: dict[str, FunctionState] = field(default_factory=dict)
    classes: dict[str, ClassState] = field(default_factory=dict)


@dataclass
class ProjectState:
    """Mutable project state, updated after each ticket."""

    schema_version: str = SCHEMA_VERSION
    project_id: str = ""
    last_updated: str = ""
    modules: dict[str, ModuleState] = field(default_factory=dict)
    tests: dict[str, list[str]] = field(default_factory=dict)
    dependencies: dict[str, dict[str, str]] = field(default_factory=dict)
    decisions: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AST scanning
# ---------------------------------------------------------------------------

def _parse_file(path: Path) -> ast.Module | None:
    """Attempt to parse a Python file. Returns None on failure."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source, filename=str(path))
    except Exception:
        return None


def _scan_module(path: Path) -> ModuleState:
    """Scan a single Python file and return its ``ModuleState``."""
    ms = ModuleState()
    tree = _parse_file(path)
    if tree is None:
        return ms

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            params: list[str] = []
            try:
                for arg in node.args.args:
                    params.append(arg.arg)
            except Exception:
                pass
            ret = ""
            if node.returns:
                try:
                    ret = ast.unparse(node.returns)
                except Exception:
                    pass
            ms.functions[node.name] = FunctionState(
                name=node.name, params=params, return_type=ret, line=node.lineno,
            )
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            methods: list[str] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(item.name)
            bases: list[str] = []
            for base in node.bases:
                try:
                    bases.append(ast.unparse(base))
                except Exception:
                    pass
            ms.classes[node.name] = ClassState(
                name=node.name, methods=methods, bases=bases, line=node.lineno,
            )
    return ms


def _scan_test_file(path: Path) -> list[str]:
    """Return test function names from a test file."""
    tree = _parse_file(path)
    if tree is None:
        return []
    tests: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                tests.append(node.name)
    return tests


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def scan_changed_files(
    repo_path: Path,
    changed_files: list[str],
    ticket_id: str,
) -> dict[str, ModuleState]:
    """Scan changed Python files and return their module states.

    Args:
        repo_path: Repository root.
        changed_files: Relative paths of files that changed.
        ticket_id: The ticket that made the changes.

    Returns:
        Dict mapping relative path to ``ModuleState``.
    """
    result: dict[str, ModuleState] = {}
    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        full_path = repo_path / rel_path
        if not full_path.exists():
            continue
        if any(part in _SKIP_DIRS for part in Path(rel_path).parts):
            continue
        ms = _scan_module(full_path)
        ms.created_by = ms.created_by or ticket_id
        result[rel_path] = ms
    return result


def update_state(
    state: ProjectState,
    ticket_id: str,
    changed_files: list[str],
    repo_path: Path,
) -> ProjectState:
    """Update project state after a ticket completes successfully.

    Args:
        state: Current project state (mutated in place).
        ticket_id: The completing ticket.
        changed_files: Relative paths of files changed by the ticket.
        repo_path: Repository root.

    Returns:
        The updated state (same object, mutated).
    """
    state.last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        full_path = repo_path / rel_path
        if not full_path.exists():
            state.modules.pop(rel_path, None)
            state.tests.pop(rel_path, None)
            continue
        if any(part in _SKIP_DIRS for part in Path(rel_path).parts):
            continue

        ms = _scan_module(full_path)
        existing = state.modules.get(rel_path)
        if existing:
            ms.created_by = existing.created_by
        else:
            ms.created_by = ticket_id
        state.modules[rel_path] = ms

        if "test" in rel_path:
            test_funcs = _scan_test_file(full_path)
            if test_funcs:
                state.tests[rel_path] = test_funcs

    return state


def _full_module_detail(path: str, ms: "ModuleState") -> str:
    """Full function signatures and classes for a module."""
    parts: list[str] = []
    for fname, fstate in sorted(ms.functions.items()):
        sig = f"{fname}({', '.join(fstate.params)})"
        if fstate.return_type:
            sig += f" -> {fstate.return_type}"
        parts.append(sig)
    for cname, cstate in sorted(ms.classes.items()):
        method_str = ""
        if cstate.methods:
            method_str = f" with {', '.join(cstate.methods)}"
        parts.append(f"class {cname}{method_str}")
    tag = f"  [{ms.created_by}]" if ms.created_by else ""
    if parts:
        return f"- {path}: {', '.join(parts)}{tag}"
    return f"- {path}: (empty){tag}"


def _compressed_module_detail(path: str, ms: "ModuleState") -> str:
    """Compressed summary: filename + counts only."""
    n_funcs = len(ms.functions)
    n_classes = len(ms.classes)
    tag = f" [{ms.created_by}]" if ms.created_by else ""
    parts = []
    if n_funcs:
        parts.append(f"{n_funcs} function{'s' if n_funcs != 1 else ''}")
    if n_classes:
        parts.append(f"{n_classes} class{'es' if n_classes != 1 else ''}")
    summary = ", ".join(parts) if parts else "empty"
    return f"- {path}: {summary}{tag}"


def _hard_truncate(result: str, max_chars: int) -> str:
    """Hard-truncate ``result`` to ``max_chars`` by removing trailing module
    lines and appending a count message.

    Lines beginning with ``"- "`` that are not section headers are treated as
    removable module entries.  The footer line starting with ``"DO NOT"`` is
    always preserved.

    Args:
        result: Assembled summary string that exceeds ``max_chars``.
        max_chars: Maximum character budget.

    Returns:
        Truncated string guaranteed to be ``<= max_chars`` characters.
    """
    if len(result) <= max_chars:
        return result

    result_lines = result.split("\n")
    # Separate the mandatory footer so it is never dropped.
    footer_idx: int | None = None
    for i in range(len(result_lines) - 1, -1, -1):
        if result_lines[i].startswith("DO NOT"):
            footer_idx = i
            break

    removed_count = 0
    # Build the truncation suffix early so we know its length.
    # We will update it as we remove lines.
    suffix_template = "... and {n} more modules (truncated)"

    while len("\n".join(result_lines)) > max_chars:
        # Walk backward to find the last removable module entry.
        removed = False
        start = len(result_lines) - 1
        if footer_idx is not None:
            start = footer_idx - 1

        for i in range(start, -1, -1):
            line = result_lines[i]
            if line.startswith("- "):
                result_lines.pop(i)
                removed_count += 1
                if footer_idx is not None and i < footer_idx:
                    footer_idx -= 1
                removed = True
                break

        if not removed:
            # Nothing removable left; hard-clip the raw string as last resort.
            suffix = suffix_template.format(n=removed_count)
            clip_at = max_chars - len(suffix)
            if clip_at < 0:
                clip_at = 0
            return result[:clip_at] + suffix

    if removed_count:
        suffix = suffix_template.format(n=removed_count)
        # Insert the suffix just before the footer, or at the end.
        if footer_idx is not None:
            result_lines.insert(footer_idx, suffix)
        else:
            result_lines.append(suffix)
        candidate = "\n".join(result_lines)
        # The suffix itself may push us over; if so, clip the raw string.
        if len(candidate) > max_chars:
            clip_at = max_chars - len(suffix) - 1
            if clip_at < 0:
                clip_at = 0
            return result[:clip_at] + "\n" + suffix
        return candidate

    return "\n".join(result_lines)


def write_context_file(
    state: ProjectState,
    output_path: Path,
    relevant_globs: tuple[str, ...] = ("**",),
    recent_ticket_ids: tuple[str, ...] = (),
) -> None:
    """Write the full (uncapped) context summary to ``output_path``.

    CLI backends read this file directly rather than receiving the summary
    inline, so no character cap is applied.  The file is written atomically
    by producing the content first and then writing in a single call.

    Args:
        state: Current project state.
        output_path: Destination file path (created if absent).
        relevant_globs: Glob patterns for files the current ticket will modify.
        recent_ticket_ids: Ticket IDs whose files get full detail.
    """
    # generate_context_summary enforces max_chars; bypass by using a limit
    # large enough that it will never trigger in practice.
    content = generate_context_summary(
        state,
        relevant_globs=relevant_globs,
        recent_ticket_ids=recent_ticket_ids,
        max_chars=10_000_000,
    )
    output_path.write_text(content, encoding="utf-8")
    logger.debug("Wrote context file to %s (%d chars)", output_path, len(content))


def generate_context_summary(
    state: ProjectState,
    relevant_globs: tuple[str, ...] = ("**",),
    recent_ticket_ids: tuple[str, ...] = (),
    max_chars: int = 8000,
) -> str:
    """Produce the text block injected into every coder prompt.

    Describes existing modules, tests, and dependencies so the coder
    knows what already exists and must not break.

    Uses three tiers of detail to stay within ``max_chars``:
    1. Full detail for files matching ``relevant_globs`` (current ticket's scope)
    2. Full detail for files created by tickets in ``recent_ticket_ids``
    3. Compressed summary (filename + counts) for everything else

    Budget-aware assembly strategy:
    - Tracks accumulated character count as sections are built.
    - When 75 % of the budget is consumed, remaining modules switch to
      compressed (one-line) format.
    - After assembly, if the result still exceeds ``max_chars``,
      ``_hard_truncate`` removes trailing module lines and appends a count
      message.  The cap is always enforced regardless of code path.

    Args:
        state: Current project state.
        relevant_globs: Glob patterns for files the current ticket will modify.
        recent_ticket_ids: Ticket IDs whose files get full detail.
        max_chars: Maximum character budget for the summary.

    Returns:
        Context summary string, capped at ``max_chars`` characters.
    """
    import fnmatch

    # Budget threshold at which we switch to compressed format.
    _COMPRESS_THRESHOLD = 0.75

    lines: list[str] = []
    lines.append("PROJECT STATE (do not contradict or remove anything listed here):")
    lines.append("")

    # Pre-compute the mandatory footer so we can reserve its size.
    footer = (
        "DO NOT remove, rename, or change the signature of any existing "
        "function unless this ticket explicitly says to."
    )
    # Reserve space for footer + surrounding newlines.
    footer_reserved = len(footer) + 2

    def _current_chars() -> int:
        return sum(len(ln) + 1 for ln in lines)

    def _budget_remaining() -> int:
        return max_chars - footer_reserved - _current_chars()

    def _append_module(path: str, ms: ModuleState, *, force_compressed: bool = False) -> None:
        """Append a module line, choosing detail level based on remaining budget."""
        compress_budget = max_chars * _COMPRESS_THRESHOLD
        if force_compressed or _current_chars() >= compress_budget:
            lines.append(_compressed_module_detail(path, ms))
        else:
            lines.append(_full_module_detail(path, ms))

    if state.modules:
        if relevant_globs == ("**",) and not recent_ticket_ids:
            # Default path: all modules, budget-aware detail selection.
            for path, ms in sorted(state.modules.items()):
                _append_module(path, ms)
        else:
            # Categorise modules into tiers.
            relevant_modules: list[tuple[str, ModuleState]] = []
            recent_modules: list[tuple[str, ModuleState]] = []
            other_modules: list[tuple[str, ModuleState]] = []

            for path, ms in sorted(state.modules.items()):
                is_relevant = any(fnmatch.fnmatch(path, g) for g in relevant_globs)
                is_recent = ms.created_by in recent_ticket_ids if ms.created_by else False

                if is_relevant:
                    relevant_modules.append((path, ms))
                elif is_recent:
                    recent_modules.append((path, ms))
                else:
                    other_modules.append((path, ms))

            if relevant_modules:
                lines.append("Relevant to this ticket (full detail):")
                for path, ms in relevant_modules:
                    _append_module(path, ms)
                lines.append("")

            if recent_modules:
                lines.append("Recently created (full detail):")
                for path, ms in recent_modules:
                    _append_module(path, ms)
                lines.append("")

            if other_modules:
                lines.append(f"Other modules ({len(other_modules)}):")
                for path, ms in other_modules:
                    _append_module(path, ms, force_compressed=True)

        lines.append("")

    total_tests = sum(len(ts) for ts in state.tests.values())
    if total_tests > 0:
        lines.append(f"Existing tests: {total_tests} across {len(state.tests)} test files")
        lines.append("")

    if state.dependencies:
        lines.append("Dependencies:")
        for pkg, info in sorted(state.dependencies.items()):
            ver = info.get("version", "")
            lines.append(f"- {pkg}{ver}")
        lines.append("")

    lines.append(footer)

    result = "\n".join(lines)

    # Final hard cap — enforced regardless of code path.
    if len(result) > max_chars:
        result = _hard_truncate(result, max_chars)

    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _module_state_to_dict(ms: ModuleState) -> dict[str, Any]:
    """Serialize a ``ModuleState`` to a JSON-compatible dict."""
    return {
        "created_by": ms.created_by,
        "functions": {
            name: {
                "name": f.name,
                "params": f.params,
                "return_type": f.return_type,
                "line": f.line,
            }
            for name, f in ms.functions.items()
        },
        "classes": {
            name: {
                "name": c.name,
                "methods": c.methods,
                "bases": c.bases,
                "line": c.line,
            }
            for name, c in ms.classes.items()
        },
    }


def _module_state_from_dict(d: dict[str, Any]) -> ModuleState:
    """Deserialize a ``ModuleState`` from a dict."""
    ms = ModuleState(created_by=d.get("created_by", ""))
    for name, fd in d.get("functions", {}).items():
        ms.functions[name] = FunctionState(
            name=fd.get("name", name),
            params=fd.get("params", []),
            return_type=fd.get("return_type", ""),
            line=fd.get("line", 0),
        )
    for name, cd in d.get("classes", {}).items():
        ms.classes[name] = ClassState(
            name=cd.get("name", name),
            methods=cd.get("methods", []),
            bases=cd.get("bases", []),
            line=cd.get("line", 0),
        )
    return ms


def save_state(state: ProjectState, repo_path: Path) -> None:
    """Save project state to ``.saturnday/state.json``.

    Writes to the new contained location under ``.saturnday/``.  The legacy
    ``saturnday-state.json`` in repo root is removed if present.
    """
    data: dict[str, Any] = {
        "schema_version": state.schema_version,
        "project_id": state.project_id,
        "last_updated": state.last_updated,
        "modules": {
            path: _module_state_to_dict(ms) for path, ms in state.modules.items()
        },
        "tests": state.tests,
        "dependencies": state.dependencies,
        "decisions": state.decisions,
    }
    saturnday_dir = repo_path / ".saturnday"
    saturnday_dir.mkdir(parents=True, exist_ok=True)
    new_path = saturnday_dir / STATE_DIR_FILENAME
    new_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("Saved project state to %s", new_path)
    # Clean up legacy location
    legacy_path = repo_path / STATE_FILENAME
    if legacy_path.exists():
        try:
            legacy_path.unlink()
            logger.info("Removed legacy state file %s", legacy_path)
        except OSError:
            pass


def load_state(repo_path: Path) -> ProjectState | None:
    """Load project state from ``.saturnday/state.json``.

    Falls back to the legacy ``saturnday-state.json`` in repo root for
    backward compatibility with existing runs.

    Returns:
        The loaded state, or ``None`` if the file does not exist or is invalid.
    """
    # Try new location first
    new_path = repo_path / ".saturnday" / STATE_DIR_FILENAME
    path = new_path if new_path.exists() else repo_path / STATE_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        logger.warning("Failed to load project state from %s", path)
        return None
    if not isinstance(data, dict):
        return None

    state = ProjectState(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        project_id=data.get("project_id", ""),
        last_updated=data.get("last_updated", ""),
    )
    for path_str, ms_dict in data.get("modules", {}).items():
        if isinstance(ms_dict, dict):
            state.modules[path_str] = _module_state_from_dict(ms_dict)
    state.tests = data.get("tests", {})
    state.dependencies = data.get("dependencies", {})
    state.decisions = data.get("decisions", [])
    return state
