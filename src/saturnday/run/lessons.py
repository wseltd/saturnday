"""Lessons database — learn from past failures.

SQLite-backed storage at ~/.saturnday/lessons.db. Records failure patterns
from ticket runs so future runs can avoid repeating the same mistakes.

Lessons are advisory only: they are injected into coder prompts as context,
never as hard gates. Confidence increments when the same rule fires again
for the same project.

Phase 1 extensions (v1.1.01):
- ``memory_items`` table: typed structured memory records.
- ``rules`` table: machine-readable enforced rules derived from lessons.
- New functions for storing, loading, and promoting typed memory.
- ``generate_candidate_rule``: produce a candidate Rule from a severe MemoryItem.
- ``generate_lessons_markdown`` / ``write_lessons_file``: render structured DB
  output to ``.saturnday/lessons.md``.

Extension 4 (Playbooks):
- ``Playbook`` dataclass + ``playbooks`` table.
- CRUD: ``store_playbook``, ``load_active_playbooks``, ``deprecate_playbook``.
- Matching: ``match_playbooks`` — trigger-condition-based playbook selection.

Extension 5 (Richer Scoping):
- ``memory_scopes`` table with indexes.
- CRUD: ``add_scope``, ``load_scopes``, ``remove_scopes``, ``bulk_add_scopes``.
- Auto-scope on ``store_memory_item``: path scopes from files_touched,
  risk_tag scopes from risk_tags.

All new tables use ``CREATE TABLE IF NOT EXISTS`` for idempotent migration.
Existing ``lessons`` table and all original functions are unchanged.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from saturnday._types import TicketSpec

logger = logging.getLogger(__name__)

__all__ = [
    # Original API
    "Lesson",
    "init_db",
    "store_lesson",
    "load_lessons",
    "format_lessons_for_prompt",
    # Phase 1
    "MemoryItem",
    "Rule",
    "init_memory_db",
    "store_memory_item",
    "store_rule",
    "load_active_items",
    "load_active_rules",
    "mark_stale",
    "increment_rule_trigger",
    "generate_candidate_rule",
    "generate_lessons_markdown",
    "write_lessons_file",
    # Phase 5
    "promote_rules",
    "promote_to_enforced",
    # Extension 1: Memory Governance
    "find_duplicates",
    "merge_duplicates",
    "run_dedup_pass",
    "detect_contradictions",
    "log_contradictions",
    "mark_reviewed",
    "mark_rule_reviewed",
    "get_items_needing_review",
    # Extension 2: Rule false-positive handling
    "record_rule_override",
    "record_rule_confirmation",
    "add_rule_exception",
    "add_rule_counterexample",
    "get_low_confidence_rules",
    # Extension 3: Contrastive lessons
    "store_contrastive_lesson",
    "load_contrastive_items",
    # Extension 4: Playbooks
    "Playbook",
    "store_playbook",
    "load_active_playbooks",
    "deprecate_playbook",
    "match_playbooks",
    # Extension 5: Richer Scoping
    "add_scope",
    "load_scopes",
    "remove_scopes",
    "bulk_add_scopes",
    # T025: False-positive tracking for spec verification layers
    "log_check_result",
    "get_false_positive_rate",
    "get_noisy_rules",
]

_DEFAULT_DB_PATH = Path.home() / ".saturnday" / "lessons.db"


def _default_db_path() -> Path:
    """Resolve the lessons.db path at call time via the state-dir resolver.

    Respects ``SATURNDAY_STATE_DIR`` and ``XDG_STATE_HOME`` with
    ``~/.saturnday`` as the backward-compatible fallback.
    """
    from saturnday.paths import state_dir
    return state_dir() / "lessons.db"

# ---------------------------------------------------------------------------
# Original dataclass — unchanged
# ---------------------------------------------------------------------------


@dataclass
class Lesson:
    """A single lesson recorded from a failed ticket run.

    Attributes:
        project_id: Identifier for the project (e.g. repo name or plan project_id).
        ticket_id: The ticket that produced this failure.
        failure_type: Category of failure — one of: governance_fail,
            post_check_fail, coder_error, contract_fail.
        rule: The specific check or pattern that failed (stable identifier,
            used for deduplication across runs).
        description: Human-readable description of the failure.
        confidence: Number of times this rule has fired for the project.
            Starts at 1, incremented on each recurrence.
    """

    project_id: str
    ticket_id: str
    failure_type: str  # governance_fail | post_check_fail | coder_error | contract_fail
    rule: str  # the specific check/pattern that failed
    description: str
    confidence: int = 1  # incremented on repeated failures


# ---------------------------------------------------------------------------
# Phase 1 dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MemoryItem:
    """A typed structured memory record persisted in the ``memory_items`` table.

    All JSON-array fields are stored as TEXT in SQLite and serialised /
    deserialised automatically by :func:`store_memory_item` and
    :func:`load_active_items`.
    """

    item_id: str
    memory_type: str  # 'lesson' | 'rule' | 'invariant' | 'exception'
    created_at: str
    # Optional provenance
    created_from_run_id: Optional[str] = None
    ticket_id: Optional[str] = None
    ticket_goal: Optional[str] = None
    ticket_class: Optional[str] = None
    outcome: Optional[str] = None  # FAIL | CODED_UNGOVERNED | PASS
    failure_mode: Optional[str] = None
    severity: str = "medium"  # low | medium | high | critical
    # JSON arrays stored as lists
    finding_ids: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    symbols_touched: list[str] = field(default_factory=list)
    risk_tags: list[str] = field(default_factory=list)
    # Analysis fields
    root_cause: Optional[str] = None
    corrective_rule_text: Optional[str] = None
    evidence_refs: list[str] = field(default_factory=list)
    residual_required: int = 0
    last_validated_at: Optional[str] = None
    stale_after: Optional[str] = None
    status: str = "active"
    superseded_by: Optional[str] = None
    # Extension 3: Contrastive lesson fields (EXT3-T02)
    wrong_pattern: Optional[str] = None
    correct_pattern: Optional[str] = None
    why_wrong: Optional[str] = None
    why_correct: Optional[str] = None


@dataclass
class Rule:
    """A machine-readable enforced rule derived from recurring failures.

    Stored in the ``rules`` table. JSON-array / JSON-object fields are
    serialised as TEXT by the persistence layer.
    """

    rule_id: str
    created_at: str
    human_rule: str
    # Optional provenance / classification
    memory_type: str = "rule"
    source: Optional[str] = None
    source_run_id: Optional[str] = None
    severity: str = "medium"
    trigger_conditions: dict = field(default_factory=dict)  # JSON object
    forbidden_patterns: list[str] = field(default_factory=list)
    required_patterns: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    status: str = "candidate"  # candidate | active | enforced | retired
    trigger_count: int = 0
    # Extension 2: false-positive tracking (EXT2-T01 / EXT2-T02)
    exceptions: list[str] = field(default_factory=list)
    counterexamples: list[str] = field(default_factory=list)
    override_count: int = 0
    last_override_reason: Optional[str] = None
    confidence: float = 0.8
    last_confirmed_valid: Optional[str] = None


@dataclass
class Playbook:
    """A reusable execution playbook for common ticket patterns.

    Stored in the ``playbooks`` table.  Matched to tickets at run time by
    ``match_playbooks`` and injected into the coder context capsule as
    advisory guidance.

    Attributes:
        playbook_id: Unique identifier (stable, caller-controlled).
        name: Short human-readable name.
        description: One-sentence summary of what the playbook covers.
        steps: Ordered list of step objects (dicts with at least an
            ``"instruction"`` key).
        trigger_conditions: Dict with optional keys ``file_patterns``,
            ``risk_tags``, ``ticket_classes`` — matching is OR logic.
        applicable_file_patterns: Additional glob patterns (informational).
        risk_level: ``low | medium | high | critical``.
        created_at: ISO UTC timestamp.
        status: ``active | deprecated``.
        version: Integer version counter (incremented on update).
    """

    playbook_id: str
    name: str
    description: str
    steps: list[dict] = field(default_factory=list)
    trigger_conditions: dict = field(default_factory=dict)
    applicable_file_patterns: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    created_at: str = ""
    status: str = "active"
    version: int = 1


# ---------------------------------------------------------------------------
# Original DB initialisation — unchanged
# ---------------------------------------------------------------------------


def init_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Create or open the lessons database.

    Creates the database file and parent directories if they do not exist.
    Idempotent — safe to call on an already-initialised database.

    Concurrency: the database is opened in WAL mode with a 30-second busy
    timeout.  This lets multiple Saturnday processes (e.g. concurrent
    ``saturnday run`` invocations against different target repos that both
    share ``~/.saturnday/lessons.db``) coexist safely — readers never
    block writers, and a writer waiting on a contended lock retries for
    up to 30 seconds before surfacing a ``database is locked`` error.
    Callers that observe a hard lock failure should treat it as
    non-fatal and log a lesson-write gap rather than aborting the run.

    Args:
        db_path: Path to the SQLite database file.  When ``None`` (the
            default), resolves via :func:`saturnday.paths.state_dir` so
            ``SATURNDAY_STATE_DIR`` and ``XDG_STATE_HOME`` overrides
            take effect.

    Returns:
        Open connection to the lessons database.
    """
    if db_path is None:
        db_path = _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 30-second busy timeout: covers realistic ticket-write contention in
    # concurrent runs without making fatal lock errors invisible.
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    # WAL keeps readers non-blocking and is durable across crashes.  Best
    # effort — if the filesystem doesn't support WAL (rare), fall through
    # to the default rollback journal.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:  # pragma: no cover — filesystem edge case
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            ticket_id TEXT NOT NULL,
            failure_type TEXT NOT NULL,
            rule TEXT NOT NULL,
            description TEXT NOT NULL,
            confidence INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lessons_rule ON lessons(rule)"
    )
    conn.commit()
    logger.debug("Lessons DB opened at %s", db_path)
    return conn


# ---------------------------------------------------------------------------
# Phase 1: init_memory_db — extends an existing or new DB
# ---------------------------------------------------------------------------


def init_memory_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Create or open the lessons database with Phase 1 memory tables.

    Calls :func:`init_db` first (idempotent), then creates the
    ``memory_items`` and ``rules`` tables if they do not yet exist.

    Args:
        db_path: Path to the SQLite database file.  When ``None``,
            resolves via :func:`saturnday.paths.state_dir`.

    Returns:
        Open connection with all tables initialised.
    """
    if db_path is None:
        db_path = _default_db_path()
    conn = init_db(db_path)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_items (
            item_id TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_from_run_id TEXT,
            ticket_id TEXT,
            ticket_goal TEXT,
            ticket_class TEXT,
            outcome TEXT,
            failure_mode TEXT,
            severity TEXT DEFAULT 'medium',
            finding_ids TEXT,
            files_touched TEXT,
            symbols_touched TEXT,
            risk_tags TEXT,
            root_cause TEXT,
            corrective_rule_text TEXT,
            evidence_refs TEXT,
            residual_required INTEGER DEFAULT 0,
            last_validated_at TEXT,
            stale_after TEXT,
            status TEXT DEFAULT 'active',
            superseded_by TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            rule_id TEXT PRIMARY KEY,
            memory_type TEXT DEFAULT 'rule',
            created_at TEXT NOT NULL,
            source TEXT,
            source_run_id TEXT,
            severity TEXT DEFAULT 'medium',
            trigger_conditions TEXT,
            forbidden_patterns TEXT,
            required_patterns TEXT,
            human_rule TEXT NOT NULL,
            evidence_refs TEXT,
            status TEXT DEFAULT 'candidate',
            trigger_count INTEGER DEFAULT 0
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_type "
        "ON memory_items(memory_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_items_status "
        "ON memory_items(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rules_status ON rules(status)"
    )

    # EXT1-T01: governance metadata columns — idempotent ALTER TABLE ADD COLUMN
    for col, default in [
        ("last_reviewed_at", None),
        ("review_status", "'unreviewed'"),
        ("dedup_group_id", None),
        ("merged_from", None),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE memory_items ADD COLUMN {col} TEXT"
                + (f" DEFAULT {default}" if default else "")
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    for col, default in [
        ("last_reviewed_at", None),
        ("review_status", "'unreviewed'"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE rules ADD COLUMN {col} TEXT"
                + (f" DEFAULT {default}" if default else "")
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # EXT2-T01: false-positive tracking columns for the rules table
    for stmt in [
        "ALTER TABLE rules ADD COLUMN exceptions TEXT DEFAULT '[]'",
        "ALTER TABLE rules ADD COLUMN counterexamples TEXT DEFAULT '[]'",
        "ALTER TABLE rules ADD COLUMN override_count INTEGER DEFAULT 0",
        "ALTER TABLE rules ADD COLUMN last_override_reason TEXT",
        "ALTER TABLE rules ADD COLUMN confidence REAL DEFAULT 0.8",
        "ALTER TABLE rules ADD COLUMN last_confirmed_valid TEXT",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists

    # EXT3-T01: contrastive pattern columns for memory_items — idempotent ALTER TABLE ADD COLUMN
    for stmt in [
        "ALTER TABLE memory_items ADD COLUMN wrong_pattern TEXT",
        "ALTER TABLE memory_items ADD COLUMN correct_pattern TEXT",
        "ALTER TABLE memory_items ADD COLUMN why_wrong TEXT",
        "ALTER TABLE memory_items ADD COLUMN why_correct TEXT",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists

    # EXT4-T01: playbooks table — new table, idempotent
    conn.execute("""
        CREATE TABLE IF NOT EXISTS playbooks (
            playbook_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            steps TEXT NOT NULL,
            trigger_conditions TEXT,
            applicable_file_patterns TEXT,
            risk_level TEXT DEFAULT 'medium',
            created_at TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            version INTEGER DEFAULT 1
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks(status)"
    )

    # EXT5-T01: memory_scopes table — new table, idempotent
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_scopes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            item_type TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_scopes_item "
        "ON memory_scopes(item_id, item_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_scopes_type "
        "ON memory_scopes(scope_type)"
    )

    # T025: false_positive_log table — tracks per-check usefulness for
    # false-positive rate analysis across spec verification layers.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS false_positive_log (
            fp_id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_type TEXT NOT NULL,
            rule_or_assertion TEXT,
            file TEXT,
            ticket_id TEXT,
            was_useful INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fp_log_check_type "
        "ON false_positive_log(check_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fp_log_ticket "
        "ON false_positive_log(ticket_id)"
    )

    conn.commit()
    logger.debug(
        "Memory DB (Phase 1 + EXT1 + EXT2 + EXT3 + EXT4 + EXT5 + T025) initialised at %s",
        db_path,
    )
    return conn


# ---------------------------------------------------------------------------
# Original CRUD — unchanged
# ---------------------------------------------------------------------------


def store_lesson(conn: sqlite3.Connection, lesson: Lesson) -> None:
    """Store a lesson. If same rule exists for this project, increment confidence.

    Deduplication key is ``(project_id, rule)``. On a match the confidence
    counter is incremented and the description is updated to the latest
    failure description. ticket_id is not part of the key so cross-ticket
    recurrences still accumulate confidence.

    Args:
        conn: Open database connection from :func:`init_db`.
        lesson: The lesson to persist.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT id, confidence FROM lessons WHERE project_id = ? AND rule = ?",
        (lesson.project_id, lesson.rule),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE lessons SET confidence = ?, description = ?, updated_at = ? WHERE id = ?",
            (existing[1] + 1, lesson.description, now, existing[0]),
        )
        logger.debug(
            "Lesson confidence incremented: rule=%s project=%s new_confidence=%d",
            lesson.rule,
            lesson.project_id,
            existing[1] + 1,
        )
    else:
        conn.execute(
            """INSERT INTO lessons
               (project_id, ticket_id, failure_type, rule, description, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lesson.project_id,
                lesson.ticket_id,
                lesson.failure_type,
                lesson.rule,
                lesson.description,
                lesson.confidence,
                now,
                now,
            ),
        )
        logger.debug(
            "New lesson stored: rule=%s project=%s", lesson.rule, lesson.project_id
        )
    conn.commit()


def load_lessons(
    conn: sqlite3.Connection,
    *,
    project_id: str = "",
    min_confidence: int = 1,
) -> list[Lesson]:
    """Load lessons, optionally filtered by project and minimum confidence.

    Results are sorted by confidence descending so the most frequently
    repeated failures appear first.

    Args:
        conn: Open database connection from :func:`init_db`.
        project_id: If non-empty, restrict to lessons for this project.
        min_confidence: Only return lessons with confidence >= this value.

    Returns:
        List of :class:`Lesson` objects ordered by confidence descending.
    """
    query = (
        "SELECT project_id, ticket_id, failure_type, rule, description, confidence "
        "FROM lessons WHERE confidence >= ?"
    )
    params: list[object] = [min_confidence]

    if project_id:
        query += " AND project_id = ?"
        params.append(project_id)

    query += " ORDER BY confidence DESC"
    rows = conn.execute(query, params).fetchall()
    return [Lesson(*row) for row in rows]


def format_lessons_for_prompt(lessons: list[Lesson]) -> str:
    """Format lessons as context for the coder prompt.

    Caps output at the 10 most confident lessons to avoid bloating the
    context window. Returns an empty string when there are no lessons so
    callers can skip injection cleanly.

    Args:
        lessons: Lessons from :func:`load_lessons`, already sorted by
            confidence descending.

    Returns:
        Formatted string suitable for prepending to a coder prompt, or
        empty string when ``lessons`` is empty.
    """
    if not lessons:
        return ""
    lines = ["LESSONS FROM PAST FAILURES (avoid repeating these patterns):"]
    for lesson in lessons[:10]:  # cap at 10 most confident
        lines.append(
            f"- [{lesson.failure_type}] {lesson.rule}: "
            f"{lesson.description} (seen {lesson.confidence}x)"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 1: memory_items CRUD
# ---------------------------------------------------------------------------


def store_memory_item(conn: sqlite3.Connection, item: MemoryItem) -> None:
    """Persist a MemoryItem to the ``memory_items`` table (upsert by item_id).

    JSON-array fields are serialised automatically. Any storage error is
    re-raised so callers can choose to swallow it.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item: The memory item to persist.
    """
    conn.execute(
        """
        INSERT INTO memory_items (
            item_id, memory_type, created_at, created_from_run_id,
            ticket_id, ticket_goal, ticket_class, outcome, failure_mode,
            severity, finding_ids, files_touched, symbols_touched, risk_tags,
            root_cause, corrective_rule_text, evidence_refs, residual_required,
            last_validated_at, stale_after, status, superseded_by,
            wrong_pattern, correct_pattern, why_wrong, why_correct
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?
        )
        ON CONFLICT(item_id) DO UPDATE SET
            memory_type=excluded.memory_type,
            outcome=excluded.outcome,
            failure_mode=excluded.failure_mode,
            severity=excluded.severity,
            finding_ids=excluded.finding_ids,
            files_touched=excluded.files_touched,
            symbols_touched=excluded.symbols_touched,
            risk_tags=excluded.risk_tags,
            root_cause=excluded.root_cause,
            corrective_rule_text=excluded.corrective_rule_text,
            evidence_refs=excluded.evidence_refs,
            residual_required=excluded.residual_required,
            last_validated_at=excluded.last_validated_at,
            stale_after=excluded.stale_after,
            status=excluded.status,
            superseded_by=excluded.superseded_by,
            wrong_pattern=excluded.wrong_pattern,
            correct_pattern=excluded.correct_pattern,
            why_wrong=excluded.why_wrong,
            why_correct=excluded.why_correct
        """,
        (
            item.item_id,
            item.memory_type,
            item.created_at,
            item.created_from_run_id,
            item.ticket_id,
            item.ticket_goal,
            item.ticket_class,
            item.outcome,
            item.failure_mode,
            item.severity,
            json.dumps(item.finding_ids),
            json.dumps(item.files_touched),
            json.dumps(item.symbols_touched),
            json.dumps(item.risk_tags),
            item.root_cause,
            item.corrective_rule_text,
            json.dumps(item.evidence_refs),
            item.residual_required,
            item.last_validated_at,
            item.stale_after,
            item.status,
            item.superseded_by,
            item.wrong_pattern,
            item.correct_pattern,
            item.why_wrong,
            item.why_correct,
        ),
    )
    conn.commit()
    logger.debug("MemoryItem stored: item_id=%s type=%s", item.item_id, item.memory_type)
    # EXT5: auto-create path and risk_tag scopes from the item's fields
    try:
        _auto_scope_memory_item(conn, item)
    except Exception as exc:
        logger.debug("store_memory_item: auto-scope failed — %s", exc)


def _row_to_memory_item(row: tuple) -> MemoryItem:
    """Deserialise a DB row into a MemoryItem.

    Supports both the legacy 22-column schema and the extended schema that
    includes EXT3 contrastive pattern columns at positions 22-25.  New columns
    are read by position with IndexError fall-through so this function works
    on old DBs without the extra columns.
    """
    # Core 22 columns — always present
    (
        item_id, memory_type, created_at, created_from_run_id,
        ticket_id, ticket_goal, ticket_class, outcome, failure_mode,
        severity, finding_ids_raw, files_touched_raw, symbols_touched_raw,
        risk_tags_raw, root_cause, corrective_rule_text, evidence_refs_raw,
        residual_required, last_validated_at, stale_after, status, superseded_by,
    ) = row[:22]

    # EXT3 contrastive columns — positions 22-25, absent on old DBs
    try:
        wrong_pattern: Optional[str] = row[22]
        correct_pattern: Optional[str] = row[23]
        why_wrong: Optional[str] = row[24]
        why_correct: Optional[str] = row[25]
    except IndexError:
        wrong_pattern = None
        correct_pattern = None
        why_wrong = None
        why_correct = None

    def _loads(v: Optional[str], default: list) -> list:
        if v is None:
            return default
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return default

    return MemoryItem(
        item_id=item_id,
        memory_type=memory_type,
        created_at=created_at,
        created_from_run_id=created_from_run_id,
        ticket_id=ticket_id,
        ticket_goal=ticket_goal,
        ticket_class=ticket_class,
        outcome=outcome,
        failure_mode=failure_mode,
        severity=severity or "medium",
        finding_ids=_loads(finding_ids_raw, []),
        files_touched=_loads(files_touched_raw, []),
        symbols_touched=_loads(symbols_touched_raw, []),
        risk_tags=_loads(risk_tags_raw, []),
        root_cause=root_cause,
        corrective_rule_text=corrective_rule_text,
        evidence_refs=_loads(evidence_refs_raw, []),
        residual_required=residual_required or 0,
        last_validated_at=last_validated_at,
        stale_after=stale_after,
        status=status or "active",
        superseded_by=superseded_by,
        wrong_pattern=wrong_pattern,
        correct_pattern=correct_pattern,
        why_wrong=why_wrong,
        why_correct=why_correct,
    )


def load_active_items(
    conn: sqlite3.Connection,
    memory_type: Optional[str] = None,
) -> list[MemoryItem]:
    """Load active MemoryItems, optionally filtered by memory_type.

    Only returns items with ``status = 'active'``.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        memory_type: If provided, restrict to this type (e.g. 'lesson',
            'rule', 'invariant', 'exception').

    Returns:
        List of active MemoryItem objects ordered by created_at descending.
    """
    _query_full = (
        "SELECT item_id, memory_type, created_at, created_from_run_id, "
        "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
        "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
        "root_cause, corrective_rule_text, evidence_refs, residual_required, "
        "last_validated_at, stale_after, status, superseded_by, "
        "wrong_pattern, correct_pattern, why_wrong, why_correct "
        "FROM memory_items WHERE status = 'active'"
    )
    _query_legacy = (
        "SELECT item_id, memory_type, created_at, created_from_run_id, "
        "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
        "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
        "root_cause, corrective_rule_text, evidence_refs, residual_required, "
        "last_validated_at, stale_after, status, superseded_by "
        "FROM memory_items WHERE status = 'active'"
    )
    params: list[object] = []
    type_filter = ""
    if memory_type is not None:
        type_filter = " AND memory_type = ?"
        params.append(memory_type)
    order = " ORDER BY created_at DESC"
    try:
        rows = conn.execute(_query_full + type_filter + order, params).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(_query_legacy + type_filter + order, params).fetchall()
    return [_row_to_memory_item(r) for r in rows]


def mark_stale(conn: sqlite3.Connection, item_id: str) -> None:
    """Set ``status = 'stale'`` on a memory item.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item_id: Primary key of the item to mark stale.
    """
    conn.execute(
        "UPDATE memory_items SET status = 'stale' WHERE item_id = ?",
        (item_id,),
    )
    conn.commit()
    logger.debug("MemoryItem marked stale: item_id=%s", item_id)


# ---------------------------------------------------------------------------
# Phase 1: rules CRUD
# ---------------------------------------------------------------------------


def store_rule(conn: sqlite3.Connection, rule: Rule) -> None:
    """Persist a Rule to the ``rules`` table (upsert by rule_id).

    JSON fields are serialised automatically.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule: The rule to persist.
    """
    conn.execute(
        """
        INSERT INTO rules (
            rule_id, memory_type, created_at, source, source_run_id,
            severity, trigger_conditions, forbidden_patterns,
            required_patterns, human_rule, evidence_refs, status, trigger_count,
            exceptions, counterexamples, override_count, last_override_reason,
            confidence, last_confirmed_valid
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rule_id) DO UPDATE SET
            severity=excluded.severity,
            trigger_conditions=excluded.trigger_conditions,
            forbidden_patterns=excluded.forbidden_patterns,
            required_patterns=excluded.required_patterns,
            human_rule=excluded.human_rule,
            evidence_refs=excluded.evidence_refs,
            status=excluded.status,
            exceptions=excluded.exceptions,
            counterexamples=excluded.counterexamples,
            override_count=excluded.override_count,
            last_override_reason=excluded.last_override_reason,
            confidence=excluded.confidence,
            last_confirmed_valid=excluded.last_confirmed_valid
        """,
        (
            rule.rule_id,
            rule.memory_type,
            rule.created_at,
            rule.source,
            rule.source_run_id,
            rule.severity,
            json.dumps(rule.trigger_conditions),
            json.dumps(rule.forbidden_patterns),
            json.dumps(rule.required_patterns),
            rule.human_rule,
            json.dumps(rule.evidence_refs),
            rule.status,
            rule.trigger_count,
            json.dumps(rule.exceptions),
            json.dumps(rule.counterexamples),
            rule.override_count,
            rule.last_override_reason,
            rule.confidence,
            rule.last_confirmed_valid,
        ),
    )
    conn.commit()
    logger.debug("Rule stored: rule_id=%s status=%s", rule.rule_id, rule.status)


def _row_to_rule(row: tuple) -> Rule:
    """Deserialise a DB row into a Rule.

    Supports both the legacy 13-column schema and the extended schema that
    includes EXT2 false-positive tracking columns.  New columns are read by
    position with IndexError fall-through so that this function works on old
    DBs without the extra columns.
    """
    def _loads_dict(v: Optional[str]) -> dict:
        if v is None:
            return {}
        try:
            result = json.loads(v)
            return result if isinstance(result, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _loads_list(v: Optional[str]) -> list:
        if v is None:
            return []
        try:
            result = json.loads(v)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    # Core 13 columns — always present
    (
        rule_id, memory_type, created_at, source, source_run_id,
        severity, trigger_conditions_raw, forbidden_patterns_raw,
        required_patterns_raw, human_rule, evidence_refs_raw,
        status, trigger_count,
    ) = row[:13]

    # EXT2 columns — positions 13-18, absent on old DBs
    try:
        exceptions_raw = row[13]
        counterexamples_raw = row[14]
        override_count = row[15]
        last_override_reason = row[16]
        confidence_raw = row[17]
        last_confirmed_valid = row[18]
    except IndexError:
        exceptions_raw = "[]"
        counterexamples_raw = "[]"
        override_count = 0
        last_override_reason = None
        confidence_raw = 0.8
        last_confirmed_valid = None

    return Rule(
        rule_id=rule_id,
        memory_type=memory_type or "rule",
        created_at=created_at,
        source=source,
        source_run_id=source_run_id,
        severity=severity or "medium",
        trigger_conditions=_loads_dict(trigger_conditions_raw),
        forbidden_patterns=_loads_list(forbidden_patterns_raw),
        required_patterns=_loads_list(required_patterns_raw),
        human_rule=human_rule,
        evidence_refs=_loads_list(evidence_refs_raw),
        status=status or "candidate",
        trigger_count=trigger_count or 0,
        exceptions=_loads_list(exceptions_raw),
        counterexamples=_loads_list(counterexamples_raw),
        override_count=override_count or 0,
        last_override_reason=last_override_reason,
        confidence=float(confidence_raw) if confidence_raw is not None else 0.8,
        last_confirmed_valid=last_confirmed_valid,
    )


def load_active_rules(
    conn: sqlite3.Connection,
    status: str = "active",
) -> list[Rule]:
    """Load rules filtered by status.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        status: Rule status to filter on. Defaults to ``'active'``.

    Returns:
        List of Rule objects.
    """
    _query_full = (
        "SELECT rule_id, memory_type, created_at, source, source_run_id, "
        "severity, trigger_conditions, forbidden_patterns, required_patterns, "
        "human_rule, evidence_refs, status, trigger_count, "
        "exceptions, counterexamples, override_count, last_override_reason, "
        "confidence, last_confirmed_valid "
        "FROM rules WHERE status = ? "
        "ORDER BY trigger_count DESC, created_at DESC"
    )
    _query_legacy = (
        "SELECT rule_id, memory_type, created_at, source, source_run_id, "
        "severity, trigger_conditions, forbidden_patterns, required_patterns, "
        "human_rule, evidence_refs, status, trigger_count "
        "FROM rules WHERE status = ? "
        "ORDER BY trigger_count DESC, created_at DESC"
    )
    try:
        rows = conn.execute(_query_full, (status,)).fetchall()
    except sqlite3.OperationalError:
        # Old DB without EXT2 columns — fall back to legacy query.
        # _row_to_rule handles the shorter tuple via IndexError fallback.
        rows = conn.execute(_query_legacy, (status,)).fetchall()
    return [_row_to_rule(r) for r in rows]


def increment_rule_trigger(conn: sqlite3.Connection, rule_id: str) -> None:
    """Increment the ``trigger_count`` on a rule.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: Primary key of the rule.
    """
    conn.execute(
        "UPDATE rules SET trigger_count = trigger_count + 1 WHERE rule_id = ?",
        (rule_id,),
    )
    conn.commit()
    logger.debug("Rule trigger count incremented: rule_id=%s", rule_id)


# ---------------------------------------------------------------------------
# T005: Candidate rule generation
# ---------------------------------------------------------------------------

# Risk-tag → rule-text templates
_RULE_TEMPLATES: dict[str, dict] = {
    "shell_exec": {
        "human_rule": (
            "Never pass shell commands as a single string to subprocess. "
            "Always use an explicit argument array and set shell=False."
        ),
        "forbidden_patterns": [r"subprocess\.run\(.*, shell=True", r"os\.system\("],
        "required_patterns": [],
    },
    "test_modification": {
        "human_rule": (
            "Verify that all test symbols referenced in the ticket still exist "
            "and pass after changes. Do not delete test files or functions."
        ),
        "forbidden_patterns": [],
        "required_patterns": [],
    },
    "security": {
        "human_rule": (
            "Do not introduce hardcoded secrets, credentials, or tokens. "
            "Follow the security pattern flagged in the finding."
        ),
        "forbidden_patterns": [],
        "required_patterns": [],
    },
}

# Severity levels that qualify for automatic rule promotion
_SEVERE_SEVERITIES = {"high", "critical"}

# Risk tags that qualify for automatic rule promotion regardless of severity
_SEVERE_RISK_TAGS = {"shell_exec", "security", "test_modification"}


def generate_candidate_rule(item: MemoryItem) -> Optional[Rule]:
    """Generate a candidate Rule from a severe or repeated failure MemoryItem.

    Promotion criteria:
    - severity is 'high' or 'critical', OR
    - risk_tags contains at least one of 'shell_exec', 'security',
      'test_modification', OR
    - failure_mode is 'CODED_UNGOVERNED' with any risk tag present.

    For items that don't meet the criteria, returns ``None``.

    Args:
        item: The MemoryItem to derive a rule from.

    Returns:
        A candidate :class:`Rule` or ``None`` if criteria are not met.
    """
    is_severe_severity = item.severity in _SEVERE_SEVERITIES
    matching_tags = _SEVERE_RISK_TAGS.intersection(item.risk_tags)
    is_ungoverned_with_tags = (
        item.failure_mode == "CODED_UNGOVERNED" and bool(item.risk_tags)
    )

    if not (is_severe_severity or matching_tags or is_ungoverned_with_tags):
        return None

    now = datetime.now(timezone.utc).isoformat()

    # Choose the best matching template
    template: dict = {}
    for tag in ("shell_exec", "security", "test_modification"):
        if tag in item.risk_tags:
            template = _RULE_TEMPLATES[tag]
            break

    # Fallback human rule
    human_rule = (
        template.get("human_rule")
        or item.corrective_rule_text
        or f"Avoid repeated failure pattern: {item.failure_mode or 'unknown'}. "
           f"Ticket: {item.ticket_id or 'unknown'}."
    )

    # Build rule_id from item context
    base = (item.ticket_id or item.item_id).replace(" ", "_")
    rule_id = f"AUTO-{base}-{item.memory_type}"

    trigger_conditions: dict = {}
    if item.files_touched:
        trigger_conditions["file_patterns"] = item.files_touched[:5]
    if item.risk_tags:
        trigger_conditions["risk_tags"] = item.risk_tags

    return Rule(
        rule_id=rule_id,
        memory_type="rule",
        created_at=now,
        source=item.item_id,
        severity=item.severity,
        trigger_conditions=trigger_conditions,
        forbidden_patterns=template.get("forbidden_patterns", []),
        required_patterns=template.get("required_patterns", []),
        human_rule=human_rule,
        evidence_refs=item.evidence_refs,
        status="candidate",
    )


# ---------------------------------------------------------------------------
# T006: Render .saturnday/lessons.md
# ---------------------------------------------------------------------------


def generate_lessons_markdown(conn: sqlite3.Connection) -> str:
    """Render the lessons DB as a structured Markdown document.

    Sections:
    1. Active Rules (rules table where status in ('active', 'enforced'))
    2. Do-Not-Touch Areas (memory_items where memory_type = 'invariant')
    3. Recent Lessons (memory_items where memory_type = 'lesson', last 10)
    4. Current Exceptions (memory_items where memory_type = 'exception' and status = 'active')
    5. Stale/Superseded Items (status != 'active', limit 5)

    Args:
        conn: Open connection from :func:`init_memory_db`.

    Returns:
        Markdown string. Never raises; returns minimal document on any error.
    """
    lines: list[str] = [
        "# Saturnday Memory — Lessons & Rules",
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
        "",
    ]

    try:
        _append_rules_section(conn, lines)
        _append_invariants_section(conn, lines)
        _append_lessons_section(conn, lines)
        _append_exceptions_section(conn, lines)
        _append_stale_section(conn, lines)
    except Exception as exc:  # pragma: no cover
        logger.warning("generate_lessons_markdown encountered error: %s", exc)
        lines.append(f"\n_Error during generation: {exc}_\n")

    return "\n".join(lines)


def _append_rules_section(conn: sqlite3.Connection, lines: list[str]) -> None:
    rows = conn.execute(
        "SELECT rule_id, human_rule, severity, status, trigger_count "
        "FROM rules WHERE status IN ('active', 'enforced') "
        "ORDER BY trigger_count DESC, created_at DESC"
    ).fetchall()
    lines.append("## Active Rules")
    if not rows:
        lines.append("_(none)_")
    else:
        for rule_id, human_rule, severity, status, trigger_count in rows:
            lines.append(
                f"- **{rule_id}** [{severity}/{status}] "
                f"(triggered {trigger_count}x): {human_rule}"
            )
    lines.append("")


def _append_invariants_section(conn: sqlite3.Connection, lines: list[str]) -> None:
    rows = conn.execute(
        "SELECT item_id, files_touched, root_cause "
        "FROM memory_items WHERE memory_type = 'invariant' AND status = 'active' "
        "ORDER BY created_at DESC"
    ).fetchall()
    lines.append("## Do-Not-Touch Areas")
    if not rows:
        lines.append("_(none)_")
    else:
        for item_id, files_raw, root_cause in rows:
            files = json.loads(files_raw) if files_raw else []
            files_str = ", ".join(files) if files else "unspecified"
            lines.append(
                f"- **{item_id}**: {files_str}"
                + (f" — {root_cause}" if root_cause else "")
            )
    lines.append("")


def _append_lessons_section(conn: sqlite3.Connection, lines: list[str]) -> None:
    rows = conn.execute(
        "SELECT item_id, ticket_id, outcome, severity, failure_mode, "
        "corrective_rule_text, created_at "
        "FROM memory_items WHERE memory_type = 'lesson' AND status = 'active' "
        "ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    lines.append("## Recent Lessons")
    if not rows:
        lines.append("_(none)_")
    else:
        for item_id, ticket_id, outcome, severity, failure_mode, rule_text, created_at in rows:
            date_str = created_at[:10] if created_at else "unknown"
            summary = rule_text or failure_mode or "no summary"
            lines.append(
                f"- **{item_id}** [{ticket_id or '?'}/{outcome or '?'}] "
                f"({severity}, {date_str}): {summary[:120]}"
            )
    lines.append("")


def _append_exceptions_section(conn: sqlite3.Connection, lines: list[str]) -> None:
    rows = conn.execute(
        "SELECT item_id, ticket_id, corrective_rule_text, stale_after "
        "FROM memory_items WHERE memory_type = 'exception' AND status = 'active' "
        "ORDER BY created_at DESC"
    ).fetchall()
    lines.append("## Current Exceptions")
    if not rows:
        lines.append("_(none)_")
    else:
        for item_id, ticket_id, rule_text, stale_after in rows:
            stale_str = f" (expires {stale_after[:10]})" if stale_after else ""
            lines.append(
                f"- **{item_id}** [{ticket_id or '?'}]{stale_str}: "
                f"{rule_text or 'no description'}"
            )
    lines.append("")


def _append_stale_section(conn: sqlite3.Connection, lines: list[str]) -> None:
    rows = conn.execute(
        "SELECT item_id, memory_type, status, created_at, failure_mode "
        "FROM memory_items WHERE status != 'active' "
        "ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    lines.append("## Stale / Superseded Items")
    if not rows:
        lines.append("_(none)_")
    else:
        for item_id, memory_type, status, created_at, failure_mode in rows:
            date_str = created_at[:10] if created_at else "unknown"
            lines.append(
                f"- **{item_id}** [{memory_type}/{status}] "
                f"({date_str}): {failure_mode or 'n/a'}"
            )
    lines.append("")


def write_lessons_file(conn: sqlite3.Connection, output_dir: Path) -> Path:
    """Write ``.saturnday/lessons.md`` into ``output_dir``.

    Creates ``output_dir`` if it does not exist.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        output_dir: Directory to write ``lessons.md`` into.

    Returns:
        Path to the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "lessons.md"
    content = generate_lessons_markdown(conn)
    output_path.write_text(content, encoding="utf-8")
    logger.debug("Lessons markdown written to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Phase 5: Rule promotion (T017)
# ---------------------------------------------------------------------------


def promote_rules(conn: sqlite3.Connection) -> int:
    """Promote candidate/advisory rules that meet enforcement thresholds.

    Promotion path:
    - ``candidate`` -> ``active`` when ``trigger_count >= 3``

    Each promoted rule is logged at INFO level.

    Args:
        conn: Open connection from :func:`init_memory_db`.

    Returns:
        Count of rules that were promoted in this call.
    """
    rows = conn.execute(
        "SELECT rule_id, status, trigger_count "
        "FROM rules WHERE status = 'candidate' AND trigger_count >= 3"
    ).fetchall()

    promoted = 0
    for rule_id, status, trigger_count in rows:
        conn.execute(
            "UPDATE rules SET status = 'active' WHERE rule_id = ?",
            (rule_id,),
        )
        logger.info(
            "Rule promoted: rule_id=%s %s -> active (trigger_count=%d)",
            rule_id,
            status,
            trigger_count,
        )
        promoted += 1

    if promoted:
        conn.commit()
    logger.debug("promote_rules: %d rule(s) promoted", promoted)
    return promoted


def promote_to_enforced(conn: sqlite3.Connection, rule_id: str) -> None:
    """Promote a specific rule to ``enforced`` status.

    Enforced rules are checked by governance (memory_enforcement.py) and
    produce findings.  Use this function when manual or critical-severity
    promotion is required.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: The ``rule_id`` primary key of the rule to enforce.
    """
    conn.execute(
        "UPDATE rules SET status = 'enforced' WHERE rule_id = ?",
        (rule_id,),
    )
    conn.commit()
    logger.info("Rule promoted to enforced: rule_id=%s", rule_id)


# ---------------------------------------------------------------------------
# Extension 2: Rule false-positive handling (EXT2-T02)
# ---------------------------------------------------------------------------

_CONFIDENCE_MIN: float = 0.1
_CONFIDENCE_MAX: float = 1.0


def record_rule_override(
    conn: sqlite3.Connection,
    rule_id: str,
    reason: str,
) -> None:
    """Record that a rule was overridden (false positive incident).

    Increments ``override_count``, sets ``last_override_reason``, and
    decreases ``confidence`` by 0.1 with a floor of :data:`_CONFIDENCE_MIN`.
    Fails gracefully — logs errors at WARNING, never raises into the pipeline.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: Primary key of the rule that was overridden.
        reason: Human-readable reason the override was necessary.
    """
    try:
        row = conn.execute(
            "SELECT override_count, confidence FROM rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            logger.warning("record_rule_override: rule_id=%s not found", rule_id)
            return
        override_count, current_confidence = row
        current_confidence = float(current_confidence) if current_confidence is not None else 0.8
        new_confidence = max(_CONFIDENCE_MIN, round(current_confidence - 0.1, 10))
        new_count = (override_count or 0) + 1
        conn.execute(
            "UPDATE rules SET override_count = ?, last_override_reason = ?, "
            "confidence = ? WHERE rule_id = ?",
            (new_count, reason, new_confidence, rule_id),
        )
        conn.commit()
        logger.info(
            "Rule override recorded: rule_id=%s count=%d new_confidence=%.2f reason=%r",
            rule_id,
            new_count,
            new_confidence,
            reason,
        )
    except Exception as exc:
        logger.warning("record_rule_override failed for rule_id=%s: %s", rule_id, exc)


def record_rule_confirmation(
    conn: sqlite3.Connection,
    rule_id: str,
) -> None:
    """Record that a rule was confirmed valid (true positive or manual review).

    Increases ``confidence`` by 0.05 (cap at :data:`_CONFIDENCE_MAX`) and sets
    ``last_confirmed_valid`` to the current UTC timestamp.
    Fails gracefully — logs errors at WARNING, never raises.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: Primary key of the rule being confirmed.
    """
    try:
        row = conn.execute(
            "SELECT confidence FROM rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            logger.warning("record_rule_confirmation: rule_id=%s not found", rule_id)
            return
        current_confidence = float(row[0]) if row[0] is not None else 0.8
        new_confidence = min(_CONFIDENCE_MAX, round(current_confidence + 0.05, 10))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE rules SET confidence = ?, last_confirmed_valid = ? WHERE rule_id = ?",
            (new_confidence, now, rule_id),
        )
        conn.commit()
        logger.info(
            "Rule confirmation recorded: rule_id=%s new_confidence=%.2f",
            rule_id,
            new_confidence,
        )
    except Exception as exc:
        logger.warning("record_rule_confirmation failed for rule_id=%s: %s", rule_id, exc)


def add_rule_exception(
    conn: sqlite3.Connection,
    rule_id: str,
    exception_text: str,
) -> None:
    """Append an exception condition to a rule's ``exceptions`` JSON array.

    Deduplicates — does not insert if ``exception_text`` is already present.
    Fails gracefully on error.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: Primary key of the rule.
        exception_text: Description of the exception condition (e.g.
            "allowed in test files").
    """
    try:
        row = conn.execute(
            "SELECT exceptions FROM rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            logger.warning("add_rule_exception: rule_id=%s not found", rule_id)
            return
        try:
            exceptions: list[str] = json.loads(row[0]) if row[0] else []
            if not isinstance(exceptions, list):
                exceptions = []
        except (json.JSONDecodeError, TypeError):
            exceptions = []
        if exception_text not in exceptions:
            exceptions.append(exception_text)
            conn.execute(
                "UPDATE rules SET exceptions = ? WHERE rule_id = ?",
                (json.dumps(exceptions), rule_id),
            )
            conn.commit()
            logger.info(
                "Rule exception added: rule_id=%s exception=%r", rule_id, exception_text
            )
    except Exception as exc:
        logger.warning("add_rule_exception failed for rule_id=%s: %s", rule_id, exc)


def add_rule_counterexample(
    conn: sqlite3.Connection,
    rule_id: str,
    example_text: str,
) -> None:
    """Append a counterexample to a rule's ``counterexamples`` JSON array.

    Counterexamples describe known false-positive scenarios.
    Deduplicates — does not insert if ``example_text`` is already present.
    Fails gracefully on error.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: Primary key of the rule.
        example_text: Description of the false-positive scenario.
    """
    try:
        row = conn.execute(
            "SELECT counterexamples FROM rules WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if row is None:
            logger.warning("add_rule_counterexample: rule_id=%s not found", rule_id)
            return
        try:
            counterexamples: list[str] = json.loads(row[0]) if row[0] else []
            if not isinstance(counterexamples, list):
                counterexamples = []
        except (json.JSONDecodeError, TypeError):
            counterexamples = []
        if example_text not in counterexamples:
            counterexamples.append(example_text)
            conn.execute(
                "UPDATE rules SET counterexamples = ? WHERE rule_id = ?",
                (json.dumps(counterexamples), rule_id),
            )
            conn.commit()
            logger.info(
                "Rule counterexample added: rule_id=%s example=%r", rule_id, example_text
            )
    except Exception as exc:
        logger.warning("add_rule_counterexample failed for rule_id=%s: %s", rule_id, exc)


def get_low_confidence_rules(
    conn: sqlite3.Connection,
    threshold: float = 0.3,
) -> list[Rule]:
    """Return active rules whose confidence is below ``threshold``.

    These are candidates for manual review or retirement.  Returns an empty
    list on any error.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        threshold: Confidence value below which rules are returned.
            Defaults to ``0.3``.

    Returns:
        List of :class:`Rule` objects with ``confidence < threshold``,
        ordered by confidence ascending (lowest first).
    """
    try:
        rows = conn.execute(
            "SELECT rule_id, memory_type, created_at, source, source_run_id, "
            "severity, trigger_conditions, forbidden_patterns, required_patterns, "
            "human_rule, evidence_refs, status, trigger_count, "
            "exceptions, counterexamples, override_count, last_override_reason, "
            "confidence, last_confirmed_valid "
            "FROM rules WHERE confidence < ? "
            "ORDER BY confidence ASC",
            (threshold,),
        ).fetchall()
        return [_row_to_rule(r) for r in rows]
    except Exception as exc:
        logger.warning("get_low_confidence_rules failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Extension 1: Memory Governance — Deduplication, Contradiction, Review
# ---------------------------------------------------------------------------

_DEDUP_LIMIT = 50  # max merges per run_dedup_pass call


def find_duplicates(conn: sqlite3.Connection) -> list[list[MemoryItem]]:
    """Group active memory items that are duplicates of each other.

    Duplicate criteria (all three must match):
    - Same ``failure_mode`` (normalised to lowercase; both NULL/empty match)
    - Same ``root_cause`` (normalised to lowercase; both NULL/empty match)
    - Overlapping ``files_touched`` (>= 1 file in common)

    Items with empty ``files_touched`` on both sides are NOT grouped (no
    shared file context to establish equivalence).

    Args:
        conn: Open connection from :func:`init_memory_db`.

    Returns:
        List of groups; each group is a list of two or more MemoryItem
        objects considered duplicates.  Returns [] on error.
    """
    try:
        rows = conn.execute(
            "SELECT item_id, memory_type, created_at, created_from_run_id, "
            "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
            "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
            "root_cause, corrective_rule_text, evidence_refs, residual_required, "
            "last_validated_at, stale_after, status, superseded_by "
            "FROM memory_items WHERE status = 'active'"
        ).fetchall()
    except Exception as exc:
        logger.warning("find_duplicates: query failed — %s", exc)
        return []

    items = [_row_to_memory_item(r) for r in rows]

    from collections import defaultdict

    # Group by (failure_mode_norm, root_cause_norm) — cheap O(n) pass
    bucket: dict[tuple, list[MemoryItem]] = defaultdict(list)
    for item in items:
        fm = (item.failure_mode or "").strip().lower()
        rc = (item.root_cause or "").strip().lower()
        bucket[(fm, rc)].append(item)

    groups: list[list[MemoryItem]] = []
    for _key, group_items in bucket.items():
        if len(group_items) < 2:
            continue
        n = len(group_items)
        parent = list(range(n))

        def _find(x: int, _p: list = parent) -> int:
            while _p[x] != x:
                _p[x] = _p[_p[x]]
                x = _p[x]
            return x

        def _union(a: int, b: int, _p: list = parent) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                _p[rb] = ra

        for i in range(n):
            for j in range(i + 1, n):
                files_i = set(group_items[i].files_touched)
                files_j = set(group_items[j].files_touched)
                if files_i and files_j and files_i & files_j:
                    _union(i, j)

        component: dict[int, list[MemoryItem]] = defaultdict(list)
        for i in range(n):
            component[_find(i)].append(group_items[i])

        for comp in component.values():
            if len(comp) >= 2:
                groups.append(comp)

    return groups


def merge_duplicates(
    conn: sqlite3.Connection,
    group: list[MemoryItem],
) -> MemoryItem:
    """Merge a group of duplicate items, keeping the most recently created one.

    The kept item receives the union of ``risk_tags``, ``files_touched``,
    ``symbols_touched``, ``evidence_refs`` from all merged items, plus the
    highest ``severity``.  Its ``merged_from`` column is set to a JSON array
    of the merged item_ids.

    All other items in the group are marked ``status='superseded'``.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        group: Two or more MemoryItems that are duplicates.

    Returns:
        The kept (surviving) MemoryItem after merge.

    Raises:
        ValueError: If ``group`` is empty.
    """
    if not group:
        raise ValueError("merge_duplicates: group must not be empty")

    _severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    sorted_items = sorted(group, key=lambda x: x.created_at or "", reverse=True)
    keep = sorted_items[0]
    to_merge = sorted_items[1:]

    merged_risk_tags = list(dict.fromkeys(keep.risk_tags))
    merged_files = list(dict.fromkeys(keep.files_touched))
    merged_symbols = list(dict.fromkeys(keep.symbols_touched))
    merged_evidence = list(dict.fromkeys(keep.evidence_refs))
    best_severity = keep.severity

    try:
        row = conn.execute(
            "SELECT merged_from FROM memory_items WHERE item_id=?",
            (keep.item_id,),
        ).fetchone()
        existing_mf: list[str] = json.loads(row[0]) if row and row[0] else []
        if not isinstance(existing_mf, list):
            existing_mf = []
    except Exception:
        existing_mf = []
    merged_from_ids: list[str] = list(existing_mf)

    for item in to_merge:
        for tag in item.risk_tags:
            if tag not in merged_risk_tags:
                merged_risk_tags.append(tag)
        for f in item.files_touched:
            if f not in merged_files:
                merged_files.append(f)
        for s in item.symbols_touched:
            if s not in merged_symbols:
                merged_symbols.append(s)
        for e in item.evidence_refs:
            if e not in merged_evidence:
                merged_evidence.append(e)
        if _severity_order.get(item.severity, 0) > _severity_order.get(best_severity, 0):
            best_severity = item.severity
        merged_from_ids.append(item.item_id)
        try:
            conn.execute(
                "UPDATE memory_items SET status='superseded', superseded_by=? "
                "WHERE item_id=?",
                (keep.item_id, item.item_id),
            )
        except Exception as exc:
            logger.warning(
                "merge_duplicates: could not supersede %s — %s", item.item_id, exc
            )

    try:
        conn.execute(
            "UPDATE memory_items SET "
            "risk_tags=?, files_touched=?, symbols_touched=?, evidence_refs=?, "
            "severity=?, merged_from=? "
            "WHERE item_id=?",
            (
                json.dumps(merged_risk_tags),
                json.dumps(merged_files),
                json.dumps(merged_symbols),
                json.dumps(merged_evidence),
                best_severity,
                json.dumps(merged_from_ids),
                keep.item_id,
            ),
        )
        conn.commit()
    except Exception as exc:
        logger.warning(
            "merge_duplicates: could not update kept item %s — %s", keep.item_id, exc
        )

    keep.risk_tags = merged_risk_tags
    keep.files_touched = merged_files
    keep.symbols_touched = merged_symbols
    keep.evidence_refs = merged_evidence
    keep.severity = best_severity
    return keep


def run_dedup_pass(conn: sqlite3.Connection, limit: int = _DEDUP_LIMIT) -> int:
    """Run a full deduplication pass over active memory items.

    Calls :func:`find_duplicates` to find duplicate groups, then
    :func:`merge_duplicates` for each group.  Capped at ``limit`` total
    item merges per call to prevent runaway processing.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        limit: Maximum number of item merges to perform.  Defaults to 50.

    Returns:
        Count of items merged (each group of N produces N-1 merges).
        Returns 0 on error.
    """
    try:
        groups = find_duplicates(conn)
    except Exception as exc:
        logger.warning("run_dedup_pass: find_duplicates failed — %s", exc)
        return 0

    merged_count = 0
    for group in groups:
        if merged_count >= limit:
            logger.debug("run_dedup_pass: reached merge limit %d, stopping", limit)
            break
        try:
            merge_duplicates(conn, group)
            merged_count += len(group) - 1
        except Exception as exc:
            logger.warning("run_dedup_pass: merge failed for group — %s", exc)

    logger.debug("run_dedup_pass: merged %d item(s)", merged_count)
    return merged_count


def detect_contradictions(conn: sqlite3.Connection) -> list[dict]:
    """Find contradictions among active and enforced rules.

    A contradiction exists when:
    - Rule A has a ``forbidden_patterns`` entry that is a substring of (or
      equals) any ``required_patterns`` entry in Rule B, or vice versa.
    - The two rules have overlapping ``trigger_conditions.file_patterns``
      (or at least one rule has no file_patterns, making it global).

    Approximates regex intersection via literal substring containment to
    prefer false negatives over false positives.

    Args:
        conn: Open connection from :func:`init_memory_db`.

    Returns:
        List of contradiction dicts with keys: rule_a, rule_b,
        conflict_type, detail.  Returns [] on error or empty DB.
    """
    try:
        rows = conn.execute(
            "SELECT rule_id, memory_type, created_at, source, source_run_id, "
            "severity, trigger_conditions, forbidden_patterns, required_patterns, "
            "human_rule, evidence_refs, status, trigger_count "
            "FROM rules WHERE status IN ('active', 'enforced', 'candidate')"
        ).fetchall()
    except Exception as exc:
        logger.warning("detect_contradictions: query failed — %s", exc)
        return []

    rules = [_row_to_rule(r) for r in rows]
    contradictions: list[dict] = []

    for i, rule_a in enumerate(rules):
        for j, rule_b in enumerate(rules):
            if j <= i:
                continue

            fp_a: list[str] = rule_a.trigger_conditions.get("file_patterns", [])
            fp_b: list[str] = rule_b.trigger_conditions.get("file_patterns", [])
            files_overlap = (
                not fp_a
                or not fp_b
                or bool(set(fp_a) & set(fp_b))
            )
            if not files_overlap:
                continue

            found = False
            for fb_pat in rule_a.forbidden_patterns:
                if not fb_pat or found:
                    break
                for rq_pat in rule_b.required_patterns:
                    if not rq_pat:
                        continue
                    if fb_pat in rq_pat or rq_pat in fb_pat:
                        contradictions.append({
                            "rule_a": rule_a.rule_id,
                            "rule_b": rule_b.rule_id,
                            "conflict_type": "forbidden_vs_required",
                            "detail": (
                                f"Rule {rule_a.rule_id!r} forbids {fb_pat!r} "
                                f"but rule {rule_b.rule_id!r} requires {rq_pat!r}"
                            ),
                        })
                        found = True
                        break

            if found:
                continue

            for fb_pat in rule_b.forbidden_patterns:
                if not fb_pat or found:
                    break
                for rq_pat in rule_a.required_patterns:
                    if not rq_pat:
                        continue
                    if fb_pat in rq_pat or rq_pat in fb_pat:
                        contradictions.append({
                            "rule_a": rule_b.rule_id,
                            "rule_b": rule_a.rule_id,
                            "conflict_type": "forbidden_vs_required",
                            "detail": (
                                f"Rule {rule_b.rule_id!r} forbids {fb_pat!r} "
                                f"but rule {rule_a.rule_id!r} requires {rq_pat!r}"
                            ),
                        })
                        found = True
                        break

    return contradictions


def log_contradictions(contradictions: list[dict]) -> None:
    """Log detected rule contradictions at WARNING level.

    Does not auto-resolve — resolution is manual or via :func:`mark_stale`.

    Args:
        contradictions: List of contradiction dicts from
            :func:`detect_contradictions`.
    """
    for c in contradictions:
        logger.warning(
            "Rule contradiction [%s]: %s vs %s — %s",
            c.get("conflict_type", "unknown"),
            c.get("rule_a", "?"),
            c.get("rule_b", "?"),
            c.get("detail", ""),
        )


_VALID_REVIEW_STATUSES = {"confirmed", "disputed", "expired"}


def mark_reviewed(
    conn: sqlite3.Connection,
    item_id: str,
    review_status: str = "confirmed",
    reviewer: str = "system",
) -> None:
    """Set ``review_status`` and ``last_reviewed_at`` on a memory_items row.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item_id: Primary key of the memory item.
        review_status: One of ``'confirmed'``, ``'disputed'``, ``'expired'``.
        reviewer: Identity of the reviewer (informational).

    Raises:
        ValueError: If ``review_status`` is not a valid value.
    """
    if review_status not in _VALID_REVIEW_STATUSES:
        raise ValueError(
            f"Invalid review_status {review_status!r}. "
            f"Must be one of: {sorted(_VALID_REVIEW_STATUSES)}"
        )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE memory_items SET review_status=?, last_reviewed_at=? WHERE item_id=?",
        (review_status, now, item_id),
    )
    conn.commit()
    logger.debug(
        "MemoryItem reviewed: item_id=%s status=%s reviewer=%s",
        item_id, review_status, reviewer,
    )


def mark_rule_reviewed(
    conn: sqlite3.Connection,
    rule_id: str,
    review_status: str = "confirmed",
    reviewer: str = "system",
) -> None:
    """Set ``review_status`` and ``last_reviewed_at`` on a rules row.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        rule_id: Primary key of the rule.
        review_status: One of ``'confirmed'``, ``'disputed'``, ``'expired'``.
        reviewer: Identity of the reviewer (informational).

    Raises:
        ValueError: If ``review_status`` is not a valid value.
    """
    if review_status not in _VALID_REVIEW_STATUSES:
        raise ValueError(
            f"Invalid review_status {review_status!r}. "
            f"Must be one of: {sorted(_VALID_REVIEW_STATUSES)}"
        )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE rules SET review_status=?, last_reviewed_at=? WHERE rule_id=?",
        (review_status, now, rule_id),
    )
    conn.commit()
    logger.debug(
        "Rule reviewed: rule_id=%s status=%s reviewer=%s",
        rule_id, review_status, reviewer,
    )


def get_items_needing_review(
    conn: sqlite3.Connection,
    days_since_review: int = 30,
) -> list[MemoryItem]:
    """Return active items that have not been reviewed recently.

    Returns items where ``last_reviewed_at`` is NULL (never reviewed) or
    older than ``days_since_review`` days.  Ordered by severity descending
    then ``created_at`` ascending.  Capped at 20 items.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        days_since_review: Days since last review before item is stale.

    Returns:
        List of MemoryItem objects needing review.  Returns [] on error.
    """
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days_since_review)
        ).isoformat()
        rows = conn.execute(
            "SELECT item_id, memory_type, created_at, created_from_run_id, "
            "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
            "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
            "root_cause, corrective_rule_text, evidence_refs, residual_required, "
            "last_validated_at, stale_after, status, superseded_by "
            "FROM memory_items "
            "WHERE status = 'active' "
            "AND (last_reviewed_at IS NULL OR last_reviewed_at < ?) "
            "ORDER BY "
            "  CASE severity "
            "    WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "    WHEN 'medium' THEN 2 ELSE 3 END ASC, "
            "  created_at ASC "
            "LIMIT 20",
            (cutoff,),
        ).fetchall()
        return [_row_to_memory_item(r) for r in rows]
    except Exception as exc:
        logger.warning("get_items_needing_review: query failed — %s", exc)
        return []


# ---------------------------------------------------------------------------
# Extension 3: Contrastive Lessons (EXT3-T02)
# ---------------------------------------------------------------------------


def store_contrastive_lesson(
    conn: sqlite3.Connection,
    item: "MemoryItem",
    wrong: str,
    correct: str,
    why_wrong: str,
    why_correct: str,
) -> None:
    """Set the four contrastive columns on an existing memory_items row.

    Updates ``wrong_pattern``, ``correct_pattern``, ``why_wrong``, and
    ``why_correct`` on the row identified by ``item.item_id``.  The item
    must already exist in the database.

    Convenience wrapper: sets the four fields on *item* in-place so the
    caller's object stays consistent, then persists with a targeted UPDATE.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item: The MemoryItem whose contrastive fields should be populated.
            ``item.item_id`` is used as the lookup key.
        wrong: Description of the pattern that caused the failure.
        correct: Description of the correct approach.
        why_wrong: Explanation of why the wrong pattern fails.
        why_correct: Explanation of why the correct pattern satisfies constraints.

    Raises:
        ValueError: If ``item.item_id`` does not exist in the database.
    """
    existing = conn.execute(
        "SELECT item_id FROM memory_items WHERE item_id = ?",
        (item.item_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(
            f"store_contrastive_lesson: item_id={item.item_id!r} not found in memory_items"
        )

    conn.execute(
        "UPDATE memory_items SET wrong_pattern=?, correct_pattern=?, "
        "why_wrong=?, why_correct=? WHERE item_id=?",
        (wrong, correct, why_wrong, why_correct, item.item_id),
    )
    conn.commit()

    # Keep in-memory object consistent
    item.wrong_pattern = wrong
    item.correct_pattern = correct
    item.why_wrong = why_wrong
    item.why_correct = why_correct

    logger.debug(
        "Contrastive lesson stored: item_id=%s wrong=%r correct=%r",
        item.item_id, wrong[:60], correct[:60],
    )


def load_contrastive_items(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[MemoryItem]:
    """Load active memory items that have both contrastive patterns set.

    Returns items where ``wrong_pattern IS NOT NULL AND correct_pattern IS NOT
    NULL`` and ``status = 'active'``.  These are the richest lessons for the
    coder because they carry both a failure example and the correct alternative.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        limit: Maximum number of items to return.  Defaults to 10.

    Returns:
        List of :class:`MemoryItem` objects with contrastive fields populated.
        Returns [] on error or empty DB.
    """
    try:
        rows = conn.execute(
            "SELECT item_id, memory_type, created_at, created_from_run_id, "
            "ticket_id, ticket_goal, ticket_class, outcome, failure_mode, "
            "severity, finding_ids, files_touched, symbols_touched, risk_tags, "
            "root_cause, corrective_rule_text, evidence_refs, residual_required, "
            "last_validated_at, stale_after, status, superseded_by, "
            "wrong_pattern, correct_pattern, why_wrong, why_correct "
            "FROM memory_items "
            "WHERE status = 'active' "
            "AND wrong_pattern IS NOT NULL "
            "AND correct_pattern IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_memory_item(r) for r in rows]
    except Exception as exc:
        logger.warning("load_contrastive_items: query failed — %s", exc)
        return []


# ---------------------------------------------------------------------------
# Extension 4: Playbooks (EXT4-T01 / EXT4-T02 / EXT4-T03)
# ---------------------------------------------------------------------------

_RISK_LEVEL_ORDER: dict[str, int] = {
    "critical": 3,
    "high": 2,
    "medium": 1,
    "low": 0,
}


def _row_to_playbook(row: tuple) -> "Playbook":
    """Deserialise a DB row from the ``playbooks`` table into a Playbook.

    Args:
        row: Tuple of columns in SELECT order:
            playbook_id, name, description, steps, trigger_conditions,
            applicable_file_patterns, risk_level, created_at, status, version.

    Returns:
        Populated :class:`Playbook` instance.
    """
    def _loads_list(v: Optional[str]) -> list:
        if v is None:
            return []
        try:
            result = json.loads(v)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _loads_dict(v: Optional[str]) -> dict:
        if v is None:
            return {}
        try:
            result = json.loads(v)
            return result if isinstance(result, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    (
        playbook_id, name, description, steps_raw, trigger_conditions_raw,
        applicable_file_patterns_raw, risk_level, created_at, status, version,
    ) = row[:10]

    return Playbook(
        playbook_id=playbook_id,
        name=name or "",
        description=description or "",
        steps=_loads_list(steps_raw),
        trigger_conditions=_loads_dict(trigger_conditions_raw),
        applicable_file_patterns=_loads_list(applicable_file_patterns_raw),
        risk_level=risk_level or "medium",
        created_at=created_at or "",
        status=status or "active",
        version=int(version) if version is not None else 1,
    )


def store_playbook(conn: sqlite3.Connection, playbook: "Playbook") -> None:
    """Persist a Playbook to the ``playbooks`` table (INSERT OR REPLACE).

    JSON-array / JSON-object fields are serialised automatically.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        playbook: The playbook to persist.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO playbooks (
            playbook_id, name, description, steps, trigger_conditions,
            applicable_file_patterns, risk_level, created_at, status, version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            playbook.playbook_id,
            playbook.name,
            playbook.description,
            json.dumps(playbook.steps),
            json.dumps(playbook.trigger_conditions),
            json.dumps(playbook.applicable_file_patterns),
            playbook.risk_level,
            playbook.created_at or datetime.now(timezone.utc).isoformat(),
            playbook.status,
            playbook.version,
        ),
    )
    conn.commit()
    logger.debug(
        "Playbook stored: playbook_id=%s status=%s", playbook.playbook_id, playbook.status
    )


def load_active_playbooks(conn: sqlite3.Connection) -> list["Playbook"]:
    """Load all playbooks with ``status = 'active'``.

    Args:
        conn: Open connection from :func:`init_memory_db`.

    Returns:
        List of active :class:`Playbook` objects ordered by created_at DESC.
        Returns [] on error.
    """
    try:
        rows = conn.execute(
            "SELECT playbook_id, name, description, steps, trigger_conditions, "
            "applicable_file_patterns, risk_level, created_at, status, version "
            "FROM playbooks WHERE status = 'active' "
            "ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_playbook(r) for r in rows]
    except Exception as exc:
        logger.warning("load_active_playbooks: query failed — %s", exc)
        return []


def deprecate_playbook(conn: sqlite3.Connection, playbook_id: str) -> None:
    """Set ``status = 'deprecated'`` on a playbook.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        playbook_id: Primary key of the playbook to deprecate.
    """
    conn.execute(
        "UPDATE playbooks SET status = 'deprecated' WHERE playbook_id = ?",
        (playbook_id,),
    )
    conn.commit()
    logger.debug("Playbook deprecated: playbook_id=%s", playbook_id)


def match_playbooks(
    conn: sqlite3.Connection,
    ticket: "TicketSpec",
    changed_files: Optional[list[str]] = None,
) -> list["Playbook"]:
    """Match active playbooks to a ticket using trigger_conditions (OR logic).

    Matching rules:
    - ``file_patterns``: any pattern fnmatches any file in ``ticket.scope.allowed_globs``
      or ``changed_files``.
    - ``risk_tags``: any tag appears in the inferred risk tags from the ticket goal.
    - ``ticket_classes``: the inferred ticket class matches any entry.
    - Empty ``trigger_conditions`` matches all tickets.

    Returns playbooks sorted by risk_level descending (critical first), then
    name ascending.  Capped at 3 to limit prompt injection size.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        ticket: The ticket being executed.
        changed_files: Additional file paths from prior execution (optional).

    Returns:
        Up to 3 matched :class:`Playbook` objects.  Returns [] on error.
    """
    try:
        playbooks = load_active_playbooks(conn)
    except Exception as exc:
        logger.debug("match_playbooks: load failed — %s", exc)
        return []

    if not playbooks:
        return []

    # Infer ticket context — reuse helpers from memory_retrieval if available
    ticket_tags: list[str] = []
    ticket_class: str = "generation"
    try:
        from saturnday.run.memory_retrieval import (  # type: ignore[import]
            _infer_risk_tags_from_ticket,
            _infer_ticket_class,
        )
        ticket_tags = _infer_risk_tags_from_ticket(ticket)
        ticket_class = _infer_ticket_class(ticket.goal)
    except Exception:
        # Fallback inline inference
        goal_lower = (ticket.goal or "").lower()
        if any(kw in goal_lower for kw in ("shell", "subprocess", "exec")):
            ticket_tags.append("shell_exec")
        if any(kw in goal_lower for kw in ("security", "secret", "credential", "auth", "token")):
            ticket_tags.append("security")
        if any(kw in goal_lower for kw in ("test", "pytest")):
            ticket_tags.append("test_modification")
        if any(w in goal_lower for w in ("repair", "fix", "remediate", "remed")):
            ticket_class = "repair"
        elif any(w in goal_lower for w in ("refactor", "migrate", "move", "rename")):
            ticket_class = "remediation"

    # File set for pattern matching
    scope_files: list[str] = (
        list(ticket.scope.allowed_globs) if ticket.scope else ["**"]
    )
    if changed_files:
        scope_files = scope_files + [f for f in changed_files if f not in scope_files]

    matched: list[Playbook] = []

    for pb in playbooks:
        tc = pb.trigger_conditions

        # Empty trigger_conditions → matches all
        if not tc:
            matched.append(pb)
            continue

        hit = False

        # file_patterns
        fp_list: list[str] = tc.get("file_patterns", [])
        if fp_list and not hit:
            for pattern in fp_list:
                for f in scope_files:
                    if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(
                        Path(f).name, pattern
                    ):
                        hit = True
                        break
                if hit:
                    break

        # risk_tags
        rt_list: list[str] = tc.get("risk_tags", [])
        if rt_list and not hit:
            if set(rt_list) & set(ticket_tags):
                hit = True

        # ticket_classes
        tc_list: list[str] = tc.get("ticket_classes", [])
        if tc_list and not hit:
            if ticket_class in tc_list:
                hit = True

        if hit:
            matched.append(pb)

    # Sort: risk_level desc, name asc; cap at 3
    matched.sort(
        key=lambda p: (-_RISK_LEVEL_ORDER.get(p.risk_level, 1), p.name),
    )
    return matched[:3]


# ---------------------------------------------------------------------------
# Extension 5: Richer Scoping (EXT5-T01 / EXT5-T02)
# ---------------------------------------------------------------------------

_VALID_ITEM_TYPES: frozenset[str] = frozenset({"memory_item", "rule", "playbook"})
_VALID_SCOPE_TYPES: frozenset[str] = frozenset(
    {"path", "file_type", "subsystem", "symbol", "risk_tag"}
)


def add_scope(
    conn: sqlite3.Connection,
    item_id: str,
    item_type: str,
    scope_type: str,
    scope_value: str,
) -> None:
    """Insert a scope entry into ``memory_scopes``.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item_id: Primary key of the memory item, rule, or playbook.
        item_type: One of ``'memory_item'``, ``'rule'``, ``'playbook'``.
        scope_type: One of ``'path'``, ``'file_type'``, ``'subsystem'``,
            ``'symbol'``, ``'risk_tag'``.
        scope_value: The pattern or value for this scope.

    Raises:
        ValueError: If ``item_type`` or ``scope_type`` is invalid.
    """
    if item_type not in _VALID_ITEM_TYPES:
        raise ValueError(
            f"Invalid item_type {item_type!r}. Must be one of: {sorted(_VALID_ITEM_TYPES)}"
        )
    if scope_type not in _VALID_SCOPE_TYPES:
        raise ValueError(
            f"Invalid scope_type {scope_type!r}. Must be one of: {sorted(_VALID_SCOPE_TYPES)}"
        )
    conn.execute(
        "INSERT INTO memory_scopes (item_id, item_type, scope_type, scope_value) "
        "VALUES (?, ?, ?, ?)",
        (item_id, item_type, scope_type, scope_value),
    )
    conn.commit()
    logger.debug(
        "Scope added: item_id=%s item_type=%s scope_type=%s value=%r",
        item_id, item_type, scope_type, scope_value,
    )


def load_scopes(
    conn: sqlite3.Connection,
    item_id: str,
    item_type: str,
) -> list[dict]:
    """Return all scope entries for a given item.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item_id: Primary key of the item.
        item_type: One of ``'memory_item'``, ``'rule'``, ``'playbook'``.

    Returns:
        List of ``{"scope_type": ..., "scope_value": ...}`` dicts.
        Returns [] on error or if the table does not exist.
    """
    try:
        rows = conn.execute(
            "SELECT scope_type, scope_value FROM memory_scopes "
            "WHERE item_id = ? AND item_type = ?",
            (item_id, item_type),
        ).fetchall()
        return [{"scope_type": r[0], "scope_value": r[1]} for r in rows]
    except Exception as exc:
        logger.debug("load_scopes failed for item_id=%s: %s", item_id, exc)
        return []


def remove_scopes(
    conn: sqlite3.Connection,
    item_id: str,
    item_type: str,
) -> int:
    """Delete all scope entries for an item.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item_id: Primary key of the item.
        item_type: One of ``'memory_item'``, ``'rule'``, ``'playbook'``.

    Returns:
        Count of rows deleted.
    """
    try:
        cursor = conn.execute(
            "DELETE FROM memory_scopes WHERE item_id = ? AND item_type = ?",
            (item_id, item_type),
        )
        conn.commit()
        deleted = cursor.rowcount
        logger.debug(
            "Scopes removed: item_id=%s item_type=%s count=%d",
            item_id, item_type, deleted,
        )
        return deleted
    except Exception as exc:
        logger.warning("remove_scopes failed for item_id=%s: %s", item_id, exc)
        return 0


def bulk_add_scopes(
    conn: sqlite3.Connection,
    item_id: str,
    item_type: str,
    scopes: list[dict],
) -> None:
    """Add multiple scope entries in a single transaction.

    Each dict in ``scopes`` must have ``scope_type`` and ``scope_value`` keys.
    Invalid entries raise ``ValueError`` before any inserts occur.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item_id: Primary key of the item.
        item_type: One of ``'memory_item'``, ``'rule'``, ``'playbook'``.
        scopes: List of scope dicts.

    Raises:
        ValueError: If ``item_type`` or any ``scope_type`` is invalid.
    """
    if item_type not in _VALID_ITEM_TYPES:
        raise ValueError(
            f"Invalid item_type {item_type!r}. Must be one of: {sorted(_VALID_ITEM_TYPES)}"
        )
    # Pre-validate all entries before touching the DB
    for s in scopes:
        st = s.get("scope_type", "")
        if st not in _VALID_SCOPE_TYPES:
            raise ValueError(
                f"Invalid scope_type {st!r}. Must be one of: {sorted(_VALID_SCOPE_TYPES)}"
            )

    try:
        with conn:
            for s in scopes:
                conn.execute(
                    "INSERT INTO memory_scopes "
                    "(item_id, item_type, scope_type, scope_value) "
                    "VALUES (?, ?, ?, ?)",
                    (item_id, item_type, s["scope_type"], s["scope_value"]),
                )
        logger.debug(
            "Bulk scopes added: item_id=%s item_type=%s count=%d",
            item_id, item_type, len(scopes),
        )
    except Exception as exc:
        logger.warning("bulk_add_scopes failed for item_id=%s: %s", item_id, exc)
        raise


def _auto_scope_memory_item(
    conn: sqlite3.Connection,
    item: "MemoryItem",
) -> None:
    """Auto-create scope entries from a MemoryItem's files_touched and risk_tags.

    Called by ``store_memory_item`` after every upsert.  Clears existing scopes
    for the item then re-inserts from the current field values so scopes stay
    in sync with the item.

    Fails gracefully — errors are logged at DEBUG level and swallowed.

    Args:
        conn: Open connection from :func:`init_memory_db`.
        item: The memory item just stored.
    """
    try:
        # Clear old scopes first so they don't accumulate on repeated updates
        conn.execute(
            "DELETE FROM memory_scopes WHERE item_id = ? AND item_type = 'memory_item'",
            (item.item_id,),
        )
        # Path scopes from files_touched
        for f in item.files_touched:
            conn.execute(
                "INSERT INTO memory_scopes (item_id, item_type, scope_type, scope_value) "
                "VALUES (?, 'memory_item', 'path', ?)",
                (item.item_id, f),
            )
        # Risk-tag scopes from risk_tags
        for tag in item.risk_tags:
            conn.execute(
                "INSERT INTO memory_scopes (item_id, item_type, scope_type, scope_value) "
                "VALUES (?, 'memory_item', 'risk_tag', ?)",
                (item.item_id, tag),
            )
        conn.commit()
    except Exception as exc:
        logger.debug("_auto_scope_memory_item failed for item_id=%s: %s", item.item_id, exc)


# ---------------------------------------------------------------------------
# T025: False-positive tracking for spec verification layers
# ---------------------------------------------------------------------------


def log_check_result(
    conn: sqlite3.Connection,
    check_type: str,
    rule: Optional[str],
    file: Optional[str],
    ticket_id: Optional[str],
    was_useful: int,
) -> None:
    """Record whether a check result was useful or a false positive.

    Inserts a row into ``false_positive_log``.  Safe to call on connections
    opened by either :func:`init_db` or :func:`init_memory_db` — the table
    is created by :func:`init_memory_db`.  If the table does not exist yet,
    the call is a no-op.

    Args:
        conn: Open SQLite connection.
        check_type: Layer that produced the finding, e.g. ``"spec_assertion"``,
            ``"property_test"``, ``"dataflow"``, ``"type_check"``, ``"reviewer"``.
        rule: Specific assertion or rule name (may be None).
        file: Repository-relative file path (may be None).
        ticket_id: ID of the ticket the check ran against (may be None).
        was_useful: 1 = useful finding, 0 = false positive / noise.
    """
    import datetime  # noqa: PLC0415

    try:
        conn.execute(
            """
            INSERT INTO false_positive_log
                (check_type, rule_or_assertion, file, ticket_id, was_useful, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                check_type,
                rule,
                file,
                ticket_id,
                int(was_useful),
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("log_check_result failed: %s", exc)


def get_false_positive_rate(
    conn: sqlite3.Connection,
    check_type: str,
) -> float:
    """Return the false-positive rate for a given check layer.

    False-positive rate = rows where ``was_useful = 0`` / total rows for
    this ``check_type``.  Returns 0.0 when there are no rows.

    Args:
        conn: Open SQLite connection.
        check_type: Layer to query, e.g. ``"spec_assertion"``.

    Returns:
        Float in [0.0, 1.0].
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN was_useful = 0 THEN 1 ELSE 0 END) "
            "FROM false_positive_log WHERE check_type = ?",
            (check_type,),
        ).fetchone()
        if not row or not row[0]:
            return 0.0
        total, fp_count = row
        return float(fp_count or 0) / float(total)
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_false_positive_rate failed: %s", exc)
        return 0.0


def get_noisy_rules(
    conn: sqlite3.Connection,
    threshold: float = 0.5,
) -> list:
    """Return rule/assertion names whose false-positive rate exceeds the threshold.

    Aggregates ``false_positive_log`` by ``rule_or_assertion``, computes the
    false-positive rate for each, and returns those above *threshold*.

    Args:
        conn: Open SQLite connection.
        threshold: Minimum false-positive rate to include in results.
            Defaults to 0.5 (50 %).

    Returns:
        List of rule/assertion name strings.
    """
    try:
        rows = conn.execute(
            """
            SELECT rule_or_assertion,
                   COUNT(*) AS total,
                   SUM(CASE WHEN was_useful = 0 THEN 1 ELSE 0 END) AS fp_count
            FROM false_positive_log
            WHERE rule_or_assertion IS NOT NULL
            GROUP BY rule_or_assertion
            HAVING total > 0
            """,
        ).fetchall()
        return [
            row[0]
            for row in rows
            if (float(row[2] or 0) / float(row[1])) > threshold
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("get_noisy_rules failed: %s", exc)
        return []
