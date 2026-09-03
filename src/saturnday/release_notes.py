"""Structured release notes manifest for Saturnday.

Each release note entry captures version metadata, highlights, notable fixes,
and upgrade instructions. Used by the update notifier to display contextual
banners to users when a new version is available.

Design principles:
- Immutable data: notes are defined at module load, never mutated at runtime
- Lookup by (version, edition) pair to support public/premium split
- Formatting helpers produce ready-to-print strings with no external deps
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseNote:
    """Structured release note for a single version + edition combination.

    Attributes:
        version: SemVer-style version string (e.g. "1.1.01").
        edition: Which product edition this note covers ("public" or "premium").
        summary: Single-line summary shown in short banners.
        highlights: Ordered list of feature highlights for this release.
        fixes: Ordered list of notable bug/behaviour fixes.
        upgrade_command: Shell command the user should run to upgrade.
        first_seen_detail_enabled: When True, the detailed banner is shown
            once (the first time the user encounters this version). Subsequent
            invocations fall back to the short banner.
    """

    version: str
    edition: str = "public"
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    upgrade_command: str = "python -m pip install -U saturnday"
    first_seen_detail_enabled: bool = True


# ---------------------------------------------------------------------------
# Notes registry — keyed by (version, edition)
# ---------------------------------------------------------------------------

_NOTES: dict[tuple[str, str], ReleaseNote] = {}


def _register(note: ReleaseNote) -> None:
    """Register a release note into the global registry."""
    key = (note.version, note.edition)
    if key in _NOTES:
        logger.warning(
            "Duplicate release note registration for version=%s edition=%s — "
            "existing entry will be overwritten.",
            note.version,
            note.edition,
        )
    _NOTES[key] = note


# ---------------------------------------------------------------------------
# Version 1.1.1 — public edition
# ---------------------------------------------------------------------------

_register(
    ReleaseNote(
        version="1.1.1",
        edition="public",
        summary=(
            "Governed terminal-first code and document production with 85+ "
            "deterministic checks"
        ),
        highlights=[
            (
                "Governed terminal-first workflow for code through the existing "
                "core commands, with planning, execution, governance, repair, "
                "and evidence"
            ),
            (
                "85+ deterministic governance and scanner checks across code "
                "and project-level hygiene"
            ),
            (
                "Stronger TypeScript and JavaScript security scanning with "
                "reduced false positives in common patterns"
            ),
            (
                "Contract checking and verify-command execution wired into "
                "the governed flow"
            ),
            (
                "Stronger standards delivery and leaner context handling to "
                "reduce prompt bloat and wasted overhead"
            ),
            (
                "Safer run behaviour with improved crash resilience, partial "
                "run summaries, auto-resume safety, and cleaner failure handling"
            ),
            (
                "Public deterministic document mode, including: document spec "
                "parsing and validation, document planning, section-by-section "
                "generation, deterministic document checks, retry loop, "
                "cross-section consistency checks, provisional status handling, "
                "document evidence pack generation"
            ),
        ],
        fixes=[
            "Improved Codex/Cursor execution stability",
            "Reduced unnecessary overhead from progress-message execution",
            "Constrained repair/simplifier behaviour to avoid over-broad edits",
            "Stronger virtual-environment use for verification and tests",
            "Project-level governance checks now run more reliably",
            (
                "Policy and exemption handling is applied more consistently "
                "during governance"
            ),
            (
                "Partial run summaries are written during execution so crashes "
                "do not hide progress"
            ),
            "Ctrl+C stops the run cleanly",
            "Verify-command handling is fixed and integrated properly",
            "Auto-resume is safer across different plans and project IDs",
            "Repair scope constraints are tighter",
            "The Ruff __init__.py unused-import suppression issue is fixed",
            "Improved new-version notification with cached non-blocking checks and structured release notes",
            "Generated artefacts moved under .saturnday/ to reduce repo-root clutter",
            "Cleaner git hygiene with .git/info/exclude instead of modifying tracked .gitignore",
            "Improved handling of broad repair instructions such as 'fix all the security issues'",
            "Reduced review noise from generated artefacts in normal commits",
            "Per-run unique evidence directories prevent overwrite on sequential runs",
        ],
        upgrade_command="python -m pip install -U saturnday",
        first_seen_detail_enabled=True,
    )
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_release_note(version: str, edition: str = "public") -> ReleaseNote | None:
    """Return the release note for the given version and edition, or None.

    Args:
        version: Version string to look up (e.g. "1.1.01").
        edition: Product edition — "public" or "premium". Defaults to "public".

    Returns:
        The matching :class:`ReleaseNote`, or ``None`` if no entry is registered
        for the requested (version, edition) pair.
    """
    key = (version, edition)
    note = _NOTES.get(key)
    if note is None:
        logger.debug(
            "No release note found for version=%s edition=%s.", version, edition
        )
    return note


def format_short_banner(current: str, latest: str, upgrade_cmd: str) -> str:
    """Return a compact one-line update prompt.

    Suitable for appending to any command output when the user has already seen
    the detailed banner for this version, or when no detailed release note
    exists.

    Args:
        current: The version the user currently has installed.
        latest: The latest available version on PyPI.
        upgrade_cmd: The shell command to run to upgrade.

    Returns:
        A formatted string ready to print. Does not include a trailing newline.

    Example::

        saturnday 1.1.01 is available (you have 1.0.0). Run: python -m pip install -U saturnday
    """
    return (
        f"saturnday {latest} is available (you have {current}). "
        f"Run: {upgrade_cmd}"
    )


def format_detailed_banner(note: ReleaseNote) -> str:
    """Return a multi-line banner with highlights and notable fixes.

    Shown once per version when :attr:`ReleaseNote.first_seen_detail_enabled`
    is ``True`` and the version has not yet been shown to this user. Subsequent
    invocations for the same version fall back to :func:`format_short_banner`.

    Args:
        note: The :class:`ReleaseNote` to render.

    Returns:
        A formatted multi-line string ready to print. Ends with a single
        newline.
    """
    indent = "  "
    separator = "-" * 60

    lines: list[str] = [
        "",
        separator,
        f"  saturnday {note.version} is available  [{note.edition}]",
    ]

    if note.summary:
        lines.append(f"  {note.summary}")

    lines.append(separator)

    if note.highlights:
        lines.append("")
        lines.append("  What's new:")
        for item in note.highlights:
            # Wrap long highlight lines at 76 chars, preserving the bullet indent
            wrapped = textwrap.fill(
                item,
                width=76,
                initial_indent=f"{indent}* ",
                subsequent_indent=f"{indent}  ",
            )
            lines.append(wrapped)

    if note.fixes:
        lines.append("")
        lines.append("  Notable fixes:")
        for fix in note.fixes:
            wrapped = textwrap.fill(
                fix,
                width=76,
                initial_indent=f"{indent}* ",
                subsequent_indent=f"{indent}  ",
            )
            lines.append(wrapped)

    lines.append("")
    lines.append(f"  Upgrade: {note.upgrade_command}")
    lines.append(separator)
    lines.append("")

    return "\n".join(lines)
