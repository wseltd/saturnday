"""Tests for Extension 5: Richer Scoping (EXT5-T01 through EXT5-T05).

Covers:
- memory_scopes table creation (EXT5-T01)
- Scope CRUD: add, load, remove, bulk_add (EXT5-T02)
- Scope-aware filtering in filter_relevant_items (EXT5-T03)
- Scope-aware scoring bonus in rank_and_select (EXT5-T03)
- Scope-aware enforcement in _get_triggered_files (EXT5-T05)
- Tolerance for missing memory_scopes table (EXT5-T03)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from saturnday._types import TicketScope, TicketSpec
from saturnday.run.lessons import (
    MemoryItem,
    Rule,
    add_scope,
    bulk_add_scopes,
    init_memory_db,
    load_scopes,
    remove_scopes,
    store_memory_item,
    store_rule,
)
from saturnday.run.memory_enforcement import _get_triggered_files
from saturnday.run.memory_retrieval import filter_relevant_items, rank_and_select


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh memory DB with all tables including memory_scopes."""
    return init_memory_db(tmp_path / "lessons.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_item(
    item_id: str = "MI-001",
    files_touched: list[str] | None = None,
    risk_tags: list[str] | None = None,
    failure_mode: str = "governance_fail",
    ticket_class: str = "generation",
    severity: str = "medium",
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        memory_type="lesson",
        created_at=_now(),
        files_touched=files_touched or [],
        risk_tags=risk_tags or [],
        failure_mode=failure_mode,
        ticket_class=ticket_class,
        severity=severity,
        status="active",
    )


def _make_ticket(
    goal: str = "Add a simple feature",
    allowed_globs: tuple[str, ...] = ("src/**/*.py",),
) -> TicketSpec:
    return TicketSpec(
        ticket_id="T-SCOPE",
        goal=goal,
        scope=TicketScope(allowed_globs=allowed_globs, forbidden_globs=()),
    )


def _make_rule(
    rule_id: str = "RULE-001",
    forbidden_patterns: list[str] | None = None,
    trigger_conditions: dict | None = None,
    status: str = "enforced",
) -> Rule:
    return Rule(
        rule_id=rule_id,
        created_at=_now(),
        human_rule="Test rule",
        status=status,
        trigger_conditions=trigger_conditions or {},
        forbidden_patterns=forbidden_patterns or ["eval("],
        required_patterns=[],
    )


# ---------------------------------------------------------------------------
# EXT5-T01: Table creation
# ---------------------------------------------------------------------------


def test_scopes_table_created(db: sqlite3.Connection) -> None:
    """memory_scopes table is created by init_memory_db."""
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "memory_scopes" in tables


def test_scopes_table_has_indexes(db: sqlite3.Connection) -> None:
    """Expected indexes exist on memory_scopes."""
    indexes = {
        row[1]
        for row in db.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_memory_scopes_item" in indexes
    assert "idx_memory_scopes_type" in indexes


# ---------------------------------------------------------------------------
# EXT5-T02: CRUD
# ---------------------------------------------------------------------------


def test_add_and_load_scope(db: sqlite3.Connection) -> None:
    """add_scope then load_scopes round-trips correctly."""
    add_scope(db, "MI-001", "memory_item", "path", "src/foo.py")
    scopes = load_scopes(db, "MI-001", "memory_item")
    assert len(scopes) == 1
    assert scopes[0]["scope_type"] == "path"
    assert scopes[0]["scope_value"] == "src/foo.py"


def test_add_scope_invalid_item_type_raises(db: sqlite3.Connection) -> None:
    """add_scope raises ValueError on invalid item_type."""
    with pytest.raises(ValueError, match="item_type"):
        add_scope(db, "MI-001", "invalid_type", "path", "src/foo.py")


def test_add_scope_invalid_scope_type_raises(db: sqlite3.Connection) -> None:
    """add_scope raises ValueError on invalid scope_type."""
    with pytest.raises(ValueError, match="scope_type"):
        add_scope(db, "MI-001", "memory_item", "invalid_scope", "src/foo.py")


def test_load_scopes_returns_empty_for_unknown_item(db: sqlite3.Connection) -> None:
    """load_scopes returns [] when item has no scope entries."""
    scopes = load_scopes(db, "NONEXISTENT", "memory_item")
    assert scopes == []


def test_remove_scopes(db: sqlite3.Connection) -> None:
    """remove_scopes deletes all scopes for an item and returns count."""
    add_scope(db, "MI-001", "memory_item", "path", "src/foo.py")
    add_scope(db, "MI-001", "memory_item", "risk_tag", "shell_exec")
    count = remove_scopes(db, "MI-001", "memory_item")
    assert count == 2
    assert load_scopes(db, "MI-001", "memory_item") == []


def test_remove_scopes_does_not_affect_other_items(db: sqlite3.Connection) -> None:
    """remove_scopes only removes scopes for the specified item."""
    add_scope(db, "MI-001", "memory_item", "path", "src/a.py")
    add_scope(db, "MI-002", "memory_item", "path", "src/b.py")
    remove_scopes(db, "MI-001", "memory_item")
    assert load_scopes(db, "MI-002", "memory_item") != []


def test_bulk_add_scopes(db: sqlite3.Connection) -> None:
    """bulk_add_scopes inserts all scopes in a single call."""
    scopes = [
        {"scope_type": "path", "scope_value": "src/a.py"},
        {"scope_type": "risk_tag", "scope_value": "security"},
        {"scope_type": "file_type", "scope_value": ".py"},
    ]
    bulk_add_scopes(db, "MI-001", "memory_item", scopes)
    loaded = load_scopes(db, "MI-001", "memory_item")
    assert len(loaded) == 3


def test_bulk_add_scopes_invalid_type_raises(db: sqlite3.Connection) -> None:
    """bulk_add_scopes raises ValueError on invalid scope_type before any insert."""
    with pytest.raises(ValueError, match="scope_type"):
        bulk_add_scopes(
            db,
            "MI-001",
            "memory_item",
            [{"scope_type": "INVALID", "scope_value": "x"}],
        )


# ---------------------------------------------------------------------------
# EXT5-T03: Scope-aware filtering
# ---------------------------------------------------------------------------


def test_filter_relevant_items_with_path_scope(db: sqlite3.Connection) -> None:
    """Item with path scope matching ticket files is included by scope boost."""
    # Create item with NO files_touched (would normally be filtered out)
    item = _make_item(item_id="MI-PATH", files_touched=[], risk_tags=[])
    store_memory_item(db, item)
    # Manually add a path scope (auto-scope won't do it with empty files)
    add_scope(db, "MI-PATH", "memory_item", "path", "src/foo.py")

    ticket = _make_ticket(allowed_globs=("src/foo.py",))
    results = filter_relevant_items(db, ticket)
    ids = [i.item_id for i in results]
    assert "MI-PATH" in ids


def test_filter_relevant_items_with_file_type_scope(db: sqlite3.Connection) -> None:
    """Item with file_type scope matching ticket's files is included."""
    item = _make_item(item_id="MI-FTYPE", files_touched=[], risk_tags=[])
    store_memory_item(db, item)
    add_scope(db, "MI-FTYPE", "memory_item", "file_type", ".py")

    ticket = _make_ticket(allowed_globs=("src/module.py",))
    results = filter_relevant_items(db, ticket)
    ids = [i.item_id for i in results]
    assert "MI-FTYPE" in ids


def test_filter_relevant_items_with_risk_tag_scope(db: sqlite3.Connection) -> None:
    """Item with risk_tag scope matching ticket's inferred tags is included."""
    item = _make_item(item_id="MI-RTAG", files_touched=[], risk_tags=[])
    store_memory_item(db, item)
    add_scope(db, "MI-RTAG", "memory_item", "risk_tag", "shell_exec")

    ticket = _make_ticket(goal="Run subprocess exec command")
    results = filter_relevant_items(db, ticket)
    ids = [i.item_id for i in results]
    assert "MI-RTAG" in ids


def test_filter_relevant_items_with_symbol_scope(db: sqlite3.Connection) -> None:
    """Item with symbol scope matching ticket goal is included."""
    item = _make_item(item_id="MI-SYM", files_touched=[], risk_tags=[])
    store_memory_item(db, item)
    add_scope(db, "MI-SYM", "memory_item", "symbol", "process_payment")

    ticket = _make_ticket(goal="Refactor process_payment to use new API")
    results = filter_relevant_items(db, ticket)
    ids = [i.item_id for i in results]
    assert "MI-SYM" in ids


def test_filter_relevant_items_without_scopes_unchanged(db: sqlite3.Connection) -> None:
    """Items without scopes behave exactly as before (no regression)."""
    item = _make_item(
        item_id="MI-PLAIN",
        files_touched=["src/foo.py"],
        risk_tags=["shell_exec"],
    )
    store_memory_item(db, item)

    ticket = _make_ticket(
        goal="Run subprocess exec",
        allowed_globs=("src/foo.py",),
    )
    results = filter_relevant_items(db, ticket)
    ids = [i.item_id for i in results]
    assert "MI-PLAIN" in ids


# ---------------------------------------------------------------------------
# EXT5: Ranking scope bonus
# ---------------------------------------------------------------------------


def test_rank_and_select_scope_bonus(db: sqlite3.Connection) -> None:
    """Items with matching scopes rank higher than equivalent unscoped items."""
    now = _now()
    item_scoped = MemoryItem(
        item_id="MI-SCOPED",
        memory_type="lesson",
        created_at=now,
        files_touched=[],
        risk_tags=[],
        failure_mode="test_fail",
        severity="medium",
        status="active",
    )
    item_unscoped = MemoryItem(
        item_id="MI-UNSCOPED",
        memory_type="lesson",
        created_at=now,  # same timestamp — recency equal
        files_touched=[],
        risk_tags=[],
        failure_mode="test_fail",
        severity="medium",
        status="active",
    )
    store_memory_item(db, item_scoped)
    store_memory_item(db, item_unscoped)

    # Add matching scope only to item_scoped
    add_scope(db, "MI-SCOPED", "memory_item", "path", "src/foo.py")

    ticket = _make_ticket(allowed_globs=("src/foo.py",))
    ranked = rank_and_select([item_scoped, item_unscoped], ticket, limit=2, conn=db)
    # Scoped item should rank first
    assert ranked[0].item_id == "MI-SCOPED"


# ---------------------------------------------------------------------------
# EXT5: Scope-aware enforcement
# ---------------------------------------------------------------------------


def test_scope_filtering_tolerates_missing_table() -> None:
    """filter_relevant_items does not fail when memory_scopes table is absent."""
    # Create a DB without memory_scopes (raw in-memory with only basics)
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.execute("""
        CREATE TABLE memory_items (
            item_id TEXT PRIMARY KEY,
            memory_type TEXT, created_at TEXT,
            created_from_run_id TEXT, ticket_id TEXT, ticket_goal TEXT,
            ticket_class TEXT, outcome TEXT, failure_mode TEXT,
            severity TEXT, finding_ids TEXT, files_touched TEXT,
            symbols_touched TEXT, risk_tags TEXT, root_cause TEXT,
            corrective_rule_text TEXT, evidence_refs TEXT,
            residual_required INTEGER, last_validated_at TEXT,
            stale_after TEXT, status TEXT, superseded_by TEXT
        )
    """)
    raw_conn.execute(
        "INSERT INTO memory_items (item_id, memory_type, created_at, status) "
        "VALUES ('X', 'lesson', '2025-01-01', 'active')"
    )
    raw_conn.commit()

    ticket = _make_ticket()
    # Should not raise even though memory_scopes doesn't exist
    results = filter_relevant_items(raw_conn, ticket)
    assert isinstance(results, list)
    raw_conn.close()


# ---------------------------------------------------------------------------
# EXT5-T05: Scope-aware enforcement
# ---------------------------------------------------------------------------


def test_get_triggered_files_with_scope_restricts_to_scope_matches(
    db: sqlite3.Connection,
) -> None:
    """Rules with path scopes (and no file_patterns) are scoped by memory_scopes."""
    rule = _make_rule(
        trigger_conditions={},  # no explicit file_patterns
    )
    store_rule(db, rule)
    # Add a path scope for only src/target.py
    add_scope(db, rule.rule_id, "rule", "path", "src/target.py")

    changed_files = ["src/target.py", "src/other.py", "tests/test_foo.py"]
    triggered = _get_triggered_files(rule, changed_files, conn=db)
    # Should be restricted to the scoped file
    assert "src/target.py" in triggered
    assert "src/other.py" not in triggered
    assert "tests/test_foo.py" not in triggered


def test_get_triggered_files_without_scope_returns_all(
    db: sqlite3.Connection,
) -> None:
    """Rules with no file_patterns and no scopes trigger on all files."""
    rule = _make_rule(trigger_conditions={})
    store_rule(db, rule)
    # No scope entries added

    changed_files = ["src/a.py", "src/b.py"]
    triggered = _get_triggered_files(rule, changed_files, conn=db)
    assert set(triggered) == set(changed_files)


def test_get_triggered_files_conn_none_no_scope_applied(
    db: sqlite3.Connection,
) -> None:
    """When conn=None, scope checking is skipped and all files are returned."""
    rule = _make_rule(trigger_conditions={})
    changed_files = ["src/x.py", "src/y.py"]
    triggered = _get_triggered_files(rule, changed_files, conn=None)
    assert set(triggered) == set(changed_files)


def test_auto_scope_created_on_store_memory_item(db: sqlite3.Connection) -> None:
    """store_memory_item auto-creates path and risk_tag scopes."""
    item = _make_item(
        item_id="MI-AUTO",
        files_touched=["src/foo.py", "src/bar.py"],
        risk_tags=["security", "shell_exec"],
    )
    store_memory_item(db, item)

    scopes = load_scopes(db, "MI-AUTO", "memory_item")
    scope_types = {s["scope_type"] for s in scopes}
    scope_values = {s["scope_value"] for s in scopes}

    assert "path" in scope_types
    assert "risk_tag" in scope_types
    assert "src/foo.py" in scope_values
    assert "src/bar.py" in scope_values
    assert "security" in scope_values
    assert "shell_exec" in scope_values
