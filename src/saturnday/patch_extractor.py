"""Parse FILE blocks or unified diffs from coder responses and apply to repo.

Cloud LLMs produce better full files than unified diffs, so FILE blocks are
tried first.  If none are found, fall back to unified diff extraction.

FILE block format::

    FILE: path/to/file.py
    <file contents>
    END FILE

Security: paths are validated against scope globs and checked for
directory traversal (``..`` and absolute paths are rejected).
"""

from __future__ import annotations

import logging
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path

from saturnday._exceptions import PatchApplicationError, PatchExtractionError

logger = logging.getLogger(__name__)

# Regex for FILE block extraction
_FILE_BLOCK_RE = re.compile(
    r"^FILE:\s*(.+?)\s*$\n(.*?)^END FILE\s*$",
    re.MULTILINE | re.DOTALL,
)

# Regex for unified diff extraction (strip markdown fences)
_DIFF_FENCE_RE = re.compile(
    r"```(?:diff)?\s*\n(.*?)```",
    re.DOTALL,
)

# Regex for stripping markdown fences from inside FILE blocks
_MARKDOWN_FENCE_RE = re.compile(
    r"^```[\w]*\s*\n(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def _strip_markdown_fences(content: str) -> str:
    """Strip markdown code fences that LLMs sometimes wrap inside FILE blocks."""
    match = _MARKDOWN_FENCE_RE.search(content)
    if match:
        return match.group(1)
    return content


def extract_changes(raw_response: str) -> dict[str, str]:
    """Try FILE blocks first, fall back to unified diff marker.

    Args:
        raw_response: Raw text from the coder.

    Returns:
        Dict mapping relative file paths to their full content.

    Raises:
        PatchExtractionError: If no valid code changes can be found.
    """
    blocks = parse_file_blocks(raw_response)
    if blocks:
        return blocks

    # Check for unified diff content
    diff_text = extract_unified_diff(raw_response)
    if diff_text:
        # Return as a special key so the caller knows to apply as diff
        return {"__unified_diff__": diff_text}

    raise PatchExtractionError(
        "No FILE blocks or unified diffs found in coder response. "
        "Expected 'FILE: path\\n...\\nEND FILE' blocks or unified diff output."
    )


def parse_file_blocks(raw: str) -> dict[str, str]:
    """Parse ``FILE: path ... END FILE`` blocks from coder output.

    Args:
        raw: Raw coder response text.

    Returns:
        Dict mapping relative paths to file content (empty if no blocks found).
    """
    blocks: dict[str, str] = {}
    for match in _FILE_BLOCK_RE.finditer(raw):
        path = match.group(1).strip()
        content = match.group(2)
        if path:
            # Strip markdown fences that some LLMs wrap inside FILE blocks
            content = _strip_markdown_fences(content)
            blocks[path] = content
    return blocks


def extract_unified_diff(raw: str) -> str:
    """Strip markdown fences and extract unified diff text.

    Args:
        raw: Raw coder response text.

    Returns:
        Extracted diff text, or empty string if none found.
    """
    # Try extracting from markdown code fences first
    fenced = _DIFF_FENCE_RE.findall(raw)
    for block in fenced:
        if "---" in block and "+++" in block:
            return block.strip()

    # Check if the raw response itself looks like a diff
    if "---" in raw and "+++" in raw and "@@" in raw:
        lines: list[str] = []
        in_diff = False
        for line in raw.splitlines():
            if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
                in_diff = True
            if in_diff:
                lines.append(line)
                if line.strip() == "" and lines and not line.startswith((" ", "+", "-", "@")):
                    break
        if lines:
            return "\n".join(lines)

    return ""


def apply_file_blocks(
    repo_path: Path,
    blocks: dict[str, str],
    allowed_globs: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> list[str]:
    """Write FILE block contents to the repository with scope enforcement.

    Args:
        repo_path: Root of the target repository.
        blocks: Dict mapping relative paths to file content.
        allowed_globs: Glob patterns the coder is allowed to write to.
        forbidden_globs: Glob patterns the coder must not write to.

    Returns:
        List of relative paths that were written.

    Raises:
        PatchApplicationError: If a path violates scope or safety constraints.
    """
    written: list[str] = []

    for rel_path, content in blocks.items():
        _validate_path(rel_path, allowed_globs, forbidden_globs)

        full_path = repo_path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        written.append(rel_path)
        logger.info("Wrote %s (%d bytes)", rel_path, len(content))

    return written


def apply_unified_diff(repo_path: Path, diff_text: str) -> list[str]:
    """Apply a unified diff to the repository using ``git apply``.

    Args:
        repo_path: Root of the target repository.
        diff_text: Unified diff text.

    Returns:
        List of files changed by the diff.

    Raises:
        PatchApplicationError: If ``git apply`` fails.
    """
    result = subprocess.run(
        ["git", "apply", "--stat", "--summary", "-"],
        input=diff_text,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )

    # Parse changed files from stat output
    changed: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2:
            changed.append(parts[0].strip())

    # Actually apply the diff
    apply_result = subprocess.run(
        ["git", "apply", "-"],
        input=diff_text,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if apply_result.returncode != 0:
        raise PatchApplicationError(
            f"git apply failed: {apply_result.stderr.strip()}"
        )

    return changed


def _validate_path(
    rel_path: str,
    allowed_globs: tuple[str, ...],
    forbidden_globs: tuple[str, ...],
) -> None:
    """Validate a relative path against scope and safety constraints.

    Raises:
        PatchApplicationError: If the path is unsafe or out of scope.
    """
    # Safety: reject absolute paths and directory traversal
    if rel_path.startswith("/"):
        raise PatchApplicationError(
            f"Absolute path rejected: {rel_path!r}"
        )
    if ".." in Path(rel_path).parts:
        raise PatchApplicationError(
            f"Directory traversal rejected: {rel_path!r}"
        )

    # Check forbidden globs first
    for pattern in forbidden_globs:
        if fnmatch(rel_path, pattern):
            raise PatchApplicationError(
                f"Path {rel_path!r} matches forbidden glob {pattern!r}"
            )

    # Check allowed globs
    if allowed_globs:
        # Standard project boilerplate files are always permitted regardless of
        # ticket scope — any ticket may create a .gitignore, LICENSE, README, etc.
        _ALWAYS_ALLOWED = frozenset({
            ".gitignore", ".gitattributes", ".editorconfig", ".env.example",
            "LICENSE", "LICENSE.txt", "LICENSE.md",
            "README.md", "README.rst", "README.txt",
            "CHANGELOG.md", "CONTRIBUTING.md",
            "pyproject.toml", "setup.py", "setup.cfg",
            "Makefile", "Dockerfile", "docker-compose.yml",
            "requirements.txt", "requirements-dev.txt",
            # Fix 70 amendment: CLI backends (claude-cli / codex-cli / cursor-cli)
            # write their own session metadata into the repo every time the
            # coder subprocess runs.  These files are NOT ticket output and
            # must not be counted as scope violations; otherwise every ticket
            # run through a CLI backend would fail on the first coder call.
            "CLAUDE.md", "AGENTS.md", ".cursorrules",
        })
        # Entire directories written by CLI agents — any path under these is
        # agent metadata, not ticket work.  Checked by path prefix so nested
        # files (e.g. .claude/settings.json) are also covered.
        _AGENT_METADATA_PREFIXES = (".claude/", ".codex/", ".cursor/")
        filename = Path(rel_path).name
        if filename in _ALWAYS_ALLOWED:
            return
        if any(rel_path.startswith(pfx) for pfx in _AGENT_METADATA_PREFIXES):
            return
        if not any(fnmatch(rel_path, pattern) for pattern in allowed_globs):
            raise PatchApplicationError(
                f"Path {rel_path!r} does not match any allowed glob"
            )
