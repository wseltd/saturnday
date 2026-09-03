"""Read-only public export hygiene audit for ``saturnday public-check``.

Inspects a repo for internal tooling traces that should be addressed
before pushing to a public remote.  Does not modify any files, git
history, or repo state.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# Tracked paths that should be BLOCKED before public push
BLOCKED_TRACKED_PATTERNS = [
    ".claude/settings.json",
    ".claude/",
    ".cursor/rules",
]

# Prefix-based blocked patterns (match any file starting with this prefix)
BLOCKED_TRACKED_PREFIXES = [
    "bin/sat-",
]

# Tracked paths that should be REVIEWED before public push
REVIEW_TRACKED_PATTERNS = [
    "CLAUDE.md",
    "AGENTS.md",
]

# Commit-message patterns that indicate tooling signatures
COMMIT_MSG_PATTERNS = [
    "[T0",
    "[T1",
    "[T2",
    "[T3",
    "[T4",
    "[T5",
    "[T6",
    "[T7",
    "[T8",
    "[T9",
    "Apply ticket changes",
    "[GOVERNANCE:",
    "saturnday repair",
    "CODED_UNGOVERNED",
]

# Tracked paths that are SAFE to keep in public repo
SAFE_TRACKED = {
    ".saturnday-policy.yaml",
    ".saturnday-baseline.json",
    ".saturnday-release-manifest.yaml",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """One public-check finding."""
    category: str  # "block", "review", "info"
    path: str
    detail: str
    guidance: str


@dataclass
class PublicCheckResult:
    """Result of a public-check audit."""
    findings: list[Finding] = field(default_factory=list)
    verdict: str = ""  # "READY", "REVIEW_REQUIRED", "NOT_READY"
    dirty_tree: bool = False
    commit_scan_performed: bool = False
    flagged_commits: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def run_public_check(
    repo_path: Path,
    commit_range: str = "",
) -> PublicCheckResult:
    """Audit a repo for public export hygiene.  Read-only.

    Args:
        repo_path: Path to the git repository root.
        commit_range: Optional git commit range for commit-message scanning
            (e.g. ``"origin/main..HEAD"``).  If empty, attempts to infer
            a safe default.

    Returns:
        PublicCheckResult with classified findings and a verdict.
    """
    repo_path = repo_path.resolve()
    result = PublicCheckResult()

    # 1. Get tracked files
    tracked = _git_tracked_files(repo_path)

    # 2. Check for blocked tracked paths
    for tf in tracked:
        matched = False
        for pattern in BLOCKED_TRACKED_PATTERNS:
            if pattern.endswith("/"):
                if tf.startswith(pattern) or tf == pattern.rstrip("/"):
                    matched = True
            else:
                if tf == pattern:
                    matched = True
            if matched:
                break
        if not matched:
            for prefix in BLOCKED_TRACKED_PREFIXES:
                if tf.startswith(prefix):
                    matched = True
                    break
        if matched:
            result.findings.append(Finding(
                category="block",
                path=tf,
                detail="Internal tooling file tracked in repo",
                guidance=f"Remove from tracking: git rm --cached {tf}",
            ))

    # 3. Check for review-required tracked paths
    for tf in tracked:
        for pattern in REVIEW_TRACKED_PATTERNS:
            if tf == pattern:
                result.findings.append(Finding(
                    category="review",
                    path=tf,
                    detail="Governance config file — may expose internal workflow details",
                    guidance=f"Review whether {tf} should be in the public repo, or remove with: git rm --cached {tf}",
                ))
                break

    # 4. Check for .saturnday/ tracked (should never be, but verify)
    for tf in tracked:
        if tf.startswith(".saturnday/"):
            result.findings.append(Finding(
                category="block",
                path=tf,
                detail="Saturnday operational artefact tracked in repo (should be local-only)",
                guidance=f"Remove from tracking: git rm --cached {tf} && add .saturnday/ to .gitignore",
            ))

    # 5. Check working tree cleanliness
    dirty = _git_is_dirty(repo_path)
    result.dirty_tree = dirty
    if dirty:
        result.findings.append(Finding(
            category="review",
            path="(working tree)",
            detail="Working tree has uncommitted changes",
            guidance="Commit or stash changes before public push",
        ))

    # 6. Scan commit messages for tooling signatures
    effective_range = commit_range
    if not effective_range:
        effective_range = _infer_commit_range(repo_path)

    if effective_range:
        flagged_commits = _scan_commit_messages(repo_path, effective_range)
        for sha, msg_line in flagged_commits:
            result.findings.append(Finding(
                category="review",
                path=f"commit {sha[:8]}",
                detail=f"Commit message contains tooling signature: {msg_line[:80]}",
                guidance="See history cleanup guidance below.",
            ))
        result.flagged_commits = flagged_commits
        result.commit_scan_performed = True
    else:
        result.commit_scan_performed = False

    # 7. Compute verdict
    has_block = any(f.category == "block" for f in result.findings)
    has_review = any(f.category == "review" for f in result.findings)

    if has_block:
        result.verdict = "NOT_READY"
    elif has_review:
        result.verdict = "REVIEW_REQUIRED"
    else:
        result.verdict = "READY"

    return result


# ---------------------------------------------------------------------------
# Git helpers (read-only)
# ---------------------------------------------------------------------------

def _git_tracked_files(repo_path: Path) -> list[str]:
    """Return list of tracked file paths relative to repo root."""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            return []
        return [line for line in proc.stdout.splitlines() if line.strip()]
    except Exception:
        return []


def _git_is_dirty(repo_path: Path) -> bool:
    """Check if working tree has uncommitted changes."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return bool(proc.stdout.strip())
    except Exception:
        return False


def _infer_commit_range(repo_path: Path) -> str:
    """Try to infer a safe commit range for message scanning.

    Returns ``"origin/main..HEAD"`` or ``"origin/master..HEAD"`` if
    the remote branch exists.  Returns empty string if no safe range
    can be determined.
    """
    for branch in ("main", "master"):
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--verify", f"origin/{branch}"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            if proc.returncode == 0:
                return f"origin/{branch}..HEAD"
        except Exception:
            pass
    return ""


def _scan_commit_messages(repo_path: Path, commit_range: str) -> list[tuple[str, str]]:
    """Scan commit messages in range for tooling signatures.

    Returns list of (sha, first_line) tuples for flagged commits.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", commit_range],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    flagged: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, msg = parts[0], parts[1]
        for pattern in COMMIT_MSG_PATTERNS:
            if pattern in msg:
                flagged.append((sha, msg))
                break

    return flagged


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def generate_history_guidance(flagged: list[tuple[str, str]]) -> str:
    """Return formatted history cleanup guidance for flagged commits.

    Pure formatter — no file writes, no git operations, no side effects.

    Args:
        flagged: List of (sha, msg_line) tuples from _scan_commit_messages.

    Returns:
        Formatted guidance string, or empty string when flagged is empty.
    """
    if not flagged:
        return ""

    _HIGH = {"CODED_UNGOVERNED"}
    _MEDIUM = {"[GOVERNANCE:", "saturnday repair"}

    def _severity(msg: str) -> str:
        for p in _HIGH:
            if p in msg:
                return "HIGH"
        for p in _MEDIUM:
            if p in msg:
                return "MEDIUM"
        return "LOW"

    indent = "  "
    lines: list[str] = []

    # Section A — severity-tiered commit listing
    lines.append(f"{indent}History cleanup guidance:")
    lines.append(f"{indent}  Flagged commits ({len(flagged)}):")
    for sha, msg in flagged:
        sev = _severity(msg)
        lines.append(f"{indent}    [{sev}] {sha[:8]}  {msg[:72]}")
    lines.append("")

    # Section B — precondition checklist
    lines.append(f"{indent}  Before any history change, verify:")
    lines.append(f"{indent}    □ Have these commits been pushed to a remote?")
    lines.append(f"{indent}    □ Is anyone else working on this branch?")
    lines.append(f"{indent}    □ Do you have a local backup of the current state?")
    lines.append(f"{indent}    □ Is history cleanup actually necessary for your use case?")
    lines.append("")

    # Section C — decision guidance
    lines.append(f"{indent}  Decision guidance:")
    lines.append(f"{indent}    Not pushed yet:")
    lines.append(f"{indent}      History can be modified locally without affecting others.")
    lines.append(f"{indent}      Consult git documentation for the appropriate technique.")
    lines.append("")
    lines.append(f"{indent}    Pushed to a personal or private branch:")
    lines.append(f"{indent}      Rewriting is possible but requires overwriting the remote branch.")
    lines.append(f"{indent}      Only do this if you are certain no one else has fetched the branch.")
    lines.append("")
    lines.append(f"{indent}    Pushed to a shared or protected branch:")
    lines.append(f"{indent}      Do not rewrite history — coordinate with your team first.")
    lines.append(f"{indent}      A forward revert commit is the safer option.")
    lines.append("")

    # Section D — explicit out-of-scope warning
    lines.append(f"{indent}  This tool does not perform history rewrites.")
    lines.append(
        f"{indent}  Operations that modify commit ancestry, collapse or remove commits,"
    )
    lines.append(
        f"{indent}  or alter commit content are outside the scope of public-check."
    )
    lines.append(f"{indent}  Consult git documentation and your team before proceeding.")

    return "\n".join(lines)


def format_public_check(result: PublicCheckResult) -> str:
    """Format PublicCheckResult as human-readable output."""
    lines: list[str] = []
    lines.append("  Public Export Hygiene Check")
    lines.append(f"  {'─' * 48}")
    lines.append("")

    # Group findings
    blocked = [f for f in result.findings if f.category == "block"]
    review = [f for f in result.findings if f.category == "review"]
    info = [f for f in result.findings if f.category == "info"]

    if blocked:
        lines.append("  BLOCK — must fix before public push:")
        for f in blocked:
            lines.append(f"    ✗ {f.path}")
            lines.append(f"      {f.detail}")
            lines.append(f"      → {f.guidance}")
        lines.append("")

    if review:
        lines.append("  REVIEW — check before public push:")
        for f in review:
            lines.append(f"    ▲ {f.path}")
            lines.append(f"      {f.detail}")
            lines.append(f"      → {f.guidance}")
        lines.append("")

    if info:
        lines.append("  INFO:")
        for f in info:
            lines.append(f"    ○ {f.path}: {f.detail}")
        lines.append("")

    if not result.commit_scan_performed:
        lines.append("  NOTE: Commit-message scan was not performed.")
        lines.append("    No safe default commit range could be determined.")
        lines.append("    Rerun with: saturnday public-check --repo . --commits origin/main..HEAD")
        lines.append("")

    if not result.findings and result.commit_scan_performed:
        lines.append("  No public export issues found.")
        lines.append("")
    elif not result.findings:
        lines.append("  No tracked-file issues found (commit messages not checked — see note above).")
        lines.append("")

    # History cleanup guidance (only when commits were flagged)
    history_guidance = generate_history_guidance(result.flagged_commits)
    if history_guidance:
        lines.append(history_guidance)
        lines.append("")

    # Verdict
    verdict_map = {
        "READY": "READY FOR PUBLIC PUSH",
        "REVIEW_REQUIRED": "REVIEW REQUIRED BEFORE PUBLIC PUSH",
        "NOT_READY": "NOT READY FOR PUBLIC PUSH",
    }
    verdict_text = verdict_map.get(result.verdict, result.verdict)
    lines.append(f"  Verdict: {verdict_text}")

    return "\n".join(lines)
