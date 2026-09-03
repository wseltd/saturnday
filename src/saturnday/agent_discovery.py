"""Claude agent discovery for Saturnday agent governance.

Discovers project-local and user-level Claude Code agent definitions
without modifying them. Used to offer governance overlay during
interactive sessions.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r'^name:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)
_DESC_RE = re.compile(r'^description:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)


def _parse_agent_frontmatter(path: Path) -> dict[str, str] | None:
    """Parse name and description from agent .md frontmatter.

    Args:
        path: Path to the agent Markdown file.

    Returns:
        Dict with ``name``, ``path``, and ``description`` keys, or ``None``
        if the file cannot be read or has no valid frontmatter.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("Cannot read agent file %s: %s", path, exc)
        return None

    # Split on --- delimiters
    parts = content.split("---", 2)
    if len(parts) < 3:
        logger.debug("No frontmatter in %s", path)
        return None

    frontmatter = parts[1]
    name_match = _NAME_RE.search(frontmatter)
    if not name_match:
        logger.debug("No name field in frontmatter of %s", path)
        return None

    desc_match = _DESC_RE.search(frontmatter)
    return {
        "name": name_match.group(1).strip(),
        "path": str(path),
        "description": desc_match.group(1).strip() if desc_match else "",
    }


def discover_project_agents(repo_path: Path) -> list[dict[str, str]]:
    """Discover Claude agents in the project's .claude/agents/ directory.

    Args:
        repo_path: Repository root directory.

    Returns:
        List of agent dicts with ``name``, ``path``, ``description``, and
        ``source="project"`` keys.
    """
    agents_dir = repo_path / ".claude" / "agents"
    if not agents_dir.is_dir():
        return []

    results: list[dict[str, str]] = []
    for md_file in sorted(agents_dir.glob("*.md")):
        parsed = _parse_agent_frontmatter(md_file)
        if parsed:
            parsed["source"] = "project"
            results.append(parsed)
    return results


def discover_user_agents() -> list[dict[str, str]]:
    """Discover Claude agents in ~/.claude/agents/.

    Returns:
        List of agent dicts with ``name``, ``path``, ``description``, and
        ``source="user"`` keys.
    """
    agents_dir = Path.home() / ".claude" / "agents"
    if not agents_dir.is_dir():
        return []

    results: list[dict[str, str]] = []
    for md_file in sorted(agents_dir.glob("*.md")):
        parsed = _parse_agent_frontmatter(md_file)
        if parsed:
            parsed["source"] = "user"
            results.append(parsed)
    return results


def discover_all_agents(repo_path: Path) -> list[dict[str, str]]:
    """Discover all Claude agents (project + user level).

    Args:
        repo_path: Repository root directory.

    Returns:
        Combined list from :func:`discover_project_agents` and
        :func:`discover_user_agents`.  Project agents come first.
    """
    return discover_project_agents(repo_path) + discover_user_agents()
