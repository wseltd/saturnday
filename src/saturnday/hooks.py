"""Claude Code hooks for Saturnday edit gating.

Provides :func:`install_claude_hooks` which writes a ``.claude/settings.json``
to the target repository, injecting pre/post-tool-use notifications that
remind the coder to read before editing and to run governance before committing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SATURNDAY_HOOKS: dict = {
    "attribution": {
        "commit": "",
        "pr": "",
    },
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": "echo 'Saturnday: Ensure you have read this file before editing. Prefer small edits over rewrites.'",
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": "echo 'Saturnday: Change recorded. Run saturnday governance --repo . --staged before committing.'",
                    }
                ],
            }
        ],
    }
}


def install_claude_hooks(repo_path: Path) -> Path | None:
    """Write Claude Code hooks to ``.claude/settings.json`` in the repo.

    Preserves existing settings — only adds hooks if not already present.
    Merges hook lists rather than overwriting so pre-existing hooks from
    other tools survive.

    Args:
        repo_path: Root of the repository where ``.claude/`` lives.

    Returns:
        Path to the written settings file, or ``None`` if not applicable
        (e.g. if the repo path does not exist).
    """
    if not repo_path.is_dir():
        logger.debug("install_claude_hooks: repo_path does not exist: %s", repo_path)
        return None

    claude_dir = repo_path / ".claude"
    settings_path = claude_dir / "settings.json"

    existing: dict = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug(
                "install_claude_hooks: could not parse existing settings at %s: %s",
                settings_path,
                exc,
            )

    # Idempotency check — skip if Saturnday hooks are already present
    existing_hooks = existing.get("hooks", {})
    pre_hooks = existing_hooks.get("PreToolUse", [])
    if any("Saturnday" in json.dumps(h) for h in pre_hooks):
        logger.debug("install_claude_hooks: hooks already present, skipping")
        return settings_path

    # Merge hooks and settings
    if "hooks" not in existing:
        existing["hooks"] = {}
    for event, hooks_list in SATURNDAY_HOOKS["hooks"].items():
        if event not in existing["hooks"]:
            existing["hooks"][event] = []
        existing["hooks"][event].extend(hooks_list)

    # Disable co-author attribution
    if "attribution" not in existing:
        existing["attribution"] = SATURNDAY_HOOKS["attribution"]

    try:
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        logger.info("install_claude_hooks: wrote hooks to %s", settings_path)
    except OSError as exc:
        logger.warning("install_claude_hooks: write failed for %s: %s", settings_path, exc)
        return None

    return settings_path
