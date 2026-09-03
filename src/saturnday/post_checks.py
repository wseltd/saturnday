"""Post-governance checks that enforce senior engineering judgment.

Five high-precision, deterministic checks.  Each returns a list of findings
(dicts with ``path``, ``line``, ``message``).  False negatives are preferred
over false positives — these checks must not waste the retry budget or
pollute repair prompts with noise.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_post_checks(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Run all post-governance checks on *changed_files*.

    Args:
        repo_path: Root of the target repository.
        changed_files: Paths (relative to *repo_path*) that were modified.

    Returns:
        Combined list of findings from every check.
    """
    findings: list[dict[str, str | int]] = []
    for check_fn in (
        check_silent_swallow,
        check_readme_sections,
        check_domain_duplicates,
        check_terminology_inflation,
        check_per_request_rebuild,
        check_shape_only_tests,
    ):
        try:
            findings.extend(check_fn(repo_path, changed_files))
        except Exception:
            # A failing heuristic must never kill the pipeline.
            logger.warning("Post-check %s raised — skipping", check_fn.__name__, exc_info=True)
    return findings


# ---------------------------------------------------------------------------
# 1. Silent exception swallowing
# ---------------------------------------------------------------------------

_SILENT_SWALLOW_RE = re.compile(
    r"except\s+Exception\s*:\s*pass",
    re.MULTILINE,
)


def check_silent_swallow(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Flag ``except Exception: pass`` in changed Python files."""
    findings: list[dict[str, str | int]] = []
    for rel in changed_files:
        if not rel.endswith(".py"):
            continue
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Normalise multi-line except blocks so the regex can match them.
        collapsed = re.sub(r"except\s+Exception\s*:\s*\n\s+pass", "except Exception: pass", text)
        for match in _SILENT_SWALLOW_RE.finditer(collapsed):
            line_no = collapsed[:match.start()].count("\n") + 1
            findings.append({
                "path": rel,
                "line": line_no,
                "message": "Silent exception swallowing: `except Exception: pass` — log at WARNING and continue.",
            })
    return findings


# ---------------------------------------------------------------------------
# 2. README required sections
# ---------------------------------------------------------------------------

_REQUIRED_README_HEADINGS = (
    re.compile(r"#+\s+.*(?:Trade[- ]?[Oo]ffs)", re.IGNORECASE),
    re.compile(r"#+\s+.*Limitations", re.IGNORECASE),
    re.compile(r"#+\s+.*(?:Non[- ]?[Gg]oals)", re.IGNORECASE),
)

_REQUIRED_LABELS = ("Trade-offs", "Limitations", "Non-goals")


def check_readme_sections(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Require Trade-offs, Limitations, Non-goals headings in README.md."""
    findings: list[dict[str, str | int]] = []
    for rel in changed_files:
        if Path(rel).name.upper() != "README.MD":
            continue
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, label in zip(_REQUIRED_README_HEADINGS, _REQUIRED_LABELS):
            if not pattern.search(text):
                findings.append({
                    "path": rel,
                    "line": 1,
                    "message": f"README missing required heading: {label}",
                })
    return findings


# ---------------------------------------------------------------------------
# 3. Domain vocabulary duplicates (NARROW)
# ---------------------------------------------------------------------------

_PRODUCTION_EXCLUDE_DIRS = frozenset({"tests", "test", "fixtures", "data", "scripts", "docs"})


def _is_production_module(rel: str) -> bool:
    """True if *rel* lives under ``src/`` and outside excluded directories."""
    parts = Path(rel).parts
    if not parts or parts[0] != "src":
        return False
    return not any(p in _PRODUCTION_EXCLUDE_DIRS for p in parts)


def _extract_domain_constants(source: str) -> dict[str, frozenset[str]]:
    """Extract module-level frozensets, StrEnum members, and UPPER_CASE dicts.

    Returns a mapping of ``name -> set of string values``.
    Only processes explicit, obvious declarations.  Skips anything ambiguous.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}

    constants: dict[str, frozenset[str]] = {}

    for node in ast.iter_child_nodes(tree):
        # Module-level assignments only.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = _get_assign_name(target)
                if name is None:
                    continue
                values = _extract_string_set(node.value)
                if values is not None:
                    constants[name] = values

        # StrEnum subclass: class Foo(StrEnum): A = "a" ...
        if isinstance(node, ast.ClassDef):
            if not _is_strenum_subclass(node):
                continue
            members: set[str] = set()
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                            members.add(item.value.value)
            if members:
                constants[node.name] = frozenset(members)

    return constants


def _get_assign_name(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name) and target.id.isupper():
        return target.id
    return None


def _extract_string_set(node: ast.expr) -> frozenset[str] | None:
    """Return a frozenset of strings if *node* is ``frozenset({...})`` or ``{k: ...}``."""
    # frozenset({...})
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        if node.args and isinstance(node.args[0], ast.Set):
            strs = _all_string_elts(node.args[0].elts)
            if strs is not None:
                return strs
    # UPPER_CASE dict literal {str: ...}
    if isinstance(node, ast.Dict):
        strs = _all_string_elts(node.keys)  # type: ignore[arg-type]
        if strs is not None:
            return strs
    return None


def _all_string_elts(elts: list[ast.expr]) -> frozenset[str] | None:
    values: set[str] = set()
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            values.add(e.value)
        else:
            return None  # Not all strings — bail out conservatively
    return frozenset(values) if values else None


def _is_strenum_subclass(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "StrEnum":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "StrEnum":
            return True
    return False


def check_domain_duplicates(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Flag overlapping-but-not-identical domain vocabularies across production modules.

    Only scans ``src/`` (excluding tests/, fixtures/, data/, scripts/, docs/).
    Only compares explicitly declared ``frozenset({...})``, ``StrEnum`` subclasses,
    and ``UPPER_CASE`` constant dicts.
    """
    findings: list[dict[str, str | int]] = []
    prod_py = [f for f in changed_files if f.endswith(".py") and _is_production_module(f)]
    if len(prod_py) < 2:
        return findings

    # Collect constants per file
    file_constants: dict[str, dict[str, frozenset[str]]] = {}
    for rel in prod_py:
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        consts = _extract_domain_constants(source)
        if consts:
            file_constants[rel] = consts

    # Compare pairs — flag overlapping but non-identical sets
    files = list(file_constants.keys())
    for i, file_a in enumerate(files):
        for file_b in files[i + 1:]:
            for name_a, vals_a in file_constants[file_a].items():
                for name_b, vals_b in file_constants[file_b].items():
                    overlap = vals_a & vals_b
                    if overlap and vals_a != vals_b:
                        findings.append({
                            "path": file_b,
                            "line": 1,
                            "message": (
                                f"Overlapping domain vocabulary with {file_a}: "
                                f"{name_a} and {name_b} share {len(overlap)} value(s) "
                                f"but differ. Centralise into one module."
                            ),
                        })
    return findings


# ---------------------------------------------------------------------------
# 4. Terminology inflation (NARROW)
# ---------------------------------------------------------------------------

_ASSURANCE_TERMS = frozenset({"safety", "secure", "containment"})

_HEURISTIC_IMPL_PATTERNS = (
    # `x in {...}` or `x in [...]` — keyword check against a collection
    re.compile(r"\bin\s+(?:\{|\[|frozenset\()", re.MULTILINE),
    # `.lower()` followed by substring matching
    re.compile(r"\.lower\(\).*\bin\b", re.MULTILINE),
)


def check_terminology_inflation(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Flag when assurance terms appear in names/docstrings of obviously heuristic code.

    Only fires when BOTH conditions are met:
    1. Function/class name or docstring contains ``safety``, ``secure``, or ``containment``.
    2. The implementation body is obviously heuristic (``in`` check against a collection
       or ``.lower()`` + substring matching).

    Does NOT flag ``guard`` alone or complex implementations.
    """
    findings: list[dict[str, str | int]] = []
    for rel in changed_files:
        if not rel.endswith(".py"):
            continue
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue

            # Condition 1: assurance term in name or docstring
            name_lower = node.name.lower()
            docstring = ast.get_docstring(node) or ""
            combined_text = name_lower + " " + docstring.lower()
            if not any(term in combined_text for term in _ASSURANCE_TERMS):
                continue

            # Condition 2: body is obviously heuristic
            body_source = _get_body_source(source, node)
            if body_source is None:
                continue
            if not any(p.search(body_source) for p in _HEURISTIC_IMPL_PATTERNS):
                continue

            findings.append({
                "path": rel,
                "line": node.lineno,
                "message": (
                    f"Terminology inflation: `{node.name}` uses assurance language "
                    f"but implementation is a keyword/substring heuristic. "
                    f"Rename to reflect what it actually does."
                ),
            })
    return findings


def _get_body_source(source: str, node: ast.AST) -> str | None:
    """Extract source lines for the body of a function/class node."""
    lines = source.splitlines()
    # node.end_lineno is available in Python 3.8+
    end = getattr(node, "end_lineno", None)
    if end is None:
        return None
    # Skip the first line (def/class) and docstring
    start = node.lineno  # type: ignore[union-attr]
    if start >= end:
        return None
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# 5. Per-request heavyweight rebuilds
# ---------------------------------------------------------------------------

_HEAVYWEIGHT_PATTERNS = (
    re.compile(r"\bbuild_knowledge_store\s*\("),
    re.compile(r"\bcreate_engine\s*\("),
    re.compile(r"\bload_policy\s*\("),
    re.compile(r"\bbuild_index\s*\("),
    re.compile(r"\bKnowledgeStore\s*\("),
)

_SERVICE_FILE_RE = re.compile(r"(?:service|route)", re.IGNORECASE)


def check_per_request_rebuild(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Flag heavyweight builder calls inside request-handling functions.

    Only scans files whose names contain ``service`` or ``route``.
    Only flags calls found inside regular function bodies (not ``__init__``
    or module-level code).
    """
    findings: list[dict[str, str | int]] = []
    for rel in changed_files:
        if not rel.endswith(".py"):
            continue
        stem = Path(rel).stem
        if stem.startswith("test_") or stem.endswith("_test"):
            continue
        if not _SERVICE_FILE_RE.search(stem):
            continue
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "__init__":
                continue
            body_src = _get_body_source(source, node)
            if body_src is None:
                continue
            for pattern in _HEAVYWEIGHT_PATTERNS:
                match = pattern.search(body_src)
                if match:
                    line_offset = body_src[:match.start()].count("\n")
                    findings.append({
                        "path": rel,
                        "line": node.lineno + line_offset,
                        "message": (
                            f"Per-request rebuild: `{match.group().rstrip('(')}` "
                            f"called inside `{node.name}()` — move to module level "
                            f"or __init__ for one-time construction."
                        ),
                    })
    return findings


# ---------------------------------------------------------------------------
# 6. Shape-only test assertions
# ---------------------------------------------------------------------------


def _is_shape_only_assert(test_node: ast.expr) -> bool:
    """Return True if *test_node* is a shape-only (non-value) assertion.

    Shape-only patterns:
    - ``isinstance(x, ...)`` call
    - ``hasattr(x, ...)`` call
    - ``x is None`` or ``x is not None``
    - ``type(x) == SomeType`` or ``type(x) is SomeType``

    Everything else — equality to a concrete value, membership in a string,
    comparisons to numbers/bools — is treated as a value assertion and clears
    the function from flagging.
    """
    # isinstance(...) / hasattr(...)
    if isinstance(test_node, ast.Call):
        func = test_node.func
        if isinstance(func, ast.Name) and func.id in ("isinstance", "hasattr"):
            return True
        return False

    if isinstance(test_node, ast.Compare):
        # Only handle single-op comparisons to keep analysis conservative.
        if len(test_node.ops) != 1 or len(test_node.comparators) != 1:
            return False
        op = test_node.ops[0]
        comparator = test_node.comparators[0]

        # x is None / x is not None
        if isinstance(op, (ast.Is, ast.IsNot)):
            if isinstance(comparator, ast.Constant) and comparator.value is None:
                return True
            # is True / is False are value assertions — not shape-only.
            return False

        # type(x) == SomeType  or  type(x) is SomeType
        if isinstance(op, (ast.Eq, ast.Is)):
            if isinstance(test_node.left, ast.Call):
                left_func = test_node.left.func
                if isinstance(left_func, ast.Name) and left_func.id == "type":
                    return True

    return False


def check_shape_only_tests(
    repo_path: Path,
    changed_files: list[str],
) -> list[dict[str, str | int]]:
    """Flag test functions where every assertion is shape-only.

    A shape-only assertion checks existence or type (``isinstance``,
    ``is not None``, ``type(x) == T``) but never checks a concrete value.
    Tests with at least one value assertion (``== 42``, ``"foo" in result``)
    are cleared entirely — the check is conservative by design.

    Only scans ``.py`` files whose names start with ``test_`` or end with
    ``_test.py``.  Test functions with zero assertions are also flagged
    (they would be caught by ``check_no_assert`` if that check exists, but
    flagging here too is harmless).
    """
    findings: list[dict[str, str | int]] = []
    for rel in changed_files:
        if not rel.endswith(".py"):
            continue
        stem = Path(rel).stem
        if not (stem.startswith("test_") or stem.endswith("_test")):
            continue
        full = repo_path / rel
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue

            # Collect all assert statements within this function body.
            asserts: list[ast.Assert] = [
                n for n in ast.walk(node) if isinstance(n, ast.Assert)
            ]

            if not asserts:
                # Zero assertions — flag as untested.
                findings.append({
                    "path": rel,
                    "line": node.lineno,
                    "message": (
                        f"`{node.name}` has no assertions — "
                        f"add at least one value assertion to verify behaviour."
                    ),
                    "kind": "shape_only_test",
                })
                continue

            # Check whether every assertion is shape-only.
            all_shape_only = all(_is_shape_only_assert(a.test) for a in asserts)
            if all_shape_only:
                findings.append({
                    "path": rel,
                    "line": node.lineno,
                    "message": (
                        f"`{node.name}` only asserts shape/type (isinstance, "
                        f"is not None, type checks) — add at least one value "
                        f"assertion (e.g. ``assert result == expected``)."
                    ),
                    "kind": "shape_only_test",
                })
    return findings


# ---------------------------------------------------------------------------
# Fix 40: repo-wide hard cross-ticket consistency gate
# ---------------------------------------------------------------------------

# Matches Python module-level UPPER_CASE constant assignments:
#   NAME = "value"   or   NAME = '/path/to/thing'
_PY_CONST_RE = re.compile(
    r"^([A-Z][A-Z0-9_]{2,})\s*=\s*(['\"])([^'\"\\]{1,200})\2\s*(?:#.*)?$",
    re.MULTILINE,
)

# Matches shell-style assignments (used in .sh / .env / Makefile fragments):
#   NAME=value   or   NAME="value"   or   NAME='value'
_SH_CONST_RE = re.compile(
    r"^([A-Z][A-Z0-9_]{2,})=(['\"]?)([^\s'\"#\\]{1,200})\2\s*(?:#.*)?$",
    re.MULTILINE,
)

# File extensions that are scanned for shell-style constants.
_SHELL_EXTENSIONS = {".sh", ".env", ".envrc", ".cfg", ".conf", ".ini", ".toml"}

# Directories always skipped — test fixtures, docs, vendored, generated.
_SKIP_DIR_PARTS = frozenset({
    # Source-like things we intentionally exclude to reduce noise.
    "tests", "test", "docs", "fixtures", "data",
    # Build / distribution artefacts.
    "dist", "build", "__pycache__",
    # Version control.
    ".git",
    # Third-party / dependency trees — NEVER scan these; conflicting
    # constants in site-packages cause spurious cross-file findings
    # (e.g. pip's own NAME constants).
    "node_modules", "vendor", "bower_components",
    ".venv", "venv", "env", ".env", ".tox", "site-packages",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis",
    # Agent metadata / editor caches.
    ".claude", ".codex", ".cursor", ".idea", ".vscode",
})


def _is_scanned_file(path: Path) -> bool:
    """Return True if *path* should be included in the cross-ticket consistency scan."""
    for part in path.parts:
        if part in _SKIP_DIR_PARTS:
            return False
    return path.suffix in {".py"} | _SHELL_EXTENSIONS


def _extract_named_constants(text: str, path: Path) -> dict[str, str]:
    """Return {NAME: value} for all UPPER_CASE constant assignments found in *text*."""
    constants: dict[str, str] = {}
    if path.suffix == ".py":
        for m in _PY_CONST_RE.finditer(text):
            name, value = m.group(1), m.group(3)
            # Take the first definition only (module-level import order)
            if name not in constants:
                constants[name] = value
    else:
        for m in _SH_CONST_RE.finditer(text):
            name, value = m.group(1), m.group(3)
            if name not in constants:
                constants[name] = value
    return constants


def check_cross_file_shared_default_divergence(repo_path: Path) -> list[dict]:
    """Repo-wide hard consistency gate: same-name UPPER_CASE constants with conflicting literal values.

    Scans all non-test Python source files and shell/env files under *repo_path*.
    When the same constant name appears in two or more files with *different*
    literal values, a hard finding is produced.

    Same-name constants with identical values are not flagged.

    Returns:
        List of finding dicts.  Each dict has keys:
        ``constant``, ``file_a``, ``value_a``, ``file_b``, ``value_b``,
        ``message``, ``category``.
    """
    file_constants: dict[str, dict[str, str]] = {}  # rel_path → {NAME: value}

    for fpath in sorted(repo_path.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(repo_path))
        if not _is_scanned_file(Path(rel)):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        consts = _extract_named_constants(text, Path(rel))
        if consts:
            file_constants[rel] = consts

    # Build name → list[(rel_path, value)] index
    name_index: dict[str, list[tuple[str, str]]] = {}
    for rel_path, consts in file_constants.items():
        for name, value in consts.items():
            name_index.setdefault(name, []).append((rel_path, value))

    findings: list[dict] = []
    reported: set[str] = set()  # avoid duplicate pairs

    for name, occurrences in name_index.items():
        if len(occurrences) < 2:
            continue
        for i, (file_a, val_a) in enumerate(occurrences):
            for file_b, val_b in occurrences[i + 1:]:
                if val_a == val_b:
                    continue  # same value — no finding
                pair_key = f"{name}:{min(file_a, file_b)}:{max(file_a, file_b)}"
                if pair_key in reported:
                    continue
                reported.add(pair_key)
                findings.append({
                    "category": "cross_file_shared_default_divergence",
                    "constant": name,
                    "file_a": file_a,
                    "value_a": val_a,
                    "file_b": file_b,
                    "value_b": val_b,
                    "message": (
                        f"Shared constant '{name}' has conflicting values across files: "
                        f"{file_a!r} defines {name}={val_a!r}, "
                        f"{file_b!r} defines {name}={val_b!r}. "
                        f"Centralise into one module or align values."
                    ),
                })

    return findings
