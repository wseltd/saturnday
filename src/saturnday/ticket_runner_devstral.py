"""Devstral-specific overrides for ticket execution.

Monkey-patches internal functions in ``ticket_runner`` to add:

- **Phase 2**: Temperature escalation on retries (0.0 → 0.1 → 0.2)
  so the model doesn't repeat the same failure trajectory.
- **Phase 3**: Stdlib import validation gate — rejects coder output
  containing non-stdlib imports (hallucinated packages like ``fancy_chars``).
- **Phase 5a**: File content injection — includes current file contents
  in the prompt so the model sees existing code before generating.
- **Phase 5b**: Symbol preservation gate — rejects output that drops
  existing top-level functions, classes, or constants.

This module DOES NOT modify ``ticket_runner.py``.  It patches at
import time and the original ``run_plan`` orchestrator runs unchanged.
"""

from __future__ import annotations

import ast
import logging
import sys
from dataclasses import replace
from pathlib import Path

from saturnday import ticket_runner as _tr
from saturnday._exceptions import PatchExtractionError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase 2 — temperature escalation on retries
# ---------------------------------------------------------------------------

# Mirrors the Devstral evaluation protocol: greedy first, then controlled
# variation on subsequent attempts.
_TEMP_SCHEDULE: dict[int, float] = {1: 0.0, 2: 0.1, 3: 0.2}

# Per-ticket attempt tracker.  Keyed by ticket_id, value is the number of
# times ``_execute_ticket`` has been called for that ticket.
_ticket_attempts: dict[str, int] = {}


def reset_attempt_tracker() -> None:
    """Clear the per-ticket attempt tracker (call before each run)."""
    _ticket_attempts.clear()


# ---------------------------------------------------------------------------
# Phase 3 — stdlib import validation gate
# ---------------------------------------------------------------------------

# Python stdlib module names (3.10+).  Extended with a few common first-party
# names that projects may define locally (e.g. the project's own package).
_STDLIB: frozenset[str] = frozenset(sys.stdlib_module_names)

# Additional modules that are always allowed (testing tools installed in the
# project venv, or well-known first-party names).
_EXTRA_ALLOWED: frozenset[str] = frozenset({
    "pytest", "_pytest", "conftest",
})


def _extract_imports(source: str) -> set[str]:
    """Return top-level module names from import statements in *source*.

    Uses ``ast.parse`` for accuracy.  Falls back to a no-op on syntax
    errors (the governance layer will catch those separately).
    """
    top_level: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return top_level
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:  # absolute imports only
                top_level.add(node.module.split(".")[0])
    return top_level


def _read_declared_deps(repo_path: Path) -> set[str]:
    """Read declared dependencies from pyproject.toml or requirements.txt.

    Returns a set of top-level package names that the project explicitly
    declares as dependencies, which should also be allowed in imports.
    """
    declared: set[str] = set()

    # pyproject.toml
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
            # Simple regex-free parse: look for lines under [project] dependencies
            in_deps = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped == "dependencies = [":
                    in_deps = True
                    continue
                if in_deps:
                    if stripped == "]":
                        break
                    # Extract package name from "package>=1.0" style
                    dep = stripped.strip('"').strip("'").strip(",").strip()
                    if dep:
                        name = dep.split(">=")[0].split("<=")[0].split("==")[0]
                        name = name.split(">")[0].split("<")[0].split("!=")[0]
                        name = name.split("[")[0].strip()
                        if name:
                            # PyPI names use hyphens but Python imports use underscores
                            declared.add(name.replace("-", "_").lower())
        except Exception:
            pass

    # requirements.txt
    reqs = repo_path / "requirements.txt"
    if reqs.is_file():
        try:
            for line in reqs.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    name = line.split(">=")[0].split("<=")[0].split("==")[0]
                    name = name.split(">")[0].split("<")[0].split("!=")[0]
                    name = name.split("[")[0].strip()
                    if name:
                        declared.add(name.replace("-", "_").lower())
        except Exception:
            pass

    return declared


def _validate_stdlib_imports(repo_path: Path, changed_files: list[str]) -> None:
    """Reject changed Python files that import non-stdlib, non-declared packages.

    Raises:
        PatchExtractionError: If any file imports a hallucinated package.
    """
    declared_deps = _read_declared_deps(repo_path)

    # Also allow the project's own package names (any top-level .py file or
    # directory with __init__.py).
    local_modules: set[str] = set()
    for item in repo_path.iterdir():
        if item.name.startswith(".") or item.name == "__pycache__":
            continue
        if item.is_file() and item.suffix == ".py":
            local_modules.add(item.stem)
        elif item.is_dir() and (item / "__init__.py").is_file():
            local_modules.add(item.name)

    allowed = _STDLIB | _EXTRA_ALLOWED | declared_deps | local_modules

    violations: list[str] = []
    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8")
        except Exception:
            continue
        imports = _extract_imports(source)
        bad = imports - allowed
        if bad:
            violations.append(f"{rel_path}: non-stdlib imports {sorted(bad)}")

    if violations:
        msg = (
            "Devstral import validation failed — hallucinated or undeclared packages:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + "\n\nOnly stdlib and declared dependencies are allowed."
        )
        logger.warning("Phase 3 gate: %s", msg)
        raise PatchExtractionError(msg)


# ---------------------------------------------------------------------------
# Phase 5a — file content injection into prompt
# ---------------------------------------------------------------------------

_original_assemble_ticket_prompt = _tr._assemble_ticket_prompt


def _read_existing_files(repo_path: Path, allowed_globs: tuple[str, ...]) -> str:
    """Read current content of Python files matching the ticket's scope.

    Returns a string block suitable for injection into the user prompt.
    Only includes .py files that already exist (not yet-to-be-created).
    Caps total injected content at 6000 chars to leave room for other
    prompt components within the 16384-token model limit.
    """
    from fnmatch import fnmatch

    files_content: list[str] = []
    total_chars = 0
    max_chars = 6000

    for item in sorted(repo_path.rglob("*.py")):
        rel = str(item.relative_to(repo_path))
        # Skip venv, __pycache__, .saturnday, hidden dirs
        if any(part.startswith(".") or part == "__pycache__" or part == ".venv"
               for part in item.parts):
            continue
        # Only include files matching the ticket's scope
        if allowed_globs and not any(fnmatch(rel, g) for g in allowed_globs):
            # Also include top-level .py files (always relevant)
            if "/" in rel:
                continue
        try:
            content = item.read_text(encoding="utf-8")
        except Exception:
            continue
        if not content.strip():
            continue
        block = f"--- CURRENT {rel} ---\n{content}\n--- END {rel} ---"
        if total_chars + len(block) > max_chars:
            break
        files_content.append(block)
        total_chars += len(block)

    if not files_content:
        return ""

    header = (
        "EXISTING CODE — you MUST preserve ALL functions, classes, constants, "
        "and imports shown below. Your FILE blocks must include the existing "
        "code PLUS your additions. Do NOT remove or skip anything.\n\n"
    )
    return header + "\n\n".join(files_content)


def _devstral_assemble_ticket_prompt(
    ticket, repo_path, coder_config, system_prompt, state, plan_notes,
    repair_context=None,
):
    """Wrapper that injects current file contents into the prompt.

    Calls the original assembler unchanged, then appends the current
    file contents to the last user message.  This does NOT change
    compact_prompts or any config flag.
    """
    messages, budget = _original_assemble_ticket_prompt(
        ticket, repo_path, coder_config, system_prompt, state,
        plan_notes, repair_context,
    )

    # Inject existing file contents into the last user message
    existing = _read_existing_files(repo_path, ticket.scope.allowed_globs)
    if existing:
        # Find the last user message and append
        for msg in reversed(messages):
            if msg["role"] == "user":
                msg["content"] = msg["content"] + "\n\n" + existing
                break
        # Recompute prompt budget
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        budget["prompt_chars"] = prompt_chars
        logger.info(
            "Devstral Phase 5a: injected existing file content (%d chars) into prompt",
            len(existing),
        )

    return messages, budget


# ---------------------------------------------------------------------------
# Phase 5b — symbol preservation gate
# ---------------------------------------------------------------------------

def _extract_top_level_symbols(source: str) -> set[tuple[str, str]]:
    """Extract top-level function, class, and constant names from source.

    Returns a set of (kind, name) tuples, e.g. {('function', 'main'),
    ('constant', 'UPPER'), ('class', 'Config')}.
    Falls back to empty set on syntax errors.
    """
    symbols: set[tuple[str, str]] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return symbols
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(("function", node.name))
        elif isinstance(node, ast.ClassDef):
            symbols.add(("class", node.name))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.add(("constant", target.id))
    return symbols


def _validate_symbol_preservation(
    repo_path: Path, changed_files: list[str], snapshots: dict[str, str],
) -> None:
    """Reject output that drops existing top-level symbols.

    Args:
        repo_path: Path to the repository root.
        changed_files: Files that were written by the coder.
        snapshots: Dict of {rel_path: original_content} taken BEFORE
            the coder ran.

    Raises:
        PatchExtractionError: If symbols were dropped.
    """
    violations: list[str] = []
    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        original = snapshots.get(rel_path)
        if not original:
            continue  # new file, nothing to preserve
        new_path = repo_path / rel_path
        if not new_path.is_file():
            continue
        try:
            new_content = new_path.read_text(encoding="utf-8")
        except Exception:
            continue
        old_symbols = _extract_top_level_symbols(original)
        new_symbols = _extract_top_level_symbols(new_content)
        dropped = old_symbols - new_symbols
        if dropped:
            dropped_names = [f"{kind} '{name}'" for kind, name in sorted(dropped)]
            violations.append(
                f"{rel_path}: dropped {', '.join(dropped_names)}"
            )

    if violations:
        msg = (
            "Devstral symbol preservation failed — existing code was overwritten:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + "\n\nYour FILE blocks must include ALL existing functions, classes, "
            "and constants plus your additions."
        )
        logger.warning("Phase 5b gate: %s", msg)
        raise PatchExtractionError(msg)


# ---------------------------------------------------------------------------
# Patched _execute_ticket — combines Phase 2 + Phase 3 + Phase 5b
# ---------------------------------------------------------------------------

_original_execute_ticket = _tr._execute_ticket


def _devstral_execute_ticket(ticket, repo_path, coder_config, messages):
    """Wrapper around ``_execute_ticket`` with Devstral-specific behaviour.

    Phase 2: escalates temperature based on per-ticket attempt count.
    Phase 3: validates that written Python files only import stdlib/declared packages.
    Phase 5b: validates that existing top-level symbols are preserved.
    """
    # Phase 2: temperature escalation
    tid = ticket.ticket_id
    attempt = _ticket_attempts.get(tid, 0) + 1
    _ticket_attempts[tid] = attempt

    temp = _TEMP_SCHEDULE.get(attempt, 0.2)
    modified_config = replace(coder_config, temperature=temp)
    logger.info(
        "Devstral Phase 2: ticket=%s attempt=%d temperature=%.2f",
        tid, attempt, temp,
    )

    # Phase 5b: snapshot existing files BEFORE coder runs
    snapshots: dict[str, str] = {}
    for py_file in repo_path.rglob("*.py"):
        if any(part.startswith(".") or part == "__pycache__" or part == ".venv"
               for part in py_file.parts):
            continue
        rel = str(py_file.relative_to(repo_path))
        try:
            snapshots[rel] = py_file.read_text(encoding="utf-8")
        except Exception:
            pass

    # Call original with modified temperature
    response, changed_files = _original_execute_ticket(
        ticket, repo_path, modified_config, messages,
    )

    # Phase 3: stdlib import validation
    _validate_stdlib_imports(repo_path, changed_files)

    # Phase 5b: symbol preservation validation
    _validate_symbol_preservation(repo_path, changed_files, snapshots)

    return response, changed_files


# ---------------------------------------------------------------------------
# Activation / deactivation
# ---------------------------------------------------------------------------

_patched = False


def activate() -> None:
    """Patch ``ticket_runner._execute_ticket`` with Devstral overrides."""
    global _patched
    if _patched:
        return
    _tr._execute_ticket = _devstral_execute_ticket
    _tr._assemble_ticket_prompt = _devstral_assemble_ticket_prompt
    _patched = True
    logger.info("Devstral overrides activated (Phase 2: temperature, Phase 3: imports, Phase 5a: file injection, Phase 5b: symbol gate)")


def deactivate() -> None:
    """Restore original ``ticket_runner._execute_ticket``."""
    global _patched
    if not _patched:
        return
    _tr._execute_ticket = _original_execute_ticket
    _tr._assemble_ticket_prompt = _original_assemble_ticket_prompt
    _patched = False
    _ticket_attempts.clear()
    logger.info("Devstral overrides deactivated")


# ---------------------------------------------------------------------------
# Convenience: run_plan with Devstral overrides active
# ---------------------------------------------------------------------------

def run_plan_devstral(*args, **kwargs):
    """Call ``ticket_runner.run_plan`` with Devstral patches active."""
    reset_attempt_tracker()
    activate()
    try:
        return _tr.run_plan(*args, **kwargs)
    finally:
        deactivate()
