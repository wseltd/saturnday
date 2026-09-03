import tempfile
from pathlib import Path

from saturnday.review import run_review


def test_review_uses_shell_policy_runner():
    calls = []

    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        calls.append(cmd)
        return {
            "kind": "shell",
            "argv": cmd if isinstance(cmd, list) else [cmd],
            "cwd": str(cwd),
            "timeout_s": timeout_s,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "status": "ERROR",
            "deny_reason": None,
            "error": "command_not_found",
        }

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()
        (repo_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo_path / "hello.py").write_text('def hello():\n    return "hi"\n')

        result = run_review(
            repo_path,
            ["hello.py"],
            Path(tmp_dir),
            run_shell_func=run_shell_stub,
            timeout_s=1,
            strict=False,
        )

    assert calls
    assert all(isinstance(cmd, list) for cmd in calls)

    tools = result["tools"]
    assert tools["ruff"]["status"] == "SKIPPED"
    assert tools["ruff"]["error"] == "command_not_found"
    assert tools["bandit"]["status"] == "SKIPPED"
    assert tools["bandit"]["error"] == "command_not_found"
    assert tools["pip_audit"]["status"] == "SKIPPED"
    assert tools["pip_audit"]["error"] == "command_not_found"


def test_review_strict_fails_when_tools_missing():
    calls = []

    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        calls.append(cmd)
        return {
            "kind": "shell",
            "argv": cmd if isinstance(cmd, list) else [cmd],
            "cwd": str(cwd),
            "timeout_s": timeout_s,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "status": "ERROR",
            "deny_reason": None,
            "error": "command_not_found",
        }

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()
        (repo_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo_path / "hello.py").write_text('def hello():\n    return "hi"\n')

        result = run_review(
            repo_path,
            ["hello.py"],
            Path(tmp_dir),
            run_shell_func=run_shell_stub,
            timeout_s=1,
            strict=True,
        )

    assert calls
    tools = result["tools"]
    assert tools["ruff"]["status"] == "FAIL"
    assert tools["bandit"]["status"] == "FAIL"
    assert tools["pip_audit"]["status"] == "FAIL"
    assert result["status"] == "FAIL"


def test_review_strict_skips_pip_audit_for_non_python_changes():
    calls = []

    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        calls.append(cmd)
        return {
            "kind": "shell",
            "argv": cmd if isinstance(cmd, list) else [cmd],
            "cwd": str(cwd),
            "timeout_s": timeout_s,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "status": "ERROR",
            "deny_reason": None,
            "error": "command_not_found",
        }

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()
        (repo_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo_path / "PROJECT_SUMMARY.md").write_text("# Summary\n")
        (repo_path / "README.md").write_text("# Demo\n## Installation\n## Usage\n## Trade-Offs\n## Limitations\n## Non-Goals\n")
        (repo_path / "LICENSE").write_text("MIT License\n")

        result = run_review(
            repo_path,
            ["PROJECT_SUMMARY.md"],
            Path(tmp_dir),
            run_shell_func=run_shell_stub,
            timeout_s=1,
            strict=True,
        )

    tools = result["tools"]
    assert tools["ruff"]["status"] == "PASS"
    assert tools["bandit"]["status"] == "PASS"
    assert tools["pip_audit"]["status"] == "SKIPPED"
    assert tools["pip_audit"]["error"] == "not_applicable"
    assert result["status"] == "PASS"
    assert all(cmd != ["pip-audit", "--format", "json"] for cmd in calls)


def test_review_ignores_bandit_assert_rule_for_test_files():
    calls = []

    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        calls.append(cmd)
        tool = cmd[0]
        if tool == "ruff":
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": 0,
                "stdout": "[]",
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        if tool == "bandit":
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": 1,
                "stdout": (
                    '{"results":[{"test_id":"B101","filename":"./test/test_example.py",'
                    '"issue_text":"Use of assert detected."}]}'
                ),
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        if tool == "pip-audit":
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": 0,
                "stdout": '{"dependencies":[],"fixes":[]}',
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        raise AssertionError(f"unexpected command: {cmd}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()
        (repo_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo_path / "test").mkdir()
        (repo_path / "test" / "test_example.py").write_text("def test_example():\n    assert True\n")

        result = run_review(
            repo_path,
            ["test/test_example.py"],
            Path(tmp_dir),
            run_shell_func=run_shell_stub,
            timeout_s=1,
            strict=True,
        )

    tools = result["tools"]
    assert tools["bandit"]["status"] == "PASS"
    assert tools["bandit"]["findings"] == []
    # test_quality detects tautological assert in test file (assert True)
    assert tools["test_quality"]["status"] == "FAIL"
    # Overall status FAIL due to test_quality, but bandit filtering worked
    assert result["status"] == "FAIL"
    assert any(cmd[0] == "bandit" for cmd in calls)


def test_review_ignores_ruff_unused_import_in_init_modules():
    calls = []

    def run_shell_stub(cmd, *, cwd, timeout_s, env=None):
        calls.append(cmd)
        tool = cmd[0]
        if tool == "ruff":
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": 1,
                "stdout": (
                    '[{"code":"F401","filename":"./app/__init__.py",'
                    '"message":"unused import"}]'
                ),
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        if tool == "bandit":
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": 0,
                "stdout": '{"results":[]}',
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        if tool == "pip-audit":
            return {
                "kind": "shell",
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_s": timeout_s,
                "returncode": 0,
                "stdout": '{"dependencies":[],"fixes":[]}',
                "stderr": "",
                "status": "RAN",
                "deny_reason": None,
                "error": None,
            }
        raise AssertionError(f"unexpected command: {cmd}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()
        (repo_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        (repo_path / "README.md").write_text("# Demo\n## Installation\n## Usage\n## Trade-Offs\n## Limitations\n## Non-Goals\n")
        (repo_path / "LICENSE").write_text("MIT License\n")
        (repo_path / "app").mkdir()
        (repo_path / "app" / "__init__.py").write_text("from .main import app\n")

        result = run_review(
            repo_path,
            ["app/__init__.py"],
            Path(tmp_dir),
            run_shell_func=run_shell_stub,
            timeout_s=1,
            strict=True,
        )

    tools = result["tools"]
    assert tools["ruff"]["status"] == "PASS"
    assert tools["ruff"]["findings"] == []
    # Overall status is not asserted here: _check_tests_pass and other checks
    # unrelated to F401 suppression may cause result["status"] to be "FAIL".
    # This test's purpose is to verify F401 filtering for __init__.py files only.
    assert any(cmd[0] == "ruff" for cmd in calls)
