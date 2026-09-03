import ast
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


_SECRET_REGEX = re.compile(r"[A-Za-z0-9]{32,}")

# Lockfiles contain many long alphanumeric hashes that are not secrets.
# Scanning them produces near-100% false-positive findings.
_LOCKFILE_BASENAMES: frozenset[str] = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "go.sum",
})


def _build_env(env: dict | None = None) -> dict:
    if env is None:
        env = os.environ.copy()
    else:
        env = env.copy()
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
    return env


def _preview_match(value: str) -> str:
    if len(value) < 8:
        return "…"
    return f"{value[:4]}…{value[-4:]}"


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    parts = normalized.split("/")
    if not parts:
        return False
    if parts[0] in {"test", "tests"}:
        return True
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _filter_bandit_findings(findings: list[dict]) -> list[dict]:
    filtered = []
    for finding in findings:
        if (
            finding.get("test_id") == "B101"
            and _is_test_path(str(finding.get("filename", "")))
        ):
            continue
        if finding.get("test_id") == "B110" and finding.get("issue_severity") == "LOW":
            continue
        filtered.append(finding)
    return filtered


def _is_init_module_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized.endswith("/__init__.py") or normalized == "__init__.py"


def _filter_ruff_findings(findings: list[dict]) -> list[dict]:
    filtered = []
    for finding in findings:
        if finding.get("code") == "F401" and _is_init_module_path(str(finding.get("filename", ""))):
            continue
        filtered.append(finding)
    return filtered


def _scan_secrets(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    scanned = []
    for rel_path in sorted(changed_files):
        if os.path.basename(rel_path) in _LOCKFILE_BASENAMES:
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        scanned.append(rel_path)
        try:
            content = path.read_text()
        except Exception:
            continue
        for match in _SECRET_REGEX.finditer(content):
            value = match.group(0)
            findings.append(
                {
                    "path": rel_path,
                    "kind": "regex_candidate",
                    "match_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    "match_preview": _preview_match(value),
                    "match_len": len(value),
                }
            )
    status = "FAIL" if findings else "PASS"
    raw_output = f"scanned_files={len(scanned)} findings={len(findings)} paths={scanned}"
    return {
        "name": "secrets",
        "status": status,
        "exit_code": 0,
        "findings": findings,
        "raw_output": raw_output,
        "error": None,
    }


_PLACEHOLDER_PATTERNS = [
    re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.IGNORECASE),
    re.compile(r"pass\s+#\s*placeholder", re.IGNORECASE),
    re.compile(r"raise\s+NotImplementedError"),
    re.compile(r"^\s*\.\.\.\s*$"),
]


def _is_inside_string(line: str, match_start: int) -> bool:
    """Check if a regex match position is inside a string literal."""
    in_single = False
    in_double = False
    in_triple_single = False
    in_triple_double = False
    i = 0
    while i < match_start:
        remaining = line[i:]
        if remaining.startswith('"""') and not in_single and not in_triple_single:
            in_triple_double = not in_triple_double
            i += 3
            continue
        if remaining.startswith("'''") and not in_double and not in_triple_double:
            in_triple_single = not in_triple_single
            i += 3
            continue
        if line[i] == '"' and not in_single and not in_triple_single and not in_triple_double:
            in_double = not in_double
        elif line[i] == "'" and not in_double and not in_triple_single and not in_triple_double:
            in_single = not in_single
        i += 1
    return in_single or in_double or in_triple_single or in_triple_double


def _scan_placeholders(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text()
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for pattern in _PLACEHOLDER_PATTERNS:
                match = pattern.search(line)
                if match:
                    # Skip if the match is inside a string literal (test data)
                    if _is_inside_string(line, match.start()):
                        continue
                    findings.append(
                        {"file": rel_path, "line": lineno, "pattern": match.group(0)}
                    )
                    break
    status = "FAIL" if findings else "PASS"
    return {
        "name": "placeholders",
        "status": status,
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


    # Well-known package name → import name mappings for packages where
    # the pip name differs from the Python import name.
_PKG_TO_IMPORT: dict[str, str] = {
    "pythonocc_core": "occ",
    "pillow": "pil",
    "scikit_learn": "sklearn",
    "scikit_image": "skimage",
    "pyyaml": "yaml",
    "python_dateutil": "dateutil",
    "beautifulsoup4": "bs4",
    "opencv_python": "cv2",
    "opencv_python_headless": "cv2",
    "attrs": "attr",
    "protobuf": "google",
    "grpcio": "grpc",
    "pyzmq": "zmq",
    "ruamel_yaml": "ruamel",
    "python_dotenv": "dotenv",
    "pyserial": "serial",
    "python_jose": "jose",
    "python_multipart": "multipart",
    # Fix 50: additional distribution→import aliases
    "rdkit_pypi": "rdkit",
    "pymatgen_core": "pymatgen",
    "tensorflow_gpu": "tensorflow",
    "torch": "torch",  # no-op but explicit
    "nvidia_cublas_cu12": "nvidia",
    "nvidia_cudnn_cu12": "nvidia",
    "msgpack_python": "msgpack",
    "pyopenssl": "openssl",
    "pycryptodome": "crypto",
    "pycryptodomex": "cryptodome",
    "jaraco_classes": "jaraco",
    "jaraco_functools": "jaraco",
    "jaraco_text": "jaraco",
    "importlib_metadata": "importlib_metadata",  # underscore import
    "importlib_resources": "importlib_resources",
    "typing_extensions": "typing_extensions",
    "zope_interface": "zope",
    "apache_airflow": "airflow",
    "google_cloud_storage": "google",
    "google_api_python_client": "googleapiclient",
    "google_auth": "google",
    "azure_storage_blob": "azure",
    "azure_identity": "azure",
    "boto3": "boto3",  # explicit
    "botocore": "botocore",  # explicit
}


def _get_declared_dependencies(repo_path: Path) -> set:
    """Read declared package names from pyproject.toml and requirements.txt.

    Also resolves well-known package→import name mappings so that
    declaring ``pythonocc-core`` also covers ``import OCC``.
    """
    declared: set[str] = set()
    pyproject = repo_path / "pyproject.toml"
    requirements = repo_path / "requirements.txt"
    if pyproject.exists():
        try:
            text = pyproject.read_text()
            for m in re.finditer(r'["\']([a-zA-Z0-9_-]+)', text):
                pkg = m.group(1).lower().replace("-", "_")
                declared.add(pkg)
                # Add the import name if the pip name differs
                if pkg in _PKG_TO_IMPORT:
                    declared.add(_PKG_TO_IMPORT[pkg])
        except Exception:
            pass
    if requirements.exists():
        try:
            for line in requirements.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    pkg = re.split(r"[>=<!\[;]", line)[0].strip()
                    if pkg:
                        norm = pkg.lower().replace("-", "_")
                        declared.add(norm)
                        if norm in _PKG_TO_IMPORT:
                            declared.add(_PKG_TO_IMPORT[norm])
        except Exception:
            pass
    return declared


def _check_dead_code(repo_path: Path, changed_files: list[str]) -> dict:
    """Check for defined-but-unused functions across the entire project.

    A function is only flagged as dead code if it is not referenced in
    ANY file in the project — not just the file where it's defined.
    This avoids false positives on modular codebases where functions
    are defined in one file and imported by another.
    """
    findings = []
    py_files = [f for f in changed_files if f.endswith(".py")]

    # Build a cross-file reference index from ALL repo Python files
    # (not just changed files) so incremental ticket builds don't
    # produce false positives for symbols used from untouched files.
    all_sources: dict[str, str] = {}
    for path in repo_path.rglob("*.py"):
        try:
            rel = str(path.relative_to(repo_path))
        except ValueError:
            continue
        if any(part.startswith(".") for part in path.relative_to(repo_path).parts):
            continue
        if path.is_file():
            try:
                all_sources[rel] = path.read_text(encoding="utf-8")
            except Exception:
                pass

    for rel_path in sorted(py_files):
        source = all_sources.get(rel_path, "")
        if not source:
            continue
        try:
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue

        # Collect __all__ exports — symbols listed here are intentional
        # public API and must not be flagged as dead code.
        exported_names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
                and isinstance(node.value, (ast.List, ast.Tuple))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        exported_names.add(elt.value)

        # Collect all top-level and class-level function definitions
        defined: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip dunder methods and test functions
                # Skip decorated functions — frameworks register them implicitly
                # (e.g. @app.post, @app.get, @login_required, @pytest.fixture)
                if node.decorator_list:
                    continue
                if node.name.startswith("_") or node.name.startswith("test"):
                    continue
                defined[node.name] = node.lineno
        if not defined:
            continue
        for name, lineno in defined.items():
            # Symbols in __all__ are intentional public API
            if name in exported_names:
                continue
            # Check if this function is referenced in ANY other file
            used_elsewhere = False
            for other_path, other_source in all_sources.items():
                if other_path == rel_path:
                    # In the same file, check if called (not just defined)
                    count = source.count(name)
                    if count > 1:
                        used_elsewhere = True
                        break
                else:
                    # In other files, any reference counts as usage
                    if name in other_source:
                        used_elsewhere = True
                        break
            if not used_elsewhere:
                findings.append({
                    "file": rel_path,   # preserved for backward compatibility
                    "path": rel_path,   # expected by _filter_findings_to_files
                    "line": lineno,
                    "kind": "dead_code",
                    "detail": f"Function '{name}' is defined but never referenced in any project file",
                })
    status = "FAIL" if findings else "PASS"
    return {
        "name": "dead_code",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": [f for f in changed_files if f.endswith(".py")],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_project_runnable(repo_path: Path, changed_files: list[str]) -> dict:
    """Check that the project has the config files needed to actually run.

    F: accepts a valid project config whether it lives at the repo root
    OR inside a bounded subroot (e.g. ``frontend/package.json`` +
    ``backend/pyproject.toml``).  A monorepo with two valid subroots
    passes this check; a single-root project behaves exactly as before.
    """
    from saturnday.workspaces import detect_workspaces

    findings = []

    # Detect what languages are in the project
    has_ts = any(f.endswith((".ts", ".tsx")) for f in changed_files)
    has_js = any(f.endswith((".js", ".jsx")) for f in changed_files)
    has_py = any(f.endswith(".py") for f in changed_files)

    workspaces = detect_workspaces(repo_path)
    _node_roots = [w for w in workspaces if w.kind == "node"]
    _python_roots = [w for w in workspaces if w.kind == "python"]

    # TypeScript/JavaScript projects need package.json — at root OR in a subroot.
    if has_ts or has_js:
        if not _node_roots:
            findings.append({
                "file": "package.json",
                "line": 0,
                "kind": "missing_package_json",
                "detail": "TypeScript/JavaScript project has no package.json (root or subroot). Cannot install dependencies or run the project.",
            })
        # tsconfig.json: still a per-workspace concern.  Accept if ANY
        # detected node workspace has a tsconfig.json (or the root does).
        # This mirrors the package.json rule — tsconfig.json at a subroot
        # alongside the package.json counts.
        if has_ts:
            _has_tsconfig = (
                (repo_path / "tsconfig.json").is_file()
                or any((w.path / "tsconfig.json").is_file() for w in _node_roots)
            )
            if not _has_tsconfig:
                findings.append({
                    "file": "tsconfig.json",
                    "line": 0,
                    "kind": "missing_tsconfig",
                    "detail": "TypeScript project has no tsconfig.json (root or subroot). Cannot compile TypeScript.",
                })

    # Python projects need pyproject.toml or setup.py — at root OR in a subroot.
    if has_py:
        if not _python_roots:
            findings.append({
                "file": "pyproject.toml",
                "line": 0,
                "kind": "missing_project_config",
                "detail": "Python project has no pyproject.toml or setup.py (root or subroot). Cannot install or distribute the project.",
            })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "project_runnable",
        "status": status,
        "severity": "error",
        "findings": findings,
        "files_checked": ["package.json", "tsconfig.json", "pyproject.toml"],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_license(repo_path: Path, changed_files: list[str]) -> dict:
    """Check that a LICENSE file exists."""
    findings = []
    has_license = any(
        (repo_path / name).is_file()
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md")
    )
    if not has_license:
        findings.append({
            "file": "LICENSE",
            "line": 0,
            "kind": "missing_license",
            "detail": "No LICENSE file found. Every project must include a license.",
        })
    status = "FAIL" if findings else "PASS"
    return {
        "name": "license",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": ["LICENSE"],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_blast_radius(repo_path: Path, changed_files: list[str]) -> dict:
    """Flag changesets that touch an unusually large fraction of the repo's Python files.

    This is a heuristic guard against full rewrites disguised as incremental
    changes.  It fires only when BOTH conditions hold:
    - more than 10 Python files are in changed_files, AND
    - those files represent more than 50 % of all Python files in the repo.

    Why the conjunction: a 15-file change in a 10 000-file repo is unremarkable;
    the same change in a 20-file repo is suspicious.
    """
    changed_py = [f for f in changed_files if f.endswith(".py")]
    if len(changed_py) <= 10:
        return {
            "name": "blast_radius",
            "status": "PASS",
            "severity": "warning",
            "findings": [],
            "files_checked": changed_py,
            "exit_code": 0,
            "raw_output": f"changed_py={len(changed_py)} (threshold not reached)",
            "error": None,
        }

    # Count all Python files in the repo (walk, skip hidden dirs and venvs)
    _SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build"}
    total_py = 0
    try:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            total_py += sum(1 for f in files if f.endswith(".py"))
    except Exception:
        pass

    findings = []
    if total_py > 0 and len(changed_py) / total_py > 0.5:
        pct = int(100 * len(changed_py) / total_py)
        findings.append({
            "file": ".",
            "line": 0,
            "kind": "excessive_blast_radius",
            "detail": (
                f"{len(changed_py)} Python files changed out of {total_py} total "
                f"({pct}% of repo). This heuristic catches full rewrites disguised "
                f"as small changes. Review whether a smaller, incremental edit would suffice."
            ),
        })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "blast_radius",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": changed_py,
        "exit_code": 1 if findings else 0,
        "raw_output": (
            f"changed_py={len(changed_py)} total_py={total_py}"
        ),
        "error": None,
    }


def _check_readme(repo_path: Path, changed_files: list[str]) -> dict:
    """Check that a README.md exists and has required sections."""
    findings = []
    readme = repo_path / "README.md"
    if not readme.is_file():
        findings.append({
            "file": "README.md",
            "line": 0,
            "kind": "missing_readme",
            "detail": "No README.md found. Every project must have a README.",
        })
    else:
        try:
            content = readme.read_text(encoding="utf-8").lower()
            required_sections = ["trade-offs", "limitations", "non-goals"]
            for section in required_sections:
                if section not in content:
                    findings.append({
                        "file": "README.md",
                        "line": 0,
                        "kind": "readme_missing_section",
                        "detail": f"README.md is missing required section: {section.title()}",
                    })
        except Exception:
            pass
    status = "FAIL" if findings else "PASS"
    return {
        "name": "readme",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": ["README.md"],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_dependency_declaration(repo_path: Path, changed_files: list[str]) -> dict:
    declared = _get_declared_dependencies(repo_path)

    stdlib_names: set[str] = set()
    if hasattr(sys, "stdlib_module_names"):
        stdlib_names = sys.stdlib_module_names
    else:
        stdlib_names = {
            "os", "sys", "re", "json", "pathlib", "collections", "functools",
            "itertools", "typing", "abc", "io", "math", "datetime", "time",
            "hashlib", "subprocess", "shutil", "tempfile", "unittest", "logging",
            "argparse", "ast", "copy", "dataclasses", "enum", "glob", "importlib",
            "inspect", "operator", "platform", "pprint", "string", "textwrap",
            "threading", "traceback", "uuid", "warnings", "contextlib", "csv",
            "fnmatch", "http", "urllib", "xml", "email", "html", "socket",
            "sqlite3", "pickle", "struct", "signal", "locale", "gettext",
            "configparser", "secrets", "statistics", "decimal", "fractions",
            "random", "shelve", "dbm", "gzip", "bz2", "lzma", "zipfile",
            "tarfile", "ctypes", "multiprocessing", "concurrent", "asyncio",
            "venv", "token", "tokenize", "dis", "compileall", "py_compile",
        }

    project_name = repo_path.name.lower().replace("-", "_")
    first_party: set[str] = {project_name}
    # Scan both repo root and src/ for first-party packages
    for search_dir in [repo_path, repo_path / "src"]:
        if search_dir.is_dir():
            try:
                for child in search_dir.iterdir():
                    if child.is_dir() and (child / "__init__.py").exists():
                        first_party.add(child.name.lower().replace("-", "_"))
            except Exception:
                pass

    findings = []
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=rel_path)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    top_norm = top.lower().replace("-", "_")
                    if top_norm not in stdlib_names and top_norm not in declared and top_norm not in first_party:
                        findings.append({"file": rel_path, "line": node.lineno, "pattern": f"import {alias.name}"})
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue
                if node.module:
                    top = node.module.split(".")[0]
                    top_norm = top.lower().replace("-", "_")
                    if top_norm not in stdlib_names and top_norm not in declared and top_norm not in first_party:
                        findings.append({"file": rel_path, "line": node.lineno, "pattern": f"from {node.module}"})

    status = "FAIL" if findings else "PASS"
    return {
        "name": "dependency_declaration",
        "status": status,
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_imports(
    repo_path: Path,
    changed_files: list[str],
    *,
    run_shell_func,
    timeout_s: int,
) -> dict:
    findings = []
    info_findings: list[dict] = []
    declared_deps = _get_declared_dependencies(repo_path)
    python_files = [f for f in changed_files if f.endswith(".py")]
    for rel_path in sorted(python_files):
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=rel_path)
        except Exception:
            continue
        modules: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.append((node.lineno, alias.name.split(".")[0]))
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0 or not node.module:
                    continue
                modules.append((node.lineno, node.module.split(".")[0]))
        for lineno, mod in modules:
            env = _build_env()
            record = run_shell_func(
                [sys.executable, "-c", f"import {mod}"],
                cwd=repo_path,
                timeout_s=timeout_s,
                env=env,
            )
            if record.get("status") == "RAN" and record.get("returncode") == 0:
                continue
            mod_norm = mod.lower().replace("-", "_")
            if mod_norm in declared_deps:
                info_findings.append({
                    "file": rel_path, "line": lineno, "pattern": f"import {mod}",
                    "kind": "declared_not_installed",
                    "detail": (
                        f"Package '{mod}' declared in project dependencies but "
                        f"not installed in current environment"
                    ),
                })
                continue
            findings.append({"file": rel_path, "line": lineno, "pattern": f"import {mod}"})

    status = "FAIL" if findings else "PASS"
    return {
        "name": "import_check",
        "status": status,
        "findings": findings + info_findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_code_quality(repo_path: Path, changed_files: list[str]) -> dict:
    """AST-based code quality check: type hints, __repr__, deprecated utcnow."""
    findings = []
    _DUNDER_RE = re.compile(r"^__\w+__$")

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue

        is_test = _is_test_path(rel_path)

        for node in ast.walk(tree):
            # Check functions/methods for missing type hints (skip test files)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if is_test or _DUNDER_RE.match(node.name):
                    pass
                else:
                    if node.returns is None:
                        findings.append({
                            "file": rel_path,
                            "line": node.lineno,
                            "kind": "missing_type_hint",
                            "detail": f"Function '{node.name}' missing return type annotation",
                        })
                    for arg in node.args.args:
                        if arg.arg in ("self", "cls"):
                            continue
                        if arg.annotation is None:
                            findings.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "kind": "missing_type_hint",
                                "detail": f"Parameter '{arg.arg}' in '{node.name}' missing type annotation",
                            })

            # Check classes with bases for missing __repr__ (skip test files)
            elif isinstance(node, ast.ClassDef) and node.bases and not is_test:
                has_repr = any(
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "__repr__"
                    for item in node.body
                )
                if not has_repr:
                    findings.append({
                        "file": rel_path,
                        "line": node.lineno,
                        "kind": "missing_repr",
                        "detail": f"Class '{node.name}' missing __repr__ method",
                    })

            # Check for deprecated datetime.utcnow() (all files)
            elif isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "utcnow"
                ):
                    findings.append({
                        "file": rel_path,
                        "line": node.lineno,
                        "kind": "deprecated_utcnow",
                        "detail": "datetime.utcnow() is deprecated, use datetime.now(tz=timezone.utc)",
                    })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "code_quality",
        "status": status,
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_stubs(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    _DUNDER_RE = re.compile(r"^__\w+__$")
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        if _is_test_path(rel_path):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _DUNDER_RE.match(node.name):
                continue
            body = node.body
            if len(body) == 0:
                continue
            # Skip single-line functions (decorator + def + body = 1 statement)
            if len(body) == 1:
                stmt = body[0]
                # Docstring-only stub
                if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    findings.append({"file": rel_path, "line": node.lineno, "kind": "docstring_only_stub",
                                     "detail": f"Function '{node.name}' body is only a docstring"})
                    continue
                # Hardcoded return (not None) — skip if function has return annotation
                if (isinstance(stmt, ast.Return) and stmt.value is not None
                        and isinstance(stmt.value, ast.Constant) and stmt.value.value is not None
                        and node.returns is None):
                    findings.append({"file": rel_path, "line": node.lineno, "kind": "hardcoded_return",
                                     "detail": f"Function '{node.name}' returns a hardcoded constant"})
                    continue
                # Print-only function
                if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
                    func = stmt.value.func
                    if isinstance(func, ast.Name) and func.id == "print":
                        findings.append({"file": rel_path, "line": node.lineno, "kind": "print_only_function",
                                         "detail": f"Function '{node.name}' body is only a print() call"})
                        continue
    status = "FAIL" if findings else "PASS"
    return {"name": "stubs", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_test_quality(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        if not _is_test_path(rel_path):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            body = node.body
            # Empty body: pass, ..., or docstring only
            if len(body) == 1:
                stmt = body[0]
                if isinstance(stmt, ast.Pass):
                    findings.append({"file": rel_path, "line": node.lineno, "kind": "test_empty_body",
                                     "detail": f"Test '{node.name}' has empty body (pass)"})
                    continue
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    if isinstance(stmt.value.value, (type(...), str)):
                        findings.append({"file": rel_path, "line": node.lineno, "kind": "test_empty_body",
                                         "detail": f"Test '{node.name}' has empty body"})
                        continue
            # Check for assert presence
            has_assert = False
            has_pytest_raises = False
            for child in ast.walk(node):
                if isinstance(child, ast.Assert):
                    has_assert = True
                if isinstance(child, ast.With) or isinstance(child, ast.AsyncWith):
                    for item in child.items:
                        ctx = item.context_expr
                        if (isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute)
                                and ctx.func.attr == "raises"):
                            has_pytest_raises = True
            if not has_assert and not has_pytest_raises:
                findings.append({"file": rel_path, "line": node.lineno, "kind": "test_no_assert",
                                 "detail": f"Test '{node.name}' has no assert or pytest.raises"})
                continue
            # Tautological assert: assert True, assert 1, assert "string"
            for child in ast.walk(node):
                if isinstance(child, ast.Assert):
                    test_val = child.test
                    if isinstance(test_val, ast.Constant) and test_val.value:
                        findings.append({"file": rel_path, "line": child.lineno, "kind": "tautological_assert",
                                         "detail": f"Tautological assert in '{node.name}': assert {test_val.value!r}"})
                        break
            # Caught assertion: assert inside try with bare except that does pass
            for child in ast.walk(node):
                if not isinstance(child, ast.Try):
                    continue
                body_has_assert = any(isinstance(n, ast.Assert) for n in ast.walk(ast.Module(body=child.body, type_ignores=[])))
                if not body_has_assert:
                    continue
                for handler in child.handlers:
                    if handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id == "Exception"):
                        if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                            findings.append({"file": rel_path, "line": child.lineno, "kind": "assert_caught",
                                             "detail": f"Assert caught by bare except in '{node.name}'"})
                            break
    status = "FAIL" if findings else "PASS"
    return {"name": "test_quality", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_syntax(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
            ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            findings.append({"file": rel_path, "line": exc.lineno or 0, "kind": "syntax_error",
                             "detail": str(exc.msg) if exc.msg else str(exc)})
    status = "FAIL" if findings else "PASS"
    return {"name": "syntax", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


_INJECTION_ROLE_MARKERS = re.compile(r"<<SYS>>|\[INST\]|<\|system\|>|<\|endoftext\|>")
_INJECTION_OVERRIDE = re.compile(r"ignore\s+previous|ignore\s+above|disregard|new\s+instructions", re.IGNORECASE)


def _scan_injection_patterns(repo_path: Path, changed_files: list[str], *, strict: bool = False) -> dict:
    findings = []
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            content = path.read_text()
        except Exception:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            m = _INJECTION_ROLE_MARKERS.search(line)
            if m:
                findings.append({"file": rel_path, "line": lineno, "kind": "role_marker",
                                 "detail": f"Prompt injection role marker: {m.group(0)}"})
            m = _INJECTION_OVERRIDE.search(line)
            if m:
                findings.append({"file": rel_path, "line": lineno, "kind": "override_pattern",
                                 "detail": f"Prompt injection override pattern: {m.group(0)}"})
    fail_status = "FAIL" if strict else "WARN"
    status = fail_status if findings else "PASS"
    return {"name": "injection_scan", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_version_pinning(repo_path: Path) -> dict:
    findings = []
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return {"name": "version_pinning", "status": "PASS", "findings": [],
                "exit_code": 0, "raw_output": "", "error": None}
    try:
        text = pyproject.read_text()
    except Exception:
        return {"name": "version_pinning", "status": "PASS", "findings": [],
                "exit_code": 0, "raw_output": "", "error": None}
    # Extract dependencies list — ONLY from [project].dependencies, not other sections
    in_project_deps = False
    in_bracket_depth = 0
    past_project_section = False
    for line in text.splitlines():
        stripped = line.strip()
        # Track which TOML section we're in
        if stripped.startswith("[") and not stripped.startswith("[["):
            past_project_section = stripped in ("[project]",)
            in_project_deps = False
            continue
        # Only parse dependencies under [project]
        if past_project_section and stripped.startswith("dependencies") and "=" in stripped:
            in_project_deps = True
            if "[" in stripped:
                in_bracket_depth = 1
            continue
        if in_project_deps:
            if stripped == "]" or ("]" in stripped and in_bracket_depth > 0):
                in_project_deps = False
                in_bracket_depth = 0
                continue
            # Extract quoted package spec (only actual pip dependencies)
            m = re.match(r'''["\']([a-zA-Z0-9_-]+)\s*([><=!~].*?)?["\']\s*,?''', stripped)
            if m:
                pkg = m.group(1)
                version = m.group(2)
                if not version or not version.strip():
                    findings.append({"file": "pyproject.toml", "line": 0, "kind": "unpinned_dependency",
                                     "detail": f"Dependency '{pkg}' has no version constraint"})
    status = "FAIL" if findings else "PASS"
    return {"name": "version_pinning", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


# Modules that should never trigger typosquat warnings.
# Includes all stdlib names + common short project module names.
_TYPOSQUAT_ALLOWLIST: frozenset = frozenset(
    (sys.stdlib_module_names if hasattr(sys, "stdlib_module_names") else {
        "os", "sys", "re", "json", "pathlib", "collections", "functools",
        "itertools", "typing", "abc", "io", "math", "datetime", "time",
        "hashlib", "subprocess", "shutil", "tempfile", "unittest", "logging",
        "argparse", "ast", "copy", "dataclasses", "enum", "glob", "importlib",
        "inspect", "operator", "platform", "pprint", "string", "textwrap",
        "threading", "traceback", "uuid", "warnings", "contextlib", "csv",
        "fnmatch", "http", "urllib", "xml", "email", "html", "socket",
        "sqlite3", "pickle", "struct", "signal", "locale", "gettext",
        "configparser", "secrets", "statistics", "decimal", "fractions",
        "random", "shelve", "dbm", "gzip", "bz2", "lzma", "zipfile",
        "tarfile", "ctypes", "multiprocessing", "concurrent", "asyncio",
        "venv", "token", "tokenize", "dis", "compileall", "py_compile",
    })
    | {
        # Common short module names that are project-local, never typosquats
        "app", "db", "cli", "api", "run", "env", "lib", "src", "test",
        "tests", "main", "config", "settings", "utils", "helpers", "core",
        "base", "common", "models", "views", "urls", "admin", "manage",
        "setup", "conf",
    }
)

_KNOWN_PACKAGES = frozenset({
    "requests", "flask", "django", "numpy", "pandas", "scipy", "matplotlib",
    "sqlalchemy", "pytest", "click", "jinja2", "pydantic", "fastapi", "uvicorn",
    "celery", "redis", "boto3", "pillow", "cryptography", "paramiko", "setuptools",
    "wheel", "pip", "virtualenv", "tox", "sphinx", "coverage", "black", "ruff",
    "mypy", "pylint", "bandit", "httpx", "aiohttp", "tornado", "gunicorn",
    "psycopg2", "pymongo", "elasticsearch", "beautifulsoup4", "lxml", "scrapy",
    "selenium", "docker", "kubernetes", "grpcio", "protobuf", "pyyaml", "toml",
    "attrs", "marshmallow", "cerberus", "jsonschema", "arrow", "pendulum",
    "dateutil", "pytz", "six", "certifi", "urllib3", "chardet", "idna",
    "packaging", "wrapt", "decorator", "more_itertools", "toolz", "networkx",
    "sympy", "scikit_learn", "tensorflow", "torch", "transformers", "tokenizers",
    "sentencepiece", "spacy", "nltk", "gensim", "opencv_python", "seaborn",
    "plotly", "dash", "streamlit", "gradio", "wandb", "mlflow", "optuna",
    "lightgbm", "xgboost", "catboost", "statsmodels", "prophet", "dask",
    "ray", "prefect", "airflow", "luigi", "nox", "invoke", "fabric",
    "alembic", "peewee", "tortoise_orm", "databases", "starlette", "sanic",
})


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def _check_typosquat(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    imported: set[str] = set()
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=rel_path)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add((rel_path, node.lineno, alias.name.split(".")[0]))
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0 or not node.module:
                    continue
                imported.add((rel_path, node.lineno, node.module.split(".")[0]))
    for rel_path, lineno, mod in sorted(imported):
        mod_norm = mod.lower().replace("-", "_")
        if mod_norm in _KNOWN_PACKAGES:
            continue
        if mod_norm in _TYPOSQUAT_ALLOWLIST:
            continue
        for known in _KNOWN_PACKAGES:
            dist = _levenshtein(mod_norm, known)
            if 1 <= dist <= 2:
                findings.append({"file": rel_path, "line": lineno, "kind": "possible_typosquat",
                                 "detail": f"Import '{mod}' is close to known package '{known}' (edit distance {dist})"})
                break
    status = "FAIL" if findings else "PASS"
    return {"name": "typosquat", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_module_conflicts(repo_path: Path, changed_files: list[str]) -> dict:
    findings = []
    # Collect .py file stems with their directories
    file_map: dict[str, list[str]] = {}
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists():
            continue
        stem = Path(rel_path).stem
        if stem == "__init__":
            continue
        file_map.setdefault(stem, []).append(rel_path)
    # Detect duplicate module names across different directories
    for stem, paths in file_map.items():
        if len(paths) > 1:
            dirs = set(str(Path(p).parent) for p in paths)
            if len(dirs) > 1:
                findings.append({"kind": "duplicate_module", "module": stem, "files": paths,
                                 "detail": f"Module '{stem}' exists in multiple directories: {paths}"})
    # Detect circular imports via simple AST scan
    import_graph: dict[str, set[str]] = {}
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(), filename=rel_path)
        except Exception:
            continue
        stem = Path(rel_path).stem
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                dep = node.module.split(".")[0]
                if dep in file_map:
                    deps.add(dep)
        if deps:
            import_graph[stem] = deps
    # Simple A->B->A cycle detection
    for a, a_deps in import_graph.items():
        for b in a_deps:
            if b in import_graph and a in import_graph[b]:
                pair = tuple(sorted([a, b]))
                detail = f"Circular import: {pair[0]} <-> {pair[1]}"
                if not any(f.get("detail") == detail for f in findings):
                    findings.append({"kind": "circular_import", "modules": list(pair), "detail": detail})
    status = "FAIL" if findings else "PASS"
    return {"name": "module_conflicts", "status": status, "findings": findings,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _get_installable_dependencies(repo_path: Path) -> list[str]:
    """Get dependency strings from pyproject.toml/requirements.txt for pip install.

    Unlike _get_declared_dependencies (which returns normalized names for matching),
    this returns the raw dependency specifiers suitable for ``pip install``.
    """
    deps: list[str] = []
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8")
            in_deps = False
            for line in content.splitlines():
                stripped = line.strip()
                if re.match(r"^dependencies\s*=\s*\[", stripped):
                    in_deps = True
                    for m in re.finditer(r'["\']([^"\']+)["\']', stripped):
                        deps.append(m.group(1))
                    if "]" in stripped.split("[", 1)[-1]:
                        in_deps = False
                    continue
                if in_deps:
                    if "]" in stripped:
                        for m in re.finditer(r'["\']([^"\']+)["\']', stripped):
                            deps.append(m.group(1))
                        in_deps = False
                        continue
                    m = re.match(r'["\']([^"\']+)["\']', stripped)
                    if m:
                        deps.append(m.group(1))
        except Exception:
            pass
    req_txt = repo_path / "requirements.txt"
    if req_txt.is_file() and not deps:
        try:
            for line in req_txt.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    deps.append(line)
        except Exception:
            pass
    return deps


def _create_dep_venv(
    repo_path: Path,
    timeout_s: int = 300,
) -> tuple[Path | None, Path | None]:
    """Create a temporary venv with the project's declared dependencies.

    Returns (python_path, venv_dir) or (None, None) on failure.
    """
    deps = _get_installable_dependencies(repo_path)
    if not deps:
        logger.info("No declared dependencies found — skipping dep venv")
        return None, None

    venv_dir = Path(tempfile.mkdtemp(prefix="saturnday-deps-"))
    try:
        logger.info(
            "Creating temp venv for API verification (%d deps)...", len(deps),
        )
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, timeout=60, check=True,
        )
        python = venv_dir / "bin" / "python"

        # Batch install first (fast path)
        result = subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet"] + deps,
            capture_output=True, timeout=timeout_s,
        )
        if result.returncode != 0:
            # Fallback: install individually so one failure doesn't block others
            logger.info("Batch pip install failed — trying packages individually...")
            for dep in deps:
                try:
                    subprocess.run(
                        [str(python), "-m", "pip", "install", "--quiet", dep],
                        capture_output=True, timeout=60,
                    )
                except Exception:
                    pkg_name = re.split(r"[><=!~\[]", dep)[0]
                    logger.info("Could not install %s (may need system libs)", pkg_name)

        logger.info("Dep venv ready at %s", venv_dir)
        return python, venv_dir
    except Exception as exc:
        logger.warning("Failed to create dep venv: %s", exc)
        shutil.rmtree(venv_dir, ignore_errors=True)
        return None, None


def _check_api_version(
    repo_path: Path,
    changed_files: list[str],
    *,
    run_shell_func,
    timeout_s: int,
    venv_python: str | None = None,
) -> dict:
    """Detect API version hallucinations: code using attrs/names that don't exist
    on the installed version of a library.

    Checks two patterns:
      1. ``from module import name`` — verifies *name* exists in *module*
      2. ``module.attr`` calls — verifies *attr* exists on the imported object

    Uses subprocess to actually inspect the installed package, so it catches
    version mismatches regardless of which version the LLM was trained on.
    """
    findings: list[dict] = []
    info_findings: list[dict] = []
    python_files = [f for f in changed_files if f.endswith(".py")]

    # Collect stdlib module names so we skip them (no version concern).
    stdlib_names: set[str] = set()
    if hasattr(sys, "stdlib_module_names"):
        stdlib_names = sys.stdlib_module_names
    else:
        stdlib_names = {
            "os", "sys", "re", "json", "pathlib", "collections", "functools",
            "itertools", "typing", "abc", "io", "math", "datetime", "time",
            "hashlib", "subprocess", "shutil", "tempfile", "unittest", "logging",
            "argparse", "ast", "copy", "dataclasses", "enum", "glob", "importlib",
            "inspect", "operator", "platform", "pprint", "string", "textwrap",
            "threading", "traceback", "uuid", "warnings", "contextlib", "csv",
            "fnmatch", "http", "urllib", "xml", "email", "html", "socket",
            "sqlite3", "pickle", "struct", "signal", "locale", "gettext",
            "configparser", "secrets", "statistics", "decimal", "fractions",
            "random", "shelve", "dbm", "gzip", "bz2", "lzma", "zipfile",
            "tarfile", "ctypes", "multiprocessing", "concurrent", "asyncio",
            "venv", "token", "tokenize", "dis", "compileall", "py_compile",
        }

    # Identify first-party package names so we skip them too.
    first_party: set[str] = {repo_path.name.lower().replace("-", "_")}
    src_dir = repo_path / "src"
    if src_dir.is_dir():
        try:
            for child in src_dir.iterdir():
                if child.is_dir() and (child / "__init__.py").exists():
                    first_party.add(child.name.lower().replace("-", "_"))
        except Exception:
            pass

    # Declared dependencies — packages listed in pyproject.toml / requirements.txt.
    declared_deps = _get_declared_dependencies(repo_path)

    # When a temp venv is provided, use its Python for import checks.
    python_exe = venv_python or sys.executable

    def _api_env() -> dict:
        """Build env dict with the correct Python on PATH."""
        env = os.environ.copy()
        env["PATH"] = f"{Path(python_exe).parent}:{env.get('PATH', '')}"
        return env

    # Cache: (module, name) → bool (exists or not).  Avoids re-running
    # the same subprocess multiple times.
    _cache: dict[tuple[str, str], bool] = {}

    # Cache: top-level module → bool (importable or not).
    _importable_cache: dict[str, bool] = {}

    def _package_importable(mod: str) -> bool:
        if mod in _importable_cache:
            return _importable_cache[mod]
        record = run_shell_func(
            [python_exe, "-c", f"import {mod}"],
            cwd=repo_path,
            timeout_s=timeout_s,
            env=_api_env(),
        )
        ok = record.get("status") == "RAN" and record.get("returncode") == 0
        _importable_cache[mod] = ok
        return ok

    def _attr_exists(module: str, name: str) -> bool:
        key = (module, name)
        if key in _cache:
            return _cache[key]
        # Use a subprocess to check — just like _check_imports does.
        snippet = f"import {module}; getattr({module}, {name!r})"
        env = _api_env()
        record = run_shell_func(
            [python_exe, "-c", snippet],
            cwd=repo_path,
            timeout_s=timeout_s,
            env=env,
        )
        ok = record.get("status") == "RAN" and record.get("returncode") == 0
        _cache[key] = ok
        return ok

    def _from_import_exists(module: str, name: str) -> bool:
        key = (f"from:{module}", name)
        if key in _cache:
            return _cache[key]
        snippet = f"from {module} import {name}"
        env = _api_env()
        record = run_shell_func(
            [python_exe, "-c", snippet],
            cwd=repo_path,
            timeout_s=timeout_s,
            env=env,
        )
        ok = record.get("status") == "RAN" and record.get("returncode") == 0
        _cache[key] = ok
        return ok

    for rel_path in sorted(python_files):
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue

        # Track local import aliases:  ``import X as Y``  →  Y maps to X
        # ``from X import Z``  →  Z maps to (X, Z)
        alias_map: dict[str, str] = {}  # local_name → top-level module

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    local = alias.asname or top
                    alias_map[local] = alias.name

            elif isinstance(node, ast.ImportFrom):
                if node.level > 0 or not node.module:
                    continue
                top = node.module.split(".")[0]
                top_norm = top.lower().replace("-", "_")
                if top_norm in stdlib_names or top_norm in first_party:
                    continue
                for alias in (node.names or []):
                    name = alias.name
                    if name == "*":
                        continue
                    if not _from_import_exists(node.module, name):
                        # Distinguish: whole package missing vs specific API missing
                        if not _package_importable(top):
                            if top_norm in declared_deps:
                                info_findings.append({
                                    "file": rel_path,
                                    "line": node.lineno,
                                    "kind": "declared_not_installed",
                                    "detail": (
                                        f"Package '{top}' declared in dependencies "
                                        f"but not installed — cannot verify "
                                        f"'from {node.module} import {name}'"
                                    ),
                                })
                            else:
                                findings.append({
                                    "file": rel_path,
                                    "line": node.lineno,
                                    "kind": "package_not_importable",
                                    "detail": (
                                        f"Package '{top}' is not installed and not "
                                        f"declared in dependencies — cannot verify "
                                        f"'from {node.module} import {name}'"
                                    ),
                                })
                        else:
                            findings.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "kind": "api_not_found",
                                "detail": (
                                    f"'from {node.module} import {name}' — "
                                    f"'{name}' does not exist in installed "
                                    f"version of '{top}'"
                                ),
                            })

            elif isinstance(node, ast.Attribute):
                # Pattern: X.some_attr where X was imported from a third-party lib
                if isinstance(node.value, ast.Name) and node.value.id in alias_map:
                    mod_full = alias_map[node.value.id]
                    top = mod_full.split(".")[0]
                    top_norm = top.lower().replace("-", "_")
                    if top_norm in stdlib_names or top_norm in first_party:
                        continue
                    if not _attr_exists(mod_full, node.attr):
                        # Distinguish: whole package missing vs specific attr missing
                        if not _package_importable(top):
                            if top_norm in declared_deps:
                                info_findings.append({
                                    "file": rel_path,
                                    "line": node.lineno,
                                    "kind": "declared_not_installed",
                                    "detail": (
                                        f"Package '{top}' declared in dependencies "
                                        f"but not installed — cannot verify "
                                        f"'{node.value.id}.{node.attr}'"
                                    ),
                                })
                            else:
                                findings.append({
                                    "file": rel_path,
                                    "line": node.lineno,
                                    "kind": "package_not_importable",
                                    "detail": (
                                        f"Package '{top}' is not installed and not "
                                        f"declared in dependencies — cannot verify "
                                        f"'{node.value.id}.{node.attr}'"
                                    ),
                                })
                        else:
                            findings.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "kind": "api_attr_not_found",
                                "detail": (
                                    f"'{node.value.id}.{node.attr}' — "
                                    f"attribute '{node.attr}' does not exist on "
                                    f"installed version of '{mod_full}'"
                                ),
                            })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "api_version_check",
        "status": status,
        "findings": findings + info_findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _record_tool_run(tool_runs: list[dict], name: str, cmd: list[str] | None, cwd: Path, record: dict) -> None:
    now = time.time()
    tool_runs.append(
        {
            "tool": name,
            "argv": cmd,
            "cwd": str(cwd),
            "status": record.get("status"),
            "exit_code": record.get("returncode"),
            "deny_reason": record.get("deny_reason"),
            "error": record.get("error"),
            "start_ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "duration_s": 0.0,
        }
    )


def _run_json_tool(
    name: str,
    cmd: list[str],
    cwd: Path,
    *,
    run_shell_func,
    timeout_s: int,
    strict: bool,
    required: bool,
    tool_runs: list[dict],
) -> tuple[dict, list]:
    env = _build_env()
    record = run_shell_func(cmd, cwd=cwd, timeout_s=timeout_s, env=env)
    _record_tool_run(tool_runs, name, cmd, cwd, record)
    stdout = record.get("stdout") or ""
    stderr = record.get("stderr") or ""
    output = stdout if stdout else stderr

    if record.get("status") != "RAN":
        if record.get("status") == "ERROR" and record.get("error") == "command_not_found":
            status = "FAIL" if strict and required else "SKIPPED"
            return (
                {
                    "name": name,
                    "status": status,
                    "exit_code": None,
                    "findings": [],
                    "raw_output": "",
                    "error": "command_not_found",
                },
                [],
            )
        reason = "command_timeout" if record.get("status") == "TIMEOUT" else "command_error"
        status = "FAIL" if strict and required else "SKIPPED"
        return (
            {
                "name": name,
                "status": status,
                "exit_code": record.get("returncode"),
                "findings": [],
                "raw_output": output,
                "error": reason,
            },
            [],
        )

    findings = []
    error = None
    status = "PASS"
    try:
        parsed = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        parsed = None
        error = "parse_error"
    if parsed is None:
        status = "FAIL"
    else:
        if name == "ruff":
            findings = parsed if isinstance(parsed, list) else []
            findings = _filter_ruff_findings(findings)
        elif name == "bandit":
            findings = parsed.get("results", []) if isinstance(parsed, dict) else []
            findings = _filter_bandit_findings(findings)
            findings = [
                {
                    "file": f.get("filename", ""),
                    "line": f.get("line_number"),
                    "detail": f.get("issue_text", ""),
                    "test_id": f.get("test_id", ""),
                    "issue_severity": f.get("issue_severity", ""),
                }
                for f in findings
            ]
        if record.get("returncode") != 0 or findings:
            status = "FAIL"
            if name == "bandit" and not findings and error is None:
                status = "PASS"
            if name == "ruff" and not findings and error is None:
                status = "PASS"
    return (
        {
            "name": name,
            "status": status,
            "exit_code": record.get("returncode"),
            "findings": findings,
            "raw_output": output,
            "error": error,
        },
        findings,
    )


def _run_pip_audit(
    repo_path: Path,
    changed_files: list[str],
    *,
    run_shell_func,
    timeout_s: int,
    strict: bool,
    tool_runs: list[dict],
) -> dict:
    relevant_paths = {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
    }
    changed_python_or_deps = any(
        path.endswith(".py") or Path(path).name in relevant_paths
        for path in changed_files
    )
    if not changed_python_or_deps:
        return {
            "name": "pip_audit",
            "status": "SKIPPED",
            "exit_code": None,
            "findings": [],
            "raw_output": "",
            "error": "not_applicable",
        }
    if not (repo_path / "pyproject.toml").exists() and not (repo_path / "requirements.txt").exists():
        return {
            "name": "pip_audit",
            "status": "SKIPPED",
            "exit_code": None,
            "findings": [],
            "raw_output": "",
            "error": "no_requirements",
        }
    env = _build_env()
    record = run_shell_func(
        ["pip-audit", "--format", "json"],
        cwd=repo_path,
        timeout_s=timeout_s,
        env=env,
    )
    _record_tool_run(tool_runs, "pip_audit", ["pip-audit", "--format", "json"], repo_path, record)
    stdout = record.get("stdout") or ""
    stderr = record.get("stderr") or ""
    output = stdout if stdout else stderr
    if record.get("status") != "RAN":
        if record.get("status") == "ERROR" and record.get("error") == "command_not_found":
            status = "FAIL" if strict else "SKIPPED"
            return {
                "name": "pip_audit",
                "status": status,
                "exit_code": None,
                "findings": [],
                "raw_output": "",
                "error": "command_not_found",
            }
        reason = "command_timeout" if record.get("status") == "TIMEOUT" else "command_error"
        status = "FAIL" if strict else "SKIPPED"
        return {
            "name": "pip_audit",
            "status": status,
            "exit_code": record.get("returncode"),
            "findings": [],
            "raw_output": output,
            "error": reason,
        }
    try:
        parsed = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return {
            "name": "pip_audit",
            "status": "SKIPPED",
            "exit_code": record.get("returncode"),
            "findings": [],
            "raw_output": output,
            "error": "parse_error",
        }
    findings = parsed if isinstance(parsed, list) else []
    has_vulns = False
    for entry in findings:
        vulns = entry.get("vulns") if isinstance(entry, dict) else None
        if vulns:
            has_vulns = True
            break
    status = "FAIL" if has_vulns else "PASS"
    return {
        "name": "pip_audit",
        "status": status,
        "exit_code": record.get("returncode"),
        "findings": findings,
        "raw_output": output,
        "error": None,
    }


def _check_shellcheck(
    repo_path: Path,
    changed_files: list[str],
    *,
    run_shell_func,
    timeout_s: int,
    strict: bool,
    tool_runs: list[dict],
) -> dict:
    shell_targets = [p for p in changed_files if p.endswith(".sh")]
    if not shell_targets:
        return {
            "name": "shellcheck",
            "status": "PASS",
            "exit_code": 0,
            "findings": [],
            "raw_output": "no shell files",
            "error": None,
        }
    env = _build_env()
    record = run_shell_func(
        ["shellcheck", "-f", "json", *shell_targets],
        cwd=repo_path,
        timeout_s=timeout_s,
        env=env,
    )
    _record_tool_run(tool_runs, "shellcheck", ["shellcheck", "-f", "json", *shell_targets], repo_path, record)
    stdout = record.get("stdout") or ""

    if record.get("status") != "RAN":
        if record.get("status") == "ERROR" and record.get("error") == "command_not_found":
            status = "FAIL" if strict else "SKIPPED"
            return {
                "name": "shellcheck",
                "status": status,
                "exit_code": None,
                "findings": [],
                "raw_output": "",
                "error": "command_not_found",
            }
        return {
            "name": "shellcheck",
            "status": "FAIL" if strict else "SKIPPED",
            "exit_code": record.get("returncode"),
            "findings": [],
            "raw_output": stdout,
            "error": "command_error",
        }

    try:
        findings = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return {
            "name": "shellcheck",
            "status": "SKIPPED",
            "exit_code": record.get("returncode"),
            "findings": [],
            "raw_output": stdout,
            "error": "parse_error",
        }
    if not isinstance(findings, list):
        findings = []

    # Filter out SC1090/SC1091 (unresolvable sourced files) and style/info level
    filtered = [
        f for f in findings
        if f.get("code") not in (1090, 1091)
        and f.get("level") in ("warning", "error")
    ]

    status = "FAIL" if filtered else "PASS"
    return {
        "name": "shellcheck",
        "status": status,
        "exit_code": record.get("returncode"),
        "findings": filtered,
        "raw_output": stdout,
        "error": None,
    }


def _check_line_continuations(repo_path: Path, changed_files: list[str]) -> dict:
    shell_targets = [p for p in changed_files if p.endswith(".sh")]
    if not shell_targets:
        return {
            "name": "line_continuations",
            "status": "PASS",
            "exit_code": 0,
            "findings": [],
            "raw_output": "no shell files",
            "error": None,
        }

    findings = []
    for rel_path in shell_targets:
        full_path = repo_path / rel_path
        if not full_path.is_file():
            continue
        try:
            lines = full_path.read_text().splitlines()
        except Exception:
            continue

        in_continuation = False
        for i, line in enumerate(lines):
            stripped = line.rstrip()

            # Detect trailing whitespace after backslash: "VAR=x \ " (space after \)
            raw_end = line.rstrip("\n").rstrip("\r")
            if "\\" in raw_end and raw_end != stripped:
                # raw_end has trailing whitespace that stripped removed
                bs_pos = raw_end.rfind("\\")
                after_bs = raw_end[bs_pos + 1:]
                if after_bs and after_bs.strip() == "":
                    findings.append({
                        "file": rel_path,
                        "line": i + 1,
                        "message": "trailing whitespace after backslash (not a continuation)",
                    })

            if in_continuation:
                # Blank line breaks the chain
                if not stripped:
                    findings.append({
                        "file": rel_path,
                        "line": i + 1,
                        "message": "blank line breaks continuation chain",
                    })
                    in_continuation = False
                    continue
                # New independent statement (contains = but doesn't end with \)
                # This detects: the run_plan.sh bug where EXTRA_ARGS="" was inserted
                if "=" in stripped and not stripped.endswith("\\") and not stripped.lstrip().startswith("#"):
                    # Check it looks like a variable assignment, not a continuation arg like --key=val
                    lstripped = stripped.lstrip()
                    if lstripped and lstripped[0].isalpha() and not lstripped.startswith("-"):
                        findings.append({
                            "file": rel_path,
                            "line": i + 1,
                            "message": "new statement breaks continuation chain",
                        })
                        in_continuation = False
                        continue

            # Update continuation state
            if stripped.endswith("\\"):
                in_continuation = True
            else:
                in_continuation = False

        # Orphaned backslash at EOF
        if lines and lines[-1].rstrip().endswith("\\"):
            findings.append({
                "file": rel_path,
                "line": len(lines),
                "message": "file ends with backslash continuation (orphaned)",
            })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "line_continuations",
        "status": status,
        "exit_code": 0,
        "findings": findings,
        "raw_output": "",
        "error": None,
    }


def _timed_tool_run(name: str, result: dict, tool_runs: list[dict], cwd: Path, start: float) -> None:
    duration = time.time() - start
    tool_runs.append(
        {
            "tool": name,
            "argv": None,
            "cwd": str(cwd),
            "status": result.get("status"),
            "exit_code": result.get("exit_code"),
            "deny_reason": None,
            "error": result.get("error"),
            "start_ts": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "duration_s": round(duration, 4),
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# Security governance checks (SEC-001 through SEC-018 + SEC-OPS-001)
# ─────────────────────────────────────────────────────────────────────────────

def _check_hardcoded_jwt(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-001: Detect hardcoded JWT secrets and fallback defaults."""
    findings = []
    _secret_var_pattern = re.compile(
        r"\b\w*(secret|jwt|signing_key|token_key|auth_key|private_key)\w*\b",
        re.IGNORECASE,
    )
    _env_fallback_pattern = re.compile(
        r"""os\.environ\.get\s*\(\s*['"][^'"]+['"]\s*,\s*['"]([^'"]+)['"]\s*\)""",
    )
    _jwt_encode_pattern = re.compile(
        r"""(jwt\.encode|jwt\.sign)\s*\(""",
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            # Check assignments: SECRET_KEY = "literal"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = ""
                    if isinstance(target, ast.Name):
                        name = target.id
                    elif isinstance(target, ast.Attribute):
                        name = target.attr
                    if _secret_var_pattern.search(name) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        val = node.value.value
                        if len(val) >= 2:  # Skip empty or single-char
                            findings.append({
                                "rule_id": "SEC-001",
                                "file": rel_path,
                                "line": node.lineno,
                                "kind": "hardcoded_secret",
                                "detail": f"JWT/auth secret is a string literal assigned to '{name}'",
                                "remediation": "Move secret to environment variable with no fallback default. Use os.environ['KEY'] (not os.environ.get with a literal fallback).",
                                "confidence": "high",
                                "cwe": "CWE-798",
                                "owasp": "A02:2021",
                            })

        # Check os.environ.get("KEY", "fallback") patterns via regex on source
        for lineno, line in enumerate(source.splitlines(), 1):
            m = _env_fallback_pattern.search(line)
            if m and _secret_var_pattern.search(line):
                fallback = m.group(1)
                if fallback.lower() not in ("", "none"):
                    findings.append({
                        "rule_id": "SEC-001",
                        "file": rel_path,
                        "line": lineno,
                        "kind": "env_fallback_secret",
                        "detail": f"Environment variable lookup has a string fallback default: '{fallback[:20]}...'",
                        "remediation": "Remove the fallback default. Use os.environ['KEY'] which raises KeyError if missing, or use None as fallback.",
                        "confidence": "high",
                        "cwe": "CWE-798",
                        "owasp": "A02:2021",
                    })

            # Check jwt.encode/sign with string literal as second arg
            if _jwt_encode_pattern.search(line):
                # Look for string literal in the line
                jwt_literal = re.search(r"""(jwt\.encode|jwt\.sign)\s*\([^,]+,\s*['"]([^'"]+)['"]""", line)
                if jwt_literal:
                    findings.append({
                        "rule_id": "SEC-001",
                        "file": rel_path,
                        "line": lineno,
                        "kind": "jwt_literal_secret",
                        "detail": f"JWT encode/sign called with a string literal secret",
                        "remediation": "Pass the secret from an environment variable or secure config, not a hardcoded string.",
                        "confidence": "high",
                        "cwe": "CWE-798",
                        "owasp": "A02:2021",
                    })

    return {
        "name": "hardcoded_jwt",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_weak_randomness(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-006: Detect non-cryptographic RNG in auth-adjacent contexts."""
    findings = []

    # Auth-adjacent context keywords in variable names, function names, or nearby code
    _auth_context = re.compile(
        r"\b(token|secret|session|reset|invite|code|otp|key|nonce|salt|password|room_code|api_key|auth)\b",
        re.IGNORECASE,
    )
    # Weak random patterns
    _weak_random_calls = re.compile(
        r"\brandom\.(random|randint|choice|choices|randrange|sample|shuffle|uniform)\s*\(",
    )
    _weak_uuid_pattern = re.compile(r"\buuid\.uuid1\s*\(")
    _timestamp_seed = re.compile(r"\brandom\.seed\s*\(\s*(time\.|int\(time)")

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        lines = source.splitlines()
        for lineno, line in enumerate(lines, 1):
            # Check context: is this line or nearby lines auth-related?
            context_window = "\n".join(lines[max(0, lineno - 4):min(len(lines), lineno + 3)])

            if _weak_random_calls.search(line) and _auth_context.search(context_window):
                findings.append({
                    "rule_id": "SEC-006",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "weak_random_in_auth",
                    "detail": f"Non-cryptographic random used in auth-adjacent context. Use secrets module instead.",
                    "remediation": "Replace random.* with secrets.token_hex(), secrets.token_urlsafe(), or secrets.randbelow().",
                    "confidence": "medium",
                    "cwe": "CWE-338",
                    "owasp": "A02:2021",
                })

            if _weak_uuid_pattern.search(line) and _auth_context.search(context_window):
                findings.append({
                    "rule_id": "SEC-006",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "weak_uuid1",
                    "detail": "uuid.uuid1() is MAC/time-based, not cryptographically random. Use uuid.uuid4() or secrets.",
                    "remediation": "Replace uuid.uuid1() with uuid.uuid4() or secrets.token_hex().",
                    "confidence": "medium",
                    "cwe": "CWE-338",
                    "owasp": "A02:2021",
                })

            if _timestamp_seed.search(line):
                findings.append({
                    "rule_id": "SEC-006",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "timestamp_seed",
                    "detail": "random.seed() with timestamp makes output predictable.",
                    "remediation": "Use secrets module instead of seeded random for security-sensitive values.",
                    "confidence": "high",
                    "cwe": "CWE-338",
                    "owasp": "A02:2021",
                })

    return {
        "name": "weak_randomness",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_jwt_verification_policy(repo_path: Path, changed_files: list[str], *, policy=None) -> dict:
    """SEC-008: Check JWT verification for algorithm pinning, alg=none, and claim validation.

    Policy-driven: if jwt_policy is configured, checks against it.
    In strict mode with no jwt_policy configured and JWT verification detected,
    emits error-severity finding.
    """
    findings = []
    _decode_pattern = re.compile(r"\bjwt\.(decode|verify)\s*\(")
    _algorithms_kwarg = re.compile(r"\balgorithms\s*=\s*\[")
    _alg_none = re.compile(r"""['"]none['"]""", re.IGNORECASE)
    _options_verify = re.compile(r"""verify_(iss|aud|exp|nbf|iat)\b""")
    jwt_detected = False

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        lines = source.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _decode_pattern.search(line):
                continue
            jwt_detected = True

            # Check context: current + next 3 lines for multi-line call
            context = "\n".join(lines[lineno - 1:min(len(lines), lineno + 3)])

            # Check algorithms param is pinned
            if not _algorithms_kwarg.search(context):
                findings.append({
                    "rule_id": "SEC-008",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "unpinned_algorithm",
                    "detail": "jwt.decode/verify called without pinning algorithms. Attacker can choose algorithm via token header.",
                    "remediation": "Always pass algorithms=['HS256'] (or your specific algorithm) to jwt.decode().",
                    "confidence": "high",
                    "cwe": "CWE-327",
                    "owasp": "A02:2021",
                })

            # Check for alg=none in allowed algorithms
            if _alg_none.search(context):
                findings.append({
                    "rule_id": "SEC-008",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "alg_none_allowed",
                    "detail": "alg='none' is allowed in JWT verification. This allows unsigned tokens.",
                    "remediation": "Remove 'none' from the algorithms list. Never allow unsigned JWTs.",
                    "confidence": "high",
                    "cwe": "CWE-327",
                    "owasp": "A02:2021",
                })

    # If strict mode and JWT verification detected but no jwt_policy configured
    if policy and jwt_detected and hasattr(policy, 'strict_mode') and policy.strict_mode:
        if not hasattr(policy, 'jwt_policy') or policy.jwt_policy is None:
            if not (hasattr(policy, 'jwt_scope') and policy.jwt_scope == "internal_single_issuer"):
                findings.append({
                    "rule_id": "SEC-008",
                    "file": "(policy)",
                    "line": 0,
                    "kind": "missing_jwt_policy",
                    "detail": "JWT verification detected but no jwt_policy configured. In strict mode, explicit algorithm, issuer, and audience policy is required.",
                    "remediation": "Add jwt_policy section to .saturnday-policy.yml with allowed_algorithms, require_issuer, require_audience. Or set jwt_scope: internal_single_issuer for minimal fallback.",
                    "confidence": "high",
                    "cwe": "CWE-327",
                    "owasp": "A02:2021",
                })

    return {
        "name": "jwt_verification_policy",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_cookie_security_hard(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-005: Critical cookie misconfigurations (HARD failures)."""
    findings = []
    _set_cookie = re.compile(r"""(set_cookie|res\.cookie|response\.set_cookie|Set-Cookie)\s*\(""")
    _samesite_none = re.compile(r"""samesite\s*[=:]\s*['"]?None['"]?""", re.IGNORECASE)
    _secure_flag = re.compile(r"""\bsecure\s*[=:]\s*(True|true|1)""", re.IGNORECASE)
    _httponly_flag = re.compile(r"""\bhttponly\s*[=:]\s*(True|true|1)""", re.IGNORECASE)
    _session_cookie_name = re.compile(r"""['"]?(session|refresh|token|access_token|sid|jwt)['"]?""", re.IGNORECASE)

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        lines = source.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _set_cookie.search(line):
                continue
            context = "\n".join(lines[lineno - 1:min(len(lines), lineno + 5)])

            # Check: SameSite=None without Secure
            if _samesite_none.search(context) and not _secure_flag.search(context):
                findings.append({
                    "rule_id": "SEC-005",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "samesite_none_no_secure",
                    "detail": "SameSite=None without Secure flag. Cookie will be rejected by browsers.",
                    "remediation": "Add Secure=True when using SameSite=None.",
                    "confidence": "high",
                    "cwe": "CWE-614",
                    "owasp": "A02:2021",
                })

            # Check: session/refresh cookie without HttpOnly
            if _session_cookie_name.search(context) and not _httponly_flag.search(context):
                findings.append({
                    "rule_id": "SEC-005",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "session_cookie_no_httponly",
                    "detail": "Session/refresh cookie set without HttpOnly. Cookie accessible via JavaScript (XSS risk).",
                    "remediation": "Add httponly=True to prevent JavaScript access to the cookie.",
                    "confidence": "high",
                    "cwe": "CWE-614",
                    "owasp": "A02:2021",
                })

    return {
        "name": "cookie_security_hard",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_cookie_security_soft(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-011: Non-critical cookie concerns (SOFT warnings)."""
    findings = []
    _set_cookie = re.compile(r"""(set_cookie|res\.cookie|response\.set_cookie|Set-Cookie)\s*\(""")
    _samesite_any = re.compile(r"""\bsamesite\s*[=:]""", re.IGNORECASE)
    _domain_broad = re.compile(r"""\bdomain\s*[=:]\s*['"]\.""", re.IGNORECASE)
    _session_cookie_name = re.compile(r"""['"]?(session|refresh|token|access_token|sid|jwt)['"]?""", re.IGNORECASE)

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        lines = source.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _set_cookie.search(line):
                continue
            context = "\n".join(lines[lineno - 1:min(len(lines), lineno + 5)])

            # Missing explicit SameSite
            if _session_cookie_name.search(context) and not _samesite_any.search(context):
                findings.append({
                    "rule_id": "SEC-011",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "missing_samesite",
                    "detail": "Session cookie without explicit SameSite. Browser default may be permissive.",
                    "remediation": "Set samesite='Lax' or samesite='Strict' explicitly.",
                    "confidence": "medium",
                    "cwe": "CWE-614",
                    "owasp": "A02:2021",
                })

            # Broad domain on sensitive cookies
            if _session_cookie_name.search(context) and _domain_broad.search(context):
                findings.append({
                    "rule_id": "SEC-011",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "broad_domain",
                    "detail": "Sensitive cookie with broad domain (starts with dot). May be shared with subdomains.",
                    "remediation": "Use a specific domain or omit domain to restrict to the current host.",
                    "confidence": "medium",
                    "cwe": "CWE-614",
                    "owasp": "A02:2021",
                })

    return {
        "name": "cookie_security_soft",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_token_revocation(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-007: Check that logout/password-reset handlers invalidate tokens."""
    findings = []
    _logout_handler = re.compile(r"""def\s+(logout|sign_out|signout|log_out)\s*\(""", re.IGNORECASE)
    _reset_handler = re.compile(r"""def\s+(reset_password|password_reset|forgot_password|change_password)\s*\(""", re.IGNORECASE)
    _invalidation_pattern = re.compile(
        r"""(blacklist|blocklist|revoke|invalidate|delete.*token|delete.*session|remove.*token|token.*delete|session.*delete|session.*clear|flush|logout_user|token_family|session_version)""",
        re.IGNORECASE,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            func_name = node.name
            is_logout = _logout_handler.search(f"def {func_name}(")
            is_reset = _reset_handler.search(f"def {func_name}(")
            if not (is_logout or is_reset):
                continue

            # Get the function body source
            func_lines = source.splitlines()[node.lineno - 1:node.end_lineno]
            func_body = "\n".join(func_lines)

            if not _invalidation_pattern.search(func_body):
                handler_type = "logout" if is_logout else "password-reset"
                findings.append({
                    "rule_id": "SEC-007",
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "missing_token_invalidation",
                    "detail": f"{handler_type} handler '{func_name}' does not appear to invalidate tokens or sessions.",
                    "remediation": "Add token blacklisting, session deletion, or token family rotation in the handler.",
                    "confidence": "medium",
                    "cwe": "CWE-613",
                    "owasp": "A07:2021",
                })

    return {
        "name": "token_revocation",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_token_expiry(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-012: Check JWT creation includes expiration."""
    findings = []
    _jwt_create = re.compile(r"""(jwt\.encode|create_access_token|create_refresh_token)\s*\(""")
    # Match 'exp' only as a dict key/assignment, not in prose comments.
    # Requires code-like context: quotes+colon for dict keys, = for assignments,
    # or timedelta which implies expiry calculation.
    _exp_pattern = re.compile(
        r"""(?x)
        ["']exp["']\s*[:\]]         |   # "exp": or "exp"] — dict key / access
        \bexp\s*=                   |   # exp= — keyword argument
        \bexpires_delta\s*[=:]      |   # FastAPI parameter in code context
        \bexpiresIn\s*[=:]          |   # JS/TS parameter in code context
        \baccess_token_expires\s*[=:]|  # common variable in code context
        \btimedelta\s*\(                # timedelta( — function call, not prose
        """,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        lines = source.splitlines()
        for lineno, line in enumerate(lines, 1):
            if not _jwt_create.search(line):
                continue
            # Check function-scoped context: from the nearest def/class above
            # to 5 lines below. This catches payload dicts defined earlier in
            # the same function without false-matching exp in other functions.
            func_start = 0
            for j in range(lineno - 2, -1, -1):
                if lines[j].lstrip().startswith(("def ", "class ", "async def ")):
                    func_start = j
                    break
            context = "\n".join(lines[func_start:min(len(lines), lineno + 4)])
            if not _exp_pattern.search(context):
                findings.append({
                    "rule_id": "SEC-012",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "missing_token_expiry",
                    "detail": "JWT created without expiration (exp claim). Token may be valid indefinitely.",
                    "remediation": "Add 'exp' claim to the JWT payload or use expiresIn/expires_delta parameter.",
                    "confidence": "medium",
                    "cwe": "CWE-613",
                    "owasp": "A07:2021",
                })

    return {
        "name": "token_expiry",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_csrf_state_change(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-009: CSRF protection on cookie-backed state-changing requests.

    Decision model: fires only when cookie-based auth is confirmed present
    AND state-changing methods exist without CSRF mitigation.
    SameSite alone does NOT satisfy — it is defense-in-depth per OWASP.
    """
    findings = []
    _cookie_auth_indicators = re.compile(
        r"""(session|login_required|@login_required|cookie|credentials.*include|csrf_exempt|set_cookie|SESSION_COOKIE|session_cookie)""",
        re.IGNORECASE,
    )
    _state_change_route = re.compile(
        r"""@\w+\.(post|put|patch|delete)\s*\(""",
        re.IGNORECASE,
    )
    _state_change_methods = re.compile(
        r"""methods\s*=\s*\[.*?(POST|PUT|PATCH|DELETE)""",
        re.IGNORECASE,
    )
    _any_route = re.compile(r"""@\w+\.route\s*\(""", re.IGNORECASE)
    _csrf_protection = re.compile(
        r"""(CSRFProtect|CSRFTokenField|csrf_token|_csrf\b|X-CSRF-Token|xsrf_token|anti_forgery|validate_csrf|check_csrf|verify_origin|request\.origin|request\.referrer|samesite\s*=\s*['"]?strict['"]?|SameSite\s*=\s*Strict|SESSION_COOKIE_SAMESITE\s*=\s*['"]Strict['"])""",
        re.IGNORECASE,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        # First: is cookie-based auth present in this file?
        if not _cookie_auth_indicators.search(source):
            continue

        # Is CSRF protection present anywhere in the file?
        file_has_csrf = _csrf_protection.search(source)

        lines = source.splitlines()
        for lineno, line in enumerate(lines, 1):
            # Find state-changing route definitions
            is_state_change = _state_change_route.search(line)
            if not is_state_change:
                # Check @app.route with methods= containing state-changing methods
                context = "\n".join(lines[lineno - 1:min(len(lines), lineno + 2)])
                if _any_route.search(line) and _state_change_methods.search(context):
                    is_state_change = True
                if not is_state_change:
                    continue

            if not file_has_csrf:
                findings.append({
                    "rule_id": "SEC-009",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "csrf_missing",
                    "detail": "State-changing route with cookie-based auth but no CSRF protection detected.",
                    "remediation": "Add CSRF mitigation: synchronizer token, double-submit cookie, or strict Origin/Referer validation. SameSite alone is not sufficient.",
                    "confidence": "medium",
                    "cwe": "CWE-352",
                    "owasp": "A01:2021",
                })

    return {
        "name": "csrf_state_change",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_oauth_flow_integrity(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-004: OAuth/social login integrity — state, PKCE, account linking."""
    findings = []
    _oauth_callback = re.compile(
        r"""(oauth.*callback|auth.*callback|social.*callback|login.*callback|oauth.*redirect|openid.*callback)\b""",
        re.IGNORECASE,
    )
    _state_validation = re.compile(r"""\bstate\b.*\b(verify|validate|check|compare|==|!=)\b""", re.IGNORECASE)
    _state_generation = re.compile(r"""\bstate\s*=\s*(secrets|uuid|token_|generate|random)""", re.IGNORECASE)
    _pkce_pattern = re.compile(r"""\b(code_verifier|code_challenge|pkce)\b""", re.IGNORECASE)
    _auto_link_email = re.compile(
        r"""(email|mail)\b.*\b(link|merge|connect|associate|find_or_create)""",
        re.IGNORECASE,
    )
    _redirect_from_input = re.compile(
        r"""redirect_uri\s*=\s*(request\.|params\[|args\[|query\.)""",
        re.IGNORECASE,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        if not _oauth_callback.search(source):
            continue

        # Check for state validation
        if not _state_validation.search(source) and not _state_generation.search(source):
            findings.append({
                "rule_id": "SEC-004",
                "file": rel_path,
                "line": 1,
                "kind": "missing_oauth_state",
                "detail": "OAuth callback handler without state/nonce validation. Vulnerable to CSRF.",
                "remediation": "Generate a random state value, bind it to the session, and validate it in the callback.",
                "confidence": "medium",
                "cwe": "CWE-352",
                "owasp": "A07:2021",
            })

        # Check for PKCE
        if not _pkce_pattern.search(source):
            findings.append({
                "rule_id": "SEC-004",
                "file": rel_path,
                "line": 1,
                "kind": "missing_pkce",
                "detail": "OAuth flow without PKCE (code_verifier/code_challenge).",
                "remediation": "Implement PKCE: generate code_verifier, compute code_challenge, send in authorization request.",
                "confidence": "low",
                "cwe": "CWE-352",
                "owasp": "A07:2021",
            })

        # Check for unsafe auto-linking on email
        if _auto_link_email.search(source):
            findings.append({
                "rule_id": "SEC-004",
                "file": rel_path,
                "line": 1,
                "kind": "unsafe_email_link",
                "detail": "Possible auto-linking of social account on email match without verification.",
                "remediation": "Do not auto-link accounts on email match alone. Require prior email verification.",
                "confidence": "low",
                "cwe": "CWE-287",
                "owasp": "A07:2021",
            })

        # Check for redirect_uri from user input
        for lineno, line in enumerate(source.splitlines(), 1):
            if _redirect_from_input.search(line):
                findings.append({
                    "rule_id": "SEC-004",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "redirect_from_input",
                    "detail": "redirect_uri constructed from user input. Potential open redirector.",
                    "remediation": "Use a hardcoded or allowlisted redirect_uri. Do not build it from request parameters.",
                    "confidence": "high",
                    "cwe": "CWE-601",
                    "owasp": "A07:2021",
                })

    return {
        "name": "oauth_flow_integrity",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_auth_bypass(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-002: Routes without auth in the middleware/decorator/DI chain."""
    findings = []
    _route_decorator = re.compile(
        r"""@\w+\.(route|get|post|put|patch|delete|api_route)\s*\(\s*['"]([^'"]+)['"]""",
    )
    _auth_decorators = re.compile(
        r"""@(login_required|jwt_required|requires_auth|permission_required|authenticated|auth_required|permissions_required|has_permission)""",
        re.IGNORECASE,
    )
    _fastapi_depends_auth = re.compile(
        r"""Depends\s*\(\s*(get_current_user|require_auth|verify_token|auth|authenticate|get_user)""",
        re.IGNORECASE,
    )
    _public_routes = {
        "/health", "/healthz", "/ready", "/readiness", "/live", "/liveness",
        "/login", "/signin", "/register", "/signup",
        "/docs", "/redoc", "/openapi.json", "/swagger",
        "/", "/favicon.ico", "/robots.txt",
    }

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Check decorators for route
            route_path = None
            has_auth_decorator = False

            for dec in node.decorator_list:
                dec_source = ast.get_source_segment(source, dec) or ""
                dec_text = f"@{dec_source}"
                route_match = _route_decorator.search(dec_text)
                if route_match:
                    route_path = route_match.group(2)
                if _auth_decorators.search(dec_text):
                    has_auth_decorator = True

            if route_path is None:
                continue

            # Skip known public routes
            clean_path = route_path.split("?")[0].rstrip("/") or "/"
            if clean_path in _public_routes:
                continue

            # Check function signature for Depends() auth (FastAPI)
            func_source = source.splitlines()[node.lineno - 1:node.end_lineno]
            func_text = "\n".join(func_source)
            has_depends_auth = _fastapi_depends_auth.search(func_text)

            if not has_auth_decorator and not has_depends_auth:
                findings.append({
                    "rule_id": "SEC-002",
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "route_no_auth",
                    "detail": f"Route '{route_path}' has no authentication decorator or Depends() auth.",
                    "remediation": "Add @login_required, @jwt_required, Depends(get_current_user), or equivalent auth middleware.",
                    "confidence": "medium",
                    "cwe": "CWE-284",
                    "owasp": "A01:2021",
                })

    return {
        "name": "auth_bypass",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_websocket_auth(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-003: WebSocket handlers without authentication."""
    findings = []
    _ws_handler = re.compile(
        r"""(@?socketio\.on|@socketio\.event|@sio\.event|@sio\.on|WebSocketConsumer|ws\.on_message|on_connect|on_open|SocketIO)""",
        re.IGNORECASE,
    )
    _ws_auth_pattern = re.compile(
        r"""(verify_token|authenticate\s*\(|get_current_user|require_auth|check_auth|validate_token|jwt\.decode|disconnect\s*\()""",
        re.IGNORECASE,
    )
    _origin_check = re.compile(r"""(ALLOWED_ORIGINS|allowed_origins|request\.origin|request\.headers.*origin|origin_allowlist|cors_origins|check_origin|validate_origin)\b""", re.IGNORECASE)

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        if not _ws_handler.search(source):
            continue

        # Find WS handler functions and check auth WITHIN them, not the whole file
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        ws_func_bodies = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Check if this function is a WS handler (has WS decorator or WS-related name)
            func_source = "\n".join(source.splitlines()[node.lineno - 1:node.end_lineno])
            dec_source = ""
            for d in node.decorator_list:
                dec_source += (ast.get_source_segment(source, d) or "") + "\n"
            if _ws_handler.search(dec_source) or _ws_handler.search(f"def {node.name}("):
                ws_func_bodies.append(func_source)

        ws_code = "\n".join(ws_func_bodies) if ws_func_bodies else source
        has_ws_auth = _ws_auth_pattern.search(ws_code)
        has_origin = _origin_check.search(source)  # Origin can be checked globally

        if not has_ws_auth:
            findings.append({
                "rule_id": "SEC-003",
                "file": rel_path,
                "line": 1,
                "kind": "ws_no_auth",
                "detail": "WebSocket handler detected without authentication. Connections may be unauthenticated.",
                "remediation": "Add authentication to the WebSocket handshake/connect handler. Verify JWT or session on connection.",
                "confidence": "medium",
                "cwe": "CWE-287",
                "owasp": "A07:2021",
            })

        if not has_origin:
            findings.append({
                "rule_id": "SEC-003",
                "file": rel_path,
                "line": 1,
                "kind": "ws_no_origin_check",
                "detail": "WebSocket handler without Origin validation.",
                "remediation": "Validate the Origin header against an allowlist on every WebSocket handshake.",
                "confidence": "low",
                "cwe": "CWE-346",
                "owasp": "A07:2021",
            })

    return {
        "name": "websocket_auth",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_rate_limit_wiring(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-010: Rate limiting imported but never applied to routes."""
    findings = []
    _limiter_import = re.compile(
        r"""(flask_limiter|slowapi|express.rate.limit|RateLimiter|Limiter)\b""",
    )
    _limiter_applied = re.compile(
        r"""(@limiter\.|\.limit\(|app\.use\s*\(\s*limiter|limiter\.limit|rate_limit)""",
        re.IGNORECASE,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        if _limiter_import.search(source) and not _limiter_applied.search(source):
            findings.append({
                "rule_id": "SEC-010",
                "file": rel_path,
                "line": 1,
                "kind": "rate_limit_not_applied",
                "detail": "Rate limiter imported/defined but never applied to any route.",
                "remediation": "Wire the rate limiter to auth endpoints: @limiter.limit() or app.use(limiter).",
                "confidence": "medium",
                "cwe": "CWE-307",
                "owasp": "A07:2021",
            })

    return {
        "name": "rate_limit_wiring",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_rate_limit_backend_quality(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-018: Rate limiting with in-memory store in production context."""
    findings = []
    _limiter_import = re.compile(r"""(flask_limiter|slowapi|Limiter)\b""")
    _memory_store = re.compile(r"""memory://|MemoryStore|default_limits""", re.IGNORECASE)
    _external_store = re.compile(r"""(redis|memcached|RedisStore|MemcachedStore|storage_uri)""", re.IGNORECASE)
    _production_marker = re.compile(r"""(Dockerfile|Procfile|docker-compose|gunicorn|uvicorn.*workers)""", re.IGNORECASE)

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        if not _limiter_import.search(source):
            continue
        if _external_store.search(source):
            continue  # Has external store configured — OK

        # Check if production context
        all_files_content = ""
        for f in changed_files:
            fp = repo_path / f
            if fp.exists():
                try:
                    all_files_content += fp.read_text()
                except Exception:
                    pass

        is_production = _production_marker.search(all_files_content)

        findings.append({
            "rule_id": "SEC-018",
            "file": rel_path,
            "line": 1,
            "kind": "memory_store_rate_limit",
            "detail": "Rate limiter using default in-memory store. Does not sync across processes/servers.",
            "remediation": "Configure an external store (Redis, Memcached) for rate limiting in production.",
            "confidence": "medium" if not is_production else "high",
            "cwe": "CWE-307",
            "owasp": "A07:2021",
        })

    return {
        "name": "rate_limit_backend_quality",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_xss(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-013: Known dangerous XSS sinks."""
    findings = []
    _dangerous_sinks = [
        (re.compile(r"""\bMarkup\s*\("""), "Markup() with user data"),
        (re.compile(r"""\|\s*safe\b"""), "|safe filter in Jinja template"),
        (re.compile(r"""\binnerHTML\s*="""), "innerHTML assignment"),
    ]

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        for lineno, line in enumerate(source.splitlines(), 1):
            for pattern, desc in _dangerous_sinks:
                if pattern.search(line):
                    findings.append({
                        "rule_id": "SEC-013",
                        "file": rel_path,
                        "line": lineno,
                        "kind": "dangerous_xss_sink",
                        "detail": f"Dangerous XSS sink: {desc}",
                        "remediation": "Escape or sanitize user input before rendering. Avoid dangerous sinks.",
                        "confidence": "medium",
                        "cwe": "CWE-79",
                        "owasp": "A03:2021",
                    })

    return {
        "name": "xss_check",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_sql_injection(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-015: SQL string building (supplementary to Bandit B608)."""
    findings = []
    _sql_keywords = re.compile(r"""\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b""", re.IGNORECASE)
    _string_format = re.compile(r"""(\.format\s*\(|f['"]|%\s*\()""")

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        for lineno, line in enumerate(source.splitlines(), 1):
            if _sql_keywords.search(line) and _string_format.search(line):
                findings.append({
                    "rule_id": "SEC-015",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "sql_string_building",
                    "detail": "SQL query built with string formatting. Use parameterized queries instead.",
                    "remediation": "Replace string formatting with parameterized queries: cursor.execute('SELECT ... WHERE id = %s', (id,)).",
                    "confidence": "medium",
                    "cwe": "CWE-89",
                    "owasp": "A03:2021",
                })

    return {
        "name": "sql_injection",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_user_enumeration(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-017: Login/register/reset revealing account existence."""
    findings = []
    _auth_handler = re.compile(
        r"""def\s+(login|signin|sign_in|register|signup|sign_up|reset_password|forgot_password)\s*\(""",
        re.IGNORECASE,
    )
    _enumeration_messages = re.compile(
        r"""['"].*\b(user not found|email not found|account not found|no account|unknown user|email already|already registered|already exists|username taken)\b.*['"]""",
        re.IGNORECASE,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        if not _auth_handler.search(source):
            continue

        for lineno, line in enumerate(source.splitlines(), 1):
            if _enumeration_messages.search(line):
                findings.append({
                    "rule_id": "SEC-017",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "user_enumeration",
                    "detail": "Auth handler uses differentiated error message that reveals account existence.",
                    "remediation": "Use generic messages like 'Invalid credentials' for all login/register/reset failures.",
                    "confidence": "medium",
                    "cwe": "CWE-204",
                    "owasp": "A07:2021",
                })

    return {
        "name": "user_enumeration",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_client_trusted_logic(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-016: Server accepting client-supplied authority fields."""
    findings = []
    _trusted_fields = re.compile(
        r"""request\.(json|form|data|body)\s*(\[|\.get\s*\()\s*['"]?(score|role|balance|admin|is_admin|permissions|price|amount|rank|level|inventory)['"]?""",
        re.IGNORECASE,
    )

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        for lineno, line in enumerate(source.splitlines(), 1):
            if _trusted_fields.search(line):
                findings.append({
                    "rule_id": "SEC-016",
                    "file": rel_path,
                    "line": lineno,
                    "kind": "client_trusted_field",
                    "detail": "Server accepts client-supplied authority field without server-side validation.",
                    "remediation": "Do not trust client-supplied values for score, role, balance, admin, or price. Compute or validate server-side.",
                    "confidence": "low",
                    "cwe": "CWE-602",
                    "owasp": "A04:2021",
                })

    return {
        "name": "client_trusted_logic",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_idor(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-014: User-supplied IDs without ownership checks."""
    findings = []
    _id_param = re.compile(r"""def\s+\w+\s*\([^)]*\b(\w+_id)\b""")
    _ownership_check = re.compile(
        r"""(current_user|request\.user|get_current_user|req\.user)\b""",
        re.IGNORECASE,
    )
    _route_decorator = re.compile(r"""@\w+\.(route|get|post|put|patch|delete)""")

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Check if it's a route handler
            has_route = any(
                _route_decorator.search(f"@{ast.get_source_segment(source, d) or ''}")
                for d in node.decorator_list
            )
            if not has_route:
                continue

            # Check if function has *_id parameters
            id_params = [arg.arg for arg in node.args.args if arg.arg.endswith("_id")]
            if not id_params:
                continue

            # Check function body for ownership verification
            func_lines = source.splitlines()[node.lineno - 1:node.end_lineno]
            func_body = "\n".join(func_lines)

            if not _ownership_check.search(func_body):
                findings.append({
                    "rule_id": "SEC-014",
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "idor_no_ownership",
                    "detail": f"Route handler with ID param(s) {id_params} but no ownership check (current_user/request.user).",
                    "remediation": "Verify the authenticated user owns or has permission to access the resource identified by the ID parameter.",
                    "confidence": "low",
                    "cwe": "CWE-639",
                    "owasp": "A01:2021",
                })

    return {
        "name": "idor_check",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


_BUILTIN_GENERIC_TYPES: frozenset[str] = frozenset(
    ("list", "dict", "tuple", "set", "type", "frozenset")
)


def _check_python_version_compat(repo_path: Path, changed_files: list[str]) -> dict:
    """Check that built-in generic syntax (list[str], dict[str, Any], etc.) is not
    used without ``from __future__ import annotations`` in projects claiming
    Python <3.10 compatibility, or flag it as a warning in all projects.

    Built-in subscript generics (``list[str]`` instead of ``List[str]``) are
    only valid at runtime on Python 3.9+; as *annotations* they require either
    Python 3.10+ or the ``from __future__ import annotations`` guard (PEP 563).
    Without the guard the code will raise ``TypeError`` on Python 3.8/3.9 at
    the moment annotations are evaluated (e.g. by dataclasses or pydantic).
    """
    findings: list[dict] = []

    # Parse requires-python from pyproject.toml once, for the whole call.
    requires_python: str | None = None
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        try:
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("requires-python"):
                    # e.g. requires-python = ">=3.8" or requires-python = ">=3.9,<4"
                    m = re.search(r'["\']([^"\']+)["\']', stripped)
                    if m:
                        requires_python = m.group(1)
                        break
        except Exception:
            pass

    # Determine if the project explicitly claims <3.10 compatibility.
    # We look for any lower-bound specifier < 3.10 (e.g. ">=3.8", ">=3.9").
    claims_pre_310: bool = False
    if requires_python:
        # Extract all lower-bound specifiers like >=3.X or ==3.X
        for part in requires_python.split(","):
            part = part.strip()
            m = re.match(r">=\s*3\.(\d+)", part)
            if m and int(m.group(1)) < 10:
                claims_pre_310 = True
                break
            m = re.match(r"==\s*3\.(\d+)", part)
            if m and int(m.group(1)) < 10:
                claims_pre_310 = True
                break

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            continue

        # Check for future import at module top level.
        has_future_annotations = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and node.names
                and any(alias.name == "annotations" for alias in node.names)
            ):
                has_future_annotations = True
                break

        if has_future_annotations:
            # The future import covers all annotations in the file — skip.
            continue

        # Walk the AST looking for Subscript nodes where the value is a
        # built-in generic type name, inside annotation contexts.
        def _in_annotation(node: ast.AST, parent_map: dict) -> bool:
            """Return True if node is directly inside an annotation context."""
            # Handled via visitor below — this helper is not used.
            return False  # pragma: no cover

        # Collect violating nodes: ast.Subscript where .value is an ast.Name
        # in _BUILTIN_GENERIC_TYPES, and the subscript appears in an
        # annotation position (function arg, return, or variable annotation).
        violation_lines: list[int] = []

        for node in ast.walk(tree):
            annotation: ast.expr | None = None
            candidates: list[ast.expr] = []

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Return annotation
                if node.returns is not None:
                    candidates.append(node.returns)
                # Argument annotations
                all_args = (
                    node.args.args
                    + node.args.posonlyargs
                    + node.args.kwonlyargs
                    + ([node.args.vararg] if node.args.vararg else [])
                    + ([node.args.kwarg] if node.args.kwarg else [])
                )
                for arg in all_args:
                    if arg.annotation is not None:
                        candidates.append(arg.annotation)

            elif isinstance(node, ast.AnnAssign):
                # Variable annotation: x: list[str] = ...
                candidates.append(node.annotation)

            for ann in candidates:
                # Walk the annotation sub-tree for Subscript on built-in names.
                for sub in ast.walk(ann):
                    if (
                        isinstance(sub, ast.Subscript)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id in _BUILTIN_GENERIC_TYPES
                    ):
                        violation_lines.append(sub.value.lineno)

        seen: set[int] = set()
        for lineno in violation_lines:
            if lineno in seen:
                continue
            seen.add(lineno)
            if claims_pre_310:
                detail = (
                    f"Uses built-in generic syntax (e.g. list[str]) without "
                    f"'from __future__ import annotations'. "
                    f"Project claims Python {requires_python} compatibility but "
                    f"this syntax requires Python 3.10+ at runtime."
                )
            else:
                detail = (
                    "Uses list[str] syntax without 'from __future__ import annotations'. "
                    "This requires Python 3.10+. Add the future import for 3.9 compatibility."
                )
            findings.append({
                "file": rel_path,
                "line": lineno,
                "kind": "python_version_compat",
                "detail": detail,
                "remediation": (
                    "Add 'from __future__ import annotations' at the top of the file, "
                    "or replace list[str] with typing.List[str] etc. for older Python support."
                ),
            })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "python_version_compat",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": [f for f in changed_files if f.endswith(".py")],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_security_event_logging(repo_path: Path, changed_files: list[str]) -> dict:
    """SEC-OPS-001: Security event logging (code-level proxy for operational readiness).

    Requires structured security events with mandatory fields.
    A bare logger.info("fail") does NOT pass.
    """
    findings = []
    _auth_handler = re.compile(
        r"""def\s+(login|signin|logout|sign_out|reset_password|forgot_password|authenticate|verify_token)\s*\(""",
        re.IGNORECASE,
    )
    _structured_log = re.compile(
        r"""(logger\.\w+\s*\(\s*\{|structlog|logging\.getLogger|log\.\w+\s*\(\s*['"].*event_type|extra\s*=\s*\{)""",
        re.IGNORECASE,
    )
    _bare_log = re.compile(r"""logger\.\w+\s*\(\s*['"]""")

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text()
        except Exception:
            continue

        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _auth_handler.search(f"def {node.name}("):
                continue

            func_lines = source.splitlines()[node.lineno - 1:node.end_lineno]
            func_body = "\n".join(func_lines)

            has_structured = _structured_log.search(func_body)
            has_bare = _bare_log.search(func_body)

            if not has_structured and not has_bare:
                findings.append({
                    "rule_id": "SEC-OPS-001",
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "missing_security_logging",
                    "detail": f"Auth handler '{node.name}' has no security event logging.",
                    "remediation": "Add structured logging with event_type, actor, outcome, reason, and correlation_id fields.",
                    "confidence": "medium",
                    "cwe": "CWE-778",
                    "owasp": "A09:2021",
                })
            elif has_bare and not has_structured:
                findings.append({
                    "rule_id": "SEC-OPS-001",
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "unstructured_security_logging",
                    "detail": f"Auth handler '{node.name}' has only bare string logging, not structured events.",
                    "remediation": "Use structured logging (dict/extra kwargs) with event_type, actor, outcome, reason, correlation_id.",
                    "confidence": "medium",
                    "cwe": "CWE-778",
                    "owasp": "A09:2021",
                })

    return {
        "name": "security_event_logging",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_tests_pass(repo_path: Path, changed_files: list[str]) -> dict:
    """Run project tests and flag failures."""
    findings = []

    # Detect test runner
    test_cmd = None
    if (repo_path / "package.json").is_file():
        try:
            pkg = json.loads((repo_path / "package.json").read_text())
            if pkg.get("scripts", {}).get("test"):
                test_cmd = ["npm", "test", "--", "--no-color"]
        except Exception:
            pass
    elif (repo_path / "pyproject.toml").is_file() or (repo_path / "setup.py").is_file():
        # Always run tests when Python source changes — not just when test
        # files are in the changeset.  A change to src/models.py can break
        # tests/test_models.py even if the test file is unchanged.
        if any(f.endswith(".py") for f in changed_files):
            # Use project venv python if available
            venv_py = repo_path / ".venv" / "bin" / "python"
            py_cmd = str(venv_py) if venv_py.is_file() else "python"
            test_cmd = [py_cmd, "-m", "pytest", "--tb=short", "-q"]

    if test_cmd:
        try:
            result = subprocess.run(
                test_cmd, cwd=str(repo_path),
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                # Extract failure summary
                output = (result.stdout + result.stderr)[-500:]
                findings.append({
                    "file": "tests",
                    "line": 0,
                    "kind": "tests_failing",
                    "detail": f"Tests failed (exit code {result.returncode}): {output[:200]}",
                })
            else:
                # Exit code 0 — but check for an empty test session.
                # pytest exits 0 with "collected 0 items" when there are no
                # test files.  This is a false pass: the project has not been
                # verified.
                combined_output = (result.stdout + result.stderr).lower()
                if (
                    "collected 0 items" in combined_output
                    or "no tests ran" in combined_output
                    or "no tests were run" in combined_output
                ):
                    findings.append({
                        "file": "tests",
                        "line": 0,
                        "kind": "tests_none_collected",
                        "detail": (
                            "Test runner exited 0 but collected no tests. "
                            "A project with no tests has not had its behaviour verified. "
                            "Add at least one test."
                        ),
                    })
        except subprocess.TimeoutExpired:
            findings.append({
                "file": "tests",
                "line": 0,
                "kind": "tests_timeout",
                "detail": "Tests timed out after 120 seconds",
            })
        except Exception:
            pass

    status = "FAIL" if findings else "PASS"
    return {
        "name": "tests_pass",
        "status": status,
        "severity": "error",
        "findings": findings,
        "files_checked": [],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_readme_language_consistency(repo_path: Path, changed_files: list[str]) -> dict:
    """Check if README mentions the wrong language for the project."""
    findings = []
    readme = repo_path / "README.md"
    if not readme.is_file():
        return {
            "name": "readme_consistency",
            "status": "PASS",
            "severity": "warning",
            "findings": [],
            "files_checked": [],
            "exit_code": 0,
            "raw_output": "",
            "error": None,
        }

    try:
        content = readme.read_text(encoding="utf-8").lower()
    except Exception:
        return {
            "name": "readme_consistency",
            "status": "PASS",
            "severity": "warning",
            "findings": [],
            "files_checked": [],
            "exit_code": 0,
            "raw_output": "",
            "error": None,
        }

    # Detect actual project language from files
    has_ts = any(f.endswith((".ts", ".tsx")) for f in changed_files)
    has_py = any(f.endswith(".py") for f in changed_files)
    has_js = any(f.endswith((".js", ".jsx")) for f in changed_files)

    # Check for language mismatches
    if has_ts and not has_py:
        # TypeScript project — README shouldn't say pip install or python
        for phrase in ["pip install", "python3", "pyproject.toml", "requires-python"]:
            if phrase in content:
                findings.append({
                    "file": "README.md",
                    "line": 0,
                    "kind": "readme_language_mismatch",
                    "detail": f"README mentions '{phrase}' but project is TypeScript (no Python files found)",
                })
                break

    if has_py and not has_ts and not has_js:
        # Python project — README shouldn't say npm install or node
        for phrase in ["npm install", "npx ", "package.json", "node "]:
            if phrase in content:
                findings.append({
                    "file": "README.md",
                    "line": 0,
                    "kind": "readme_language_mismatch",
                    "detail": f"README mentions '{phrase}' but project is Python (no JS/TS files found)",
                })
                break

    status = "FAIL" if findings else "PASS"
    return {
        "name": "readme_consistency",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": ["README.md"],
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


# ---------------------------------------------------------------------------
# DevOps governance checks (experimental, warning-only)
# ---------------------------------------------------------------------------

_DOCKERFILE_RE = re.compile(r'^(Dockerfile(\.[\w-]+)?|[\w-]+\.Dockerfile)$')


def _is_dockerfile(path: str) -> bool:
    """Match both Docker naming families."""
    return bool(_DOCKERFILE_RE.match(Path(path).name))


def _is_dockerignore(path: str) -> bool:
    name = Path(path).name
    return name == ".dockerignore" or name.endswith(".dockerignore")


def _devops_pass(name: str) -> dict:
    return {"name": name, "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [], "exit_code": 0,
            "raw_output": "", "error": None}


def _devops_skipped(name: str, error: str, files_checked: list[str] | None = None) -> dict:
    return {"name": name, "status": "SKIPPED", "severity": "warning",
            "findings": [], "files_checked": files_checked or [],
            "exit_code": None, "raw_output": "", "error": error}


def _trivy_available() -> bool:
    """Check if trivy binary is in PATH."""
    try:
        result = safe_subprocess_run(
            ["trivy", "--version"], capture_output=True, text=True, check=False, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _run_trivy_config(repo_path: Path, changed_files: list[str]) -> dict | None:
    """Run trivy config on changed IaC files only. One scan, cached result.

    Returns parsed JSON or None if trivy unavailable or no IaC files changed.
    """
    if not _trivy_available():
        return None

    iac_files = [f for f in changed_files
                 if f.endswith((".tf", ".hcl", ".yaml", ".yml"))
                 or _is_dockerfile(f)]
    if not iac_files:
        return None

    import shutil as _shutil
    try:
        with tempfile.TemporaryDirectory(prefix="saturnday-trivy-") as tmpdir:
            for f in iac_files:
                src = repo_path / f
                dst = Path(tmpdir) / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_file():
                    _shutil.copy2(src, dst)

            result = safe_subprocess_run(
                ["trivy", "config", "--format", "json", "--scanners", "misconfig",
                 tmpdir],
                capture_output=True, text=True, check=False, timeout=60,
            )

        if result.returncode not in (0, 1):
            return None
        import json as _json
        return _json.loads(result.stdout)
    except Exception:
        return None


def _check_dockerfile(repo_path: Path, changed_files: list[str]) -> dict:
    """Dockerfile checks: unpinned images, root user, ADD over COPY, secrets in build, missing dockerignore."""
    targets = [f for f in changed_files if _is_dockerfile(f)]
    dockerignore_changed = any(_is_dockerignore(f) for f in changed_files)
    if not targets and not dockerignore_changed:
        return _devops_pass("dockerfile")

    findings = []
    files_checked = list(targets)

    try:
        for rel in targets:
            full = repo_path / rel
            if not full.is_file():
                continue
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()

            has_user = False
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                # unpinned_base_image
                if stripped.upper().startswith("FROM "):
                    image_ref = stripped[5:].strip().split()[0] if len(stripped) > 5 else ""
                    if image_ref and image_ref.lower() != "scratch" and not image_ref.startswith("$"):
                        if "@sha256:" not in image_ref:
                            if ":latest" in image_ref or ":" not in image_ref:
                                findings.append({"file": rel, "line": i, "kind": "unpinned_base_image",
                                    "detail": f"Base image '{image_ref}' uses mutable tag. Pin to a digest (@sha256:...) for reproducible builds."})

                # dockerfile_run_as_root
                if stripped.upper().startswith("USER "):
                    has_user = True

                # dockerfile_add_over_copy
                if stripped.upper().startswith("ADD ") and not any(x in stripped for x in ("http://", "https://", ".tar", ".gz")):
                    findings.append({"file": rel, "line": i, "kind": "dockerfile_add_over_copy",
                        "detail": "ADD used where COPY would suffice. COPY is preferred unless extracting archives or fetching URLs."})

                # dockerfile_secret_in_build
                if stripped.upper().startswith(("ARG ", "ENV ")):
                    lower = stripped.lower()
                    if any(kw in lower for kw in ("password", "secret", "token", "api_key", "apikey", "private_key")):
                        findings.append({"file": rel, "line": i, "kind": "dockerfile_secret_in_build",
                            "detail": "Secret passed via ARG/ENV may persist in image layers. Use --mount=type=secret instead."})

            if not has_user:
                findings.append({"file": rel, "line": 0, "kind": "dockerfile_run_as_root",
                    "detail": "No USER directive — container runs as root. Add USER to switch to a non-root user."})

        # dockerfile_missing_dockerignore (heuristic)
        if targets or dockerignore_changed:
            has_root_dockerignore = (repo_path / ".dockerignore").is_file()
            for rel in targets:
                specific_ignore = repo_path / f"{rel}.dockerignore"
                if not specific_ignore.is_file() and not has_root_dockerignore:
                    findings.append({"file": rel, "line": 0, "kind": "dockerfile_missing_dockerignore",
                        "detail": "No .dockerignore found. The entire build context is sent to the daemon. Verify the actual build context used in CI or local builds."})

    except Exception as exc:
        return _devops_skipped("dockerfile", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "dockerfile", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_github_actions(repo_path: Path, changed_files: list[str]) -> dict:
    """GitHub Actions checks: SHA pinning, broad permissions, pull_request_target, secrets."""
    targets = [f for f in changed_files if ".github/workflows" in f and f.endswith((".yml", ".yaml"))]
    if not targets:
        return _devops_pass("github_actions")

    findings = []
    files_checked = list(targets)

    try:
        for rel in targets:
            full = repo_path / rel
            if not full.is_file():
                continue
            content = full.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            has_permissions = False
            for i, line in enumerate(lines, 1):
                stripped = line.strip()

                # github_action_not_sha_pinned
                if "uses:" in stripped:
                    uses_match = re.search(r'uses:\s*([^\s]+)', stripped)
                    if uses_match:
                        uses_val = uses_match.group(1)
                        if not uses_val.startswith("./") and "@" in uses_val:
                            ref = uses_val.split("@", 1)[1]
                            if not re.match(r'^[a-f0-9]{40}$', ref):
                                kind = "branch_ref" if ref in ("master", "main", "dev") else "tag_ref"
                                findings.append({"file": rel, "line": i, "kind": "github_action_not_sha_pinned",
                                    "detail": f"Action '{uses_val}' pinned to {kind} '{ref}', not a commit SHA. Tags and branches are mutable."})

                # github_broad_permissions
                if re.match(r'^permissions:\s*write-all', stripped):
                    has_permissions = True
                    findings.append({"file": rel, "line": i, "kind": "github_broad_permissions",
                        "detail": "Workflow has write-all permissions. Scope permissions to the minimum required."})
                elif re.match(r'^permissions:', stripped):
                    has_permissions = True

                # github_pull_request_target
                if "pull_request_target" in stripped and not stripped.startswith("#"):
                    findings.append({"file": rel, "line": i, "kind": "github_pull_request_target",
                        "detail": "pull_request_target gives read-write GITHUB_TOKEN even for public forks. Ensure untrusted code cannot run with elevated permissions."})

                # github_self_hosted_runner_risk
                if "self-hosted" in stripped and "runs-on" in stripped:
                    findings.append({"file": rel, "line": i, "kind": "github_self_hosted_runner_risk",
                        "detail": "Self-hosted runner detected. If this is a public repo with forks, self-hosted runners can be persistently compromised. Verify repository visibility and fork settings."})

                # cicd_secret_in_plaintext
                if re.search(r'(password|token|secret|api_key)\s*[:=]\s*["\'][^$\{]', stripped, re.IGNORECASE):
                    if not stripped.startswith("#"):
                        findings.append({"file": rel, "line": i, "kind": "cicd_secret_in_plaintext",
                            "detail": "Possible plaintext secret in workflow file. Use GitHub Secrets instead."})

                # cicd_no_verify
                if "--no-verify" in stripped and not stripped.startswith("#"):
                    findings.append({"file": rel, "line": i, "kind": "cicd_no_verify",
                        "detail": "--no-verify bypasses pre-commit hooks and governance checks."})

                # github_cloud_secret_reference
                if re.search(r'secrets\.(AWS_|AZURE_|GCP_|GOOGLE_)', stripped):
                    findings.append({"file": rel, "line": i, "kind": "github_cloud_secret_reference",
                        "detail": "References a cloud provider secret. Consider OIDC for short-lived tokens if your provider supports it."})

            # Missing permissions (heuristic — repo defaults may be restrictive)
            if not has_permissions:
                findings.append({"file": rel, "line": 0, "kind": "github_broad_permissions",
                    "detail": "No explicit permissions set. This workflow inherits repository defaults. If your repo defaults are broad, consider adding explicit permission scoping."})

    except Exception as exc:
        return _devops_skipped("github_actions", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "github_actions", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_gitlab_ci(repo_path: Path, changed_files: list[str]) -> dict:
    """GitLab CI checks: secrets in YAML, docker-in-docker patterns."""
    targets = [f for f in changed_files if Path(f).name == ".gitlab-ci.yml"]
    if not targets:
        return _devops_pass("gitlab_ci")

    findings = []
    files_checked = list(targets)

    try:
        for rel in targets:
            full = repo_path / rel
            if not full.is_file():
                continue
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue

                # gitlab_secret_in_yaml
                if re.search(r'(password|token|secret|api_key|private_key)\s*[:=]\s*["\'][^$\{]', stripped, re.IGNORECASE):
                    findings.append({"file": rel, "line": i, "kind": "gitlab_secret_in_yaml",
                        "detail": "Secret-like variable in .gitlab-ci.yml. Variables in YAML are visible to anyone with repo access. Use CI/CD settings or external secret store."})

                # gitlab_dind_or_privileged_pattern
                if "docker:dind" in stripped or ("privileged" in stripped and "true" in stripped):
                    findings.append({"file": rel, "line": i, "kind": "gitlab_dind_or_privileged_pattern",
                        "detail": "Docker-in-Docker or privileged mode detected. This gives jobs elevated host access. Verify this is necessary."})

    except Exception as exc:
        return _devops_skipped("gitlab_ci", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "gitlab_ci", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_jenkinsfile(repo_path: Path, changed_files: list[str]) -> dict:
    """Jenkins checks: hardcoded credentials, plaintext passwords."""
    targets = [f for f in changed_files if Path(f).name == "Jenkinsfile"]
    if not targets:
        return _devops_pass("jenkinsfile")

    findings = []
    files_checked = list(targets)

    try:
        for rel in targets:
            full = repo_path / rel
            if not full.is_file():
                continue
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("//"):
                    continue

                # jenkins_hardcoded_credential
                if re.search(r'(password|token|secret|apiKey)\s*[:=]\s*["\'][^$\{]', stripped, re.IGNORECASE):
                    if "withCredentials" not in stripped:
                        findings.append({"file": rel, "line": i, "kind": "jenkins_hardcoded_credential",
                            "detail": "Hardcoded credential in Jenkinsfile. Use withCredentials binding instead."})

                # jenkins_plaintext_password
                if re.search(r'usernamePassword|string\s*\(\s*credentialsId', stripped):
                    pass  # This is correct usage — skip
                elif re.search(r'(PASSWORD|TOKEN)\s*=\s*["\'][A-Za-z0-9]', stripped):
                    findings.append({"file": rel, "line": i, "kind": "jenkins_plaintext_password",
                        "detail": "Plaintext password/token in pipeline. Use Jenkins credentials store."})

    except Exception as exc:
        return _devops_skipped("jenkinsfile", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "jenkinsfile", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_terraform(repo_path: Path, changed_files: list[str]) -> dict:
    """Terraform checks.

    Coverage (line-scoped unless noted):
      * tf_hardcoded_credential — access/secret key / password / token = literal
      * tf_public_access_cidr — 0.0.0.0/0 on a security-relevant attribute
      * tf_sensitive_in_state — long literal in a default = "..."
      * tf_s3_unencrypted — file declares an aws_s3_bucket but never
        mentions server_side_encryption_configuration (file-scope)
      * tf_s3_public_acl — acl = "public-read" / "public-read-write" /
        "authenticated-read"
      * tf_rds_unencrypted — storage_encrypted = false
      * tf_rds_public — publicly_accessible = true
      * tf_iam_wildcard_resource — "Resource": "*" in IAM policy
      * tf_iam_wildcard_action — "Action": "*" in IAM policy
      * tf_ecs_privileged — privileged = true
    """
    targets = [f for f in changed_files if f.endswith((".tf", ".hcl"))]
    if not targets:
        return _devops_pass("terraform")

    findings = []
    files_checked = list(targets)

    try:
        for rel in targets:
            full = repo_path / rel
            if not full.is_file():
                continue
            content = full.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()

            # File-scope flags for cross-line patterns.  Comments are
            # not stripped here on purpose — using the substring as a
            # cheap check for "does this file even declare encryption
            # anywhere?"  False positive only happens if the keyword
            # is mentioned in a comment with no actual encryption
            # block, which is rare enough that we accept the trade-off.
            _has_s3_bucket = bool(
                re.search(r'resource\s+"aws_s3_bucket"\s', content)
            )
            _has_s3_encryption = (
                "server_side_encryption_configuration" in content
            )

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue

                # tf_hardcoded_credential
                if re.search(r'(access_key|secret_key|password|token)\s*=\s*"[^$\{]', stripped, re.IGNORECASE):
                    findings.append({"file": rel, "line": i, "kind": "tf_hardcoded_credential",
                        "detail": "Hardcoded credential in Terraform file. Use variables, tfvars, or a secret manager."})

                # tf_public_access_cidr
                if "0.0.0.0/0" in stripped and re.search(r'(cidr|ingress|security_group|acl)', stripped, re.IGNORECASE):
                    findings.append({"file": rel, "line": i, "kind": "tf_public_access_cidr",
                        "detail": "Public access CIDR (0.0.0.0/0) on a security-relevant resource. Restrict to specific IPs."})

                # tf_sensitive_in_state
                if re.search(r'(default\s*=\s*"[A-Za-z0-9+/=]{20,}")', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_sensitive_in_state",
                        "detail": "Long literal value that may leak into .terraform state. Mark as sensitive or use a secret manager."})

                # tf_s3_unencrypted — emit one finding per aws_s3_bucket
                # declaration line, but only when the file as a whole
                # lacks a server_side_encryption_configuration block.
                if (
                    _has_s3_bucket and not _has_s3_encryption
                    and re.search(r'resource\s+"aws_s3_bucket"\s', stripped)
                ):
                    findings.append({"file": rel, "line": i, "kind": "tf_s3_unencrypted",
                        "detail": "S3 bucket has no server_side_encryption_configuration in this file. Add SSE (AES256 or aws:kms) at the bucket or aws_s3_bucket_server_side_encryption_configuration resource level."})

                # tf_s3_public_acl
                if re.search(r'\bacl\s*=\s*"(public-read|public-read-write|authenticated-read)"', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_s3_public_acl",
                        "detail": "S3 bucket ACL grants public or authenticated-AWS-user read access. Use a private bucket policy or signed URLs."})

                # tf_rds_unencrypted
                if re.search(r'\bstorage_encrypted\s*=\s*false\b', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_rds_unencrypted",
                        "detail": "RDS instance has storage_encrypted = false. Enable encryption at rest (storage_encrypted = true) and supply a kms_key_id when stronger key control is required."})

                # tf_rds_public
                if re.search(r'\bpublicly_accessible\s*=\s*true\b', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_rds_public",
                        "detail": "RDS instance has publicly_accessible = true. Restrict to a private subnet and access via VPC peering / VPN / bastion."})

                # tf_iam_wildcard_resource — match within JSON-ish IAM
                # policy strings embedded in Terraform.
                if re.search(r'"Resource"\s*:\s*"\*"', stripped) or re.search(r'"Resource"\s*:\s*\[\s*"\*"\s*\]', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_iam_wildcard_resource",
                        "detail": "IAM policy uses Resource = \"*\". Scope to specific ARNs to enforce least privilege."})

                # tf_iam_wildcard_action
                if re.search(r'"Action"\s*:\s*"\*"', stripped) or re.search(r'"Action"\s*:\s*\[\s*"\*"\s*\]', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_iam_wildcard_action",
                        "detail": "IAM policy uses Action = \"*\". Replace with the specific service:Operation entries the workload actually needs."})

                # tf_ecs_privileged
                if re.search(r'\bprivileged\s*=\s*true\b', stripped):
                    findings.append({"file": rel, "line": i, "kind": "tf_ecs_privileged",
                        "detail": "Container has privileged = true. Drop privileged mode and grant only the specific Linux capabilities the workload needs."})

    except Exception as exc:
        return _devops_skipped("terraform", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "terraform", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_kubernetes(repo_path: Path, changed_files: list[str]) -> dict:
    """Kubernetes checks: privileged containers, unpinned images, secrets in manifests, resource limits, readiness probes."""
    yaml_files = [f for f in changed_files if f.endswith((".yaml", ".yml"))]
    if not yaml_files:
        return _devops_pass("kubernetes")

    findings = []
    files_checked = []

    try:
        for rel in yaml_files:
            full = repo_path / rel
            if not full.is_file():
                continue
            content = full.read_text(encoding="utf-8", errors="replace")

            # Only check Kubernetes manifests (must have apiVersion and kind)
            if "apiVersion:" not in content or "kind:" not in content:
                continue

            files_checked.append(rel)
            lines = content.splitlines()

            has_limits = False
            has_readiness = False
            is_secret = False
            is_sealed = False

            for i, line in enumerate(lines, 1):
                stripped = line.strip()

                # Detect sealed/external secrets — exempt from secret finding
                if re.search(r'kind:\s*(ExternalSecret|SealedSecret|SecretStore|ClusterSecretStore)', stripped):
                    is_sealed = True
                if "external-secrets.io" in stripped or "sealedsecrets.bitnami.com" in stripped:
                    is_sealed = True

                # k8s_privileged_container
                if re.search(r'privileged:\s*true', stripped):
                    findings.append({"file": rel, "line": i, "kind": "k8s_privileged_container",
                        "detail": "Privileged container — has full host access. Disallowed under baseline and restricted Pod Security Standards."})

                # k8s_unpinned_image
                if re.search(r'image:\s*\S+:latest\b', stripped) or re.search(r'image:\s*\S+\s*$', stripped) and ":" not in stripped.split("image:")[-1]:
                    findings.append({"file": rel, "line": i, "kind": "k8s_unpinned_image",
                        "detail": "Container image uses :latest or no tag. Use a specific version or digest for reproducible deployments."})

                # Detect Secret kind
                if re.search(r'kind:\s*Secret\b', stripped):
                    is_secret = True

                if "resources:" in stripped:
                    has_limits = True
                if "readinessProbe:" in stripped:
                    has_readiness = True

            # k8s_secret_in_manifest (only plain Secrets, not sealed/external)
            if is_secret and not is_sealed:
                if re.search(r'(data:|stringData:)', content):
                    findings.append({"file": rel, "line": 0, "kind": "k8s_secret_in_manifest",
                        "detail": "Plain Secret with data committed to repo. Kubernetes stores Secrets unencrypted by default. Use ExternalSecret, SealedSecret, or encrypt at rest."})

            # k8s_no_resource_limits (info only)
            if not has_limits and "kind:" in content and "Deployment" in content:
                findings.append({"file": rel, "line": 0, "kind": "k8s_no_resource_limits",
                    "detail": "No resource limits defined. May be enforced at namespace level by LimitRange. Consider adding explicit limits."})

            # k8s_no_readiness_probe (info only)
            if not has_readiness and "kind:" in content and "Deployment" in content:
                findings.append({"file": rel, "line": 0, "kind": "k8s_no_readiness_probe",
                    "detail": "No readinessProbe defined. Without it, the service may receive traffic before it is ready."})

    except Exception as exc:
        return _devops_skipped("kubernetes", f"Check failed: {exc}", files_checked)

    if not files_checked:
        return _devops_pass("kubernetes")

    status = "FAIL" if findings else "PASS"
    return {"name": "kubernetes", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_config_security(repo_path: Path, changed_files: list[str]) -> dict:
    """Config file checks: permissive CORS, secrets in config. Skips test/dev/example directories."""
    _SKIP_DIRS = {"test", "tests", "dev", "local", "example", "examples", "sample", "samples", "fixture", "fixtures"}
    config_exts = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg")
    targets = []
    for f in changed_files:
        if not f.endswith(config_exts):
            continue
        parts = Path(f).parts
        if any(p.lower() in _SKIP_DIRS for p in parts[:-1]):
            continue
        # Skip CI/CD files (handled by other checks)
        if ".github" in f or Path(f).name == ".gitlab-ci.yml":
            continue
        targets.append(f)

    if not targets:
        return _devops_pass("config_security")

    findings = []
    files_checked = list(targets)

    try:
        for rel in targets:
            full = repo_path / rel
            if not full.is_file():
                continue
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()

            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue

                # config_permissive_cors
                if "Access-Control-Allow-Origin" in stripped and "*" in stripped:
                    findings.append({"file": rel, "line": i, "kind": "config_permissive_cors",
                        "detail": "Permissive CORS: Access-Control-Allow-Origin: *. Restrict to specific origins in production."})

                # config_secret_in_config
                if re.search(r'(password|secret|token|api_key|private_key)\s*[:=]\s*["\'][^$\{]', stripped, re.IGNORECASE):
                    findings.append({"file": rel, "line": i, "kind": "config_secret_in_config",
                        "detail": "Credential-like value in config file. Use environment variables or a secret manager."})

    except Exception as exc:
        return _devops_skipped("config_security", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "config_security", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_skill_md_operational(repo_path: Path, changed_files: list[str]) -> dict:
    """Check that SKILL.md has operational instructions, not just a description."""
    skill_md = repo_path / "SKILL.md"
    if not skill_md.is_file():
        return {"name": "skill_md_operational", "status": "PASS", "severity": "warning",
                "findings": [], "files_checked": [], "exit_code": 0,
                "raw_output": "no SKILL.md", "error": None}

    findings = []
    files_checked = ["SKILL.md"]

    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
        lower = content.lower()

        # Must have usage/invocation instructions
        usage_signals = [
            "usage", "how to use", "when to use", "invoke", "## use",
            "```bash", "```shell", "```python", "```typescript",
            "run ", "execute", "command",
        ]
        has_usage = sum(1 for kw in usage_signals if kw in lower) >= 2
        if not has_usage:
            findings.append({
                "file": "SKILL.md", "line": 0,
                "kind": "skill_md_no_usage",
                "detail": "SKILL.md has no usage instructions. An OpenClaw agent won't know how to invoke this skill. Add a 'When to Use' or 'Usage' section with invocation examples.",
            })

        # Must have input/output description
        io_signals = [
            "input", "output", "returns", "accepts", "parameter",
            "argument", "response", "result", "produces", "expects",
        ]
        has_io = sum(1 for kw in io_signals if kw in lower) >= 2
        if not has_io:
            findings.append({
                "file": "SKILL.md", "line": 0,
                "kind": "skill_md_no_io",
                "detail": "SKILL.md has no input/output description. Add what the skill accepts and what it returns.",
            })

    except Exception as exc:
        return _devops_skipped("skill_md_operational", f"Check failed: {exc}", files_checked)

    status = "FAIL" if findings else "PASS"
    return {"name": "skill_md_operational", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_project_entrypoint(repo_path: Path, changed_files: list[str]) -> dict:
    """Check that the project has a way to be invoked (bin, main, start, console_scripts).

    For Python projects: also verifies that each console_scripts target
    (module:function) resolves to an existing module file with the declared
    function present.
    """
    findings = []
    files_checked = []

    try:
        # Node/TS projects
        pkg_path = repo_path / "package.json"
        if pkg_path.is_file():
            files_checked.append("package.json")
            import json as _json_ep
            pkg = _json_ep.loads(pkg_path.read_text(encoding="utf-8"))
            has_bin = bool(pkg.get("bin"))
            has_main = bool(pkg.get("main"))
            scripts = pkg.get("scripts", {})
            has_start = "start" in scripts
            is_skill = (repo_path / "SKILL.md").is_file()
            has_cli_hint = any(kw in str(pkg).lower() for kw in ["cli", "command", "bin"])
            if not has_bin and not has_main and not has_start and (is_skill or has_cli_hint):
                findings.append({
                    "file": "package.json", "line": 0,
                    "kind": "project_no_entrypoint",
                    "detail": "package.json has no bin, main, or start script. The project cannot be invoked. Add a bin field or a start script.",
                })

        # Python projects
        pyproject_path = repo_path / "pyproject.toml"
        if pyproject_path.is_file():
            files_checked.append("pyproject.toml")
            content = pyproject_path.read_text(encoding="utf-8")
            has_scripts = "[project.scripts]" in content or "[tool.poetry.scripts]" in content
            has_console = "console_scripts" in content
            is_skill = (repo_path / "SKILL.md").is_file()
            has_cli_hint = "cli" in content.lower() or "command" in content.lower()
            if not has_scripts and not has_console and (is_skill or has_cli_hint):
                findings.append({
                    "file": "pyproject.toml", "line": 0,
                    "kind": "project_no_entrypoint",
                    "detail": "pyproject.toml has no entry points (scripts or console_scripts). The project cannot be run from the command line.",
                })
            else:
                # Fix 12.A / G8: Verify that each [project.scripts] target resolves
                # to an existing module file with the declared function present.
                # Pattern: mycli = "mypackage.cli:main"
                import re as _re_ep
                # Extract the [project.scripts] section content
                _section_re = _re_ep.compile(
                    r"\[project\.scripts\](.*?)(?=\n\[|\Z)",
                    _re_ep.DOTALL,
                )
                _entry_re = _re_ep.compile(
                    r"^\s*\w[\w.-]*\s*=\s*[\"']([^\"']+)[\"']",
                    _re_ep.MULTILINE,
                )
                _section_match = _section_re.search(content)
                if _section_match:
                    section_body = _section_match.group(1)
                    for entry_match in _entry_re.finditer(section_body):
                        target = entry_match.group(1).strip()
                        if ":" not in target:
                            findings.append({
                                "file": "pyproject.toml", "line": 0,
                                "kind": "script_target_malformed",
                                "detail": (
                                    f"Console script target '{target}' is not in "
                                    "'module.path:function' format."
                                ),
                            })
                            continue
                        module_path, func_name = target.rsplit(":", 1)
                        # Resolve module_path to a .py file
                        rel_parts = module_path.replace(".", "/")
                        candidate_files = [
                            repo_path / "src" / (rel_parts + ".py"),
                            repo_path / (rel_parts + ".py"),
                            repo_path / "src" / rel_parts / "__init__.py",
                            repo_path / rel_parts / "__init__.py",
                        ]
                        module_file = next(
                            (p for p in candidate_files if p.is_file()), None
                        )
                        if module_file is None:
                            # Suppress during scaffolding phase: if the repo has
                            # no Python source files at all, the module will be
                            # created by a later ticket — don't block the scaffold.
                            _any_py = bool(list(repo_path.rglob("*.py"))[:1])
                            if not _any_py:
                                continue
                            findings.append({
                                "file": "pyproject.toml", "line": 0,
                                "kind": "script_target_module_missing",
                                "detail": (
                                    f"Console script target '{target}': module "
                                    f"'{module_path}' could not be resolved to a "
                                    "Python file. The script will fail on install."
                                ),
                            })
                            continue
                        # Verify function exists via AST
                        try:
                            tree = ast.parse(
                                module_file.read_text(encoding="utf-8"),
                                filename=str(module_file),
                            )
                            top_level_names = {
                                node.name
                                for node in ast.walk(tree)
                                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                                and isinstance(getattr(node, "col_offset", None), int)
                                and node.col_offset == 0
                            }
                            if func_name not in top_level_names:
                                findings.append({
                                    "file": str(module_file.relative_to(repo_path)), "line": 0,
                                    "kind": "script_target_function_missing",
                                    "detail": (
                                        f"Console script target '{target}': function "
                                        f"'{func_name}' not found at top level of "
                                        f"'{module_file.relative_to(repo_path)}'. "
                                        "The script will fail to load."
                                    ),
                                })
                        except Exception:
                            pass  # Parse failures are caught by syntax check

    except Exception as exc:
        return _devops_skipped("project_entrypoint", f"Check failed: {exc}", files_checked)

    if not files_checked:
        return {"name": "project_entrypoint", "status": "PASS", "severity": "warning",
                "findings": [], "files_checked": [], "exit_code": 0,
                "raw_output": "no package config", "error": None}

    status = "FAIL" if findings else "PASS"
    return {"name": "project_entrypoint", "status": status, "severity": "warning",
            "findings": findings, "files_checked": files_checked,
            "exit_code": 1 if findings else 0, "raw_output": "", "error": None}


def _check_packaging_coverage(repo_path: Path, changed_files: list[str]) -> dict:
    """Detect first-party packages under src/ not covered by packaging config.

    Fix 12.A: Catches the pattern where a project imports from e.g.
    ``casework.foo`` but only declares ``agentic_casework_assistant*`` in
    ``[tool.setuptools.packages.find] include``.  The import works in editable
    mode (src/ is on sys.path) but breaks on a clean pip install.
    """
    findings = []
    files_checked = []

    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return {"name": "packaging_coverage", "status": "PASS",
                "findings": [], "files_checked": [], "exit_code": 0,
                "raw_output": "no pyproject.toml", "error": None}

    files_checked.append("pyproject.toml")

    try:
        content = pyproject_path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"name": "packaging_coverage", "status": "PASS",
                "findings": [], "files_checked": files_checked, "exit_code": 0,
                "raw_output": f"could not read pyproject.toml: {exc}", "error": None}

    # Determine source root (where = ["src"] is the default, but may be absent)
    import re as _re_pc
    # Extract [tool.setuptools.packages.find] where = [...]
    _where_re = _re_pc.compile(
        r"\[tool\.setuptools\.packages\.find\].*?where\s*=\s*\[(.*?)\]",
        _re_pc.DOTALL,
    )
    _where_match = _where_re.search(content)
    if _where_match:
        # Parse the first string value from the list
        _str_re = _re_pc.compile(r"[\"']([^\"']+)[\"']")
        _src_roots = [m.group(1) for m in _str_re.finditer(_where_match.group(1))]
    else:
        _src_roots = ["src"]  # setuptools default

    # Extract include patterns from packages.find
    _include_re = _re_pc.compile(
        r"\[tool\.setuptools\.packages\.find\].*?include\s*=\s*\[(.*?)\]",
        _re_pc.DOTALL,
    )
    _include_match = _include_re.search(content)
    if _include_match:
        _str_re2 = _re_pc.compile(r"[\"']([^\"']+)[\"']")
        include_patterns = [m.group(1) for m in _str_re2.finditer(_include_match.group(1))]
    else:
        include_patterns = []  # No include = all packages are included (no gap)

    # If there are no include restrictions, all packages are covered — pass.
    if not include_patterns:
        return {"name": "packaging_coverage", "status": "PASS",
                "findings": [], "files_checked": files_checked, "exit_code": 0,
                "raw_output": "no include restriction", "error": None}

    import fnmatch as _fnmatch

    # Discover first-party packages under each source root
    uncovered: list[str] = []
    for src_root_str in _src_roots:
        src_dir = repo_path / src_root_str
        if not src_dir.is_dir():
            continue
        for child in src_dir.iterdir():
            if not child.is_dir():
                continue
            if not (child / "__init__.py").is_file():
                continue
            pkg_name = child.name
            # Check if this package is covered by any include pattern
            covered = any(
                _fnmatch.fnmatch(pkg_name, pat.rstrip("*") + "*")
                or _fnmatch.fnmatch(pkg_name, pat)
                for pat in include_patterns
            )
            if not covered:
                uncovered.append(pkg_name)

    for pkg in sorted(uncovered):
        findings.append({
            "file": "pyproject.toml",
            "line": 0,
            "kind": "packaging_coverage_gap",
            "detail": (
                f"First-party package '{pkg}' exists under '{_src_roots[0]}/' "
                "but is not matched by any setuptools packages.find.include pattern. "
                "It will not be installed by pip and imports from it will fail "
                "outside editable mode."
            ),
        })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "packaging_coverage",
        "status": status,
        "findings": findings,
        "files_checked": files_checked,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_install_truth_smoke(repo_path: Path, changed_files: list[str]) -> dict:
    """Smoke-test that the project can be installed from its own packaging config.

    Fix 12.A / G7: Advisory-only.  Creates a minimal fresh venv, runs
    ``pip install --no-deps -e .`` to verify that pyproject.toml is valid and
    the declared packages are installable, then imports each first-party package.

    Severity: warning (advisory).  Does not block on failure in this first
    version — findings are surfaced for human review.

    Deliberately uses --no-deps to avoid blocking on missing system libraries;
    the goal is packaging-config truth, not full dependency resolution.
    """
    findings = []
    files_checked = []

    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.is_file():
        return {"name": "install_truth_smoke", "status": "PASS", "severity": "warning",
                "findings": [], "files_checked": [], "exit_code": 0,
                "raw_output": "no pyproject.toml — skipped", "error": None}

    files_checked.append("pyproject.toml")

    # Discover first-party packages to import-test after install
    first_party_pkgs: list[str] = []
    for search_dir in [repo_path / "src", repo_path]:
        if search_dir.is_dir():
            try:
                for child in search_dir.iterdir():
                    if child.is_dir() and (child / "__init__.py").is_file():
                        first_party_pkgs.append(child.name)
            except Exception:
                pass
    if not first_party_pkgs:
        return {"name": "install_truth_smoke", "status": "PASS", "severity": "warning",
                "findings": [], "files_checked": files_checked, "exit_code": 0,
                "raw_output": "no first-party packages found — skipped", "error": None}

    import tempfile as _tempfile_smoke
    venv_dir = None
    try:
        venv_dir = Path(_tempfile_smoke.mkdtemp(prefix="saturnday-smoke-"))
        # Create isolated venv
        _venv_result = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, timeout=60, check=False,
        )
        if _venv_result.returncode != 0:
            return {"name": "install_truth_smoke", "status": "SKIPPED", "severity": "warning",
                    "findings": [], "files_checked": files_checked, "exit_code": None,
                    "raw_output": "venv creation failed — skipped", "error": None}

        venv_python = venv_dir / "bin" / "python"
        if not venv_python.is_file():
            venv_python = venv_dir / "bin" / "python3"
        if not venv_python.is_file():
            return {"name": "install_truth_smoke", "status": "SKIPPED", "severity": "warning",
                    "findings": [], "files_checked": files_checked, "exit_code": None,
                    "raw_output": "venv python not found — skipped", "error": None}

        # Install project without deps (tests packaging config only)
        _install_result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--no-deps", "--quiet", "-e", str(repo_path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if _install_result.returncode != 0:
            install_output = (_install_result.stdout + _install_result.stderr)[-400:]
            findings.append({
                "file": "pyproject.toml",
                "line": 0,
                "kind": "install_truth_smoke_install_failed",
                "detail": (
                    "pip install --no-deps -e . failed in a fresh venv. "
                    "The project cannot be installed from its own packaging config. "
                    f"pip output: {install_output[:300]}"
                ),
            })
            # Don't try to import if install failed
        else:
            # Try importing each first-party package
            for pkg in first_party_pkgs:
                _import_result = subprocess.run(
                    [str(venv_python), "-c", f"import {pkg}"],
                    capture_output=True, text=True, timeout=30, check=False,
                    cwd=str(repo_path),
                )
                if _import_result.returncode != 0:
                    err_output = (_import_result.stdout + _import_result.stderr)[-300:]
                    findings.append({
                        "file": "pyproject.toml",
                        "line": 0,
                        "kind": "install_truth_smoke_import_failed",
                        "detail": (
                            f"Package '{pkg}' installed but failed to import in a fresh venv. "
                            f"Error: {err_output[:200]}"
                        ),
                    })

    except subprocess.TimeoutExpired:
        return {"name": "install_truth_smoke", "status": "SKIPPED", "severity": "warning",
                "findings": [], "files_checked": files_checked, "exit_code": None,
                "raw_output": "install smoke timed out — skipped", "error": None}
    except Exception as exc:
        return {"name": "install_truth_smoke", "status": "SKIPPED", "severity": "warning",
                "findings": [], "files_checked": files_checked, "exit_code": None,
                "raw_output": f"install smoke error — skipped: {exc}", "error": None}
    finally:
        if venv_dir is not None:
            shutil.rmtree(venv_dir, ignore_errors=True)

    status = "FAIL" if findings else "PASS"
    return {
        "name": "install_truth_smoke",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": files_checked,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


def _check_competing_ddl(repo_path: Path, changed_files: list[str]) -> dict:
    """Detect the same database table defined incompatibly in multiple places.

    Fix 12.A: Finds CREATE TABLE statements in .sql files and Python source
    files.  When the same table name appears in more than one place with
    different column sets, it flags the conflict.  This catches the pattern
    where schema.sql and a repository class both define the same table but
    with incompatible schemas.

    Only flags confirmed column-set mismatches, not mere repetition (some
    projects legitimately repeat CREATE TABLE IF NOT EXISTS).
    """
    import re as _re_ddl

    findings = []
    files_checked: list[str] = []

    # Regex to extract CREATE TABLE statements (basic, intentionally conservative)
    _create_table_re = _re_ddl.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?\s*\(([^;]+?)\)",
        _re_ddl.IGNORECASE | _re_ddl.DOTALL,
    )

    # Map: table_name_lower -> list of (source_file, frozenset_of_column_names)
    table_definitions: dict[str, list[tuple[str, frozenset[str]]]] = {}

    def _extract_columns(ddl_body: str) -> frozenset[str]:
        """Extract column names from a CREATE TABLE body."""
        cols: set[str] = set()
        for line in ddl_body.splitlines():
            line = line.strip().rstrip(",")
            # Skip constraint lines (PRIMARY KEY, FOREIGN KEY, UNIQUE, CHECK, INDEX)
            if _re_ddl.match(r"(?:PRIMARY|FOREIGN|UNIQUE|CHECK|INDEX|CONSTRAINT)\b", line, _re_ddl.IGNORECASE):
                continue
            if not line or line.startswith("--"):
                continue
            # First word is column name (may be quoted)
            m = _re_ddl.match(r"[\"'`]?(\w+)[\"'`]?", line)
            if m:
                col = m.group(1).lower()
                if col:
                    cols.add(col)
        return frozenset(cols)

    def _scan_text(text: str, source_file: str) -> None:
        for m in _create_table_re.finditer(text):
            tname = m.group(1).lower()
            body = m.group(2)
            cols = _extract_columns(body)
            if cols:  # Only record if we extracted at least one column
                table_definitions.setdefault(tname, []).append((source_file, cols))

    # Scan .sql files
    try:
        for sql_file in repo_path.rglob("*.sql"):
            rel = str(sql_file.relative_to(repo_path))
            # Skip migration directories (they may legitimately recreate tables)
            if any(part in ("migrations", "alembic", "flyway") for part in sql_file.parts):
                continue
            if any(part.startswith(".saturnday") for part in sql_file.parts):
                continue
            files_checked.append(rel)
            try:
                _scan_text(sql_file.read_text(encoding="utf-8", errors="replace"), rel)
            except Exception:
                pass
    except Exception:
        pass

    # Scan Python files for CREATE TABLE strings (in triple-quoted strings or
    # single-quoted string literals common in repository classes).
    # Test files are excluded: they contain intentional fixture DDL that is
    # not production schema and would produce false positives.
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        # Skip test files — fixture DDL is intentionally incompatible
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            if "CREATE TABLE" in text.upper():
                files_checked.append(rel_path)
                _scan_text(text, rel_path)
        except Exception:
            pass

    # Check for conflicts
    for tname, defs in table_definitions.items():
        if len(defs) < 2:
            continue
        # Compare column sets across definitions
        col_sets = [cols for _, cols in defs]
        # Find pairs with different column sets
        for i in range(len(col_sets)):
            for j in range(i + 1, len(col_sets)):
                if col_sets[i] != col_sets[j]:
                    only_in_i = col_sets[i] - col_sets[j]
                    only_in_j = col_sets[j] - col_sets[i]
                    diff_desc = []
                    if only_in_i:
                        diff_desc.append(f"only in {defs[i][0]}: {sorted(only_in_i)}")
                    if only_in_j:
                        diff_desc.append(f"only in {defs[j][0]}: {sorted(only_in_j)}")
                    findings.append({
                        "file": defs[i][0],
                        "line": 0,
                        "kind": "competing_ddl",
                        "detail": (
                            f"Table '{tname}' is defined in multiple places with "
                            f"incompatible column sets. {'; '.join(diff_desc)}. "
                            "Schema conflicts cause crashes depending on initialization order."
                        ),
                    })
                    break  # One finding per table is enough

    # Deduplicate findings by table name
    seen_tables: set[str] = set()
    deduped: list[dict] = []
    for f in findings:
        tname_key = f["detail"].split("'")[1] if "'" in f["detail"] else f["detail"]
        if tname_key not in seen_tables:
            seen_tables.add(tname_key)
            deduped.append(f)
    findings = deduped

    if not files_checked:
        return {"name": "competing_ddl", "status": "PASS", "severity": "warning",
                "findings": [], "files_checked": [], "exit_code": 0,
                "raw_output": "no SQL files or Python DDL found", "error": None}

    status = "FAIL" if findings else "PASS"
    return {
        "name": "competing_ddl",
        "status": status,
        "severity": "warning",
        "findings": findings,
        "files_checked": files_checked,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


# ---------------------------------------------------------------------------
# Fix 12.B: Truthfulness guards — generated-repo fabrication patterns
# ---------------------------------------------------------------------------


def _check_constant_risk_suppression(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-CRS: Detect module-level True constants suppressing safety branches.

    AI-generated code sometimes disables governance, auth, or security branches by
    setting a module-level flag constant to True.  Example: ``SKIP_GOVERNANCE = True``.
    These are harder to spot in review than a conditional because the flag looks like
    a legitimate feature toggle.  Only non-test Python files are scanned.
    """
    import ast as _ast_crs

    _RISK_PREFIXES = ("SKIP_", "DISABLE_", "BYPASS_", "SUPPRESS_", "NO_")
    _RISK_KEYWORDS = frozenset({
        "GOVERNANCE", "GUARD", "AUTH", "SECURITY", "VALIDATION",
        "CHECK", "SAFETY", "APPROVAL", "PERMISSION", "POLICY",
        "SCAN", "REVIEW", "GATE", "FILTER", "BLOCK",
    })

    findings: list[dict] = []
    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_crs.parse(text, filename=rel_path)
        except Exception:
            continue

        files_checked.append(rel_path)
        for node in tree.body:  # module-level only
            if not isinstance(node, _ast_crs.Assign):
                continue
            if not (isinstance(node.value, _ast_crs.Constant) and node.value.value is True):
                continue
            for target in node.targets:
                if not isinstance(target, _ast_crs.Name):
                    continue
                name_upper = target.id.upper()
                if (any(name_upper.startswith(p) for p in _RISK_PREFIXES)
                        and any(kw in name_upper for kw in _RISK_KEYWORDS)):
                    findings.append({
                        "file": rel_path,
                        "line": node.lineno,
                        "kind": "constant_risk_suppression",
                        "detail": (
                            f"Module-level constant '{target.id} = True' suppresses a safety "
                            f"or governance branch at compile time. Use runtime configuration "
                            f"or environment variables instead of a flag literal."
                        ),
                    })

    if not files_checked:
        return {
            "name": "constant_risk_suppression",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "constant_risk_suppression",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_schema_write_read_coherence(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-SWR: Detect Optional schema fields that are read but never written.

    Targets dataclass, TypedDict, and BaseModel-style classes in non-test Python files.
    For each field annotated ``Optional[X]``, ``X | None``, or defaulting to ``None``,
    checks whether a write/populate site (attribute assignment or constructor keyword
    argument) exists anywhere in the changed-file corpus.  If the field is accessed
    (read via attribute load) in the corpus but has no write site, it is a schema write
    gap: the field exists in the contract but nothing ever populates it — callers always
    receive ``None``.

    Bounded to the changed-file corpus only.  Cross-file writes within that corpus are
    recognised.  Only Optional/None-defaulted fields are inspected to reduce noise.
    Private (``_``-prefixed) and ``ClassVar`` fields are excluded.
    """
    import ast as _ast_swr

    # Collect non-test Python files
    py_files: list[str] = []
    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if full.is_file():
            py_files.append(rel_path)

    if not py_files:
        return {
            "name": "schema_write_read_coherence",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }

    # Step 1: collect Optional/None-defaulted field definitions from schema-like classes.
    # field_defs: field_name -> list of (rel_path, class_name, lineno)
    field_defs: dict[str, list[tuple[str, str, int]]] = {}

    for rel_path in py_files:
        full = repo_path / rel_path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_swr.parse(text, filename=rel_path)
        except Exception:
            continue

        for node in _ast_swr.walk(tree):
            if not isinstance(node, _ast_swr.ClassDef):
                continue
            # Only inspect schema-like classes: @dataclass, TypedDict subclasses, BaseModel
            is_dataclass = any(
                (isinstance(d, _ast_swr.Name) and d.id == "dataclass")
                or (isinstance(d, _ast_swr.Attribute) and d.attr == "dataclass")
                or (isinstance(d, _ast_swr.Call) and (
                    (isinstance(d.func, _ast_swr.Name) and d.func.id == "dataclass")
                    or (isinstance(d.func, _ast_swr.Attribute) and d.func.attr == "dataclass")
                ))
                for d in node.decorator_list
            )
            is_typed_dict = any(
                (isinstance(b, _ast_swr.Name) and b.id == "TypedDict")
                or (isinstance(b, _ast_swr.Attribute) and b.attr == "TypedDict")
                for b in node.bases
            )
            is_model = any(
                (isinstance(b, _ast_swr.Name) and b.id in ("BaseModel", "Model"))
                or (isinstance(b, _ast_swr.Attribute) and b.attr in ("BaseModel", "Model"))
                for b in node.bases
            )
            if not (is_dataclass or is_typed_dict or is_model):
                continue

            for item in node.body:
                if not isinstance(item, _ast_swr.AnnAssign):
                    continue
                if not isinstance(item.target, _ast_swr.Name):
                    continue
                fname = item.target.id
                if fname.startswith("_"):
                    continue  # private
                # Skip ClassVar
                ann = item.annotation
                if (isinstance(ann, _ast_swr.Subscript)
                        and isinstance(ann.value, _ast_swr.Name)
                        and ann.value.id == "ClassVar"):
                    continue

                # Determine if Optional / None-defaulted
                is_optional = False
                if isinstance(ann, _ast_swr.Subscript):
                    outer = ann.value
                    if isinstance(outer, _ast_swr.Name) and outer.id == "Optional":
                        is_optional = True
                # X | None  (Python 3.10+ union)
                if isinstance(ann, _ast_swr.BinOp) and isinstance(ann.op, _ast_swr.BitOr):
                    if (isinstance(ann.right, _ast_swr.Constant) and ann.right.value is None):
                        is_optional = True
                    if (isinstance(ann.left, _ast_swr.Constant) and ann.left.value is None):
                        is_optional = True
                # Default = None
                if (item.value is not None
                        and isinstance(item.value, _ast_swr.Constant)
                        and item.value.value is None):
                    is_optional = True

                if is_optional:
                    field_defs.setdefault(fname, []).append((rel_path, node.name, item.lineno))

    if not field_defs:
        return {
            "name": "schema_write_read_coherence",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": py_files,
            "exit_code": 0, "raw_output": "no Optional schema fields found", "error": None,
        }

    # Step 2: collect write sites (attribute assignment or constructor keyword arg)
    # and read sites (attribute load) across the entire corpus.
    write_sites: set[str] = set()
    read_sites: set[str] = set()

    for rel_path in py_files:
        full = repo_path / rel_path
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_swr.parse(text, filename=rel_path)
        except Exception:
            continue

        for node in _ast_swr.walk(tree):
            # Attribute write: self.field = ... or obj.field = ...
            if isinstance(node, _ast_swr.Assign):
                for target in node.targets:
                    if isinstance(target, _ast_swr.Attribute):
                        write_sites.add(target.attr)
            if (isinstance(node, _ast_swr.AugAssign)
                    and isinstance(node.target, _ast_swr.Attribute)):
                write_sites.add(node.target.attr)
            # Constructor keyword arg: ClassName(field=value)
            if isinstance(node, _ast_swr.Call):
                for kw in node.keywords:
                    if kw.arg:
                        write_sites.add(kw.arg)
            # Attribute read: obj.field (Load context)
            if (isinstance(node, _ast_swr.Attribute)
                    and isinstance(node.ctx, _ast_swr.Load)):
                read_sites.add(node.attr)

    # Step 3: flag fields that are read but never written in the corpus.
    findings: list[dict] = []
    for fname, defs in field_defs.items():
        if fname in write_sites:
            continue  # has a write site — coherent
        if fname not in read_sites:
            continue  # neither read nor written — not actively used, skip
        for rel_path, cls_name, lineno in defs:
            findings.append({
                "file": rel_path,
                "line": lineno,
                "kind": "schema_write_gap",
                "detail": (
                    f"Field '{fname}' in '{cls_name}' is Optional/None-defaulted and is "
                    f"read in the codebase but has no write or populate site in the "
                    f"changed-file corpus. Callers will always receive None."
                ),
            })

    status = "FAIL" if findings else "PASS"
    return {
        "name": "schema_write_read_coherence",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": py_files,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_lookup_table_fabrication(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-LTF: Detect analytical functions that substitute a static dict for computation.

    An analytical function (check_*, analyze_*, evaluate_*, etc.) that assigns a
    hard-coded dict literal and immediately returns from it — with no conditional logic,
    no loops, and no other function calls — is not computing anything.  It is a canned
    answer pretending to be analysis.
    """
    import ast as _ast_ltf

    _ANALYTICAL_PREFIXES = (
        "check_", "analyze_", "analyse_", "compute_", "evaluate_",
        "assess_", "score_", "grade_", "audit_", "inspect_",
    )

    def _is_fabrication(func_node: "_ast_ltf.FunctionDef") -> bool:
        """Return True when the function body is only dict literals and returns from them."""
        dict_names: set[str] = set()
        has_dict_content = False

        for stmt in func_node.body:
            # Docstring
            if (isinstance(stmt, _ast_ltf.Expr)
                    and isinstance(stmt.value, _ast_ltf.Constant)
                    and isinstance(stmt.value.value, str)):
                continue
            # Dict literal assignment
            if isinstance(stmt, _ast_ltf.Assign) and isinstance(stmt.value, _ast_ltf.Dict):
                for t in stmt.targets:
                    if isinstance(t, _ast_ltf.Name):
                        dict_names.add(t.id)
                has_dict_content = True
                continue
            # Return statement
            if isinstance(stmt, _ast_ltf.Return) and stmt.value is not None:
                val = stmt.value
                if isinstance(val, _ast_ltf.Dict):
                    has_dict_content = True
                    continue
                if isinstance(val, _ast_ltf.Name) and val.id in dict_names:
                    continue
                if (isinstance(val, _ast_ltf.Call)
                        and isinstance(val.func, _ast_ltf.Attribute)
                        and isinstance(val.func.value, _ast_ltf.Name)
                        and val.func.value.id in dict_names
                        and val.func.attr == "get"):
                    continue
                if (isinstance(val, _ast_ltf.Subscript)
                        and isinstance(val.value, _ast_ltf.Name)
                        and val.value.id in dict_names):
                    continue
            # Any other statement = real computation
            return False

        # Require at least one dict and at least one parameter
        all_args = func_node.args.args + func_node.args.posonlyargs
        has_param = len(all_args) >= 1
        return has_dict_content and has_param

    findings: list[dict] = []
    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_ltf.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for node in _ast_ltf.walk(tree):
            if not isinstance(node, _ast_ltf.FunctionDef):
                continue
            if not any(node.name.lower().startswith(p) for p in _ANALYTICAL_PREFIXES):
                continue
            if _is_fabrication(node):
                findings.append({
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "lookup_table_fabrication",
                    "detail": (
                        f"Function '{node.name}' accepts input but its body is only a static "
                        f"dict with a return — no computation. Analytical functions must derive "
                        f"their output from the input they receive."
                    ),
                })

    if not files_checked:
        return {
            "name": "lookup_table_fabrication",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "lookup_table_fabrication",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_same_name_shape_divergence(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-SND: Detect the same class name in multiple files with incompatible shapes.

    When an AI generates two modules that each define a class with the same name but
    different field sets, callers that receive one and pass it to the other will lose
    data silently.  Only non-test Python files are scanned.  A class must have at least
    two fields and the divergence must be at least two fields to reduce noise.
    """
    import ast as _ast_snd

    # Maps class_name -> list of (rel_path, frozenset_of_field_names)
    class_shapes: dict[str, list[tuple[str, frozenset[str]]]] = {}

    files_seen: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_snd.parse(text, filename=rel_path)
        except Exception:
            continue
        files_seen.append(rel_path)

        for node in _ast_snd.walk(tree):
            if not isinstance(node, _ast_snd.ClassDef):
                continue
            # Collect fields from __init__ params
            init_params: frozenset[str] = frozenset()
            for item in node.body:
                if isinstance(item, _ast_snd.FunctionDef) and item.name == "__init__":
                    init_params = frozenset(
                        a.arg for a in item.args.args if a.arg != "self"
                    )
                    break
            # Collect annotated class-level fields (dataclasses, TypedDict)
            ann_fields = frozenset(
                item.target.id
                for item in node.body
                if isinstance(item, _ast_snd.AnnAssign) and isinstance(item.target, _ast_snd.Name)
            )
            shape = init_params if len(init_params) >= len(ann_fields) else ann_fields
            if len(shape) >= 2:
                class_shapes.setdefault(node.name, []).append((rel_path, shape))

    findings: list[dict] = []

    for cls_name, defs in class_shapes.items():
        if len(defs) < 2:
            continue
        for i in range(len(defs)):
            for j in range(i + 1, len(defs)):
                in_i_only = defs[i][1] - defs[j][1]
                in_j_only = defs[j][1] - defs[i][1]
                if len(in_i_only) >= 2 or len(in_j_only) >= 2:
                    findings.append({
                        "file": defs[i][0],
                        "line": 0,
                        "kind": "same_name_shape_divergence",
                        "detail": (
                            f"Class '{cls_name}' has incompatible shapes across files. "
                            f"Fields only in {defs[i][0]}: {sorted(in_i_only)}. "
                            f"Fields only in {defs[j][0]}: {sorted(in_j_only)}. "
                            f"Same-name divergence causes silent data loss at runtime."
                        ),
                    })
                    break  # One finding per class is enough

    if not files_seen:
        return {
            "name": "same_name_shape_divergence",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "same_name_shape_divergence",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_seen,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_eval_preseeding(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-EP: Detect expected-output variables used as evaluation inputs.

    In test and eval scripts a variable whose name signals expected output
    (``expected_*``, ``ground_truth_*``, ``gold_*``) should only appear in assertions,
    not as an argument to the function under test.  Passing the expected value as input
    creates circular evaluation: the model answers by seeing its own answer.

    Only files whose name starts with ``test_`` or whose path contains ``test``/``eval``/
    ``benchmark`` are scanned.  The variable must be assigned from a data-loading call
    (json.load, yaml.safe_load, open, load_fixture, etc.) to reduce noise from simple
    computed expectations.
    """
    import ast as _ast_ep

    _PRESEED_PREFIXES = ("expected", "ground_truth", "gold_", "reference_output", "target_output")
    _LOAD_KEYWORDS = frozenset({
        "load", "read", "open", "fetch", "get", "parse",
        "load_fixture", "load_dataset", "load_json", "load_yaml",
        "json_load", "yaml_load", "read_json", "read_text",
    })

    findings: list[dict] = []
    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        is_test = _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path
        is_eval = any(kw in _p.name.lower() for kw in ("eval", "benchmark", "assess"))
        if not (is_test or is_eval):
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_ep.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for func_node in _ast_ep.walk(tree):
            if not isinstance(func_node, _ast_ep.FunctionDef):
                continue
            # Collect preseed variables: assigned from a load-type call
            preseed_vars: dict[str, int] = {}
            for stmt in _ast_ep.walk(func_node):
                if not isinstance(stmt, _ast_ep.Assign):
                    continue
                if not isinstance(stmt.value, _ast_ep.Call):
                    continue
                call_name = ""
                if isinstance(stmt.value.func, _ast_ep.Name):
                    call_name = stmt.value.func.id.lower()
                elif isinstance(stmt.value.func, _ast_ep.Attribute):
                    call_name = stmt.value.func.attr.lower()
                if not any(kw in call_name for kw in _LOAD_KEYWORDS):
                    continue
                for target in stmt.targets:
                    if not isinstance(target, _ast_ep.Name):
                        continue
                    vname_l = target.id.lower()
                    if any(vname_l.startswith(p) for p in _PRESEED_PREFIXES):
                        preseed_vars[target.id] = stmt.lineno

            if not preseed_vars:
                continue

            # Check if any preseed var is used as a function call argument
            flagged: set[str] = set()
            for stmt in _ast_ep.walk(func_node):
                if not isinstance(stmt, _ast_ep.Call):
                    continue
                for arg in stmt.args:
                    if isinstance(arg, _ast_ep.Name) and arg.id in preseed_vars:
                        flagged.add(arg.id)
                for kw in stmt.keywords:
                    if isinstance(kw.value, _ast_ep.Name) and kw.value.id in preseed_vars:
                        flagged.add(kw.value.id)

            for vname in sorted(flagged):
                findings.append({
                    "file": rel_path,
                    "line": preseed_vars[vname],
                    "kind": "eval_preseeding",
                    "detail": (
                        f"Variable '{vname}' carries expected-output semantics and was loaded "
                        f"from a data source, but is also passed as a function call argument "
                        f"in '{func_node.name}'. This risks leaking the expected answer into "
                        f"the evaluation input."
                    ),
                })

    # Deduplicate by (file, line)
    seen_locs: set[tuple[str, int]] = set()
    deduped: list[dict] = []
    for f in findings:
        key = (f["file"], f["line"])
        if key not in seen_locs:
            seen_locs.add(key)
            deduped.append(f)
    findings = deduped

    if not files_checked:
        return {
            "name": "eval_preseeding",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no test/eval files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "eval_preseeding",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_test_vs_real_shape_parity(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-TVR: Detect mock return shapes that disagree with real function returns.

    When a test sets ``mock.return_value = {"key1": ..., "ghost_key": ...}`` for a
    function whose real implementation never returns ``"ghost_key"``, the test is
    teaching callers to expect a shape that does not exist in production.  This causes
    silent failures when mocked tests pass but real integration fails.

    Matching is by function name only (last component of the patch path).  Only flags
    when the mock contains keys that do NOT appear in ANY return dict of the real
    function — conservative to avoid false positives from optional fields.
    """
    import ast as _ast_tvr

    # Maps function_name -> list of frozenset of mock return_value dict keys
    mock_shapes: dict[str, list[frozenset[str]]] = {}
    mock_locs: dict[str, list[tuple[str, int]]] = {}

    # Maps function_name -> frozenset of all keys from all real return dicts
    real_shapes: dict[str, frozenset[str]] = {}

    test_files: list[str] = []
    source_files: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            test_files.append(rel_path)
        else:
            source_files.append(rel_path)

    files_checked: list[str] = []

    # Step 1: Parse test files for mock return_value dict shapes
    for rel_path in test_files:
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_tvr.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for with_node in _ast_tvr.walk(tree):
            if not isinstance(with_node, _ast_tvr.With):
                continue
            for item in with_node.items:
                if not (isinstance(item.context_expr, _ast_tvr.Call)
                        and item.optional_vars is not None
                        and isinstance(item.optional_vars, _ast_tvr.Name)):
                    continue
                call = item.context_expr
                is_patch = (
                    (isinstance(call.func, _ast_tvr.Name) and call.func.id == "patch")
                    or (isinstance(call.func, _ast_tvr.Attribute) and call.func.attr == "patch")
                )
                if not is_patch:
                    continue
                if not call.args or not isinstance(call.args[0], _ast_tvr.Constant):
                    continue
                patch_target = call.args[0].value
                if not isinstance(patch_target, str):
                    continue
                func_name = patch_target.split(".")[-1]
                mock_var = item.optional_vars.id

                for stmt in _ast_tvr.walk(with_node):
                    if not isinstance(stmt, _ast_tvr.Assign):
                        continue
                    for t in stmt.targets:
                        if not (isinstance(t, _ast_tvr.Attribute)
                                and t.attr == "return_value"
                                and isinstance(t.value, _ast_tvr.Name)
                                and t.value.id == mock_var):
                            continue
                        if not isinstance(stmt.value, _ast_tvr.Dict):
                            continue
                        mock_keys = frozenset(
                            k.value
                            for k in stmt.value.keys
                            if isinstance(k, _ast_tvr.Constant) and isinstance(k.value, str)
                        )
                        if mock_keys:
                            mock_shapes.setdefault(func_name, []).append(mock_keys)
                            mock_locs.setdefault(func_name, []).append((rel_path, stmt.lineno))

    if not mock_shapes:
        return {
            "name": "test_vs_real_shape_parity",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": files_checked,
            "exit_code": 0, "raw_output": "no mock return_value dict shapes found", "error": None,
        }

    # Step 2: Parse source files for real function return dict keys
    for rel_path in source_files:
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_tvr.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for node in _ast_tvr.walk(tree):
            if not isinstance(node, _ast_tvr.FunctionDef):
                continue
            if node.name not in mock_shapes:
                continue
            all_keys: set[str] = set()
            for stmt in _ast_tvr.walk(node):
                if isinstance(stmt, _ast_tvr.Return) and isinstance(stmt.value, _ast_tvr.Dict):
                    for k in stmt.value.keys:
                        if isinstance(k, _ast_tvr.Constant) and isinstance(k.value, str):
                            all_keys.add(k.value)
            if all_keys:
                existing = real_shapes.get(node.name, frozenset())
                real_shapes[node.name] = existing | frozenset(all_keys)

    # Step 3: Compare
    findings: list[dict] = []
    for func_name, mock_key_sets in mock_shapes.items():
        if func_name not in real_shapes:
            continue
        real_keys = real_shapes[func_name]
        locs = mock_locs.get(func_name, [])
        for i, mock_keys in enumerate(mock_key_sets):
            ghost_keys = mock_keys - real_keys
            if ghost_keys:
                loc = locs[i] if i < len(locs) else ("unknown", 0)
                findings.append({
                    "file": loc[0],
                    "line": loc[1],
                    "kind": "mock_shape_mismatch",
                    "detail": (
                        f"Mock for '{func_name}' sets return keys {sorted(ghost_keys)} that "
                        f"the real function never returns. The test teaches callers to expect "
                        f"a shape that does not exist in production code."
                    ),
                })

    if not files_checked:
        return {
            "name": "test_vs_real_shape_parity",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "test_vs_real_shape_parity",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_doc_code_response_shape(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.B / G-DCR: Detect docstring return shapes that have drifted from actual code.

    When a function docstring explicitly lists return dict keys in Python dict syntax
    (``{"key1": ..., "key2": ...}``) and one or more of those keys never appears in
    any actual ``return`` dict in the function body, the documentation is misleading.
    Callers written against the docs will access keys that do not exist.

    Only flags when the docstring contains an explicit ``{...}`` dict pattern in a
    Returns section — narrative descriptions are not parsed to minimise false positives.
    """
    import ast as _ast_dcr
    import re as _re_dcr

    _RETURNS_RE = _re_dcr.compile(
        r"[Rr]eturns?[:\s].*?(?=\n\s*\n|\n\s*[A-Z][a-z]+s?:|\Z)",
        _re_dcr.DOTALL,
    )
    _DICT_LITERAL_RE = _re_dcr.compile(r"\{([^}]+)\}")
    _QUOTED_KEY_RE = _re_dcr.compile(r"""[\"'](\w+)[\"']""")

    findings: list[dict] = []
    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_dcr.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for node in _ast_dcr.walk(tree):
            if not isinstance(node, _ast_dcr.FunctionDef):
                continue
            if not (node.body
                    and isinstance(node.body[0], _ast_dcr.Expr)
                    and isinstance(node.body[0].value, _ast_dcr.Constant)
                    and isinstance(node.body[0].value.value, str)):
                continue
            docstring = node.body[0].value.value
            if "return" not in docstring.lower():
                continue

            # Extract Returns section
            ret_match = _RETURNS_RE.search(docstring)
            if not ret_match:
                continue
            ret_text = ret_match.group(0)

            # Only proceed if there is an explicit dict pattern in the Returns section
            claimed_keys: set[str] = set()
            for dict_match in _DICT_LITERAL_RE.finditer(ret_text):
                for key_match in _QUOTED_KEY_RE.finditer(dict_match.group(1)):
                    claimed_keys.add(key_match.group(1).lower())

            if not claimed_keys:
                continue

            # Collect actual return dict keys (skipping nested scopes)
            actual_keys: set[str] = set()

            def _visit_for_keys(stmts: list) -> None:
                for stmt in stmts:
                    if isinstance(stmt, (_ast_dcr.FunctionDef,
                                         _ast_dcr.AsyncFunctionDef, _ast_dcr.ClassDef)):
                        continue
                    if (isinstance(stmt, _ast_dcr.Return)
                            and isinstance(stmt.value, _ast_dcr.Dict)):
                        for k in stmt.value.keys:
                            if isinstance(k, _ast_dcr.Constant) and isinstance(k.value, str):
                                actual_keys.add(k.value.lower())
                    for sub_attr in ("body", "orelse", "finalbody"):
                        _visit_for_keys(getattr(stmt, sub_attr, None) or [])
                    for handler in getattr(stmt, "handlers", None) or []:
                        _visit_for_keys(getattr(handler, "body", None) or [])

            _visit_for_keys(node.body)

            if not actual_keys:
                continue

            missing = claimed_keys - actual_keys
            if missing:
                findings.append({
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "doc_shape_drift",
                    "detail": (
                        f"Function '{node.name}' docstring claims return fields "
                        f"{sorted(missing)} but none of the actual return dicts contain them. "
                        f"The documented response shape has drifted from the implementation."
                    ),
                })

    if not files_checked:
        return {
            "name": "doc_code_response_shape",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "doc_code_response_shape",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


# ---------------------------------------------------------------------------
# Fix 12.C: Live-path call-graph reachability
# ---------------------------------------------------------------------------


def _check_live_path_reachability(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 12.C / G-LPR: Detect implementation functions unreachable from registered entrypoints.

    Within the changed-file corpus, finds Python files that contain statically recognisable
    web-route or CLI-command registration decorators (Flask, FastAPI, Click, Typer).  For
    each such file, builds an intra-file call graph and computes which top-level functions
    are reachable from any registered entrypoint via BFS (depth bound: 10 hops).

    Flags top-level public functions whose names contain action verbs strongly associated
    with implementation layers (``handle``, ``process``, ``execute``, ``perform``,
    ``dispatch``, ``invoke``) that are NOT reachable from any entrypoint in the same file
    and have non-trivial bodies (≥ 3 statements, excluding any leading docstring).

    Bounded to intra-file analysis only.  Only files containing at least one recognised
    entrypoint registration are examined.  Private (``_``-prefixed) functions and functions
    with fewer than 3 body statements are excluded to suppress noise from stubs and helpers.
    This is not generic dead-code detection: it targets the specific pattern where a live
    surface (route/command) exists alongside a claimed implementation that nothing on the
    live path ever calls.
    """
    import ast as _ast_lpr

    # Decorator attribute names that mark a route or command registration
    _ROUTE_ATTRS = frozenset({"route", "get", "post", "put", "delete", "patch", "head", "options"})
    _CMD_ATTRS = frozenset({"command"})
    _ALL_EP_ATTRS = _ROUTE_ATTRS | _CMD_ATTRS

    # Action verbs whose presence in a function name strongly suggests an implementation layer.
    # Deliberately conservative — excludes generic verbs (run, get, load, save) that are
    # equally common in helpers and utilities.
    _IMPL_VERBS = frozenset({"handle", "process", "execute", "perform", "dispatch", "invoke"})

    # Minimum body statements (after any leading docstring) to treat as non-trivial
    _MIN_STMTS = 3

    def _is_entrypoint_deco(deco: "_ast_lpr.expr") -> bool:
        """Return True if the decorator is a route or command registration."""
        if isinstance(deco, _ast_lpr.Call):
            func = deco.func
        elif isinstance(deco, _ast_lpr.Attribute):
            func = deco
        elif isinstance(deco, _ast_lpr.Name):
            return deco.id in _CMD_ATTRS
        else:
            return False
        if isinstance(func, _ast_lpr.Attribute):
            return func.attr in _ALL_EP_ATTRS
        if isinstance(func, _ast_lpr.Name):
            return func.id in _CMD_ATTRS
        return False

    def _is_impl_candidate(fname: str, func_node: "_ast_lpr.FunctionDef") -> bool:
        """Return True if the function looks like a disconnected implementation layer."""
        if fname.startswith("_"):
            return False
        if not any(verb in fname.lower() for verb in _IMPL_VERBS):
            return False
        body = func_node.body
        # Skip leading docstring when counting statements
        if (body
                and isinstance(body[0], _ast_lpr.Expr)
                and isinstance(body[0].value, _ast_lpr.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        return len(body) >= _MIN_STMTS

    def _intra_calls(
        func_node: "_ast_lpr.FunctionDef | _ast_lpr.AsyncFunctionDef",
        local: set[str],
    ) -> set[str]:
        """Names of locally-defined functions directly called inside func_node.

        Also recognises FastAPI dependency injection via ``Depends(fn)`` in both
        the positional-default form (``param: T = Depends(fn)``) and the
        ``Annotated`` form (``param: Annotated[T, Depends(fn)]``).  Both patterns
        appear inside the function node's argument tree, which ``ast.walk``
        already traverses.
        """
        called: set[str] = set()
        for node in _ast_lpr.walk(func_node):
            if not isinstance(node, _ast_lpr.Call):
                continue
            # Direct call: fn() where fn is a locally-defined function
            if isinstance(node.func, _ast_lpr.Name) and node.func.id in local:
                called.add(node.func.id)
            # FastAPI DI: Depends(dep_fn) — covers both default-value and
            # Annotated[T, Depends(dep_fn)] forms since ast.walk reaches both
            elif (
                isinstance(node.func, _ast_lpr.Name)
                and node.func.id == "Depends"
                and node.args
                and isinstance(node.args[0], _ast_lpr.Name)
                and node.args[0].id in local
            ):
                called.add(node.args[0].id)
        return called

    def _bfs(seeds: set[str], graph: dict[str, set[str]]) -> set[str]:
        """BFS reachability from seeds through graph, depth-bound 10."""
        reachable: set[str] = set(seeds)
        frontier = set(seeds)
        for _ in range(10):
            nxt: set[str] = set()
            for fn in frontier:
                for callee in graph.get(fn, set()):
                    if callee not in reachable:
                        reachable.add(callee)
                        nxt.add(callee)
            if not nxt:
                break
            frontier = nxt
        return reachable

    findings: list[dict] = []
    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_lpr.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        # Collect top-level function definitions (sync and async)
        top_funcs: dict[str, "_ast_lpr.FunctionDef | _ast_lpr.AsyncFunctionDef"] = {}
        entrypoints: set[str] = set()
        for node in tree.body:
            if not isinstance(node, (_ast_lpr.FunctionDef, _ast_lpr.AsyncFunctionDef)):
                continue
            top_funcs[node.name] = node
            if any(_is_entrypoint_deco(d) for d in node.decorator_list):
                entrypoints.add(node.name)

        if not entrypoints:
            continue  # No recognised entrypoints in this file

        local_names = set(top_funcs.keys())

        # Build intra-file call graph
        call_graph: dict[str, set[str]] = {
            fname: _intra_calls(fn, local_names)
            for fname, fn in top_funcs.items()
        }

        # Compute reachable set from all entrypoints
        reachable = _bfs(entrypoints, call_graph)

        # Flag implementation candidates not reachable from any entrypoint
        for fname, func_node in top_funcs.items():
            if fname in entrypoints or fname in reachable:
                continue
            if not _is_impl_candidate(fname, func_node):
                continue
            findings.append({
                "file": rel_path,
                "line": func_node.lineno,
                "kind": "live_path_unreachable",
                "detail": (
                    f"Function '{fname}' has an implementation-suggesting name but is not "
                    f"reachable from any of the {len(entrypoints)} registered entrypoint(s) "
                    f"in this file ({', '.join(sorted(entrypoints))}). "
                    f"The live path does not reach this supposed implementation."
                ),
            })

    if not files_checked:
        return {
            "name": "live_path_reachability",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "live_path_reachability",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


# ---------------------------------------------------------------------------
# Fix 13: Generated-app operator truth surfaces
# ---------------------------------------------------------------------------


def _check_read_without_write_surface(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 13.1 / G-RWW: Detect ORM read surfaces with no corresponding write path.

    Finds model class names used in ORM read patterns (SQLAlchemy ``session.query``,
    Django ``Model.objects.filter/get/all``) that have NO write counterpart in the
    changed-file corpus.  A write counterpart is one of:

    - ``session.add(Model(...))`` — direct constructor inside a write call
    - ``Model.objects.create(...)`` — Django manager create/save/etc.
    - ``user = Model(...); session.add(user)`` — variable assigned from a constructor
      call and later passed into a genuine ORM write method (.add/.create/.save/...)
      within the same function/module scope (Fix 68 — narrow assign-then-add tracking)

    Bare construction alone (``Model(...)`` with no persistence call) does NOT count
    as a write path: instantiating a model object is not the same as persisting it.

    Bounded to the changed-file corpus.  Only classes whose names start with a capital
    letter (PEP 8 class convention) are considered.  If the write is in a module not in
    the corpus the check will not flag (conservative).
    """
    import ast as _ast_rww

    _READ_ATTRS = frozenset({"query", "filter", "get", "all", "first", "one", "scalar"})
    _WRITE_ORM_ATTRS = frozenset({"add", "create", "bulk_create", "insert", "save"})

    read_entities: dict[str, tuple[str, int]] = {}   # classname -> (first_read_file, lineno)
    write_entities: set[str] = set()

    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_rww.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for node in _ast_rww.walk(tree):
            if not isinstance(node, _ast_rww.Call):
                continue
            func = node.func

            # session.query(ModelClass)  — read
            if (isinstance(func, _ast_rww.Attribute) and func.attr in _READ_ATTRS
                    and node.args
                    and isinstance(node.args[0], _ast_rww.Name)
                    and node.args[0].id[:1].isupper()):
                cname = node.args[0].id
                if cname not in read_entities:
                    read_entities[cname] = (rel_path, node.col_offset)

            # ModelClass.objects.filter()/get()/all()  — read
            if (isinstance(func, _ast_rww.Attribute) and func.attr in _READ_ATTRS
                    and isinstance(func.value, _ast_rww.Attribute)
                    and func.value.attr == "objects"
                    and isinstance(func.value.value, _ast_rww.Name)
                    and func.value.value.id[:1].isupper()):
                cname = func.value.value.id
                if cname not in read_entities:
                    read_entities[cname] = (rel_path, node.col_offset)

            # session.add(ModelClass(...))  — write (direct constructor inline)
            if (isinstance(func, _ast_rww.Attribute) and func.attr in _WRITE_ORM_ATTRS
                    and node.args and isinstance(node.args[0], _ast_rww.Call)
                    and isinstance(node.args[0].func, _ast_rww.Name)
                    and node.args[0].func.id[:1].isupper()):
                write_entities.add(node.args[0].func.id)

            # ModelClass.objects.create(...)  — write
            if (isinstance(func, _ast_rww.Attribute) and func.attr in _WRITE_ORM_ATTRS
                    and isinstance(func.value, _ast_rww.Attribute)
                    and func.value.attr == "objects"
                    and isinstance(func.value.value, _ast_rww.Name)
                    and func.value.value.id[:1].isupper()):
                write_entities.add(func.value.value.id)

        # Fix 68 (corrected): per-scope assign-then-add tracking.
        # Match `var = Model(...)` followed by `.add(var)/.create(var)/.save(var)/...`
        # within the SAME function/module scope.  Constructing without persistence
        # is not write evidence — that would mask real read-without-write defects.
        def _scope_bodies():
            yield tree.body
            for _sub in _ast_rww.walk(tree):
                if isinstance(_sub, (_ast_rww.FunctionDef, _ast_rww.AsyncFunctionDef)):
                    yield _sub.body

        def _scope_nodes(stmts):
            _stack: list = list(stmts)
            while _stack:
                _n = _stack.pop()
                yield _n
                if isinstance(_n, (_ast_rww.FunctionDef, _ast_rww.AsyncFunctionDef, _ast_rww.ClassDef)):
                    continue
                for _c in _ast_rww.iter_child_nodes(_n):
                    _stack.append(_c)

        for _body in _scope_bodies():
            _var_to_model: dict[str, str] = {}
            for _n in _scope_nodes(_body):
                _target = None
                _val = None
                if isinstance(_n, _ast_rww.Assign) and len(_n.targets) == 1 and isinstance(_n.targets[0], _ast_rww.Name):
                    _target = _n.targets[0].id
                    _val = _n.value
                elif isinstance(_n, _ast_rww.AnnAssign) and _n.value is not None and isinstance(_n.target, _ast_rww.Name):
                    _target = _n.target.id
                    _val = _n.value
                if (_target is not None
                        and isinstance(_val, _ast_rww.Call)
                        and isinstance(_val.func, _ast_rww.Name)
                        and _val.func.id[:1].isupper()
                        and _val.func.id not in _READ_ATTRS):
                    _var_to_model[_target] = _val.func.id
            for _n in _scope_nodes(_body):
                if (isinstance(_n, _ast_rww.Call)
                        and isinstance(_n.func, _ast_rww.Attribute)
                        and _n.func.attr in _WRITE_ORM_ATTRS
                        and _n.args
                        and isinstance(_n.args[0], _ast_rww.Name)):
                    _varname = _n.args[0].id
                    if _varname in _var_to_model:
                        write_entities.add(_var_to_model[_varname])

    findings: list[dict] = []
    for cname, (fpath, _col) in read_entities.items():
        if cname not in write_entities:
            findings.append({
                "file": fpath,
                "line": 0,
                "kind": "read_without_write_path",
                "detail": (
                    f"Model '{cname}' is queried/read in the corpus but has no "
                    f"corresponding write path (session.add(...) / objects.create(...) "
                    f"/ assign-then-add). The read surface will always return empty "
                    f"or stale data if nothing ever writes to this model."
                ),
            })

    if not files_checked:
        return {
            "name": "read_without_write_surface",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "read_without_write_surface",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_frozen_status_fields(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 13.2 / G-FSF: Detect status/state ORM column fields that are never updated.

    Finds SQLAlchemy ``Column(...)`` field definitions in non-test Python classes whose
    names contain ``status``, ``state``, ``phase``, or ``stage``.  Across the entire
    corpus checks whether any attribute assignment to those field names uses a
    non-literal RHS (a variable or expression — meaning a dynamic update).  If the
    field is only ever assigned string/int literals (or never assigned outside the class
    definition), it is frozen: operators see it as live progression but it never changes.
    """
    import ast as _ast_fsf

    _STATUS_NAMES = frozenset({"status", "state", "phase", "stage", "step"})

    # Step 1: find Column-decorated status-like fields in ORM model classes
    # field_defs: {field_name: [(rel_path, class_name, lineno)]}
    field_defs: dict[str, list[tuple[str, str, int]]] = {}

    # Step 2: track assignment types seen for each field name across corpus
    # literal_assignments: field names assigned only literals (const strings/ints)
    # dynamic_assignments: field names assigned non-literal values
    literal_assigns: set[str] = set()
    dynamic_assigns: set[str] = set()

    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_fsf.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        # Find Column field definitions in class bodies
        for node in _ast_fsf.walk(tree):
            if not isinstance(node, _ast_fsf.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, _ast_fsf.Assign):
                    continue
                for target in item.targets:
                    if not isinstance(target, _ast_fsf.Name):
                        continue
                    if target.id.lower() not in _STATUS_NAMES:
                        continue
                    # Check if RHS is a Column(...) call
                    if not (isinstance(item.value, _ast_fsf.Call)
                            and isinstance(item.value.func, (_ast_fsf.Name, _ast_fsf.Attribute))
                            and (
                                (isinstance(item.value.func, _ast_fsf.Name)
                                 and item.value.func.id == "Column")
                                or (isinstance(item.value.func, _ast_fsf.Attribute)
                                    and item.value.func.attr == "Column")
                            )):
                        continue
                    field_defs.setdefault(target.id, []).append(
                        (rel_path, node.name, item.lineno)
                    )

        # Collect attribute assignments to status-like names
        for node in _ast_fsf.walk(tree):
            if not isinstance(node, _ast_fsf.Assign):
                continue
            for target in node.targets:
                if not (isinstance(target, _ast_fsf.Attribute)
                        and target.attr.lower() in _STATUS_NAMES):
                    continue
                fname = target.attr
                # Dynamic: RHS is a Name (variable) or any non-constant
                is_literal = isinstance(node.value, _ast_fsf.Constant)
                if is_literal:
                    literal_assigns.add(fname)
                else:
                    dynamic_assigns.add(fname)

    findings: list[dict] = []
    for fname, defs in field_defs.items():
        # Only flag if the field is never dynamically assigned
        if fname in dynamic_assigns:
            continue
        for rel_path, cls_name, lineno in defs:
            findings.append({
                "file": rel_path,
                "line": lineno,
                "kind": "frozen_status_field",
                "detail": (
                    f"Column field '{fname}' in '{cls_name}' appears to be frozen: "
                    f"it is defined as an ORM Column with a default but has no dynamic "
                    f"update assignment (non-literal RHS) anywhere in the corpus. "
                    f"Operator surfaces that show this field as live progression will "
                    f"always display the initial default value."
                ),
            })

    if not files_checked:
        return {
            "name": "frozen_status_fields",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "frozen_status_fields",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_write_read_schema_asymmetry(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 13.3 / G-WRS: Detect within-function write/read dict key mismatches.

    Within each non-test Python function, tracks dict literals assigned to a named
    variable (``data = {"k1": v, "k2": v}``).  When the same variable is later subscript-
    accessed with a key (``data["k3"]``) that was NEVER added to that dict via literal
    assignment, direct subscript write, or ``update()`` call within the same function,
    flags it as a write/read schema asymmetry.

    This catches the pattern where a serialiser or response builder creates a dict with
    one schema but then reads back a field that was never written — a KeyError waiting to
    happen that silently returns wrong data if a ``.get()`` fallback masks the absence.
    """
    import ast as _ast_wrs

    findings: list[dict] = []
    files_checked: list[str] = []

    def _literal_keys(dict_node: "_ast_wrs.Dict") -> frozenset[str]:
        return frozenset(
            k.value for k in dict_node.keys
            if isinstance(k, _ast_wrs.Constant) and isinstance(k.value, str)
        )

    def _analyse_function(func_node: "_ast_wrs.FunctionDef", rel_path: str) -> list[dict]:
        local_findings: list[dict] = []
        # dict_var -> set of known string keys
        known_keys: dict[str, set[str]] = {}

        def _collect(stmts: list) -> None:
            for stmt in stmts:
                if isinstance(stmt, (_ast_wrs.FunctionDef, _ast_wrs.AsyncFunctionDef,
                                     _ast_wrs.ClassDef)):
                    continue
                # Assign: varname = { "k": v, ... }
                if isinstance(stmt, _ast_wrs.Assign) and isinstance(stmt.value, _ast_wrs.Dict):
                    for target in stmt.targets:
                        if isinstance(target, _ast_wrs.Name):
                            keys = set(_literal_keys(stmt.value))
                            known_keys.setdefault(target.id, set()).update(keys)
                # varname["key"] = value  — extend known keys
                if (isinstance(stmt, _ast_wrs.Assign)
                        and isinstance(stmt.targets[0], _ast_wrs.Subscript)
                        and isinstance(stmt.targets[0].value, _ast_wrs.Name)
                        and isinstance(stmt.targets[0].slice, _ast_wrs.Constant)
                        and isinstance(stmt.targets[0].slice.value, str)):
                    vname = stmt.targets[0].value.id
                    known_keys.setdefault(vname, set()).add(stmt.targets[0].slice.value)
                # varname.update({"key": value})  — extend known keys
                if (isinstance(stmt, _ast_wrs.Expr)
                        and isinstance(stmt.value, _ast_wrs.Call)
                        and isinstance(stmt.value.func, _ast_wrs.Attribute)
                        and stmt.value.func.attr == "update"
                        and isinstance(stmt.value.func.value, _ast_wrs.Name)
                        and stmt.value.args
                        and isinstance(stmt.value.args[0], _ast_wrs.Dict)):
                    vname = stmt.value.func.value.id
                    for k in _literal_keys(stmt.value.args[0]):
                        known_keys.setdefault(vname, set()).add(k)
                # Check subscript reads: varname["key"] in Load context
                for child in _ast_wrs.walk(stmt):
                    if (isinstance(child, _ast_wrs.Subscript)
                            and isinstance(child.value, _ast_wrs.Name)
                            and isinstance(child.ctx, _ast_wrs.Load)
                            and isinstance(child.slice, _ast_wrs.Constant)
                            and isinstance(child.slice.value, str)):
                        vname = child.value.id
                        accessed_key = child.slice.value
                        if (vname in known_keys
                                and accessed_key not in known_keys[vname]):
                            local_findings.append({
                                "file": rel_path,
                                "line": getattr(child, "lineno", func_node.lineno),
                                "kind": "write_read_key_mismatch",
                                "detail": (
                                    f"In '{func_node.name}': dict variable '{vname}' "
                                    f"was created with keys {sorted(known_keys[vname])} "
                                    f"but key '{accessed_key}' is accessed without ever "
                                    f"being written. This is a write/read schema "
                                    f"asymmetry — the key does not exist at read time."
                                ),
                            })
                # Recurse into control flow (but not nested functions)
                for sub_attr in ("body", "orelse", "finalbody"):
                    _collect(getattr(stmt, sub_attr, None) or [])
                for handler in getattr(stmt, "handlers", None) or []:
                    _collect(getattr(handler, "body", None) or [])

        _collect(func_node.body)
        # Deduplicate by (vname, accessed_key) within function
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for f in local_findings:
            key = (f["file"], f["line"], f["detail"][:60])
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_wrs.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for node in _ast_wrs.walk(tree):
            if isinstance(node, _ast_wrs.FunctionDef):
                findings.extend(_analyse_function(node, rel_path))

    if not files_checked:
        return {
            "name": "write_read_schema_asymmetry",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "write_read_schema_asymmetry",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _check_hollow_resumed_flow(repo_path: Path, changed_files: list[str]) -> dict:
    """Fix 13.4 / G-HRF: Detect resumed/reconstructed flows that return hollow responses.

    Finds functions whose names contain ``resume``, ``reconstruct``, ``recover``,
    ``retry``, ``restart``, or ``restore`` in non-test Python files.  Collects all
    return-dict literals from those functions.  If every return dict contains ONLY
    acknowledgement-type keys (``status``, ``success``, ``message``, ``ok``, ``error``,
    ``resumed``, ``restarted``, ``recovered``, ``retried``) and no substantive data keys,
    the response is hollow: operators see a status message but lose the actual result
    that the flow was supposed to produce.

    Functions with no dict-literal return statements are skipped (conservative: CLI
    commands that print rather than return are not flagged).
    """
    import ast as _ast_hrf

    _RESUME_VERBS = ("resume", "reconstruct", "recover", "retry", "restart", "restore")
    _ACK_KEYS = frozenset({
        "status", "success", "message", "ok", "error", "resumed", "restarted",
        "recovered", "retried", "restarted", "restored", "acknowledged",
        "accepted", "queued", "scheduled", "msg", "detail", "code",
    })

    def _collect_return_dict_keys(func_node: "_ast_hrf.FunctionDef") -> list[frozenset[str]]:
        """Collect key sets from all return dict literals in the function."""
        key_sets: list[frozenset[str]] = []

        def _visit(stmts: list) -> None:
            for stmt in stmts:
                if isinstance(stmt, (_ast_hrf.FunctionDef, _ast_hrf.AsyncFunctionDef,
                                     _ast_hrf.ClassDef)):
                    continue
                if isinstance(stmt, _ast_hrf.Return) and isinstance(stmt.value, _ast_hrf.Dict):
                    keys = frozenset(
                        k.value for k in stmt.value.keys
                        if isinstance(k, _ast_hrf.Constant) and isinstance(k.value, str)
                    )
                    if keys:
                        key_sets.append(keys)
                for sub_attr in ("body", "orelse", "finalbody"):
                    _visit(getattr(stmt, sub_attr, None) or [])
                for handler in getattr(stmt, "handlers", None) or []:
                    _visit(getattr(handler, "body", None) or [])

        _visit(func_node.body)
        return key_sets

    findings: list[dict] = []
    files_checked: list[str] = []

    for rel_path in sorted(changed_files):
        if not rel_path.endswith(".py"):
            continue
        _p = Path(rel_path)
        if _p.name.startswith("test_") or "tests/" in rel_path or "/tests/" in rel_path:
            continue
        full = repo_path / rel_path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
            tree = _ast_hrf.parse(text, filename=rel_path)
        except Exception:
            continue
        files_checked.append(rel_path)

        for node in _ast_hrf.walk(tree):
            if not isinstance(node, _ast_hrf.FunctionDef):
                continue
            fname_lower = node.name.lower()
            if not any(verb in fname_lower for verb in _RESUME_VERBS):
                continue

            return_key_sets = _collect_return_dict_keys(node)
            if not return_key_sets:
                continue  # No dict returns — skip (conservative)

            # Check if ALL return dicts contain only acknowledgement keys
            all_hollow = all(
                keys.issubset(_ACK_KEYS) for keys in return_key_sets
            )
            if all_hollow:
                # Collect all keys ever returned for the finding message
                all_returned = frozenset().union(*return_key_sets)
                findings.append({
                    "file": rel_path,
                    "line": node.lineno,
                    "kind": "hollow_resumed_response",
                    "detail": (
                        f"Function '{node.name}' resumes/reconstructs a flow but all "
                        f"{len(return_key_sets)} return dict(s) contain only "
                        f"acknowledgement keys ({sorted(all_returned)}). "
                        f"The original flow outputs are lost — operators receive a "
                        f"status acknowledgement instead of the actual result."
                    ),
                })

    if not files_checked:
        return {
            "name": "hollow_resumed_flow",
            "status": "PASS", "severity": "warning",
            "findings": [], "files_checked": [],
            "exit_code": 0, "raw_output": "no Python source files", "error": None,
        }
    status = "FAIL" if findings else "PASS"
    return {
        "name": "hollow_resumed_flow",
        "status": status, "severity": "warning",
        "findings": findings, "files_checked": files_checked,
        "exit_code": 1 if findings else 0, "raw_output": "", "error": None,
    }


def _load_mypy_baseline(saturnday_dir: Path) -> dict[str, int]:
    """Load mypy error counts per file from baseline JSON.

    Returns a mapping of ``file_path -> error_count`` or an empty dict when
    the baseline file does not exist or cannot be parsed.
    """
    baseline_path = saturnday_dir / "mypy-baseline.json"
    if not baseline_path.exists():
        return {}
    try:
        return json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_mypy_baseline(saturnday_dir: Path, counts: dict[str, int]) -> None:
    """Persist updated mypy per-file error counts to the baseline file."""
    try:
        saturnday_dir.mkdir(parents=True, exist_ok=True)
        (saturnday_dir / "mypy-baseline.json").write_text(
            json.dumps(counts, sort_keys=True, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("Could not save mypy baseline: %s", exc)


def _detect_mypy_config(repo_path: Path) -> bool:
    """Return True when the repo contains a mypy config section."""
    if (repo_path / "mypy.ini").exists():
        return True
    for cfg_file in ("setup.cfg",):
        cfg = repo_path / cfg_file
        if cfg.exists():
            try:
                if "[mypy]" in cfg.read_text(encoding="utf-8"):
                    return True
            except Exception:
                pass
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        try:
            if "[tool.mypy]" in pyproject.read_text(encoding="utf-8"):
                return True
        except Exception:
            pass
    return False


def _check_mypy(repo_path: Path, changed_files: list[str]) -> dict:
    """Run mypy on changed Python files and return a WARNING-severity result.

    Degrades gracefully to SKIPPED when mypy is not installed.  Only reports
    findings that are *new* relative to the stored baseline so pre-existing
    type errors do not fire on every ticket.

    Returns a dict matching the standard tool result format:
        {name, status, severity, findings, exit_code, raw_output, error}
    """
    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return {
            "name": "mypy",
            "status": "PASS",
            "severity": "warning",
            "findings": [],
            "exit_code": 0,
            "raw_output": "no python files",
            "error": None,
        }

    mypy_path = shutil.which("mypy")
    if not mypy_path:
        return {
            "name": "mypy",
            "status": "SKIPPED",
            "severity": "warning",
            "findings": [],
            "exit_code": None,
            "raw_output": "",
            "error": "mypy_not_found",
        }

    has_config = _detect_mypy_config(repo_path)
    cmd = [mypy_path, "--no-error-summary", "--no-color"]
    if not has_config:
        cmd.append("--ignore-missing-imports")
    cmd.extend(py_files)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": "mypy",
            "status": "SKIPPED",
            "severity": "warning",
            "findings": [],
            "exit_code": None,
            "raw_output": "",
            "error": "mypy_timeout",
        }
    except OSError as exc:
        return {
            "name": "mypy",
            "status": "SKIPPED",
            "severity": "warning",
            "findings": [],
            "exit_code": None,
            "raw_output": "",
            "error": f"mypy_error:{exc}",
        }

    raw_output = (result.stdout or "") + (result.stderr or "")

    # Parse: path/to/file.py:12: error: some message  [error-code]
    error_pattern = re.compile(r"^(.+?):(\d+):\s*error:\s*(.+)$", re.MULTILINE)
    raw_findings: dict[str, list[dict]] = {}
    for m in error_pattern.finditer(raw_output):
        file_rel = m.group(1).strip()
        line_no = int(m.group(2))
        message = m.group(3).strip()
        raw_findings.setdefault(file_rel, []).append({
            "file": file_rel,
            "line": line_no,
            "kind": "mypy_error",
            "detail": message,
        })

    # Ratchet: suppress files whose error count is no worse than the baseline
    saturnday_dir = repo_path / ".saturnday"
    baseline = _load_mypy_baseline(saturnday_dir)
    findings = []
    for file_path, file_findings in raw_findings.items():
        current_count = len(file_findings)
        baseline_count = baseline.get(file_path, 0)
        if current_count > baseline_count:
            # Only surface the excess findings (new errors beyond baseline)
            findings.extend(file_findings[baseline_count:])

    return {
        "name": "mypy",
        "status": "WARN" if findings else "PASS",
        "severity": "warning",
        "findings": findings,
        "exit_code": result.returncode,
        "raw_output": raw_output,
        "error": None,
    }


def run_review(
    repo_path: Path,
    changed_files: list[str],
    tmpdir: Path,
    *,
    run_shell_func,
    timeout_s: int = 30,
    strict: bool = False,
    auto_install_deps: bool = False,
) -> dict:
    _t_review = time.monotonic()
    changed_files = sorted({path for path in changed_files if path})
    tools = {}
    errors = []
    tool_runs: list[dict] = []
    python_targets = [path for path in changed_files if path.endswith(".py")]

    # Syntax check FIRST — if it fails, skip downstream AST-based checks
    syntax_failed = False
    if python_targets:
        t0 = time.time()
        syntax_result = _check_syntax(repo_path, changed_files)
        tools["syntax"] = syntax_result
        _timed_tool_run("syntax", syntax_result, tool_runs, repo_path, t0)
        if syntax_result["status"] == "FAIL":
            errors.append("syntax_failed")
            syntax_failed = True

    # Secrets — always runs
    t0 = time.time()
    secrets_result = _scan_secrets(repo_path, changed_files)
    tools["secrets"] = secrets_result
    _timed_tool_run("secrets", secrets_result, tool_runs, repo_path, t0)
    if secrets_result["status"] == "FAIL":
        errors.append("secrets_failed")

    # Placeholders — always runs
    t0 = time.time()
    placeholders_result = _scan_placeholders(repo_path, changed_files)
    tools["placeholders"] = placeholders_result
    _timed_tool_run("placeholders", placeholders_result, tool_runs, repo_path, t0)
    if placeholders_result["status"] == "FAIL":
        errors.append("placeholders_failed")

    # Project runnable check — always runs
    t0 = time.time()
    runnable_result = _check_project_runnable(repo_path, changed_files)
    tools["project_runnable"] = runnable_result
    _timed_tool_run("project_runnable", runnable_result, tool_runs, repo_path, t0)

    # Tests pass check — always runs (skips silently when no test runner detected)
    t0 = time.time()
    tests_pass_result = _check_tests_pass(repo_path, changed_files)
    tools["tests_pass"] = tests_pass_result
    _timed_tool_run("tests_pass", tests_pass_result, tool_runs, repo_path, t0)
    if tests_pass_result["status"] == "FAIL":
        errors.append("tests_failing")

    # LICENSE check — always runs
    t0 = time.time()
    license_result = _check_license(repo_path, changed_files)
    tools["license"] = license_result
    _timed_tool_run("license", license_result, tool_runs, repo_path, t0)

    # README check — always runs
    t0 = time.time()
    readme_result = _check_readme(repo_path, changed_files)
    tools["readme"] = readme_result
    _timed_tool_run("readme", readme_result, tool_runs, repo_path, t0)

    # README language consistency check — always runs
    t0 = time.time()
    readme_consistency_result = _check_readme_language_consistency(repo_path, changed_files)
    tools["readme_consistency"] = readme_consistency_result
    _timed_tool_run("readme_consistency", readme_consistency_result, tool_runs, repo_path, t0)

    # Dead code check — always runs
    t0 = time.time()
    dead_code_result = _check_dead_code(repo_path, changed_files)
    tools["dead_code"] = dead_code_result
    _timed_tool_run("dead_code", dead_code_result, tool_runs, repo_path, t0)

    # Blast radius — always runs
    t0 = time.time()
    blast_radius_result = _check_blast_radius(repo_path, changed_files)
    tools["blast_radius"] = blast_radius_result
    _timed_tool_run("blast_radius", blast_radius_result, tool_runs, repo_path, t0)

    # Dependency declaration — always runs
    t0 = time.time()
    dep_decl_result = _check_dependency_declaration(repo_path, changed_files)
    tools["dependency_declaration"] = dep_decl_result
    _timed_tool_run("dependency_declaration", dep_decl_result, tool_runs, repo_path, t0)
    if dep_decl_result["status"] == "FAIL":
        errors.append("dependency_declaration_failed")

    # The following checks are skipped if syntax failed (they produce noise)
    if python_targets and not syntax_failed:
        t0 = time.time()
        ruff_result, _ = _run_json_tool(
            "ruff",
            ["ruff", "check", "--output-format=json", *python_targets],
            repo_path,
            run_shell_func=run_shell_func,
            timeout_s=timeout_s,
            strict=strict,
            required=True,
            tool_runs=tool_runs,
        )
        tools["ruff"] = ruff_result
        if ruff_result["status"] == "FAIL":
            errors.append("ruff_failed")

        t0 = time.time()
        bandit_result, _ = _run_json_tool(
            "bandit",
            ["bandit", "-f", "json", *python_targets],
            repo_path,
            run_shell_func=run_shell_func,
            timeout_s=timeout_s,
            strict=strict,
            required=True,
            tool_runs=tool_runs,
        )
        tools["bandit"] = bandit_result
        if bandit_result["status"] == "FAIL":
            errors.append("bandit_failed")
    elif python_targets and syntax_failed:
        for name in ("ruff", "bandit"):
            skipped = {"name": name, "status": "SKIPPED", "exit_code": None,
                       "findings": [], "raw_output": "", "error": "syntax_failed"}
            tools[name] = skipped
            _timed_tool_run(name, skipped, tool_runs, repo_path, time.time())
    else:
        for name in ("ruff", "bandit"):
            tools[name] = {"name": name, "status": "PASS", "exit_code": 0,
                           "findings": [], "raw_output": "no python files", "error": None}
            _timed_tool_run(name, tools[name], tool_runs, repo_path, time.time())

    t0 = time.time()
    pip_audit_result = _run_pip_audit(
        repo_path,
        changed_files,
        run_shell_func=run_shell_func,
        timeout_s=timeout_s,
        strict=strict,
        tool_runs=tool_runs,
    )
    tools["pip_audit"] = pip_audit_result
    if pip_audit_result["status"] == "FAIL":
        errors.append("pip_audit_failed")

    # Shellcheck — runs on .sh files
    shell_targets = [p for p in changed_files if p.endswith(".sh")]
    if shell_targets:
        t0 = time.time()
        shellcheck_result = _check_shellcheck(
            repo_path, changed_files,
            run_shell_func=run_shell_func,
            timeout_s=timeout_s, strict=strict,
            tool_runs=tool_runs,
        )
        tools["shellcheck"] = shellcheck_result
        if shellcheck_result["status"] == "FAIL":
            errors.append("shellcheck_failed")
    else:
        tools["shellcheck"] = {"name": "shellcheck", "status": "PASS", "exit_code": 0,
                               "findings": [], "raw_output": "no shell files", "error": None}
        _timed_tool_run("shellcheck", tools["shellcheck"], tool_runs, repo_path, time.time())

    # Line continuation check — .sh files
    if shell_targets:
        t0 = time.time()
        line_cont_result = _check_line_continuations(repo_path, changed_files)
        tools["line_continuations"] = line_cont_result
        _timed_tool_run("line_continuations", line_cont_result, tool_runs, repo_path, t0)
        if line_cont_result["status"] == "FAIL":
            errors.append("line_continuations_failed")
    else:
        tools["line_continuations"] = {"name": "line_continuations", "status": "PASS", "exit_code": 0,
                                       "findings": [], "raw_output": "no shell files", "error": None}
        _timed_tool_run("line_continuations", tools["line_continuations"], tool_runs, repo_path, time.time())

    if not syntax_failed:
        t0 = time.time()
        import_check_result = _check_imports(
            repo_path, changed_files, run_shell_func=run_shell_func, timeout_s=timeout_s,
        )
        tools["import_check"] = import_check_result
        _timed_tool_run("import_check", import_check_result, tool_runs, repo_path, t0)
        if import_check_result["status"] == "FAIL":
            errors.append("import_check_failed")

        t0 = time.time()
        code_quality_result = _check_code_quality(repo_path, changed_files)
        tools["code_quality"] = code_quality_result
        _timed_tool_run("code_quality", code_quality_result, tool_runs, repo_path, t0)
        if code_quality_result["status"] == "FAIL":
            errors.append("code_quality_failed")

        t0 = time.time()
        stubs_result = _check_stubs(repo_path, changed_files)
        tools["stubs"] = stubs_result
        _timed_tool_run("stubs", stubs_result, tool_runs, repo_path, t0)
        if stubs_result["status"] == "FAIL":
            errors.append("stubs_failed")

        t0 = time.time()
        test_quality_result = _check_test_quality(repo_path, changed_files)
        tools["test_quality"] = test_quality_result
        _timed_tool_run("test_quality", test_quality_result, tool_runs, repo_path, t0)
        if test_quality_result["status"] == "FAIL":
            errors.append("test_quality_failed")
    else:
        for name in ("import_check", "code_quality", "stubs", "test_quality"):
            skipped = {"name": name, "status": "SKIPPED", "exit_code": None,
                       "findings": [], "raw_output": "", "error": "syntax_failed"}
            tools[name] = skipped
            _timed_tool_run(name, skipped, tool_runs, repo_path, time.time())

    # Injection scan — always runs on .py files
    t0 = time.time()
    injection_result = _scan_injection_patterns(repo_path, changed_files, strict=strict)
    tools["injection_scan"] = injection_result
    _timed_tool_run("injection_scan", injection_result, tool_runs, repo_path, t0)
    if injection_result["status"] == "FAIL":
        errors.append("injection_scan_failed")

    # Version pinning
    t0 = time.time()
    version_pinning_result = _check_version_pinning(repo_path)
    tools["version_pinning"] = version_pinning_result
    _timed_tool_run("version_pinning", version_pinning_result, tool_runs, repo_path, t0)
    if version_pinning_result["status"] == "FAIL":
        errors.append("version_pinning_failed")

    # Typosquat detection
    t0 = time.time()
    typosquat_result = _check_typosquat(repo_path, changed_files)
    tools["typosquat"] = typosquat_result
    _timed_tool_run("typosquat", typosquat_result, tool_runs, repo_path, t0)
    if typosquat_result["status"] == "FAIL":
        errors.append("typosquat_detected")

    # Module conflict detection
    t0 = time.time()
    module_conflicts_result = _check_module_conflicts(repo_path, changed_files)
    tools["module_conflicts"] = module_conflicts_result
    _timed_tool_run("module_conflicts", module_conflicts_result, tool_runs, repo_path, t0)
    if module_conflicts_result["status"] == "FAIL":
        errors.append("module_conflicts_detected")

    # Python version compatibility — always runs on .py files
    if python_targets:
        t0 = time.time()
        py_compat_result = _check_python_version_compat(repo_path, changed_files)
        tools["python_version_compat"] = py_compat_result
        _timed_tool_run("python_version_compat", py_compat_result, tool_runs, repo_path, t0)
        if py_compat_result["status"] == "FAIL":
            errors.append("python_version_compat_failed")

    # mypy type check — WARNING only, runs on .py files
    if python_targets:
        t0 = time.time()
        mypy_result = _check_mypy(repo_path, changed_files)
        tools["mypy"] = mypy_result
        _timed_tool_run("mypy", mypy_result, tool_runs, repo_path, t0)
        # WARNING severity — does NOT append to errors; does NOT flip disposition to FAIL

    # ─── Security governance checks ───

    # SEC-001: Hardcoded JWT secrets
    if python_targets:
        t0 = time.time()
        jwt_result = _check_hardcoded_jwt(repo_path, changed_files)
        tools["hardcoded_jwt"] = jwt_result
        _timed_tool_run("hardcoded_jwt", jwt_result, tool_runs, repo_path, t0)
        if jwt_result["status"] == "FAIL":
            errors.append("hardcoded_jwt_failed")

    # SEC-008: JWT verification policy
    if python_targets:
        t0 = time.time()
        jwt_verify_result = _check_jwt_verification_policy(repo_path, changed_files)
        tools["jwt_verification_policy"] = jwt_verify_result
        _timed_tool_run("jwt_verification_policy", jwt_verify_result, tool_runs, repo_path, t0)
        if jwt_verify_result["status"] == "FAIL":
            errors.append("jwt_verification_policy_failed")

    # SEC-006: Weak randomness in auth contexts
    if python_targets:
        t0 = time.time()
        weak_rand_result = _check_weak_randomness(repo_path, changed_files)
        tools["weak_randomness"] = weak_rand_result
        _timed_tool_run("weak_randomness", weak_rand_result, tool_runs, repo_path, t0)
        if weak_rand_result["status"] == "FAIL":
            errors.append("weak_randomness_failed")

    # SEC-005: Cookie security (HARD)
    if python_targets:
        t0 = time.time()
        cookie_hard_result = _check_cookie_security_hard(repo_path, changed_files)
        tools["cookie_security_hard"] = cookie_hard_result
        _timed_tool_run("cookie_security_hard", cookie_hard_result, tool_runs, repo_path, t0)
        if cookie_hard_result["status"] == "FAIL":
            errors.append("cookie_security_hard_failed")

    # SEC-011: Cookie security (SOFT)
    if python_targets:
        t0 = time.time()
        cookie_soft_result = _check_cookie_security_soft(repo_path, changed_files)
        tools["cookie_security_soft"] = cookie_soft_result
        _timed_tool_run("cookie_security_soft", cookie_soft_result, tool_runs, repo_path, t0)
        if cookie_soft_result["status"] == "FAIL":
            errors.append("cookie_security_soft_failed")

    # SEC-007: Token revocation
    if python_targets and not syntax_failed:
        t0 = time.time()
        token_rev_result = _check_token_revocation(repo_path, changed_files)
        tools["token_revocation"] = token_rev_result
        _timed_tool_run("token_revocation", token_rev_result, tool_runs, repo_path, t0)
        if token_rev_result["status"] == "FAIL":
            errors.append("token_revocation_failed")

    # SEC-012: Token expiry
    if python_targets:
        t0 = time.time()
        token_exp_result = _check_token_expiry(repo_path, changed_files)
        tools["token_expiry"] = token_exp_result
        _timed_tool_run("token_expiry", token_exp_result, tool_runs, repo_path, t0)
        if token_exp_result["status"] == "FAIL":
            errors.append("token_expiry_failed")

    # SEC-009: CSRF state change
    if python_targets:
        t0 = time.time()
        csrf_result = _check_csrf_state_change(repo_path, changed_files)
        tools["csrf_state_change"] = csrf_result
        _timed_tool_run("csrf_state_change", csrf_result, tool_runs, repo_path, t0)
        if csrf_result["status"] == "FAIL":
            errors.append("csrf_state_change_failed")

    # SEC-004: OAuth flow integrity
    if python_targets:
        t0 = time.time()
        oauth_result = _check_oauth_flow_integrity(repo_path, changed_files)
        tools["oauth_flow_integrity"] = oauth_result
        _timed_tool_run("oauth_flow_integrity", oauth_result, tool_runs, repo_path, t0)
        if oauth_result["status"] == "FAIL":
            errors.append("oauth_flow_integrity_failed")

    # SEC-002: Auth bypass
    if python_targets and not syntax_failed:
        t0 = time.time()
        auth_result = _check_auth_bypass(repo_path, changed_files)
        tools["auth_bypass"] = auth_result
        _timed_tool_run("auth_bypass", auth_result, tool_runs, repo_path, t0)
        if auth_result["status"] == "FAIL":
            errors.append("auth_bypass_failed")

    # SEC-003: WebSocket auth
    if python_targets:
        t0 = time.time()
        ws_result = _check_websocket_auth(repo_path, changed_files)
        tools["websocket_auth"] = ws_result
        _timed_tool_run("websocket_auth", ws_result, tool_runs, repo_path, t0)
        if ws_result["status"] == "FAIL":
            errors.append("websocket_auth_failed")

    # SEC-010: Rate limit wiring
    if python_targets:
        t0 = time.time()
        rl_wire_result = _check_rate_limit_wiring(repo_path, changed_files)
        tools["rate_limit_wiring"] = rl_wire_result
        _timed_tool_run("rate_limit_wiring", rl_wire_result, tool_runs, repo_path, t0)
        if rl_wire_result["status"] == "FAIL":
            errors.append("rate_limit_wiring_failed")

    # SEC-018: Rate limit backend quality
    if python_targets:
        t0 = time.time()
        rl_backend_result = _check_rate_limit_backend_quality(repo_path, changed_files)
        tools["rate_limit_backend_quality"] = rl_backend_result
        _timed_tool_run("rate_limit_backend_quality", rl_backend_result, tool_runs, repo_path, t0)
        if rl_backend_result["status"] == "FAIL":
            errors.append("rate_limit_backend_quality_failed")

    # SEC-013: XSS check
    if python_targets:
        t0 = time.time()
        xss_result = _check_xss(repo_path, changed_files)
        tools["xss_check"] = xss_result
        _timed_tool_run("xss_check", xss_result, tool_runs, repo_path, t0)
        if xss_result["status"] == "FAIL":
            errors.append("xss_check_failed")

    # SEC-015: SQL injection
    if python_targets:
        t0 = time.time()
        sql_result = _check_sql_injection(repo_path, changed_files)
        tools["sql_injection"] = sql_result
        _timed_tool_run("sql_injection", sql_result, tool_runs, repo_path, t0)
        if sql_result["status"] == "FAIL":
            errors.append("sql_injection_failed")

    # SEC-017: User enumeration
    if python_targets:
        t0 = time.time()
        enum_result = _check_user_enumeration(repo_path, changed_files)
        tools["user_enumeration"] = enum_result
        _timed_tool_run("user_enumeration", enum_result, tool_runs, repo_path, t0)
        if enum_result["status"] == "FAIL":
            errors.append("user_enumeration_failed")

    # SEC-016: Client trusted logic
    if python_targets:
        t0 = time.time()
        client_result = _check_client_trusted_logic(repo_path, changed_files)
        tools["client_trusted_logic"] = client_result
        _timed_tool_run("client_trusted_logic", client_result, tool_runs, repo_path, t0)
        if client_result["status"] == "FAIL":
            errors.append("client_trusted_logic_failed")

    # SEC-014: IDOR check
    if python_targets and not syntax_failed:
        t0 = time.time()
        idor_result = _check_idor(repo_path, changed_files)
        tools["idor_check"] = idor_result
        _timed_tool_run("idor_check", idor_result, tool_runs, repo_path, t0)
        if idor_result["status"] == "FAIL":
            errors.append("idor_check_failed")

    # SEC-OPS-001: Security event logging
    if python_targets and not syntax_failed:
        t0 = time.time()
        logging_result = _check_security_event_logging(repo_path, changed_files)
        tools["security_event_logging"] = logging_result
        _timed_tool_run("security_event_logging", logging_result, tool_runs, repo_path, t0)
        if logging_result["status"] == "FAIL":
            errors.append("security_event_logging_failed")

    # ─── Security Expansion Pack 1 checks ───
    # These run on any changed file (not just .py) because they target
    # frontend/browser-delivered code that may be .ts/.tsx/.jsx/.vue etc.
    from .security_pack_1 import SECURITY_PACK_1_CHECKS
    for pack_name, pack_fn, requires_python in SECURITY_PACK_1_CHECKS:
        if requires_python and not python_targets:
            tools[pack_name] = {
                "name": pack_name, "status": "PASS", "exit_code": 0,
                "findings": [], "raw_output": "no python files", "error": None,
            }
            _timed_tool_run(pack_name, tools[pack_name], tool_runs, repo_path, time.time())
            continue
        t0 = time.time()
        pack_result = pack_fn(repo_path, changed_files)
        tools[pack_name] = pack_result
        _timed_tool_run(pack_name, pack_result, tool_runs, repo_path, t0)
        if pack_result["status"] == "FAIL":
            errors.append(f"{pack_name}_failed")

    # TS/JS checks — run on .ts/.js/.mjs/.cjs/.tsx/.jsx files
    from .language_detect import is_ts_js
    ts_targets = [p for p in changed_files if is_ts_js(p)]
    if ts_targets:
        from .review_ts import run_all_ts_checks
        ts_results = run_all_ts_checks(repo_path, changed_files)
        for ts_result in ts_results:
            ts_name = ts_result["name"]
            t0 = time.time()
            tools[ts_name] = ts_result
            _timed_tool_run(ts_name, ts_result, tool_runs, repo_path, t0)
            if ts_result["status"] == "FAIL":
                errors.append(f"{ts_name}_failed")

    # API version hallucination detection (requires working imports → skip if syntax failed)
    if python_targets and not syntax_failed:
        # When auto_install_deps is enabled, create a temp venv with the
        # project's declared dependencies so we can actually verify API usage
        # even when running on a machine that doesn't have them installed.
        dep_venv_python = None
        dep_venv_dir = None
        if auto_install_deps:
            try:
                dep_venv_python, dep_venv_dir = _create_dep_venv(repo_path)
            except Exception as exc:
                logger.warning("Could not create dep venv: %s", exc)

        t0 = time.time()
        try:
            api_version_result = _check_api_version(
                repo_path, changed_files,
                run_shell_func=run_shell_func, timeout_s=timeout_s,
                venv_python=str(dep_venv_python) if dep_venv_python else None,
            )
        finally:
            if dep_venv_dir:
                shutil.rmtree(dep_venv_dir, ignore_errors=True)
        tools["api_version_check"] = api_version_result
        _timed_tool_run("api_version_check", api_version_result, tool_runs, repo_path, t0)
        if api_version_result["status"] == "FAIL":
            errors.append("api_version_check_failed")
    elif python_targets and syntax_failed:
        skipped = {"name": "api_version_check", "status": "SKIPPED", "exit_code": None,
                   "findings": [], "raw_output": "", "error": "syntax_failed"}
        tools["api_version_check"] = skipped
        _timed_tool_run("api_version_check", skipped, tool_runs, repo_path, time.time())

    # ─── DevOps checks (experimental, warning-only, changed-files scoped) ───

    # Optional Trivy integration — one cached scan for all IaC files
    t0 = time.time()
    _trivy_data = _run_trivy_config(repo_path, changed_files)
    if _trivy_data and isinstance(_trivy_data, dict):
        trivy_findings = []
        trivy_files = []
        for result_entry in _trivy_data.get("Results", []):
            target = result_entry.get("Target", "")
            trivy_files.append(target)
            for misconfig in result_entry.get("Misconfigurations", []):
                trivy_findings.append({
                    "file": target,
                    "line": misconfig.get("CauseMetadata", {}).get("StartLine", 0),
                    "kind": f"trivy:{misconfig.get('ID', 'unknown')}",
                    "detail": misconfig.get("Message", misconfig.get("Title", "")),
                })
        if trivy_files:
            trivy_status = "FAIL" if trivy_findings else "PASS"
            tools["trivy_config"] = {
                "name": "trivy_config", "status": trivy_status, "severity": "warning",
                "findings": trivy_findings, "files_checked": trivy_files,
                "exit_code": 1 if trivy_findings else 0, "raw_output": "", "error": None,
            }
            _timed_tool_run("trivy_config", tools["trivy_config"], tool_runs, repo_path, t0)

    t0 = time.time()
    dockerfile_result = _check_dockerfile(repo_path, changed_files)
    if dockerfile_result["files_checked"]:
        tools["dockerfile"] = dockerfile_result
        _timed_tool_run("dockerfile", dockerfile_result, tool_runs, repo_path, t0)

    t0 = time.time()
    github_actions_result = _check_github_actions(repo_path, changed_files)
    if github_actions_result["files_checked"]:
        tools["github_actions"] = github_actions_result
        _timed_tool_run("github_actions", github_actions_result, tool_runs, repo_path, t0)

    t0 = time.time()
    gitlab_ci_result = _check_gitlab_ci(repo_path, changed_files)
    if gitlab_ci_result["files_checked"]:
        tools["gitlab_ci"] = gitlab_ci_result
        _timed_tool_run("gitlab_ci", gitlab_ci_result, tool_runs, repo_path, t0)

    t0 = time.time()
    jenkinsfile_result = _check_jenkinsfile(repo_path, changed_files)
    if jenkinsfile_result["files_checked"]:
        tools["jenkinsfile"] = jenkinsfile_result
        _timed_tool_run("jenkinsfile", jenkinsfile_result, tool_runs, repo_path, t0)

    t0 = time.time()
    terraform_result = _check_terraform(repo_path, changed_files)
    if terraform_result["files_checked"]:
        tools["terraform"] = terraform_result
        _timed_tool_run("terraform", terraform_result, tool_runs, repo_path, t0)

    t0 = time.time()
    kubernetes_result = _check_kubernetes(repo_path, changed_files)
    if kubernetes_result["files_checked"]:
        tools["kubernetes"] = kubernetes_result
        _timed_tool_run("kubernetes", kubernetes_result, tool_runs, repo_path, t0)

    t0 = time.time()
    config_security_result = _check_config_security(repo_path, changed_files)
    if config_security_result["files_checked"]:
        tools["config_security"] = config_security_result
        _timed_tool_run("config_security", config_security_result, tool_runs, repo_path, t0)

    # ─── Usability completeness checks ───

    t0 = time.time()
    skill_md_op_result = _check_skill_md_operational(repo_path, changed_files)
    if skill_md_op_result["files_checked"]:
        tools["skill_md_operational"] = skill_md_op_result
        _timed_tool_run("skill_md_operational", skill_md_op_result, tool_runs, repo_path, t0)

    t0 = time.time()
    entrypoint_result = _check_project_entrypoint(repo_path, changed_files)
    if entrypoint_result["files_checked"]:
        tools["project_entrypoint"] = entrypoint_result
        _timed_tool_run("project_entrypoint", entrypoint_result, tool_runs, repo_path, t0)

    # Fix 12.A: Static packaging coverage
    t0 = time.time()
    pkg_cov_result = _check_packaging_coverage(repo_path, changed_files)
    if pkg_cov_result["files_checked"]:
        tools["packaging_coverage"] = pkg_cov_result
        _timed_tool_run("packaging_coverage", pkg_cov_result, tool_runs, repo_path, t0)

    # Fix 12.A: Competing DDL detection
    t0 = time.time()
    ddl_result = _check_competing_ddl(repo_path, changed_files)
    if ddl_result["files_checked"]:
        tools["competing_ddl"] = ddl_result
        _timed_tool_run("competing_ddl", ddl_result, tool_runs, repo_path, t0)

    # Fix 12.A / G7: Install-truth smoke (advisory, warning severity)
    # Only run when pyproject.toml exists and has Python source changes
    # (or pyproject.toml itself changed) to avoid slow venv creation on
    # every check.
    _has_py_changes = any(f.endswith(".py") or f == "pyproject.toml" for f in changed_files)
    if _has_py_changes and (repo_path / "pyproject.toml").is_file():
        t0 = time.time()
        smoke_result = _check_install_truth_smoke(repo_path, changed_files)
        if smoke_result["files_checked"]:
            tools["install_truth_smoke"] = smoke_result
            _timed_tool_run("install_truth_smoke", smoke_result, tool_runs, repo_path, t0)

    # Fix 12.B: Truthfulness guards
    t0 = time.time()
    crs_result = _check_constant_risk_suppression(repo_path, changed_files)
    if crs_result["files_checked"]:
        tools["constant_risk_suppression"] = crs_result
        _timed_tool_run("constant_risk_suppression", crs_result, tool_runs, repo_path, t0)

    t0 = time.time()
    swr_result = _check_schema_write_read_coherence(repo_path, changed_files)
    if swr_result["files_checked"]:
        tools["schema_write_read_coherence"] = swr_result
        _timed_tool_run("schema_write_read_coherence", swr_result, tool_runs, repo_path, t0)

    t0 = time.time()
    ltf_result = _check_lookup_table_fabrication(repo_path, changed_files)
    if ltf_result["files_checked"]:
        tools["lookup_table_fabrication"] = ltf_result
        _timed_tool_run("lookup_table_fabrication", ltf_result, tool_runs, repo_path, t0)

    t0 = time.time()
    snd_result = _check_same_name_shape_divergence(repo_path, changed_files)
    if snd_result["files_checked"]:
        tools["same_name_shape_divergence"] = snd_result
        _timed_tool_run("same_name_shape_divergence", snd_result, tool_runs, repo_path, t0)

    t0 = time.time()
    ep_result = _check_eval_preseeding(repo_path, changed_files)
    if ep_result["files_checked"]:
        tools["eval_preseeding"] = ep_result
        _timed_tool_run("eval_preseeding", ep_result, tool_runs, repo_path, t0)

    t0 = time.time()
    tvr_result = _check_test_vs_real_shape_parity(repo_path, changed_files)
    if tvr_result["files_checked"]:
        tools["test_vs_real_shape_parity"] = tvr_result
        _timed_tool_run("test_vs_real_shape_parity", tvr_result, tool_runs, repo_path, t0)

    t0 = time.time()
    dcr_result = _check_doc_code_response_shape(repo_path, changed_files)
    if dcr_result["files_checked"]:
        tools["doc_code_response_shape"] = dcr_result
        _timed_tool_run("doc_code_response_shape", dcr_result, tool_runs, repo_path, t0)

    # Fix 12.C: Live-path call-graph reachability
    t0 = time.time()
    lpr_result = _check_live_path_reachability(repo_path, changed_files)
    if lpr_result["files_checked"]:
        tools["live_path_reachability"] = lpr_result
        _timed_tool_run("live_path_reachability", lpr_result, tool_runs, repo_path, t0)

    # Fix 13: Generated-app operator truth surfaces
    t0 = time.time()
    rww_result = _check_read_without_write_surface(repo_path, changed_files)
    if rww_result["files_checked"]:
        tools["read_without_write_surface"] = rww_result
        _timed_tool_run("read_without_write_surface", rww_result, tool_runs, repo_path, t0)

    t0 = time.time()
    fsf_result = _check_frozen_status_fields(repo_path, changed_files)
    if fsf_result["files_checked"]:
        tools["frozen_status_fields"] = fsf_result
        _timed_tool_run("frozen_status_fields", fsf_result, tool_runs, repo_path, t0)

    t0 = time.time()
    wrs_result = _check_write_read_schema_asymmetry(repo_path, changed_files)
    if wrs_result["files_checked"]:
        tools["write_read_schema_asymmetry"] = wrs_result
        _timed_tool_run("write_read_schema_asymmetry", wrs_result, tool_runs, repo_path, t0)

    t0 = time.time()
    hrf_result = _check_hollow_resumed_flow(repo_path, changed_files)
    if hrf_result["files_checked"]:
        tools["hollow_resumed_flow"] = hrf_result
        _timed_tool_run("hollow_resumed_flow", hrf_result, tool_runs, repo_path, t0)

    status = "PASS"
    if any(tool["status"] == "FAIL" for tool in tools.values()):
        status = "FAIL"

    logger.info("TIMING %-30s %.2fs (%d files, %d checks)", "run_review", time.monotonic() - _t_review, len(changed_files), len(tools))
    return {
        "status": status,
        "tools": tools,
        "errors": errors,
        "strict": bool(strict),
        "tool_runs": tool_runs,
    }
