import sys
import tempfile
from pathlib import Path

from saturnday.review import (
    _check_code_quality,
    _check_dependency_declaration,
    _check_imports,
    _check_module_conflicts,
    _check_stubs,
    _check_syntax,
    _check_test_quality,
    _check_typosquat,
    _check_version_pinning,
    _scan_injection_patterns,
    _scan_placeholders,
    run_review,
)


def test_scan_placeholders_fail_todo():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def run():\n    pass  # TODO finish this\n")
        result = _scan_placeholders(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert result["name"] == "placeholders"
        assert len(result["findings"]) >= 1
        assert result["findings"][0]["file"] == "app.py"
        assert result["findings"][0]["line"] == 2


def test_scan_placeholders_fail_not_implemented():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "lib.py").write_text("def process():\n    raise NotImplementedError\n")
        result = _scan_placeholders(repo, ["lib.py"])
        assert result["status"] == "FAIL"
        assert any("NotImplementedError" in f["pattern"] for f in result["findings"])


def test_scan_placeholders_fail_ellipsis():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "stub.py").write_text("def placeholder():\n    ...\n")
        result = _scan_placeholders(repo, ["stub.py"])
        assert result["status"] == "FAIL"


def test_scan_placeholders_pass_clean():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "clean.py").write_text('def hello():\n    return "world"\n')
        result = _scan_placeholders(repo, ["clean.py"])
        assert result["status"] == "PASS"
        assert result["findings"] == []


def test_scan_placeholders_skips_non_python():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "notes.txt").write_text("TODO: remember to do this\n")
        result = _scan_placeholders(repo, ["notes.txt"])
        assert result["status"] == "PASS"


def test_check_dependency_declaration_fail_undeclared():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (repo / "app.py").write_text("import requests\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert result["name"] == "dependency_declaration"
        assert any("requests" in f["pattern"] for f in result["findings"])


def test_check_dependency_declaration_pass_stdlib():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (repo / "app.py").write_text("import os\nimport json\nimport sys\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_check_dependency_declaration_pass_declared():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["requests"]\n'
        )
        (repo / "app.py").write_text("import requests\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_check_dependency_declaration_pass_first_party():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "myproject"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('[project]\nname = "myproject"\n')
        src = repo / "src" / "myproject"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("")
        (repo / "app.py").write_text("import myproject\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_check_dependency_declaration_skips_relative():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (repo / "app.py").write_text("from . import utils\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Fix 50: dependency declaration alias tests
# ---------------------------------------------------------------------------

def test_check_dependency_declaration_alias_rdkit():
    """rdkit-pypi declared, import rdkit — must PASS via alias."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["rdkit-pypi"]\n'
        )
        (repo / "app.py").write_text("import rdkit\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS", f"rdkit-pypi alias failed: {result['findings']}"


def test_check_dependency_declaration_alias_pyyaml():
    """pyyaml declared, import yaml — must PASS via alias."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pyyaml"]\n'
        )
        (repo / "app.py").write_text("import yaml\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS", f"pyyaml alias failed: {result['findings']}"


def test_check_dependency_declaration_alias_sklearn():
    """scikit-learn declared, import sklearn — must PASS via alias."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["scikit-learn"]\n'
        )
        (repo / "app.py").write_text("from sklearn import tree\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS", f"scikit-learn alias failed: {result['findings']}"


def test_check_dependency_declaration_exact_match_still_works():
    """Direct match (requests declared, import requests) must still PASS."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["requests"]\n'
        )
        (repo / "app.py").write_text("import requests\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_check_dependency_declaration_undeclared_still_fails():
    """Truly undeclared import must still FAIL."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (repo / "app.py").write_text("import totally_fake_package\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "FAIL"


def test_check_dependency_declaration_dash_underscore_normalization():
    """Dash vs underscore normalization: python-dateutil declared, import dateutil."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["python-dateutil"]\n'
        )
        (repo / "app.py").write_text("import dateutil\n")
        result = _check_dependency_declaration(repo, ["app.py"])
        assert result["status"] == "PASS", f"python-dateutil alias failed: {result['findings']}"


def test_check_imports_pass():
    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import os\nimport json\n")
        result = _check_imports(repo, ["app.py"], run_shell_func=run_shell_stub, timeout_s=5)
        assert result["status"] == "PASS"
        assert result["name"] == "import_check"


def test_check_imports_fail():
    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        if isinstance(cmd, list) and len(cmd) >= 3 and "nonexistent_pkg" in cmd[2]:
            return {"status": "RAN", "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError"}
        return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import nonexistent_pkg\n")
        result = _check_imports(repo, ["app.py"], run_shell_func=run_shell_stub, timeout_s=5)
        assert result["status"] == "FAIL"
        assert any("nonexistent_pkg" in f["pattern"] for f in result["findings"])


def test_code_quality_pass_annotated():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def greet(name: str) -> str:\n    return f'hello {name}'\n")
        result = _check_code_quality(repo, ["app.py"])
        assert result["status"] == "PASS"
        assert result["name"] == "code_quality"
        assert result["findings"] == []


def test_code_quality_fail_missing_return_type():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def greet(name: str):\n    return f'hello {name}'\n")
        result = _check_code_quality(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "missing_type_hint" for f in result["findings"])


def test_code_quality_fail_missing_param_type():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def greet(name) -> str:\n    return f'hello {name}'\n")
        result = _check_code_quality(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "missing_type_hint" for f in result["findings"])


def test_code_quality_fail_missing_repr():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "models.py").write_text("class Task(Base):\n    pass\n")
        result = _check_code_quality(repo, ["models.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "missing_repr" for f in result["findings"])


def test_code_quality_fail_utcnow():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("from datetime import datetime\nx = datetime.utcnow()\n")
        result = _check_code_quality(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "deprecated_utcnow" for f in result["findings"])


def test_code_quality_skips_dunders():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("class Foo:\n    def __init__(self, x):\n        self.x = x\n")
        result = _check_code_quality(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_code_quality_skips_non_python():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "readme.md").write_text("# Hello\ndef no_hints(x):\n    pass\n")
        result = _check_code_quality(repo, ["readme.md"])
        assert result["status"] == "PASS"


# --- Stub detection tests (Phase 1C) ---

def test_stubs_fail_hardcoded_return():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def get_count():\n    return 42\n")
        result = _check_stubs(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "hardcoded_return" for f in result["findings"])


def test_stubs_fail_print_only():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text('def process():\n    print("done")\n')
        result = _check_stubs(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "print_only_function" for f in result["findings"])


def test_stubs_pass_real_function():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def add(a: int, b: int) -> int:\n    result = a + b\n    return result\n")
        result = _check_stubs(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_stubs_skips_test_files():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "test_app.py").write_text("def get_count():\n    return 42\n")
        result = _check_stubs(repo, ["test_app.py"])
        assert result["status"] == "PASS"


# --- Timing metadata test (Phase 1D) ---

def test_tool_run_has_timing():
    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        return {"status": "ERROR", "returncode": None, "stdout": "", "stderr": "",
                "deny_reason": None, "error": "command_not_found"}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo / "app.py").write_text('def hello() -> str:\n    return "hi"\n')
        result = run_review(repo, ["app.py"], Path(tmp), run_shell_func=run_shell_stub, timeout_s=1)
        tool_runs = result["tool_runs"]
        assert len(tool_runs) > 0
        for tr in tool_runs:
            assert "start_ts" in tr
            assert "duration_s" in tr
            assert isinstance(tr["duration_s"], float)


# --- Fake test detection tests (Phase 2B) ---

def test_test_quality_fail_no_assert():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "test_app.py").write_text("def test_x():\n    x = 1\n")
        result = _check_test_quality(repo, ["test_app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "test_no_assert" for f in result["findings"])


def test_test_quality_fail_tautological():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "test_app.py").write_text("def test_x():\n    assert True\n")
        result = _check_test_quality(repo, ["test_app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "tautological_assert" for f in result["findings"])


def test_test_quality_fail_empty_body():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "test_app.py").write_text("def test_x():\n    pass\n")
        result = _check_test_quality(repo, ["test_app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "test_empty_body" for f in result["findings"])


def test_test_quality_fail_caught_assert():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        code = (
            "def test_x():\n"
            "    try:\n"
            "        assert 1 == 2\n"
            "    except Exception:\n"
            "        pass\n"
        )
        (repo / "test_app.py").write_text(code)
        result = _check_test_quality(repo, ["test_app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "assert_caught" for f in result["findings"])


def test_test_quality_pass_real_test():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "test_app.py").write_text("def test_add():\n    assert 1 + 1 == 2\n")
        result = _check_test_quality(repo, ["test_app.py"])
        assert result["status"] == "PASS"


def test_test_quality_skips_non_test():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def test_x():\n    x = 1\n")
        result = _check_test_quality(repo, ["app.py"])
        assert result["status"] == "PASS"


# --- Prompt injection tests (Phase 3B) ---

def test_injection_scan_detects_role_marker():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text('x = "<<SYS>>"\n')
        result = _scan_injection_patterns(repo, ["app.py"])
        assert len(result["findings"]) >= 1
        assert any(f["kind"] == "role_marker" for f in result["findings"])


def test_injection_scan_pass_normal_code():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("def hello() -> str:\n    return 'hi'\n")
        result = _scan_injection_patterns(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_injection_scan_skips_docs():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "notes.md").write_text("<<SYS>> marker here\n")
        result = _scan_injection_patterns(repo, ["notes.md"])
        assert result["status"] == "PASS"
        assert result["findings"] == []


# --- Version pinning tests (Phase 4A) ---

def test_version_pinning_fail_unpinned():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = [\n    "requests",\n]\n'
        )
        result = _check_version_pinning(repo)
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "unpinned_dependency" for f in result["findings"])


def test_version_pinning_pass_pinned():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = [\n    "requests>=2.31.0",\n]\n'
        )
        result = _check_version_pinning(repo)
        assert result["status"] == "PASS"


# --- Typosquat tests (Phase 4A) ---

def test_typosquat_detects_misspelling():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import reqeusts\n")
        result = _check_typosquat(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "possible_typosquat" for f in result["findings"])


def test_typosquat_pass_real_package():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import requests\n")
        result = _check_typosquat(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_typosquat_skips_stdlib_os():
    """'os' must not be flagged as typosquat of 'tox'."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import os\n")
        result = _check_typosquat(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_typosquat_skips_stdlib_sys():
    """'sys' must not be flagged as typosquat of 'six'."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import sys\n")
        result = _check_typosquat(repo, ["app.py"])
        assert result["status"] == "PASS"


def test_typosquat_skips_common_app():
    """'app' must not be flagged as typosquat of 'pip'."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "main.py").write_text("import app\n")
        result = _check_typosquat(repo, ["main.py"])
        assert result["status"] == "PASS"


def test_typosquat_still_catches_misspelling():
    """'reqeusts' must still be caught (regression guard)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import reqeusts\n")
        result = _check_typosquat(repo, ["app.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "possible_typosquat" for f in result["findings"])


def test_import_check_info_when_declared_not_installed():
    """Import of a declared-but-not-installed package should be INFO, not FAIL."""
    def shell_fail(cmd, *, cwd, timeout_s, env=None):
        return {"status": "RAN", "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError"}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = [\n    "some_exotic_pkg>=1.0",\n]\n'
        )
        (repo / "app.py").write_text("import some_exotic_pkg\n")
        result = _check_imports(repo, ["app.py"], run_shell_func=shell_fail, timeout_s=5)
        assert result["status"] == "PASS"
        assert any(f.get("kind") == "declared_not_installed" for f in result["findings"])


def test_import_check_still_fails_undeclared():
    """Import of truly unknown package still FAILs."""
    def shell_fail(cmd, *, cwd, timeout_s, env=None):
        return {"status": "RAN", "returncode": 1, "stdout": "", "stderr": "ModuleNotFoundError"}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        (repo / "app.py").write_text("import totally_unknown_pkg\n")
        result = _check_imports(repo, ["app.py"], run_shell_func=shell_fail, timeout_s=5)
        assert result["status"] == "FAIL"


# --- Syntax check tests (Phase 5A) ---

def test_syntax_check_fail():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "broken.py").write_text("def f(\n")
        result = _check_syntax(repo, ["broken.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "syntax_error" for f in result["findings"])


def test_syntax_check_pass():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "ok.py").write_text("def f():\n    return 1\n")
        result = _check_syntax(repo, ["ok.py"])
        assert result["status"] == "PASS"


def test_syntax_fail_skips_downstream():
    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        return {"status": "ERROR", "returncode": None, "stdout": "", "stderr": "",
                "deny_reason": None, "error": "command_not_found"}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo / "broken.py").write_text("def f(\n")
        result = run_review(repo, ["broken.py"], Path(tmp), run_shell_func=run_shell_stub, timeout_s=1)
        assert result["tools"]["syntax"]["status"] == "FAIL"
        assert result["tools"]["ruff"]["status"] == "SKIPPED"
        assert result["tools"]["bandit"]["status"] == "SKIPPED"
        assert result["tools"]["import_check"]["status"] == "SKIPPED"
        assert result["tools"]["code_quality"]["status"] == "SKIPPED"
        assert result["tools"]["stubs"]["status"] == "SKIPPED"
        assert result["tools"]["test_quality"]["status"] == "SKIPPED"


# --- Module conflict tests (Phase 5B) ---

def test_module_conflict_duplicate_names():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "utils.py").write_text("x = 1\n")
        subdir = repo / "pkg"
        subdir.mkdir()
        (subdir / "utils.py").write_text("y = 2\n")
        result = _check_module_conflicts(repo, ["utils.py", "pkg/utils.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "duplicate_module" for f in result["findings"])


def test_module_conflict_circular():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "alpha.py").write_text("from beta import something\n")
        (repo / "beta.py").write_text("from alpha import other\n")
        result = _check_module_conflicts(repo, ["alpha.py", "beta.py"])
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "circular_import" for f in result["findings"])


def test_module_conflict_pass():
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "app.py").write_text("import os\n")
        (repo / "main.py").write_text("from app import something\n")
        result = _check_module_conflicts(repo, ["app.py", "main.py"])
        assert result["status"] == "PASS"
