"""Shell execution wrapper with policy enforcement for the Run pipeline.

All subprocess calls made by the ticket runner go through ``safe_subprocess_run``
so that the runner's own git operations are subject to the same policy checks
that govern external commands.

Known limitation
----------------
CLI coder backends (``codex-cli``, ``claude-cli``) run as autonomous agents
inside the repo directory and are NOT constrained at the process level by this
module.  Those backends can invoke arbitrary shell commands because they are
launched as full sub-processes with their own shells.  Proper sandboxing of
CLI coder processes requires container or seccomp isolation, which is tracked
as a future work item.  This module covers only the commands that the ticket
runner itself issues (git operations).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from saturnday.shell_policy import (
    ALLOWED_GIT_SUBCOMMANDS,
    DANGEROUS_COMMANDS,
    NETWORK_COMMANDS,
    SYSTEM_PATHS,
    _contains_path_traversal,
    _command_basename,
)

logger = logging.getLogger(__name__)


class ShellPolicyViolation(Exception):
    """Raised when ``safe_subprocess_run`` blocks a command.

    Attributes:
        cmd: The command list that was blocked.
        reason: Human-readable denial reason from the policy check.
    """

    def __init__(self, message: str, cmd: list[str] | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.cmd = cmd or []
        self.reason = reason


def safe_subprocess_run(
    cmd: list[str] | str,
    **kwargs,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` wrapper that enforces shell policy before execution.

    Checks are applied only when *cmd* is a ``list``; if *cmd* is a plain
    string it is passed through unchanged (string commands with ``shell=True``
    are out of scope for this module — they must be audited separately).

    Args:
        cmd: Command as a list of strings (preferred) or a plain string.
        **kwargs: Forwarded verbatim to ``subprocess.run``.

    Returns:
        ``subprocess.CompletedProcess`` on success.

    Raises:
        ShellPolicyViolation: If the command is blocked by shell policy.
    """
    if isinstance(cmd, list) and cmd:
        _check_policy(cmd)
    return subprocess.run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# Policy enforcement internals
# ---------------------------------------------------------------------------

def _check_policy(cmd: list[str]) -> None:
    """Raise ``ShellPolicyViolation`` if *cmd* violates any shell policy rule.

    Rules applied (in order):
    1. Path traversal in any argument (``..`` components).
    2. System path references in non-first arguments (``/etc``, ``/usr``, …).
    3. Dangerous command names (``rm``, ``sudo``, ``dd``, …).
    4. Network command names (``curl``, ``wget``, ``ssh``, ``scp``).
    5. Command allowlist: only ``git <allowed-subcommand>``, ``python*``,
       and review tools (``ruff``, ``bandit``, ``pip-audit``, ``shellcheck``)
       are permitted.

    Args:
        cmd: Non-empty list of command tokens.

    Raises:
        ShellPolicyViolation: On any policy violation.
    """
    base = _command_basename(cmd[0])

    # 1. Path traversal
    for arg in cmd:
        if _contains_path_traversal(arg):
            raise ShellPolicyViolation(
                f"Command blocked by shell policy (path traversal): {cmd}",
                cmd=cmd,
                reason="path_traversal",
            )

    # 2. System path references in arguments
    for arg in cmd[1:]:
        for sp in SYSTEM_PATHS:
            if arg == sp or arg.startswith(sp + "/"):
                raise ShellPolicyViolation(
                    f"Command blocked by shell policy (system path): {cmd}",
                    cmd=cmd,
                    reason="system_path",
                )

    # 3. Dangerous command names anywhere in the argv
    for arg in cmd:
        if _command_basename(arg) in DANGEROUS_COMMANDS:
            raise ShellPolicyViolation(
                f"Command blocked by shell policy (dangerous command): {cmd}",
                cmd=cmd,
                reason="dangerous_command",
            )
        if _command_basename(arg) in NETWORK_COMMANDS:
            raise ShellPolicyViolation(
                f"Command blocked by shell policy (network command): {cmd}",
                cmd=cmd,
                reason="network_command",
            )

    # 4. Allowlist: git, python, review tools
    _REVIEW_TOOLS = frozenset({"ruff", "bandit", "pip-audit", "shellcheck"})

    if base in _REVIEW_TOOLS:
        logger.debug("safe_subprocess_run: allowed review tool %s", cmd)
        return

    if base.startswith("python"):
        logger.debug("safe_subprocess_run: allowed python %s", cmd)
        return

    if base == "git":
        if len(cmd) < 2:
            raise ShellPolicyViolation(
                f"Command blocked by shell policy (bare git): {cmd}",
                cmd=cmd,
                reason="bare_git",
            )
        subcommand = cmd[1]
        if subcommand not in ALLOWED_GIT_SUBCOMMANDS:
            raise ShellPolicyViolation(
                f"Command blocked by shell policy (git subcommand {subcommand!r} not allowed): {cmd}",
                cmd=cmd,
                reason=f"denied_git_subcommand:{subcommand}",
            )
        logger.debug("safe_subprocess_run: allowed git %s", subcommand)
        return

    raise ShellPolicyViolation(
        f"Command blocked by shell policy (not in allowlist): {cmd}",
        cmd=cmd,
        reason="not_in_allowlist",
    )
