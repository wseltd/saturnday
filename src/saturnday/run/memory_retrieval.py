"""Memory retrieval for the Saturnday governed memory system (v1.1.01).

Phase 4 of the memory system.  Provides hard filtering (T014), ranking and
injection formatting (T015), and staleness validation (T016) for MemoryItems
before they are injected into the coder context capsule.

Design:
- All functions are deterministic — no LLM calls.
- All functions fail gracefully; exceptions are caught at the call site.
- Staleness cleanup is called once per run from ``run_plan()``.
- Ranking uses simple integer scoring: recency + file-overlap + risk-tag-overlap
  + severity boost.  No embeddings, no ML.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from saturnday.run.lessons import MemoryItem, load_scopes, mark_stale

if TYPE_CHECKING:
    from saturnday._types import TicketSpec

logger = logging.getLogger(__name__)

__all__ = [
    "filter_relevant_items",
    "rank_and_select",
    "format_lessons_for_injection",
    "validate_before_injection",
    "run_staleness_cleanup",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SEVERITY_SCORE: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
}

_STALE_STATUSES = frozenset({"stale", "superseded", "retired"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_past(iso_str: str | None) -> bool:
    """Return True when *iso_str* is a non-empty ISO datetime in the past."""
    if not iso_str:
        return False
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _infer_risk_tags_from_ticket(ticket: "TicketSpec") -> list[str]:
    """Infer likely risk tags from the ticket goal and allowed_globs."""
    tags: list[str] = []
    goal_lower = ticket.goal.lower() if ticket.goal else ""
    globs = list(ticket.scope.allowed_globs) if ticket.scope else []

    if any(kw in goal_lower for kw in ("shell", "subprocess", "exec")):
        tags.append("shell_exec")
    if any(kw in goal_lower for kw in ("security", "secret", "credential", "auth", "token")):
        tags.append("security")
    if any(kw in goal_lower for kw in ("delete", "remove")):
        tags.append("deletion")
    if any(kw in goal_lower for kw in ("test", "pytest", "unittest")):
        tags.append("test_modification")
    if any(
        fnmatch.fnmatch(g, "*.yaml") or fnmatch.fnmatch(g, "*.yml")
        or fnmatch.fnmatch(g, "*.toml") or fnmatch.fnmatch(g, "Dockerfile")
        for g in globs
    ):
        tags.append("config_change")
    if any(fnmatch.fnmatch(g, "*.lock") for g in globs):
        tags.append("dependency_change")
    return tags


def _infer_ticket_class(goal: str) -> str:
    """Coarse ticket class from goal text — mirrors context_capsule logic."""
    lower = goal.lower() if goal else ""
    if any(w in lower for w in ("repair", "fix", "remediate", "remed")):
        return "repair"
    if any(w in lower for w in ("refactor", "migrate", "move", "rename")):
        return "remediation"
    return "generation"


def _files_overlap(item_files: list[str], scope_globs: list[str]) -> bool:
    """Return True when any item file matches any scope glob."""
    for f in item_files:
        for pattern in scope_globs:
            if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(Path(f).name, pattern):
                return True
    return False


def _tags_overlap(item_tags: list[str], ticket_tags: list[str]) -> bool:
    """Return True when there is at least one shared risk tag."""
    return bool(set(item_tags) & set(ticket_tags))


def _failure_mode_matches(item_failure_mode: str | None, ticket_class: str) -> bool:
    """Simple substring match between item failure_mode and ticket_class."""
    if not item_failure_mode:
        return False
    return ticket_class.lower() in item_failure_mode.lower()


def _days_since(created_at: str) -> int:
    """Return whole days since *created_at* ISO string (0 on parse error)."""
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        return 999


def _recency_score(created_at: str) -> int:
    """Return 3 / 2 / 1 / 0 based on how recently the item was created."""
    days = _days_since(created_at)
    if days <= 7:
        return 3
    if days <= 30:
        return 2
    if days <= 90:
        return 1
    return 0


# ---------------------------------------------------------------------------
# T014: Hard filtering
# ---------------------------------------------------------------------------


def filter_relevant_items(
    conn: sqlite3.Connection,
    ticket: "TicketSpec",
    changed_files: list[str] | None = None,
) -> list[MemoryItem]:
    """Filter memory items to those relevant to the current ticket.

    Hard filters applied in order:
    1. status = 'active' (skip stale, retired, superseded).
    2. NOT past stale_after date.
    3. Overlapping file paths: ``files_touched`` overlaps with
       ``ticket.scope.allowed_globs`` or ``changed_files``.
    4. Same risk tags: ``risk_tags`` overlaps with ticket's inferred risk tags.
    5. Same failure mode: simple substring match on ``failure_mode`` vs ticket
       class.
    6. Same ticket class: match on ``ticket_class`` field.

    Any item matching at least one of criteria 3–6 is included (OR logic).
    Items failing criteria 1 or 2 are always excluded.

    If the full filter returns zero items, falls back to: active + not stale,
    ordered by ``created_at DESC``, top 10.

    Args:
        conn: Open connection from :func:`lessons.init_memory_db`.
        ticket: The ticket being executed.
        changed_files: Additional file paths from prior execution history.

    Returns:
        List of :class:`MemoryItem` objects after hard filtering.
    """
    _select_full = (
        "SELECT item_id, memory_type, created_at, created_from_run_id, "
        "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
        "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
        "root_cause, corrective_rule_text, evidence_refs, residual_required, "
        "last_validated_at, stale_after, status, superseded_by, "
        "wrong_pattern, correct_pattern, why_wrong, why_correct "
        "FROM memory_items WHERE status = 'active' "
        "ORDER BY created_at DESC"
    )
    _select_legacy = (
        "SELECT item_id, memory_type, created_at, created_from_run_id, "
        "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
        "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
        "root_cause, corrective_rule_text, evidence_refs, residual_required, "
        "last_validated_at, stale_after, status, superseded_by "
        "FROM memory_items WHERE status = 'active' "
        "ORDER BY created_at DESC"
    )
    try:
        try:
            rows = conn.execute(_select_full).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(_select_legacy).fetchall()
    except Exception as exc:
        logger.warning("filter_relevant_items: DB query failed — %s", exc)
        return []

    from saturnday.run.lessons import _row_to_memory_item  # internal helper

    now = _now_iso()
    all_files: list[str] = list(ticket.scope.allowed_globs) if ticket.scope else ["**"]
    if changed_files:
        all_files = all_files + [f for f in changed_files if f not in all_files]

    ticket_tags = _infer_risk_tags_from_ticket(ticket)
    ticket_class = _infer_ticket_class(ticket.goal)

    candidates: list[MemoryItem] = []
    stale_ids: list[str] = []

    for row in rows:
        item = _row_to_memory_item(row)

        # Hard exclude: past stale_after — mark stale then skip
        if _is_past(item.stale_after):
            stale_ids.append(item.item_id)
            continue

        # Relevance criteria (OR logic — any one is enough)
        relevant = (
            _files_overlap(item.files_touched, all_files)
            or _tags_overlap(item.risk_tags, ticket_tags)
            or _failure_mode_matches(item.failure_mode, ticket_class)
            or (
                item.ticket_class is not None
                and item.ticket_class.lower() == ticket_class.lower()
            )
        )

        # EXT5: scope-aware relevance boost — check memory_scopes table
        if not relevant:
            try:
                scopes = load_scopes(conn, item.item_id, "memory_item")
                for scope in scopes:
                    st = scope["scope_type"]
                    sv = scope["scope_value"]
                    if st == "path" and _files_overlap([sv], all_files):
                        relevant = True
                        break
                    if st == "file_type" and any(
                        f.endswith(sv) for f in all_files
                    ):
                        relevant = True
                        break
                    if st == "risk_tag" and sv in ticket_tags:
                        relevant = True
                        break
                    if st == "symbol" and sv.lower() in ticket.goal.lower():
                        relevant = True
                        break
            except Exception:
                pass  # scopes table may not exist on older DBs

        if relevant:
            candidates.append(item)

    # Side-effect: mark expired items stale in the DB
    for sid in stale_ids:
        try:
            mark_stale(conn, sid)
        except Exception:
            pass

    if candidates:
        return candidates

    # Fallback: active + not-past-stale, top 10 by recency
    fallback: list[MemoryItem] = []
    _fb_full = (
        "SELECT item_id, memory_type, created_at, created_from_run_id, "
        "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
        "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
        "root_cause, corrective_rule_text, evidence_refs, residual_required, "
        "last_validated_at, stale_after, status, superseded_by, "
        "wrong_pattern, correct_pattern, why_wrong, why_correct "
        "FROM memory_items WHERE status = 'active' "
        "ORDER BY created_at DESC LIMIT 10"
    )
    _fb_legacy = (
        "SELECT item_id, memory_type, created_at, created_from_run_id, "
        "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
        "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
        "root_cause, corrective_rule_text, evidence_refs, residual_required, "
        "last_validated_at, stale_after, status, superseded_by "
        "FROM memory_items WHERE status = 'active' "
        "ORDER BY created_at DESC LIMIT 10"
    )
    try:
        try:
            fb_rows = conn.execute(_fb_full).fetchall()
        except sqlite3.OperationalError:
            fb_rows = conn.execute(_fb_legacy).fetchall()
        for row in fb_rows:
            item = _row_to_memory_item(row)
            if not _is_past(item.stale_after):
                fallback.append(item)
    except Exception as exc:
        logger.warning("filter_relevant_items: fallback query failed — %s", exc)

    return fallback


# ---------------------------------------------------------------------------
# T015: Ranking and injection
# ---------------------------------------------------------------------------


def rank_and_select(
    items: list[MemoryItem],
    ticket: "TicketSpec",
    limit: int = 5,
    conn: "sqlite3.Connection | None" = None,
) -> list[MemoryItem]:
    """Rank filtered memory items and return the top ``limit`` items.

    Scoring signals:
    - Recency: newer items score higher (3 / 2 / 1 / 0 by days-since-creation).
    - File overlap: count of matching files between item and ticket scope.
    - Risk tag overlap: count of shared tags.
    - Severity: critical=4, high=3, medium=2, low=1.
    - EXT5 scope bonus: +2 for items with matching memory_scopes entries.

    Args:
        items: Pre-filtered :class:`MemoryItem` list from
            :func:`filter_relevant_items`.
        ticket: The ticket being executed (used to score file/tag overlap).
        limit: Maximum number of items to return.
        conn: Optional open DB connection for EXT5 scope bonus scoring.

    Returns:
        Top ``limit`` items sorted by score descending.
    """
    if not items:
        return []

    scope_globs = list(ticket.scope.allowed_globs) if ticket.scope else ["**"]
    ticket_tags = _infer_risk_tags_from_ticket(ticket)

    def _score(item: MemoryItem) -> int:
        score = _recency_score(item.created_at)
        # File overlap count (capped at 3 to avoid outsized influence)
        file_matches = sum(
            1 for f in item.files_touched
            if any(
                fnmatch.fnmatch(f, p) or fnmatch.fnmatch(Path(f).name, p)
                for p in scope_globs
            )
        )
        score += min(file_matches, 3)
        # Risk tag overlap count (capped at 3)
        tag_matches = len(set(item.risk_tags) & set(ticket_tags))
        score += min(tag_matches, 3)
        # Severity boost
        score += _SEVERITY_SCORE.get(item.severity, 1)
        # EXT5: +2 bonus for items with matching scope entries (more precise)
        try:
            scopes = load_scopes(conn, item.item_id, "memory_item")
            if scopes:
                score += 2
        except Exception:
            pass
        return score

    scored = sorted(items, key=_score, reverse=True)
    return scored[:limit]


def format_lessons_for_injection(items: list[MemoryItem]) -> str:
    """Convert ranked memory items to a compact injection string.

    Format per item (2–5 lines):
        N. [risk_tag] file_path / failure_mode
           Rule: corrective_rule_text
           Wrong: wrong_pattern (when present)
           Instead: correct_pattern (when present)
           Because: why_wrong (when present)

    Total output capped at 1500 chars.

    Args:
        items: Ranked :class:`MemoryItem` list from :func:`rank_and_select`.

    Returns:
        Formatted string ready for capsule injection, or empty string.
    """
    if not items:
        return ""

    lines: list[str] = ["MEMORY LESSONS:"]
    for i, item in enumerate(items, start=1):
        tag = item.risk_tags[0] if item.risk_tags else "general"
        file_hint = item.files_touched[0] if item.files_touched else "—"
        failure = item.failure_mode or item.outcome or "unknown"
        rule = (item.corrective_rule_text or "").strip()

        entry = f"{i}. [{tag}] {file_hint} / {failure}"
        if rule:
            entry += f"\n   Rule: {rule[:120]}"

        # Extension 3: render contrastive fields when both are present
        if item.wrong_pattern and item.correct_pattern:
            entry += f"\n   Wrong: {item.wrong_pattern[:80]}"
            entry += f"\n   Instead: {item.correct_pattern[:80]}"
            if item.why_wrong:
                entry += f"\n   Because: {item.why_wrong[:80]}"

        lines.append(entry)

    result = "\n".join(lines)
    return result[:1500]


# ---------------------------------------------------------------------------
# T016: Validation and staleness
# ---------------------------------------------------------------------------


def validate_before_injection(
    items: list[MemoryItem],
    repo_path: Path | None,
    conn: sqlite3.Connection | None = None,
) -> list[MemoryItem]:
    """Validate memory items before injecting them into the coder prompt.

    Checks per item:
    1. If ``stale_after`` has passed, mark stale and skip.
    2. If ``files_touched`` primary file is gone from the repo, mark stale and
       skip.  Skipped when *repo_path* is ``None`` (no repo context available).
    3. If a later successful fix exists for the same
       ``files_touched`` + ``failure_mode`` combo (outcome=PASS on overlapping
       files), skip without marking stale.

    Args:
        items: Memory items to validate (typically from :func:`rank_and_select`).
        repo_path: Root of the target repository for file existence checks.
            Pass ``None`` to skip file-existence validation (e.g. in tests
            without a real repo on disk).
        conn: Optional open DB connection.  Required for supersession check
            and for marking items stale in the DB.

    Returns:
        List of validated items with stale / superseded items removed.
    """
    if not items:
        return []

    valid: list[MemoryItem] = []

    for item in items:
        # Check 1: stale_after passed
        if _is_past(item.stale_after):
            if conn is not None:
                try:
                    mark_stale(conn, item.item_id)
                except Exception:
                    pass
            continue

        # Check 2: primary file no longer exists in repo (only when repo_path given)
        if repo_path is not None and item.files_touched:
            primary_path = Path(item.files_touched[0])
            if not primary_path.is_absolute():
                primary_path = repo_path / item.files_touched[0]
            if not primary_path.exists():
                if conn is not None:
                    try:
                        mark_stale(conn, item.item_id)
                    except Exception:
                        pass
                continue

        # Check 3: superseded by a later PASS on the same files/failure_mode
        if conn is not None and item.files_touched and item.failure_mode:
            try:
                superseded = _check_superseded(conn, item)
                if superseded:
                    continue  # skip without marking stale
            except Exception:
                pass  # defensive — if check fails, keep the item

        valid.append(item)

    return valid


def _check_superseded(conn: sqlite3.Connection, item: MemoryItem) -> bool:
    """Return True when a later PASS item exists for the same file+failure_mode."""
    if not item.files_touched or not item.failure_mode:
        return False
    # Look for items with outcome=PASS, same failure_mode, created after this item
    try:
        rows = conn.execute(
            "SELECT files_touched FROM memory_items "
            "WHERE outcome = 'PASS' AND failure_mode = ? AND created_at > ? "
            "AND status = 'active' LIMIT 20",
            (item.failure_mode, item.created_at),
        ).fetchall()
    except Exception:
        return False

    item_files = set(item.files_touched)
    for (files_raw,) in rows:
        try:
            pass_files = set(json.loads(files_raw) if files_raw else [])
        except (json.JSONDecodeError, TypeError):
            pass_files = set()
        if item_files & pass_files:  # overlap
            return True
    return False


def run_staleness_cleanup(
    conn: sqlite3.Connection,
    repo_path: Path,
) -> int:
    """Scan all active memory items and mark stale those that are expired or
    whose primary file no longer exists.

    This should be called once per run, at the START of ``run_plan()``, before
    ticket execution begins.

    Args:
        conn: Open connection from :func:`lessons.init_memory_db`.
        repo_path: Root of the target repository for file existence checks.

    Returns:
        Count of items marked stale during this call.
    """
    try:
        rows = conn.execute(
            "SELECT item_id, stale_after, files_touched "
            "FROM memory_items WHERE status = 'active'"
        ).fetchall()
    except Exception as exc:
        logger.warning("run_staleness_cleanup: query failed — %s", exc)
        return 0

    marked = 0
    for item_id, stale_after, files_raw in rows:
        stale = False

        # Expired stale_after
        if _is_past(stale_after):
            stale = True

        # Primary file missing from repo
        if not stale and files_raw:
            try:
                files: list[str] = json.loads(files_raw)
                if files:
                    primary = repo_path / files[0]
                    if not primary.is_absolute():
                        primary = repo_path / files[0]
                    if not primary.exists():
                        stale = True
            except (json.JSONDecodeError, TypeError, OSError):
                pass

        if stale:
            try:
                mark_stale(conn, item_id)
                marked += 1
                logger.debug("Staleness cleanup: marked stale item_id=%s", item_id)
            except Exception:
                pass

    if marked:
        logger.info("Staleness cleanup: %d item(s) marked stale", marked)
    return marked
