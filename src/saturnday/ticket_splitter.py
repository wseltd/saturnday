"""Runtime ticket splitting: analyze complex tickets and break them down.

Before coding, the coder is asked to analyze the ticket's complexity.
If it estimates >80 lines of implementation, >2 files, or the goal
contains multiple distinct deliverables, it produces sub-tickets that
are executed sequentially in place of the original.

This follows the Saturnday dossier rules:
- Tickets >80 lines must be split
- Max 2 files per ticket (source + test)
- Each sub-ticket does ONE thing
- Integration tickets must be thin (imports/wiring only)
"""

from __future__ import annotations

import json
import logging
import re
from fnmatch import fnmatch
from pathlib import Path

from saturnday._exceptions import CoderAPIError
from saturnday._types import CoderConfig, TicketScope, TicketSpec
from saturnday.coder_adapter import call_coder, is_cli_backend

logger = logging.getLogger(__name__)

# Tickets with goals shorter than this are never split
_MIN_GOAL_LENGTH_FOR_SPLIT = 50

# Max sub-tickets from a single split
_MAX_SUB_TICKETS = 10

# Scope-consistency guard: file-path tokens to look for in a sub-ticket goal.
# Conservative — matches concrete filesystem-shaped tokens whose extension is
# one the coder realistically writes.  Used to defend against split payloads
# whose ``files`` list contradicts the goal text (e.g. goal says "write
# ``app/__init__.py``" but the returned files list omits it), which otherwise
# causes the runtime to reject the goal-required write with
# ``PatchApplicationError("Path X does not match any allowed glob")``.
_GOAL_PATH_EXTENSIONS = (
    "py", "pyi", "pyx",
    "md", "rst", "txt",
    "yaml", "yml", "toml", "ini", "cfg", "json",
    "sh", "sql",
    "js", "ts", "tsx", "jsx",
    "css", "html",
)
_GOAL_PATH_RE = re.compile(
    r"(?:[\w.\-]+/)*[\w.\-]+\.(?:" + "|".join(_GOAL_PATH_EXTENSIONS) + r")\b"
)


def _extract_goal_paths(goal: str) -> list[str]:
    """Return concrete file-path tokens mentioned in *goal*, in first-seen order.

    Used by the splitter scope-consistency guard.  Limited to tokens that
    look like real filesystem paths (word-chars, dots, dashes, slashes) with
    a known code/config extension.  Prose words, bare filenames without an
    extension, and directory-only references are deliberately NOT matched —
    directory-scoped goals are handled by trusting the splitter's ``files``
    list (see ``_parse_split_response``).
    """
    if not goal:
        return []
    return list(dict.fromkeys(_GOAL_PATH_RE.findall(goal)))


def _matches_any_glob(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """True when *path* matches at least one glob in *globs*."""
    return any(fnmatch(path, g) for g in globs)

_SPLIT_ANALYSIS_PROMPT = """\
You are a senior software architect. Analyze this ticket and decide if it \
should be split into smaller sub-tickets.

SPLITTING RULES — be aggressive, prefer many small tickets over few large ones:
- If the ticket would produce >40 lines of implementation code, SPLIT IT.
- Max 1 source file per sub-ticket (plus its test file).
- Each sub-ticket does ONE thing. If you can describe it with "and", split it.
- Integration tickets (app.py, cli.py) must be thin: imports + wiring only.
- Max 2 endpoints per sub-ticket for FastAPI.
- Large modules: groups of 3-5 functions per sub-ticket.
- One file = one sub-ticket owner. Never split a file across sub-tickets.

TICKET TO ANALYZE:
{goal}

SCOPE: {scope_summary}

Respond with ONLY valid JSON. No markdown, no explanation.

If the ticket is small enough (<=80 lines, <=2 files, one concern), respond:
{{"split": false}}

If it should be split, respond:
{{"split": true, "sub_tickets": [
  {{"id": "a", "goal": "...", "files": ["path1.py", "test_path1.py"]}},
  {{"id": "b", "goal": "...", "files": ["path2.py", "test_path2.py"], "depends_on": ["a"]}}
]}}

Keep sub-ticket goals detailed and explicit. Include exact function signatures \
and return types. Max {max_sub} sub-tickets."""


_LAST_RESORT_SPLIT_PROMPT = """\
You are a senior software architect reviewing a ticket that has failed all \
execution attempts, possibly because the prompt context is too large. Your \
task is to decide if it can be cleanly split into independent sub-tickets.

IMPORTANT — be CONSERVATIVE. Prefer responding with {{"split": false}}.
DO NOT split any of the following:
- Atomic refactors (renaming, moving, type changes across files)
- Database migrations (must land as one atomic unit)
- API contracts (caller and callee must be written together)
- Thin wiring tickets (imports, routing, dependency injection)
- Any ticket where all parts must be present at the same time to compile

ONLY split if the ticket contains truly independent file-level work: each \
sub-ticket can be coded, committed, and tested in isolation without any \
other sub-ticket being present.

TICKET TO ANALYZE:
{goal}

SCOPE: {scope_summary}

Respond with ONLY valid JSON. No markdown, no explanation.

If the ticket cannot be split into fully independent parts, respond:
{{"split": false}}

If it can be split into truly independent file-level sub-tickets, respond:
{{"split": true, "sub_tickets": [
  {{"id": "a", "goal": "...", "files": ["path1.py"]}},
  {{"id": "b", "goal": "...", "files": ["path2.py"]}}
]}}

Max {max_sub} sub-tickets. Each sub-ticket must be completable with zero \
knowledge of the other sub-tickets."""


def should_split(ticket: TicketSpec) -> bool:
    """Heuristic check: does this ticket look complex enough to split?

    Args:
        ticket: The ticket to evaluate.

    Returns:
        True if the ticket should be analyzed for splitting.
    """
    goal = ticket.goal

    # Short goals are fine
    if len(goal) < _MIN_GOAL_LENGTH_FOR_SPLIT:
        return False

    # Multiple files mentioned in the goal
    file_mentions = re.findall(r'(?:File|FILE|src/|tests/)\S+\.py', goal)
    if len(file_mentions) > 2:
        return True

    # Goal contains "and" joining distinct deliverables
    if re.search(r'\band\b.*\band\b', goal, re.IGNORECASE):
        return True

    # Multiple "must" or "MUST" clauses suggest complexity
    must_count = len(re.findall(r'\bMUST\b|\bmust\b', goal))
    if must_count > 4:
        return True

    # Large scope budget suggests complex ticket
    if ticket.scope.max_files_changed > 3:
        return True

    return False


def analyze_and_split(
    ticket: TicketSpec,
    config: CoderConfig,
    repo_path: Path,
    plan_notes: str,
) -> list[TicketSpec]:
    """Ask the coder to analyze a ticket and split if needed.

    Args:
        ticket: The original ticket.
        config: Coder backend configuration.
        repo_path: Repository path.
        plan_notes: Plan notes for context.

    Returns:
        List of sub-tickets if split, or [ticket] unchanged if not.
    """
    if not should_split(ticket):
        logger.debug("Ticket %s: no split needed (simple)", ticket.ticket_id)
        return [ticket]

    logger.info("Ticket %s: analyzing for split (goal=%d chars)", ticket.ticket_id, len(ticket.goal))

    scope_summary = f"allowed={list(ticket.scope.allowed_globs)}, max_files={ticket.scope.max_files_changed}"

    prompt = _SPLIT_ANALYSIS_PROMPT.format(
        goal=ticket.goal,
        scope_summary=scope_summary,
        max_sub=_MAX_SUB_TICKETS,
    )

    messages = [
        {"role": "system", "content": "You are a software architect. Respond ONLY with valid JSON."},
        {"role": "user", "content": prompt},
    ]

    try:
        if is_cli_backend(config):
            # For CLI backends, use a simple prompt that just returns JSON
            from saturnday.coder_adapter import _call_claude_cli, _call_codex_cli, _messages_to_text
            text = _messages_to_text(messages)
            if config.backend == "claude-cli":
                response = _call_claude_cli(config, text, repo_path)
            else:
                response = _call_codex_cli(config, text, repo_path)
        else:
            response = call_coder(config, messages, repo_path)
    except CoderAPIError as exc:
        logger.warning("Split analysis failed for %s: %s — running unsplit", ticket.ticket_id, exc)
        return [ticket]

    # Parse JSON from response
    sub_tickets = _parse_split_response(response, ticket)
    if sub_tickets is None:
        logger.info("Ticket %s: coder says no split needed", ticket.ticket_id)
        return [ticket]

    logger.info(
        "Ticket %s: split into %d sub-tickets: %s",
        ticket.ticket_id,
        len(sub_tickets),
        [s.ticket_id for s in sub_tickets],
    )
    return sub_tickets


def _parse_split_response(
    response: str,
    original: TicketSpec,
) -> list[TicketSpec] | None:
    """Parse the split analysis JSON response.

    Returns:
        List of sub-tickets, or None if no split.
    """
    # Extract JSON from response (might have surrounding text)
    json_match = re.search(r'\{.*\}', response, re.DOTALL)
    if not json_match:
        logger.warning("No JSON found in split response")
        return None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in split response")
        return None

    if not data.get("split", False):
        return None

    raw_subs = data.get("sub_tickets", [])
    if not raw_subs or len(raw_subs) < 2:
        return None
    if len(raw_subs) > _MAX_SUB_TICKETS:
        raw_subs = raw_subs[:_MAX_SUB_TICKETS]

    sub_tickets: list[TicketSpec] = []
    completed_sub_ids: list[str] = []

    parent_globs = original.scope.allowed_globs
    parent_is_wildcard = (not parent_globs) or parent_globs == ("**",)

    for raw in raw_subs:
        sub_id = f"{original.ticket_id}.{raw.get('id', str(len(sub_tickets) + 1))}"
        goal = raw.get("goal", "")
        if not goal:
            continue

        # --- Scope-consistency guard -----------------------------------
        # The splitter's contract is: if the sub-ticket goal names file
        # paths, ``allowed_globs`` must not exclude them.  A weak backend
        # sometimes returns a ``files`` list that omits paths named by its
        # own goal, which would otherwise cause the runtime's scope check
        # (see ``patch_extractor._validate_path``) to reject the
        # goal-required write with "does not match any allowed glob".
        # We defend at the contract boundary: normalise, detect
        # inconsistency, augment with goal paths, reject escapes, and
        # fall back to parent scope only when we have nothing safer.
        raw_files = raw.get("files", [])
        clean_files: list[str] = []
        if isinstance(raw_files, list):
            _seen: set[str] = set()
            for f in raw_files:
                if isinstance(f, str):
                    stripped = f.strip()
                    if stripped and stripped not in _seen:
                        clean_files.append(stripped)
                        _seen.add(stripped)

        goal_paths = _extract_goal_paths(goal)

        # Goal paths missing from clean_files (glob-aware: a file entry
        # like ``src/app/*.py`` already covers a goal mention of
        # ``src/app/core.py``).
        missing_goal_paths: list[str] = []
        for p in goal_paths:
            if clean_files and _matches_any_glob(p, clean_files):
                continue
            missing_goal_paths.append(p)

        # Candidate scope: union of clean_files + missing goal paths.
        candidate: list[str] = list(clean_files)
        for p in missing_goal_paths:
            if p not in candidate:
                candidate.append(p)

        # Parent-scope escape check.  If parent scope is anything tighter
        # than wildcard, every candidate path must match the parent's
        # allowed_globs.  An escaping candidate means the split would
        # silently widen past the operator-approved ticket scope — reject
        # the whole split so the runner falls back to the parent ticket.
        if not parent_is_wildcard:
            escaping = [p for p in candidate if not _matches_any_glob(p, parent_globs)]
            if escaping:
                logger.warning(
                    "splitter guard: rejecting split of %s — sub-ticket %s "
                    "would require paths %r outside parent scope %s",
                    original.ticket_id, sub_id, escaping, list(parent_globs),
                )
                return None

        fallback_reason = ""
        if candidate:
            allowed_globs = tuple(candidate)
            max_files_changed = len(allowed_globs) + 1
            if missing_goal_paths:
                # Payload was salvageable but inconsistent — record it.
                fallback_reason = (
                    f"goal_paths_missing_from_files={missing_goal_paths}"
                )
                logger.warning(
                    "splitter guard: sub-ticket %s goal names paths %r "
                    "not covered by returned files %s — augmented "
                    "allowed_globs to %s",
                    sub_id, missing_goal_paths, clean_files, list(allowed_globs),
                )
        else:
            # Empty / malformed files AND no extractable goal paths —
            # trust the parent's scope rather than produce a broken
            # sub-ticket with no writable targets.
            allowed_globs = parent_globs
            max_files_changed = original.scope.max_files_changed
            fallback_reason = "empty_or_malformed_files_and_no_goal_paths"
            logger.warning(
                "splitter guard: sub-ticket %s has no usable scope "
                "(raw files=%r, goal paths=%r) — falling back to parent "
                "scope %s",
                sub_id, raw_files, goal_paths, list(parent_globs),
            )

        # Sub-ticket dependencies: original's deps + any prior sub-tickets it depends on
        raw_deps = raw.get("depends_on", [])
        sub_deps = list(original.dependencies)
        for dep_id in raw_deps:
            full_dep = f"{original.ticket_id}.{dep_id}"
            if full_dep in completed_sub_ids:
                sub_deps.append(full_dep)

        scope = TicketScope(
            allowed_globs=allowed_globs,
            forbidden_globs=original.scope.forbidden_globs,
            max_files_changed=max_files_changed,
            max_total_diff_lines=original.scope.max_total_diff_lines,
            max_per_file_diff_lines=original.scope.max_per_file_diff_lines,
        )

        sub_goal = goal
        if fallback_reason:
            # Surface the fallback in evidence that travels with the
            # sub-ticket (coder prompts, run summary via goal text).
            sub_goal = (
                f"{goal}\n\n[splitter-guard] scope widened at parse time: "
                f"{fallback_reason}"
            )

        sub_tickets.append(TicketSpec(
            ticket_id=sub_id,
            goal=sub_goal,
            scope=scope,
            verify_cmd=original.verify_cmd,
            acceptance_criteria=original.acceptance_criteria,
            dependencies=tuple(sub_deps),
        ))
        completed_sub_ids.append(sub_id)

    if len(sub_tickets) < 2:
        return None

    return sub_tickets


def analyze_and_split_last_resort(
    ticket: TicketSpec,
    config: CoderConfig,
    repo_path: Path,
    plan_notes: str,
) -> list[TicketSpec]:
    """Conservative last-resort split for oversize tickets that exhausted retries.

    Unlike :func:`analyze_and_split`, this function:

    - Skips :func:`should_split` heuristics — always queries the coder.
    - Uses :data:`_LAST_RESORT_SPLIT_PROMPT`, which prefers ``{split: false}``
      and rejects atomic, migration, API-contract, and wiring tickets.
    - Falls back to ``[ticket]`` on any error or when the coder declines.

    Args:
        ticket: The original ticket that exhausted all retries.
        config: Coder backend configuration.
        repo_path: Repository path.
        plan_notes: Plan notes for context.

    Returns:
        List of sub-tickets if a clean, independent split was found, or
        ``[ticket]`` unchanged if the coder declines or the call fails.
    """
    logger.info(
        "Ticket %s: attempting last-resort split (goal=%d chars)",
        ticket.ticket_id, len(ticket.goal),
    )

    scope_summary = (
        f"allowed={list(ticket.scope.allowed_globs)}, "
        f"max_files={ticket.scope.max_files_changed}"
    )

    prompt = _LAST_RESORT_SPLIT_PROMPT.format(
        goal=ticket.goal,
        scope_summary=scope_summary,
        max_sub=_MAX_SUB_TICKETS,
    )

    messages = [
        {"role": "system", "content": "You are a software architect. Respond ONLY with valid JSON."},
        {"role": "user", "content": prompt},
    ]

    try:
        if is_cli_backend(config):
            from saturnday.coder_adapter import _call_claude_cli, _call_codex_cli, _messages_to_text  # noqa: PLC0415
            text = _messages_to_text(messages)
            if config.backend == "claude-cli":
                response = _call_claude_cli(config, text, repo_path)
            else:
                response = _call_codex_cli(config, text, repo_path)
        else:
            response = call_coder(config, messages, repo_path)
    except CoderAPIError as exc:
        logger.warning(
            "Last-resort split analysis failed for %s: %s — keeping unsplit",
            ticket.ticket_id, exc,
        )
        return [ticket]

    sub_tickets = _parse_split_response(response, ticket)
    if sub_tickets is None:
        logger.info(
            "Ticket %s: last-resort split — coder declined to split",
            ticket.ticket_id,
        )
        return [ticket]

    logger.info(
        "Ticket %s: last-resort split into %d sub-tickets: %s",
        ticket.ticket_id,
        len(sub_tickets),
        [s.ticket_id for s in sub_tickets],
    )
    return sub_tickets
