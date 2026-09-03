"""Contract verification for ticket acceptance criteria.

After a ticket executes, this module programmatically verifies that acceptance
criteria are actually satisfied in the repo — not just claimed in the prompt.

Supported patterns (parsed from acceptance criteria strings):
  - "function <name> exists [in <file>]"  → AST: def <name> in .py files
  - "class <name> exists [in <file>]"     → AST: class <name> in .py files
  - "file <path> exists"                  → filesystem presence check
  - "test <name> exists [in <file>]"      → AST: def test_<name>/ test_<name> in test files

The verifier is intentionally conservative: a contract that cannot be parsed
from a criterion string is skipped (not failed), so noise is kept low.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Contract:
    """A single verifiable acceptance criterion extracted from a ticket.

    Attributes:
        kind: Category of the contract: ``"function"``, ``"class"``,
            ``"file"``, or ``"test"``.
        name: The symbol or path to verify.
        file_hint: Optional file path extracted from the criterion string.
            Used as the first lookup target before falling back to repo scan.
        source: The original acceptance criterion string, for diagnostics.
    """

    kind: str
    name: str
    file_hint: str = ""
    source: str = ""


@dataclass
class ContractResult:
    """Outcome of verifying a single contract.

    Attributes:
        contract: The contract that was verified.
        verified: ``True`` if the contract is satisfied in the repo.
        detail: Human-readable explanation of the outcome (pass or fail reason).
        severity: ``"error"`` (blocking) or ``"warning"`` (advisory).
            Existing patterns default to ``"error"``.  New heuristic
            patterns (``has_field``, ``has_param``) use ``"warning"``
            until validated on real runs.
    """

    contract: Contract
    verified: bool
    detail: str = ""
    severity: str = "error"


# ---------------------------------------------------------------------------
# Regex patterns for criterion parsing
# ---------------------------------------------------------------------------

# "function add exists" / "function add exists in math.py"
_RE_FUNCTION = re.compile(
    r"\bfunction\s+([\w.]+)\s+exists(?:\s+in\s+(\S+))?",
    re.IGNORECASE,
)
# "class Foo exists" / "class Foo exists in models.py"
_RE_CLASS = re.compile(
    r"\bclass\s+([\w.]+)\s+exists(?:\s+in\s+(\S+))?",
    re.IGNORECASE,
)
# "file config.yaml exists" / "file src/foo.py exists"
_RE_FILE = re.compile(
    r"\bfile\s+(\S+)\s+exists",
    re.IGNORECASE,
)
# "test test_add_two exists" / "test add_two exists" / "test function test_add_two"
_RE_TEST = re.compile(
    r"\btest(?:\s+function)?\s+(test_[\w]+|[\w]+)\s+exists(?:\s+in\s+(\S+))?",
    re.IGNORECASE,
)
# "class User has field email" / "class User has field email in models.py"
_RE_HAS_FIELD = re.compile(
    r"\bclass\s+([\w.]+)\s+has\s+field\s+([\w]+)(?:\s+in\s+(\S+))?",
    re.IGNORECASE,
)
# "function create_user accepts parameter email" / "... in routes.py"
_RE_HAS_PARAM = re.compile(
    r"\bfunction\s+([\w.]+)\s+accepts\s+parameter\s+([\w]+)(?:\s+in\s+(\S+))?",
    re.IGNORECASE,
)

# Directories to skip when searching the repo
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    "runs", ".saturnday", ".pytest_cache",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_contracts(acceptance_criteria: tuple[str, ...]) -> list[Contract]:
    """Parse acceptance criterion strings into verifiable ``Contract`` objects.

    Each criterion string is matched against known patterns.  Unrecognised
    strings produce no contracts (they are silently skipped to keep noise low).

    Args:
        acceptance_criteria: Tuple of criterion strings from ``TicketSpec``.

    Returns:
        List of ``Contract`` objects, one per matched pattern per criterion.
        May be empty if no criterion string matches a known pattern.
    """
    contracts: list[Contract] = []
    seen: set[tuple[str, str]] = set()

    for criterion in acceptance_criteria:
        if not isinstance(criterion, str) or not criterion.strip():
            continue

        _parse_criterion(criterion, contracts, seen)

    logger.debug(
        "extract_contracts: %d criterion(s) → %d contract(s)",
        len(acceptance_criteria), len(contracts),
    )
    return contracts


def verify_contracts(
    contracts: list[Contract],
    repo_path: Path,
) -> list[ContractResult]:
    """Verify each contract against the actual state of the repo.

    Args:
        contracts: Contracts extracted by ``extract_contracts``.
        repo_path: Absolute path to the repository root.

    Returns:
        One ``ContractResult`` per contract, in the same order.
        Results include pass/fail and a human-readable detail string.
    """
    results: list[ContractResult] = []
    for c in contracts:
        try:
            result = _dispatch(c, repo_path)
        except Exception as exc:
            logger.warning(
                "verify_contracts: unexpected error verifying %s %r: %s",
                c.kind, c.name, exc,
            )
            result = ContractResult(
                contract=c,
                verified=False,
                detail=f"Verification error: {exc}",
            )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Criterion parsing internals
# ---------------------------------------------------------------------------

def _parse_criterion(
    criterion: str,
    out: list[Contract],
    seen: set[tuple[str, str]],
) -> None:
    """Try every pattern against *criterion* and append matched contracts."""
    for m in _RE_FUNCTION.finditer(criterion):
        _add(out, seen, "function", m.group(1), m.group(2) or "", criterion)
    for m in _RE_CLASS.finditer(criterion):
        _add(out, seen, "class", m.group(1), m.group(2) or "", criterion)
    for m in _RE_FILE.finditer(criterion):
        _add(out, seen, "file", m.group(1), "", criterion)
    for m in _RE_TEST.finditer(criterion):
        raw_name = m.group(1)
        # Normalise: "add_two" → "test_add_two" if not already prefixed
        name = raw_name if raw_name.startswith("test_") else f"test_{raw_name}"
        _add(out, seen, "test", name, m.group(2) or "", criterion)
    for m in _RE_HAS_FIELD.finditer(criterion):
        # Encode class+field as "ClassName.field_name" in name
        _add(out, seen, "has_field", f"{m.group(1)}.{m.group(2)}", m.group(3) or "", criterion)
    for m in _RE_HAS_PARAM.finditer(criterion):
        # Encode function+param as "func_name.param_name" in name
        _add(out, seen, "has_param", f"{m.group(1)}.{m.group(2)}", m.group(3) or "", criterion)


def _add(
    out: list[Contract],
    seen: set[tuple[str, str]],
    kind: str,
    name: str,
    file_hint: str,
    source: str,
) -> None:
    key = (kind, name)
    if key in seen:
        return
    seen.add(key)
    out.append(Contract(kind=kind, name=name, file_hint=file_hint, source=source))


# ---------------------------------------------------------------------------
# Verification dispatch
# ---------------------------------------------------------------------------

def _dispatch(contract: Contract, repo_path: Path) -> ContractResult:
    """Route a contract to the appropriate verifier."""
    kind = contract.kind
    if kind == "function":
        return _verify_function(contract, repo_path)
    elif kind == "class":
        return _verify_class(contract, repo_path)
    elif kind == "file":
        return _verify_file(contract, repo_path)
    elif kind == "test":
        return _verify_test(contract, repo_path)
    elif kind == "has_field":
        return _verify_has_field(contract, repo_path)
    elif kind == "has_param":
        return _verify_has_param(contract, repo_path)
    else:
        return ContractResult(
            contract=contract,
            verified=False,
            detail=f"Unknown contract kind: {kind!r}",
        )


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _parse_file(path: Path) -> ast.Module | None:
    """Parse a Python file; return None on any error."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source, filename=str(path))
    except Exception:
        return None


def _collect_functions(tree: ast.Module) -> set[str]:
    """Return all function/method names in the AST (top-level and nested)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _collect_classes(tree: ast.Module) -> set[str]:
    """Return all class names in the AST."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
    return names


def _py_files(repo_path: Path) -> list[Path]:
    """Yield .py files under repo_path, skipping build/cache directories."""
    try:
        return [
            p for p in repo_path.rglob("*.py")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]
    except Exception:
        return []


def _test_files(repo_path: Path) -> list[Path]:
    """Yield test_*.py files under repo_path, skipping build/cache dirs."""
    try:
        return [
            p for p in repo_path.rglob("test_*.py")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]
    except Exception:
        return []


# Extensions the AST-based verifiers can parse (Python only).
# For non-Python files, we fall back to file-existence check.
_PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})


def _is_python_file(path: str) -> bool:
    """Return True if the path has a Python extension."""
    return Path(path).suffix.lower() in _PYTHON_EXTENSIONS


def _bare_name(name: str) -> str:
    """Extract the bare symbol name from a possibly-dotted qualified name.

    Planners may generate criteria like ``"function mypackage.cli.main exists"``
    but AST function/class names are bare (just ``"main"``).  Returns the last
    dotted segment so matching works for both qualified and bare forms.
    """
    return name.rsplit(".", 1)[-1] if "." in name else name


def _non_python_file_check(contract: Contract, repo_path: Path) -> ContractResult | None:
    """For non-Python file hints, verify the file exists instead of parsing AST.

    Returns a ContractResult if the file hint is non-Python (verified=True if
    the file exists, False if not).  Returns None if the file is Python or
    there is no file hint — caller should proceed with AST verification.
    """
    if not contract.file_hint:
        return None
    if _is_python_file(contract.file_hint):
        return None
    # Non-Python file — can't parse AST, just check file exists
    target = repo_path / contract.file_hint
    if target.exists():
        return ContractResult(
            contract=contract, verified=True,
            detail=f"{contract.kind} {contract.name!r}: file {contract.file_hint} exists (non-Python, AST check skipped)",
        )
    return ContractResult(
        contract=contract, verified=False,
        detail=f"{contract.kind} {contract.name!r}: file {contract.file_hint} does not exist",
    )


# ---------------------------------------------------------------------------
# Per-kind verifiers
# ---------------------------------------------------------------------------

def _verify_function(contract: Contract, repo_path: Path) -> ContractResult:
    """Verify that a function with ``contract.name`` exists somewhere in the repo."""
    # Non-Python files: fall back to file-existence check
    non_py = _non_python_file_check(contract, repo_path)
    if non_py is not None:
        return non_py

    bare = _bare_name(contract.name)

    # Try file hint first
    if contract.file_hint:
        target = repo_path / contract.file_hint
        if target.exists():
            tree = _parse_file(target)
            if tree is not None and bare in _collect_functions(tree):
                return ContractResult(
                    contract=contract, verified=True,
                    detail=f"function {contract.name!r} found in {contract.file_hint}",
                )

    # Fallback: scan all .py files
    for py in _py_files(repo_path):
        tree = _parse_file(py)
        if tree is None:
            continue
        if bare in _collect_functions(tree):
            rel = _rel(py, repo_path)
            return ContractResult(
                contract=contract, verified=True,
                detail=f"function {contract.name!r} found in {rel}",
            )

    return ContractResult(
        contract=contract, verified=False,
        detail=f"function {contract.name!r} not found in repo",
    )


def _verify_class(contract: Contract, repo_path: Path) -> ContractResult:
    """Verify that a class with ``contract.name`` exists somewhere in the repo."""
    non_py = _non_python_file_check(contract, repo_path)
    if non_py is not None:
        return non_py

    bare = _bare_name(contract.name)

    if contract.file_hint:
        target = repo_path / contract.file_hint
        if target.exists():
            tree = _parse_file(target)
            if tree is not None and bare in _collect_classes(tree):
                return ContractResult(
                    contract=contract, verified=True,
                    detail=f"class {contract.name!r} found in {contract.file_hint}",
                )

    for py in _py_files(repo_path):
        tree = _parse_file(py)
        if tree is None:
            continue
        if bare in _collect_classes(tree):
            rel = _rel(py, repo_path)
            return ContractResult(
                contract=contract, verified=True,
                detail=f"class {contract.name!r} found in {rel}",
            )

    return ContractResult(
        contract=contract, verified=False,
        detail=f"class {contract.name!r} not found in repo",
    )


def _verify_file(contract: Contract, repo_path: Path) -> ContractResult:
    """Verify that the file at ``contract.name`` exists relative to repo_path.

    Tries the exact relative path first.  If that fails, falls back to a
    basename search so planners that guess the wrong directory structure
    don't cause spurious failures during incremental governed builds.
    """
    target = repo_path / contract.name
    if target.exists():
        return ContractResult(
            contract=contract, verified=True,
            detail=f"file {contract.name!r} exists",
        )

    # Basename fallback — search for a file with the same name anywhere
    # in the repo (skipping build/cache dirs).  Accept only if exactly
    # one match exists; fail with ambiguity detail if multiple match.
    basename = Path(contract.name).name
    matches: list[Path] = []
    for candidate in repo_path.rglob(basename):
        if any(part in _SKIP_DIRS for part in candidate.parts):
            continue
        if candidate.is_file():
            matches.append(candidate)

    if len(matches) == 1:
        rel = _rel(matches[0], repo_path)
        return ContractResult(
            contract=contract, verified=True,
            detail=f"file {contract.name!r} found at {rel} (basename match)",
        )
    if len(matches) > 1:
        locations = ", ".join(sorted(_rel(m, repo_path) for m in matches))
        return ContractResult(
            contract=contract, verified=False,
            detail=f"file {contract.name!r} not found at exact path; {len(matches)} ambiguous basename matches: {locations}",
        )

    return ContractResult(
        contract=contract, verified=False,
        detail=f"file {contract.name!r} does not exist at {target}",
    )


def _verify_test(contract: Contract, repo_path: Path) -> ContractResult:
    """Verify that a test function with ``contract.name`` exists in any test file."""
    non_py = _non_python_file_check(contract, repo_path)
    if non_py is not None:
        return non_py

    bare = _bare_name(contract.name)

    if contract.file_hint:
        target = repo_path / contract.file_hint
        if target.exists():
            tree = _parse_file(target)
            if tree is not None and bare in _collect_functions(tree):
                return ContractResult(
                    contract=contract, verified=True,
                    detail=f"test {contract.name!r} found in {contract.file_hint}",
                )

    for tf in _test_files(repo_path):
        tree = _parse_file(tf)
        if tree is None:
            continue
        if bare in _collect_functions(tree):
            rel = _rel(tf, repo_path)
            return ContractResult(
                contract=contract, verified=True,
                detail=f"test {contract.name!r} found in {rel}",
            )

    return ContractResult(
        contract=contract, verified=False,
        detail=f"test {contract.name!r} not found in any test file",
    )


# ---------------------------------------------------------------------------
# Field and parameter verifiers (warning-severity)
# ---------------------------------------------------------------------------

def _class_has_field(tree: ast.Module, class_name: str, field_name: str) -> bool:
    """Check if a class has a field via AnnAssign, Assign, or self.X in __init__."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        # Check direct class body: AnnAssign and Assign
        for child in node.body:
            if isinstance(child, ast.AnnAssign):
                if isinstance(child.target, ast.Name) and child.target.id == field_name:
                    return True
            elif isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == field_name:
                        return True
        # Check self.field_name = ... in __init__
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == "__init__":
                for stmt in ast.walk(child):
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if (isinstance(target, ast.Attribute)
                                    and isinstance(target.value, ast.Name)
                                    and target.value.id == "self"
                                    and target.attr == field_name):
                                return True
    return False


def _function_has_param(tree: ast.Module, func_name: str, param_name: str) -> bool:
    """Check if a function/method has a specific parameter (skipping self/cls)."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        all_params = (
            [a.arg for a in node.args.posonlyargs]
            + [a.arg for a in node.args.args]
            + [a.arg for a in node.args.kwonlyargs]
        )
        # Skip self/cls
        params = [p for p in all_params if p not in ("self", "cls")]
        if param_name in params:
            return True
    return False


def _verify_has_field(contract: Contract, repo_path: Path) -> ContractResult:
    """Verify that a class has a specific field. Warning-severity."""
    non_py = _non_python_file_check(contract, repo_path)
    if non_py is not None:
        non_py.severity = "warning"
        return non_py

    parts = contract.name.split(".", 1)
    if len(parts) != 2:
        return ContractResult(
            contract=contract, verified=False, severity="warning",
            detail=f"Invalid has_field contract: {contract.name!r}",
        )
    class_name, field_name = parts

    if contract.file_hint:
        target = repo_path / contract.file_hint
        if target.exists():
            tree = _parse_file(target)
            if tree is not None and _class_has_field(tree, class_name, field_name):
                return ContractResult(
                    contract=contract, verified=True, severity="warning",
                    detail=f"class {class_name!r} has field {field_name!r} in {contract.file_hint}",
                )

    for py in _py_files(repo_path):
        tree = _parse_file(py)
        if tree is None:
            continue
        if _class_has_field(tree, class_name, field_name):
            rel = _rel(py, repo_path)
            return ContractResult(
                contract=contract, verified=True, severity="warning",
                detail=f"class {class_name!r} has field {field_name!r} in {rel}",
            )

    return ContractResult(
        contract=contract, verified=False, severity="warning",
        detail=f"class {class_name!r} does not have field {field_name!r}",
    )


def _verify_has_param(contract: Contract, repo_path: Path) -> ContractResult:
    """Verify that a function has a specific parameter. Warning-severity."""
    non_py = _non_python_file_check(contract, repo_path)
    if non_py is not None:
        non_py.severity = "warning"
        return non_py

    parts = contract.name.split(".", 1)
    if len(parts) != 2:
        return ContractResult(
            contract=contract, verified=False, severity="warning",
            detail=f"Invalid has_param contract: {contract.name!r}",
        )
    func_name, param_name = parts

    if contract.file_hint:
        target = repo_path / contract.file_hint
        if target.exists():
            tree = _parse_file(target)
            if tree is not None and _function_has_param(tree, func_name, param_name):
                return ContractResult(
                    contract=contract, verified=True, severity="warning",
                    detail=f"function {func_name!r} accepts parameter {param_name!r} in {contract.file_hint}",
                )

    for py in _py_files(repo_path):
        tree = _parse_file(py)
        if tree is None:
            continue
        if _function_has_param(tree, func_name, param_name):
            rel = _rel(py, repo_path)
            return ContractResult(
                contract=contract, verified=True, severity="warning",
                detail=f"function {func_name!r} accepts parameter {param_name!r} in {rel}",
            )

    return ContractResult(
        contract=contract, verified=False, severity="warning",
        detail=f"function {func_name!r} does not accept parameter {param_name!r}",
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _rel(path: Path, base: Path) -> str:
    """Return path relative to base; fall back to str(path) if not possible."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def format_contract_results(results: list[ContractResult]) -> str:
    """Format contract results into a compact multi-line string for repair context.

    Args:
        results: List of ``ContractResult`` objects.

    Returns:
        Human-readable string summarising each result, one per line.
    """
    lines: list[str] = []
    for r in results:
        if r.verified:
            tag = "PASS"
        elif r.severity == "warning":
            tag = "WARN"
        else:
            tag = "FAIL"
        lines.append(f"  [{tag}] {r.contract.kind} {r.contract.name!r}: {r.detail}")
    return "\n".join(lines)
