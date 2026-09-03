import sys
import tempfile
from pathlib import Path

from saturnday.shell_policy import run_shell


def test_shell_policy_denies_dangerous_rm():
    with tempfile.TemporaryDirectory() as tmp_dir:
        record = run_shell(["rm", "-rf", "/"], cwd=Path(tmp_dir), timeout_s=1)
    assert record["status"] == "DENIED"
    assert record["deny_reason"] == "denied_dangerous_command"


def test_shell_policy_denies_network_tool():
    with tempfile.TemporaryDirectory() as tmp_dir:
        record = run_shell(["curl", "http://example.com"], cwd=Path(tmp_dir), timeout_s=1)
    assert record["status"] == "DENIED"
    assert record["deny_reason"] == "denied_network_tool"


def test_shell_policy_denies_shell_operator_in_string():
    with tempfile.TemporaryDirectory() as tmp_dir:
        record = run_shell('python -c "print(\\"ok\\")" && echo ok', cwd=Path(tmp_dir), timeout_s=1)
    assert record["status"] == "DENIED"
    assert record["deny_reason"] == "denied_shell_operator"


def test_deny_system_path():
    with tempfile.TemporaryDirectory() as tmp_dir:
        record = run_shell(["python", "/etc/passwd"], cwd=Path(tmp_dir), timeout_s=1)
    assert record["status"] == "DENIED"
    assert record["deny_reason"] == "denied_system_path"


def test_deny_nohup():
    with tempfile.TemporaryDirectory() as tmp_dir:
        record = run_shell(["nohup", "python", "x.py"], cwd=Path(tmp_dir), timeout_s=1)
    assert record["status"] == "DENIED"
    assert record["deny_reason"] == "denied_dangerous_command"


def test_shell_policy_allows_python_only_as_argv_list():
    with tempfile.TemporaryDirectory() as tmp_dir:
        denied = run_shell(f'{sys.executable} -c "print(\\"ok\\")"', cwd=Path(tmp_dir), timeout_s=5)
        allowed = run_shell([sys.executable, "-c", "print('ok')"], cwd=Path(tmp_dir), timeout_s=5)
    assert denied["status"] == "DENIED"
    assert denied["deny_reason"] == "denied_string_command"
    assert allowed["status"] == "RAN"
    assert allowed["returncode"] == 0
