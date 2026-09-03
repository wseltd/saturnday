"""Tests for the API version hallucination detector."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday.review import _check_api_version, _get_installable_dependencies, _create_dep_venv


def _shell_ok(cmd, *, cwd, timeout_s, env=None):
    """Stub: every subprocess succeeds (attribute exists)."""
    return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}


def _shell_fail_all(cmd, *, cwd, timeout_s, env=None):
    """Stub: every subprocess fails (attribute missing)."""
    return {"status": "RAN", "returncode": 1, "stdout": "", "stderr": "AttributeError"}


def _shell_selective(fail_snippets):
    """Return a shell stub that fails only when the snippet contains one of fail_snippets."""
    def run(cmd, *, cwd, timeout_s, env=None):
        if isinstance(cmd, list) and len(cmd) >= 3:
            snippet = cmd[2]
            for pattern in fail_snippets:
                if pattern in snippet:
                    return {"status": "RAN", "returncode": 1, "stdout": "", "stderr": "ImportError"}
        return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}
    return run


class TestFromImport:
    def test_valid_from_import_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
            (repo / "app.py").write_text("from collections import OrderedDict\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_ok, timeout_s=5)
            assert result["status"] == "PASS"
            assert result["findings"] == []

    def test_nonexistent_from_import_fails(self):
        """from sqlalchemy import nonexistent_thing → should be caught."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["sqlalchemy>=2.0"]\n'
            )
            (repo / "app.py").write_text("from sqlalchemy import nonexistent_thing\n")
            shell = _shell_selective(["from sqlalchemy import nonexistent_thing"])
            result = _check_api_version(repo, ["app.py"], run_shell_func=shell, timeout_s=5)
            assert result["status"] == "FAIL"
            assert len(result["findings"]) == 1
            f = result["findings"][0]
            assert f["kind"] == "api_not_found"
            assert "nonexistent_thing" in f["detail"]
            assert f["file"] == "app.py"

    def test_star_import_skipped(self):
        """from module import * should not be checked."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
            (repo / "app.py").write_text("from os import *\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_ok, timeout_s=5)
            assert result["status"] == "PASS"

    def test_stdlib_imports_skipped(self):
        """Stdlib imports should not be checked (no version concern)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text(
                "from os.path import join\nfrom collections import defaultdict\n"
            )
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"

    def test_first_party_imports_skipped(self):
        """First-party imports should not be checked."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            src = repo / "src" / "myapp"
            src.mkdir(parents=True)
            (src / "__init__.py").write_text("")
            (repo / "app.py").write_text("from myapp import utils\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"

    def test_relative_imports_skipped(self):
        """Relative imports should not be checked."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("from . import utils\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"


class TestAttributeAccess:
    def test_valid_attribute_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("import json\njson.loads('{}')\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_ok, timeout_s=5)
            assert result["status"] == "PASS"

    def test_nonexistent_attribute_fails(self):
        """requests.nonexistent_method() → should be caught."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["requests"]\n'
            )
            (repo / "app.py").write_text(
                "import requests\nresponse = requests.nonexistent_method()\n"
            )
            shell = _shell_selective(["nonexistent_method"])
            result = _check_api_version(repo, ["app.py"], run_shell_func=shell, timeout_s=5)
            assert result["status"] == "FAIL"
            assert len(result["findings"]) == 1
            f = result["findings"][0]
            assert f["kind"] == "api_attr_not_found"
            assert "nonexistent_method" in f["detail"]

    def test_stdlib_attribute_not_checked(self):
        """os.nonexistent should not be checked (stdlib)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("import os\nos.nonexistent\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"

    def test_aliased_import(self):
        """import pandas as pd; pd.nonexistent → should be caught."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["pandas"]\n'
            )
            (repo / "app.py").write_text(
                "import pandas as pd\ndf = pd.nonexistent()\n"
            )
            shell = _shell_selective(["nonexistent"])
            result = _check_api_version(repo, ["app.py"], run_shell_func=shell, timeout_s=5)
            assert result["status"] == "FAIL"
            assert result["findings"][0]["kind"] == "api_attr_not_found"
            assert "pd.nonexistent" in result["findings"][0]["detail"]


class TestEdgeCases:
    def test_non_python_files_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "readme.md").write_text("# Hello\n")
            result = _check_api_version(repo, ["readme.md"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"

    def test_syntax_error_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "bad.py").write_text("def broken(\n")
            result = _check_api_version(repo, ["bad.py"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"

    def test_missing_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            result = _check_api_version(repo, ["ghost.py"], run_shell_func=_shell_fail_all, timeout_s=5)
            assert result["status"] == "PASS"

    def test_result_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("x = 1\n")
            result = _check_api_version(repo, ["app.py"], run_shell_func=_shell_ok, timeout_s=5)
            assert result["name"] == "api_version_check"
            assert "status" in result
            assert "findings" in result
            assert "exit_code" in result
            assert "error" in result

    def test_caching_avoids_duplicate_calls(self):
        """Same (module, attr) pair should only spawn one subprocess."""
        call_count = 0

        def counting_shell(cmd, *, cwd, timeout_s, env=None):
            nonlocal call_count
            call_count += 1
            return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Two files both do `import requests; requests.get(...)`
            (repo / "a.py").write_text("import requests\nrequests.get('url')\n")
            (repo / "b.py").write_text("import requests\nrequests.get('url2')\n")
            result = _check_api_version(
                repo, ["a.py", "b.py"], run_shell_func=counting_shell, timeout_s=5,
            )
            assert result["status"] == "PASS"
            # Should only call subprocess once for (requests, get), not twice
            assert call_count == 1


class TestMultipleFindings:
    def test_multiple_bad_imports_in_one_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["sqlalchemy>=2.0"]\n'
            )
            (repo / "app.py").write_text(
                "from sqlalchemy import bad_one\n"
                "from sqlalchemy import bad_two\n"
            )
            # Package itself importable, but specific names fail
            shell = _shell_selective(["from sqlalchemy import bad_one", "from sqlalchemy import bad_two"])
            result = _check_api_version(
                repo, ["app.py"], run_shell_func=shell, timeout_s=5,
            )
            assert result["status"] == "FAIL"
            assert len([f for f in result["findings"] if f.get("kind") == "api_not_found"]) == 2
            names = {f["detail"] for f in result["findings"] if f.get("kind") == "api_not_found"}
            assert any("bad_one" in n for n in names)
            assert any("bad_two" in n for n in names)


class TestDeclaredNotInstalled:
    def test_from_import_declared_not_installed_skips(self):
        """from declared_pkg import Foo should be INFO when pkg not installed."""
        # Shell fails for all imports (package not installed)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = [\n    "occ>=7.0",\n]\n'
            )
            (repo / "app.py").write_text("from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Fuse\n")
            result = _check_api_version(
                repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5,
            )
            assert result["status"] == "PASS"
            assert any(f.get("kind") == "declared_not_installed" for f in result["findings"])

    def test_attr_declared_not_installed_skips(self):
        """module.attr should be INFO when declared pkg not installed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = [\n    "openai>=1.0",\n]\n'
            )
            (repo / "app.py").write_text(
                "import openai\nopenai.ChatCompletion.create()\n"
            )
            result = _check_api_version(
                repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5,
            )
            assert result["status"] == "PASS"
            assert any(f.get("kind") == "declared_not_installed" for f in result["findings"])

    def test_from_import_undeclared_still_fails(self):
        """from unknown_pkg import Foo still FAILs when not declared."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text('[project]\nname = "demo"\n')
            (repo / "app.py").write_text("from unknown_pkg import Foo\n")
            result = _check_api_version(
                repo, ["app.py"], run_shell_func=_shell_fail_all, timeout_s=5,
            )
            assert result["status"] == "FAIL"
            assert any(f.get("kind") == "package_not_importable" for f in result["findings"])


class TestGetInstallableDependencies:
    def test_reads_pyproject_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = [\n'
                '    "fastapi>=0.100",\n'
                '    "sqlalchemy>=2.0",\n'
                ']\n'
            )
            deps = _get_installable_dependencies(repo)
            assert "fastapi>=0.100" in deps
            assert "sqlalchemy>=2.0" in deps

    def test_reads_requirements_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "requirements.txt").write_text("flask>=2.0\nrequests\n# comment\n")
            deps = _get_installable_dependencies(repo)
            assert "flask>=2.0" in deps
            assert "requests" in deps
            assert len(deps) == 2

    def test_empty_when_no_deps(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            deps = _get_installable_dependencies(repo)
            assert deps == []

    def test_prefers_pyproject_over_requirements(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "x"\ndependencies = ["click"]\n'
            )
            (repo / "requirements.txt").write_text("flask\n")
            deps = _get_installable_dependencies(repo)
            assert "click" in deps
            # requirements.txt is only read when pyproject.toml has no deps
            assert "flask" not in deps


class TestVenvPython:
    def test_venv_python_used_when_provided(self):
        """When venv_python is set, inner functions should use it instead of sys.executable."""
        calls = []

        def recording_shell(cmd, *, cwd, timeout_s, env=None):
            calls.append(cmd)
            return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["requests"]\n'
            )
            (repo / "app.py").write_text("import requests\nrequests.get('url')\n")
            result = _check_api_version(
                repo, ["app.py"],
                run_shell_func=recording_shell,
                timeout_s=5,
                venv_python="/fake/venv/bin/python",
            )
            # All subprocess calls should use the venv python, not sys.executable
            for cmd in calls:
                assert cmd[0] == "/fake/venv/bin/python", f"Expected venv python, got {cmd[0]}"

    def test_default_uses_sys_executable(self):
        """When venv_python is None, uses sys.executable."""
        calls = []

        def recording_shell(cmd, *, cwd, timeout_s, env=None):
            calls.append(cmd)
            return {"status": "RAN", "returncode": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "app.py").write_text("import requests\nrequests.get('url')\n")
            _check_api_version(
                repo, ["app.py"],
                run_shell_func=recording_shell,
                timeout_s=5,
            )
            for cmd in calls:
                assert cmd[0] == sys.executable


class TestCreateDepVenv:
    def test_creates_venv_with_deps(self):
        """Smoke test: creates a real venv and installs a small package."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text(
                '[project]\nname = "demo"\ndependencies = ["six"]\n'
            )
            python, venv_dir = _create_dep_venv(repo, timeout_s=120)
            try:
                assert python is not None
                assert venv_dir is not None
                assert python.exists()
                # Verify six is importable in the venv
                import subprocess
                result = subprocess.run(
                    [str(python), "-c", "import six; print(six.__version__)"],
                    capture_output=True, text=True,
                )
                assert result.returncode == 0
            finally:
                if venv_dir:
                    import shutil
                    shutil.rmtree(venv_dir, ignore_errors=True)

    def test_returns_none_when_no_deps(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            python, venv_dir = _create_dep_venv(repo)
            assert python is None
            assert venv_dir is None
