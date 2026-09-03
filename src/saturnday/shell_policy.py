import shlex
import subprocess
from pathlib import Path

DANGEROUS_COMMANDS = {"rm", "sudo", "dd", "mkfs", "mount", "umount", "chmod", "chown", "nohup"}
NETWORK_COMMANDS = {"curl", "wget", "ssh", "scp"}
ALLOWED_GIT_SUBCOMMANDS = {
    "rev-parse",
    "status",
    "diff",
    "apply",
    "worktree",
    "add",
    "reset",
    "ls-files",
    "stash",
    "update-ref",
    # Runner operations: commit, checkout, and clean are used by ticket_runner
    # to stage, commit, and reset changes during governed execution.
    "commit",
    "checkout",
    "clean",
}

SYSTEM_PATHS = frozenset({"/etc", "/usr", "/var", "/boot", "/sbin", "/proc", "/sys"})

DENY_DANGEROUS = "denied_dangerous_command"
DENY_NETWORK = "denied_network_tool"
DENY_PATH = "denied_path_traversal"
DENY_SYSTEM_PATH = "denied_system_path"
DENY_SHELL = "denied_shell_operator"
DENY_GIT = "denied_git_subcommand"
DENY_STRING = "denied_string_command"
DENY_UNKNOWN = "denied_command"

SHELL_OPERATORS = (";", "&&", "||", "`", "$(")


def _contains_shell_operator(text: str) -> bool:
    return any(op in text for op in SHELL_OPERATORS)


def _contains_path_traversal(arg: str) -> bool:
    if not arg:
        return False
    parts = []
    if "/" in arg:
        parts.extend(arg.split("/"))
    if "\\" in arg:
        parts.extend(arg.split("\\"))
    if not parts:
        parts = [arg]
    return any(part == ".." for part in parts)


def _command_basename(arg: str) -> str:
    try:
        return Path(arg).name
    except Exception:
        return arg


def _deny_record(argv: list[str], cwd: Path, timeout_s: int, reason: str, workflow: str | None) -> dict:
    return {
        "kind": "shell",
        "argv": argv,
        "cwd": str(cwd),
        "timeout_s": timeout_s,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "status": "DENIED",
        "deny_reason": reason,
        "error": None,
        "workflow": workflow,
    }


def _error_record(argv: list[str], cwd: Path, timeout_s: int, error: str, workflow: str | None) -> dict:
    return {
        "kind": "shell",
        "argv": argv,
        "cwd": str(cwd),
        "timeout_s": timeout_s,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "status": "ERROR",
        "deny_reason": None,
        "error": error,
        "workflow": workflow,
    }


def _classify_workflow(argv: list[str]) -> str | None:
    if not argv:
        return None
    cmd = _command_basename(argv[0])
    if cmd == "git":
        if len(argv) >= 2:
            return f"git:{argv[1]}"
        return "git"
    if cmd.startswith("python"):
        return "python"
    if cmd in {"ruff", "bandit", "pip-audit", "shellcheck"}:
        return "review"
    return None


def _is_allowed(argv: list[str], was_string: bool) -> tuple[bool, str | None]:
    cmd = _command_basename(argv[0])
    if cmd in {"ruff", "bandit", "pip-audit", "shellcheck"}:
        if was_string:
            return False, DENY_STRING
        return True, None
    if cmd == "git":
        if len(argv) < 2:
            return False, DENY_GIT
        subcommand = argv[1]
        if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
            return False, DENY_GIT
        return True, None
    if cmd.startswith("python"):
        if was_string:
            return False, DENY_STRING
        return True, None
    return False, DENY_UNKNOWN


def run_shell(cmd: str | list[str], *, cwd: Path, timeout_s: int, env: dict | None = None) -> dict:
    argv: list[str] = []
    was_string = isinstance(cmd, str)
    if was_string:
        if _contains_shell_operator(cmd):
            return _deny_record([], cwd, timeout_s, DENY_SHELL, None)
        try:
            argv = [str(part) for part in shlex.split(cmd)]
        except Exception:
            return _error_record([], cwd, timeout_s, "shlex_failed", None)
    else:
        argv = [str(part) for part in cmd]

    if not argv:
        return _error_record([], cwd, timeout_s, "empty_command", None)

    for arg in argv:
        if _contains_path_traversal(arg):
            return _deny_record(argv, cwd, timeout_s, DENY_PATH, _classify_workflow(argv))

    for arg in argv[1:]:
        for sp in SYSTEM_PATHS:
            if arg == sp or arg.startswith(sp + "/"):
                return _deny_record(argv, cwd, timeout_s, DENY_SYSTEM_PATH, _classify_workflow(argv))

    for arg in argv:
        base = _command_basename(arg)
        if base in DANGEROUS_COMMANDS:
            return _deny_record(argv, cwd, timeout_s, DENY_DANGEROUS, _classify_workflow(argv))
        if base in NETWORK_COMMANDS:
            return _deny_record(argv, cwd, timeout_s, DENY_NETWORK, _classify_workflow(argv))

    allowed, deny_reason = _is_allowed(argv, was_string)
    if not allowed:
        return _deny_record(argv, cwd, timeout_s, deny_reason or DENY_UNKNOWN, _classify_workflow(argv))

    workflow = _classify_workflow(argv)
    record = {
        "kind": "shell",
        "argv": argv,
        "cwd": str(cwd),
        "timeout_s": timeout_s,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "status": "ERROR",
        "deny_reason": None,
        "error": None,
        "workflow": workflow,
    }
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_s,
        )
        record["returncode"] = result.returncode
        record["stdout"] = result.stdout or ""
        record["stderr"] = result.stderr or ""
        record["status"] = "RAN"
        return record
    except subprocess.TimeoutExpired as exc:
        record["status"] = "TIMEOUT"
        record["stdout"] = exc.stdout or ""
        record["stderr"] = exc.stderr or ""
        record["error"] = "timeout"
        return record
    except FileNotFoundError:
        record["status"] = "ERROR"
        record["error"] = "command_not_found"
        return record
    except Exception as exc:
        record["status"] = "ERROR"
        record["error"] = str(exc)
        return record
