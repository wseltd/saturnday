"""I.4 — opt-in work-branch pre-flight and resolution.

A ``--work-branch`` flag on governed-execution commands (run, repair,
resume, rerun-failed, rerun-remaining, start) creates a dedicated git
branch BEFORE any Saturnday commit lands, so an operator can keep their
parent branch clean.

The feature is strictly opt-in.  When the flag is absent, no git state
is inspected and no refusal path fires — existing operators see
identical behaviour.

Safety rules (refuse before any git write):

  * working tree must be clean
  * HEAD must not be detached
  * requested branch name must not already exist locally
  * branch creation must succeed

All refusals raise :class:`WorkBranchRefused` with an operator-facing
message.  Callers translate that to an exit code + stderr print.

This module does not push, does not delete, does not auto-checkout back
to the parent branch.  Those are deliberately operator decisions.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# Sentinel argparse value used when the operator passes ``--work-branch``
# without a name.  Resolved to an auto-generated name at pre-flight time.
AUTO_NAME_SENTINEL = "__saturnday_auto__"


class WorkBranchRefused(RuntimeError):
    """Raised when pre-flight refuses to create or use a work branch.

    The message is operator-facing: print it to stderr, then exit with
    a non-zero status.  No automatic recovery is attempted.
    """


@dataclass(frozen=True)
class WorkBranchContext:
    """Captured pre-flight state for a work-branch session.

    Populated by :func:`pre_flight_create` (and by
    :func:`pre_flight_resume` for resume-family commands that continue a
    previously recorded work branch).  Fields are propagated into run /
    repair evidence so the branch lifecycle is auditable.
    """

    # Parent branch the work branch was created from (the branch the
    # operator was on when they invoked the command).
    parent_branch: str
    # SHA of the parent branch at creation time.
    parent_head_sha: str
    # The work branch's name — either operator-supplied or auto-generated.
    work_branch: str
    # True if Saturnday created the branch itself in this pre-flight.
    # False when continuing a previously recorded branch (resume path).
    auto_created: bool = True


def _run_git(repo_path: Path, args: list[str], check: bool = True) -> str:
    """Run a git subcommand inside ``repo_path`` and return stripped stdout.

    Raises :class:`WorkBranchRefused` on non-zero exit when ``check`` is
    ``True`` with the git stderr verbatim in the message.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if check and result.returncode != 0:
        raise WorkBranchRefused(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )
    return (result.stdout or "").strip()


def _current_branch(repo_path: Path) -> str:
    """Return the current branch name, or ``HEAD`` if detached."""
    return _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])


def _head_sha(repo_path: Path) -> str:
    return _run_git(repo_path, ["rev-parse", "HEAD"])


def _branch_exists(repo_path: Path, name: str) -> bool:
    """True iff the named branch exists locally.

    Uses ``rev-parse --verify --quiet`` rather than ``branch --list``
    because ``branch`` is not in the shell-policy allowlist; ``rev-parse``
    is.  Both approaches produce the same answer for local refs.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path),
         "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    return result.returncode == 0


def _is_working_tree_dirty(repo_path: Path) -> tuple[bool, str]:
    """Return ``(dirty, summary)``.  The summary lists up to 5 changed paths.

    ``.saturnday/`` internal state is excluded so Saturnday's own
    evidence writes don't self-block a later invocation's pre-flight.
    """
    raw = _run_git(repo_path, ["status", "--porcelain", "--untracked-files=all"])
    if not raw:
        return False, ""
    lines = []
    for line in raw.splitlines():
        # First 3 chars are the status code + space; remainder is the path.
        path = line[3:] if len(line) >= 3 else line
        if path.startswith(".saturnday/") or path == ".saturnday":
            continue
        lines.append(path)
    if not lines:
        return False, ""
    summary = ", ".join(lines[:5]) + (f" (+{len(lines) - 5} more)" if len(lines) > 5 else "")
    return True, summary


def _auto_name(command: str) -> str:
    """Generate ``saturnday/<command>-<UTC timestamp>`` auto-name."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"saturnday/{command}-{stamp}"


def resolve_work_branch_arg(raw: str | None, command: str) -> str | None:
    """Translate a raw argparse value into an explicit branch name.

    Convention used across the CLI surface:
      * ``None``                       → flag not passed; caller does nothing
      * :data:`AUTO_NAME_SENTINEL`     → auto-generate
      * any other string               → use verbatim
    """
    if raw is None:
        return None
    if raw == AUTO_NAME_SENTINEL:
        return _auto_name(command)
    return raw


def pre_flight_create(
    repo_path: Path,
    requested_name: str,
) -> WorkBranchContext:
    """Pre-flight a work-branch creation.

    On success, the repo's HEAD is on the work branch and the returned
    :class:`WorkBranchContext` captures parent metadata for evidence.

    Raises :class:`WorkBranchRefused` on any refusal condition.
    """
    parent_branch = _current_branch(repo_path)
    if parent_branch == "HEAD" or not parent_branch:
        raise WorkBranchRefused(
            "--work-branch: HEAD is detached.  Check out a branch first "
            "(e.g. `git checkout <branch>`) before using --work-branch."
        )

    dirty, summary = _is_working_tree_dirty(repo_path)
    if dirty:
        raise WorkBranchRefused(
            "--work-branch: working tree is dirty.  Commit, stash, or "
            f"clean before using --work-branch.  Files: {summary}"
        )

    if _branch_exists(repo_path, requested_name):
        raise WorkBranchRefused(
            f"--work-branch: branch {requested_name!r} already exists "
            f"locally.  Pass a different name, or delete the existing "
            f"branch first (`git branch -D {requested_name}`)."
        )

    parent_sha = _head_sha(repo_path)

    try:
        _run_git(repo_path, ["checkout", "-b", requested_name])
    except WorkBranchRefused as exc:
        # Re-raise with a clearer prefix.
        raise WorkBranchRefused(
            f"--work-branch: failed to create branch {requested_name!r}: {exc}"
        ) from None

    logger.info(
        "I.4: created work branch %r from %s @ %s",
        requested_name, parent_branch, parent_sha[:12],
    )
    return WorkBranchContext(
        parent_branch=parent_branch,
        parent_head_sha=parent_sha,
        work_branch=requested_name,
        auto_created=True,
    )


def pre_flight_resume(
    repo_path: Path,
    recorded_work_branch: str,
) -> WorkBranchContext:
    """Pre-flight a resume-family command continuing a recorded work branch.

    Used by ``saturnday resume`` / ``rerun-failed`` / ``rerun-remaining``
    when the prior run recorded a work branch.  If the current branch
    matches the recorded one, continue in place.  If it differs, check
    it out — but only if the working tree is clean.

    Raises :class:`WorkBranchRefused` on refusal conditions.
    """
    if not recorded_work_branch:
        raise WorkBranchRefused(
            "--work-branch resume: no recorded work branch in evidence."
        )

    current = _current_branch(repo_path)
    if current == recorded_work_branch:
        # Already on the recorded branch — continue in place.  The parent
        # metadata is only meaningful at create time; for resume we
        # record the branch itself and leave parent fields empty (they
        # live on the originating run's evidence).
        head_sha = _head_sha(repo_path)
        return WorkBranchContext(
            parent_branch="",
            parent_head_sha=head_sha,
            work_branch=recorded_work_branch,
            auto_created=False,
        )

    # Different branch — must switch cleanly.
    dirty, summary = _is_working_tree_dirty(repo_path)
    if dirty:
        raise WorkBranchRefused(
            f"--work-branch resume: recorded branch is "
            f"{recorded_work_branch!r} but current is {current!r} AND "
            f"working tree is dirty.  Commit, stash, or clean first.  "
            f"Files: {summary}"
        )

    if not _branch_exists(repo_path, recorded_work_branch):
        raise WorkBranchRefused(
            f"--work-branch resume: recorded branch "
            f"{recorded_work_branch!r} no longer exists locally — cannot "
            f"continue.  Either recreate it from the original parent SHA "
            f"or start a fresh run with --work-branch."
        )

    try:
        _run_git(repo_path, ["checkout", recorded_work_branch])
    except WorkBranchRefused as exc:  # pragma: no cover — git edge case
        raise WorkBranchRefused(
            f"--work-branch resume: failed to check out "
            f"{recorded_work_branch!r}: {exc}"
        ) from None

    logger.info(
        "I.4: resumed work branch %r (was on %r)",
        recorded_work_branch, current,
    )
    head_sha = _head_sha(repo_path)
    return WorkBranchContext(
        parent_branch=current,
        parent_head_sha=head_sha,
        work_branch=recorded_work_branch,
        auto_created=False,
    )


def current_head_sha(repo_path: Path) -> str:
    """Public helper — return the current HEAD SHA.  Used by callers
    that want to record the final HEAD after a successful run."""
    try:
        return _head_sha(repo_path)
    except Exception:  # pragma: no cover — defensive
        return ""


__all__ = [
    "AUTO_NAME_SENTINEL",
    "WorkBranchRefused",
    "WorkBranchContext",
    "resolve_work_branch_arg",
    "pre_flight_create",
    "pre_flight_resume",
    "current_head_sha",
]
