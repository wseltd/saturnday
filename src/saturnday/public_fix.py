"""Guided public export hygiene cleanup for ``saturnday public-fix``.

Removes internal tooling traces from the git index so they do not
appear in public pushes.  Files are kept on disk — only tracking
is removed.  Does not rewrite history, edit .gitignore, or auto-commit.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from saturnday.public_check import (
    BLOCKED_TRACKED_PATTERNS,
    BLOCKED_TRACKED_PREFIXES,
    REVIEW_TRACKED_PATTERNS,
    SAFE_TRACKED,
    _git_tracked_files,
    _git_is_dirty,
    _infer_commit_range,
    _scan_commit_messages,
)

logger = logging.getLogger(__name__)


def _is_blocked(path: str) -> bool:
    """Check if a tracked path matches BLOCK classification."""
    for pattern in BLOCKED_TRACKED_PATTERNS:
        if pattern.endswith("/"):
            if path.startswith(pattern) or path == pattern.rstrip("/"):
                return True
        else:
            if path == pattern:
                return True
    for prefix in BLOCKED_TRACKED_PREFIXES:
        if path.startswith(prefix):
            return True
    if path.startswith(".saturnday/"):
        return True
    return False


def _is_review(path: str) -> bool:
    """Check if a tracked path matches REVIEW classification."""
    return path in REVIEW_TRACKED_PATTERNS


def _is_safe(path: str) -> bool:
    """Check if a tracked path is explicitly safe to keep."""
    return path in SAFE_TRACKED


def run_public_fix_dry_run(repo_path: Path) -> dict:
    """Show planned cleanup actions without changing anything.

    Returns a dict describing what would happen.
    """
    repo_path = repo_path.resolve()
    tracked = _git_tracked_files(repo_path)

    block_targets = [f for f in tracked if _is_blocked(f)]
    review_targets = [f for f in tracked if _is_review(f)]
    dirty = _git_is_dirty(repo_path)

    commit_range = _infer_commit_range(repo_path)
    flagged_commits = _scan_commit_messages(repo_path, commit_range) if commit_range else []

    return {
        "block_targets": block_targets,
        "review_targets": review_targets,
        "dirty_tree": dirty,
        "flagged_commits": flagged_commits,
        "commit_scan_performed": bool(commit_range),
    }


def run_public_fix_apply(
    repo_path: Path,
    *,
    input_fn=None,
) -> dict:
    """Apply safe cleanup actions with confirmation.

    Args:
        repo_path: Path to the git repo.
        input_fn: Callable for user input (default: builtin input).
            Accepts a prompt string, returns user response.

    Returns:
        Dict with summary of actions taken.
    """
    if input_fn is None:
        input_fn = input

    repo_path = repo_path.resolve()
    tracked = _git_tracked_files(repo_path)

    block_targets = [f for f in tracked if _is_blocked(f)]
    review_targets = [f for f in tracked if _is_review(f)]
    dirty = _git_is_dirty(repo_path)
    commit_range = _infer_commit_range(repo_path)
    flagged_commits = _scan_commit_messages(repo_path, commit_range) if commit_range else []

    removed: list[str] = []
    skipped: list[str] = []
    review_accepted: list[str] = []
    review_declined: list[str] = []

    # 1. BLOCK items — batch confirmation
    if block_targets:
        print("\n  Files to remove from git tracking (kept on disk):")
        for f in block_targets:
            print(f"    {f}")
        print()
        try:
            confirm = input_fn("  Remove these files from tracking? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"

        if confirm == "y":
            for f in block_targets:
                ok = _git_rm_cached(repo_path, f)
                if ok:
                    removed.append(f)
                else:
                    skipped.append(f)
        else:
            skipped.extend(block_targets)
    else:
        print("\n  No blocked tracked files found.")

    # 2. REVIEW items — individual prompts, default NO
    for f in review_targets:
        print()
        try:
            choice = input_fn(f"  Remove {f} from tracking? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "n"

        if choice == "y":
            ok = _git_rm_cached(repo_path, f)
            if ok:
                removed.append(f)
                review_accepted.append(f)
            else:
                skipped.append(f)
                review_declined.append(f)
        else:
            review_declined.append(f)

    # 3. Manual-only guidance
    manual_items: list[str] = []
    if dirty:
        manual_items.append("Working tree has uncommitted changes — commit or stash before push")
    if flagged_commits:
        manual_items.append(
            f"{len(flagged_commits)} commit(s) contain tooling signatures — "
            "consider squashing or rewording with git rebase -i"
        )
    if not commit_range:
        manual_items.append(
            "Commit-message scan was not performed — "
            "rerun with: saturnday public-check --repo . --commits origin/main..HEAD"
        )

    return {
        "removed": removed,
        "skipped": skipped,
        "review_accepted": review_accepted,
        "review_declined": review_declined,
        "manual_items": manual_items,
        "dirty_tree": dirty,
    }


def _gitignore_entry(path: str) -> str:
    """Return the appropriate .gitignore pattern for a removed tracked path.

    Rules (in priority order):
    - ``.saturnday/`` subtree  →  ``/.saturnday/``
    - ``bin/sat-*`` wrappers   →  ``/bin/sat-*``
    - ``.claude/`` directory   →  ``/.claude/``
    - everything else          →  ``/<path>``
    """
    if path.startswith(".saturnday/"):
        return "/.saturnday/"
    if path.startswith("bin/sat-"):
        return "/bin/sat-*"
    if path.startswith(".claude/") or path == ".claude":
        return "/.claude/"
    return f"/{path}"


def generate_next_steps(
    removed: list[str],
    manual_items: list[str],
    dirty: bool,
) -> str:
    """Return a formatted next-steps block for display after public-fix apply.

    Pure formatter — no file writes, no git operations, no side effects.

    Args:
        removed: Paths removed from git tracking by the apply run.
        manual_items: Guidance items that require manual operator action.
        dirty: Whether the working tree had uncommitted changes at apply time.

    Returns:
        Formatted string block, or empty string when nothing to show.
    """
    if not removed:
        return ""

    lines: list[str] = []
    indent = "  "

    # ------------------------------------------------------------------
    # Section 1 — Immediate next step
    # ------------------------------------------------------------------
    lines.append(f"{indent}Immediate next step — commit the untracking changes:")
    lines.append(f"{indent}  Verify staged changes before committing:")
    lines.append(f"{indent}    git status")
    if dirty:
        lines.append(
            f"{indent}  Warning: working tree has uncommitted changes. "
            "Resolve these before committing."
        )
    lines.append(
        f"{indent}  If only the untracking removals are staged, commit with:"
    )
    lines.append(
        f'{indent}    git commit -m "Remove internal tooling files from tracking"'
    )
    lines.append("")

    # ------------------------------------------------------------------
    # Section 2 — Prevent re-tracking
    # ------------------------------------------------------------------
    seen: dict[str, None] = {}
    for path in removed:
        entry = _gitignore_entry(path)
        seen[entry] = None
    gitignore_entries = list(seen.keys())

    lines.append(f"{indent}Prevent re-tracking — add to .gitignore:")
    for entry in gitignore_entries:
        lines.append(f"{indent}  {entry}")
    lines.append(
        f"{indent}  Review before adding — do not add entries that would exclude "
        "unrelated files."
    )
    lines.append("")

    # ------------------------------------------------------------------
    # Section 3 — Local-only exclusion option
    # ------------------------------------------------------------------
    lines.append(f"{indent}Alternative — local-only exclusion (not committed):")
    lines.append(
        f"{indent}  Add the same entries to .git/info/exclude instead of .gitignore."
    )
    lines.append(
        f"{indent}  This keeps the exclusion local and does not modify any tracked file."
    )
    lines.append("")

    # ------------------------------------------------------------------
    # Section 4 — Deferred history work (only when commit signatures found)
    # ------------------------------------------------------------------
    history_items = [
        m for m in manual_items
        if "tooling signature" in m.lower()
    ]
    if history_items:
        # Extract commit count from the first matching item if present
        commit_count_str = ""
        for item in history_items:
            # e.g. "3 commit(s) contain tooling signatures — ..."
            parts = item.split(" ")
            if parts and parts[0].isdigit():
                n = int(parts[0])
                commit_count_str = f"{n} commit(s)"
                break
        if not commit_count_str:
            commit_count_str = "Some commit(s)"

        lines.append(f"{indent}Deferred — requires history cleanup:")
        lines.append(
            f"{indent}  {commit_count_str} contain tooling signatures."
        )
        lines.append(
            f"{indent}  These must be addressed before public push."
        )
        lines.append(
            f"{indent}  Run saturnday public-check --repo . --commits <range> for detailed history guidance."
        )
        lines.append("")

    return "\n".join(lines)


def format_dry_run(plan: dict) -> str:
    """Format dry-run plan as human-readable output."""
    lines: list[str] = []
    lines.append("  Public Fix — Dry Run (no changes will be made)")
    lines.append(f"  {'─' * 48}")
    lines.append("")

    block = plan.get("block_targets", [])
    if block:
        lines.append("  WILL REMOVE from tracking (git rm --cached):")
        for f in block:
            lines.append(f"    ✗ {f}")
    else:
        lines.append("  No blocked tracked files to remove.")
    lines.append("")

    review = plan.get("review_targets", [])
    if review:
        lines.append("  WILL PROMPT for review:")
        for f in review:
            lines.append(f"    ▲ {f}  [y/N — default NO]")
    else:
        lines.append("  No review-only files to prompt about.")
    lines.append("")

    manual = []
    if plan.get("dirty_tree"):
        manual.append("Working tree has uncommitted changes")
    if plan.get("flagged_commits"):
        manual.append(f"{len(plan['flagged_commits'])} commit(s) with tooling signatures")
    if not plan.get("commit_scan_performed"):
        manual.append("Commit-message scan not performed (no safe range)")

    if manual:
        lines.append("  MANUAL (no action taken — guidance only):")
        for m in manual:
            lines.append(f"    ○ {m}")
    lines.append("")
    lines.append("  To apply: saturnday public-fix --repo . --apply")

    return "\n".join(lines)


def format_apply_summary(summary: dict) -> str:
    """Format apply summary as human-readable output."""
    lines: list[str] = []
    lines.append("")
    lines.append(f"  {'─' * 48}")
    lines.append("  Public Fix Summary")
    lines.append("")

    removed = summary.get("removed", [])
    if removed:
        lines.append(f"  Removed from tracking: {len(removed)} file(s)")
        for f in removed:
            lines.append(f"    ✓ {f}")
    else:
        lines.append("  Removed from tracking: none")

    skipped = summary.get("skipped", [])
    if skipped:
        lines.append(f"  Skipped: {len(skipped)} file(s)")

    declined = summary.get("review_declined", [])
    if declined:
        lines.append(f"  Review declined: {', '.join(declined)}")

    manual = summary.get("manual_items", [])
    if manual:
        lines.append("")
        lines.append("  Still remaining (manual):")
        for m in manual:
            lines.append(f"    ○ {m}")

    if removed:
        lines.append("")
        next_steps = generate_next_steps(
            removed=summary.get("removed", []),
            manual_items=summary.get("manual_items", []),
            dirty=summary.get("dirty_tree", False),
        )
        if next_steps:
            lines.append(next_steps)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git helper (write operation — only used in apply mode)
# ---------------------------------------------------------------------------

def _git_rm_cached(repo_path: Path, rel_path: str) -> bool:
    """Remove a file from git tracking without deleting from disk.

    Returns True on success, False on failure.
    """
    try:
        proc = subprocess.run(
            ["git", "rm", "--cached", rel_path],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0:
            logger.info("Removed from tracking: %s", rel_path)
            return True
        logger.warning("git rm --cached failed for %s: %s", rel_path, proc.stderr.strip())
        return False
    except Exception as exc:
        logger.warning("git rm --cached error for %s: %s", rel_path, exc)
        return False
