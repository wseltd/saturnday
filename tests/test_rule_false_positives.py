"""Tests for Extension 2: Rule false-positive handling (EXT2-T01 / EXT2-T02).

Covers schema migration, Rule dataclass new fields, and the five false-positive
tracking functions added to lessons.py.

Each test uses its own tmp_path-based DB — no shared state between tests.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from saturnday.run.lessons import (
    Rule,
    add_rule_counterexample,
    add_rule_exception,
    get_low_confidence_rules,
    init_db,
    init_memory_db,
    load_active_rules,
    record_rule_confirmation,
    record_rule_override,
    store_rule,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_rule(
    rule_id: str = "RULE-1",
    status: str = "active",
    confidence: float = 0.8,
    override_count: int = 0,
    last_override_reason: Optional[str] = None,
    exceptions: Optional[list[str]] = None,
    counterexamples: Optional[list[str]] = None,
    last_confirmed_valid: Optional[str] = None,
) -> Rule:
    now = datetime.now(timezone.utc).isoformat()
    return Rule(
        rule_id=rule_id,
        created_at=now,
        human_rule="Do not use shell=True",
        status=status,
        confidence=confidence,
        override_count=override_count,
        last_override_reason=last_override_reason,
        exceptions=exceptions or [],
        counterexamples=counterexamples or [],
        last_confirmed_valid=last_confirmed_valid,
    )


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Fresh memory DB with EXT2 columns."""
    return init_memory_db(tmp_path / "lessons.db")


@pytest.fixture()
def rule_in_db(db: sqlite3.Connection) -> tuple[sqlite3.Connection, Rule]:
    """DB with a single active rule already stored."""
    rule = _make_rule(rule_id="RULE-1", status="active", confidence=0.8)
    store_rule(db, rule)
    return db, rule


# ---------------------------------------------------------------------------
# EXT2-T01: Schema migration tests
# ---------------------------------------------------------------------------


def test_ext2_columns_exist_after_init(tmp_path: Path) -> None:
    """init_memory_db must create all 6 EXT2 columns on the rules table."""
    conn = init_memory_db(tmp_path / "new.db")
    row = conn.execute("PRAGMA table_info(rules)").fetchall()
    col_names = {r[1] for r in row}
    expected = {
        "exceptions",
        "counterexamples",
        "override_count",
        "last_override_reason",
        "confidence",
        "last_confirmed_valid",
    }
    assert expected.issubset(col_names), f"Missing columns: {expected - col_names}"


def test_ext2_migration_idempotent(tmp_path: Path) -> None:
    """Calling init_memory_db twice on the same DB must not raise or lose data."""
    db_path = tmp_path / "idem.db"
    conn1 = init_memory_db(db_path)
    rule = _make_rule("R-IDEM")
    store_rule(conn1, rule)
    conn1.close()

    conn2 = init_memory_db(db_path)
    rules = load_active_rules(conn2, status="active")
    assert any(r.rule_id == "R-IDEM" for r in rules)


def test_ext2_migration_on_old_db(tmp_path: Path) -> None:
    """EXT2 columns must be added to a DB created without them (old schema)."""
    db_path = tmp_path / "old.db"
    # Create rules table WITHOUT EXT2 columns (old schema)
    conn = init_db(db_path)
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
    conn.commit()

    # Migrate — should add EXT2 columns without raising
    conn = init_memory_db(db_path)
    row = conn.execute("PRAGMA table_info(rules)").fetchall()
    col_names = {r[1] for r in row}
    assert "confidence" in col_names
    assert "exceptions" in col_names


# ---------------------------------------------------------------------------
# EXT2-T02: Rule dataclass new fields
# ---------------------------------------------------------------------------


def test_rule_dataclass_defaults() -> None:
    """Rule dataclass must expose EXT2 fields with correct defaults."""
    rule = Rule(
        rule_id="R-DEF",
        created_at="2026-01-01T00:00:00+00:00",
        human_rule="test rule",
    )
    assert rule.exceptions == []
    assert rule.counterexamples == []
    assert rule.override_count == 0
    assert rule.last_override_reason is None
    assert rule.confidence == 0.8
    assert rule.last_confirmed_valid is None


def test_rule_roundtrip_with_new_fields(db: sqlite3.Connection) -> None:
    """store_rule then load_active_rules must roundtrip all EXT2 fields."""
    rule = _make_rule(
        rule_id="R-RT",
        confidence=0.65,
        override_count=2,
        last_override_reason="test override",
        exceptions=["in test files"],
        counterexamples=["example A"],
    )
    store_rule(db, rule)
    loaded = load_active_rules(db, status="active")
    assert len(loaded) == 1
    r = loaded[0]
    assert r.rule_id == "R-RT"
    assert abs(r.confidence - 0.65) < 1e-9
    assert r.override_count == 2
    assert r.last_override_reason == "test override"
    assert r.exceptions == ["in test files"]
    assert r.counterexamples == ["example A"]


def test_rule_roundtrip_without_new_fields_uses_defaults(db: sqlite3.Connection) -> None:
    """A rule stored without EXT2 fields must load with safe defaults."""
    # Use raw SQL to insert a rule in the old style (no EXT2 cols specified).
    # The DEFAULT expressions on the columns handle the values.
    db.execute(
        """
        INSERT INTO rules (rule_id, created_at, human_rule, status, trigger_count)
        VALUES ('R-OLD', '2026-01-01T00:00:00+00:00', 'old rule', 'active', 0)
        """
    )
    db.commit()
    rules = load_active_rules(db, status="active")
    r = next(x for x in rules if x.rule_id == "R-OLD")
    assert r.exceptions == []
    assert r.counterexamples == []
    assert r.override_count == 0
    assert r.confidence == pytest.approx(0.8, abs=1e-6)
    assert r.last_confirmed_valid is None


# ---------------------------------------------------------------------------
# record_rule_override
# ---------------------------------------------------------------------------


def test_record_rule_override_decrements_confidence(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """record_rule_override must reduce confidence by 0.1 and increment override_count."""
    conn, rule = rule_in_db
    record_rule_override(conn, rule.rule_id, reason="false positive in CI")

    row = conn.execute(
        "SELECT confidence, override_count, last_override_reason FROM rules WHERE rule_id = ?",
        (rule.rule_id,),
    ).fetchone()
    assert abs(row[0] - 0.7) < 1e-9, f"Expected 0.7, got {row[0]}"
    assert row[1] == 1
    assert row[2] == "false positive in CI"


def test_record_rule_override_min_bound(db: sqlite3.Connection) -> None:
    """record_rule_override must floor confidence at 0.1 (never below)."""
    rule = _make_rule("R-MINB", confidence=0.15)
    store_rule(db, rule)

    # Two overrides: 0.15 - 0.1 = 0.05 (clamped to 0.1), then 0.1 - 0.1 = 0.0 (clamped to 0.1)
    record_rule_override(db, "R-MINB", reason="first")
    record_rule_override(db, "R-MINB", reason="second")

    row = db.execute(
        "SELECT confidence FROM rules WHERE rule_id = 'R-MINB'"
    ).fetchone()
    assert row[0] >= 0.1, f"Confidence below minimum: {row[0]}"
    assert abs(row[0] - 0.1) < 1e-9


def test_record_rule_override_unknown_rule_is_noop(db: sqlite3.Connection) -> None:
    """record_rule_override on a non-existent rule must not raise."""
    record_rule_override(db, "NONEXISTENT", reason="test")  # must not raise


# ---------------------------------------------------------------------------
# record_rule_confirmation
# ---------------------------------------------------------------------------


def test_record_rule_confirmation_increments(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """record_rule_confirmation must increase confidence by 0.05."""
    conn, rule = rule_in_db
    record_rule_confirmation(conn, rule.rule_id)

    row = conn.execute(
        "SELECT confidence, last_confirmed_valid FROM rules WHERE rule_id = ?",
        (rule.rule_id,),
    ).fetchone()
    assert abs(row[0] - 0.85) < 1e-9, f"Expected 0.85, got {row[0]}"
    assert row[1] is not None  # timestamp set


def test_record_rule_confirmation_max_bound(db: sqlite3.Connection) -> None:
    """record_rule_confirmation must cap confidence at 1.0."""
    rule = _make_rule("R-MAXB", confidence=0.98)
    store_rule(db, rule)

    record_rule_confirmation(db, "R-MAXB")  # 0.98 + 0.05 = 1.0 (capped)
    record_rule_confirmation(db, "R-MAXB")  # 1.0 + 0.05 = still 1.0

    row = db.execute(
        "SELECT confidence FROM rules WHERE rule_id = 'R-MAXB'"
    ).fetchone()
    assert row[0] <= 1.0, f"Confidence above maximum: {row[0]}"
    assert abs(row[0] - 1.0) < 1e-9


def test_record_rule_confirmation_unknown_rule_is_noop(db: sqlite3.Connection) -> None:
    """record_rule_confirmation on a non-existent rule must not raise."""
    record_rule_confirmation(db, "NONEXISTENT")  # must not raise


# ---------------------------------------------------------------------------
# add_rule_exception
# ---------------------------------------------------------------------------


def test_add_rule_exception_appends(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """add_rule_exception must append a new exception text."""
    conn, rule = rule_in_db
    add_rule_exception(conn, rule.rule_id, "allowed in test files")

    row = conn.execute(
        "SELECT exceptions FROM rules WHERE rule_id = ?", (rule.rule_id,)
    ).fetchone()
    exceptions = json.loads(row[0])
    assert "allowed in test files" in exceptions


def test_add_rule_exception_deduplicates(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """add_rule_exception must not insert the same text twice."""
    conn, rule = rule_in_db
    add_rule_exception(conn, rule.rule_id, "dup exception")
    add_rule_exception(conn, rule.rule_id, "dup exception")

    row = conn.execute(
        "SELECT exceptions FROM rules WHERE rule_id = ?", (rule.rule_id,)
    ).fetchone()
    exceptions = json.loads(row[0])
    assert exceptions.count("dup exception") == 1


def test_add_rule_exception_multiple(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """add_rule_exception must accumulate distinct exceptions."""
    conn, rule = rule_in_db
    add_rule_exception(conn, rule.rule_id, "exception A")
    add_rule_exception(conn, rule.rule_id, "exception B")

    row = conn.execute(
        "SELECT exceptions FROM rules WHERE rule_id = ?", (rule.rule_id,)
    ).fetchone()
    exceptions = json.loads(row[0])
    assert "exception A" in exceptions
    assert "exception B" in exceptions
    assert len(exceptions) == 2


def test_add_rule_exception_unknown_rule_is_noop(db: sqlite3.Connection) -> None:
    """add_rule_exception on a non-existent rule must not raise."""
    add_rule_exception(db, "NONEXISTENT", "test")  # must not raise


# ---------------------------------------------------------------------------
# add_rule_counterexample
# ---------------------------------------------------------------------------


def test_add_rule_counterexample_appends(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """add_rule_counterexample must append a new counterexample text."""
    conn, rule = rule_in_db
    add_rule_counterexample(conn, rule.rule_id, "false positive in subprocess test")

    row = conn.execute(
        "SELECT counterexamples FROM rules WHERE rule_id = ?", (rule.rule_id,)
    ).fetchone()
    counterexamples = json.loads(row[0])
    assert "false positive in subprocess test" in counterexamples


def test_add_rule_counterexample_deduplicates(
    rule_in_db: tuple[sqlite3.Connection, Rule],
) -> None:
    """add_rule_counterexample must not insert the same text twice."""
    conn, rule = rule_in_db
    add_rule_counterexample(conn, rule.rule_id, "dup counter")
    add_rule_counterexample(conn, rule.rule_id, "dup counter")

    row = conn.execute(
        "SELECT counterexamples FROM rules WHERE rule_id = ?", (rule.rule_id,)
    ).fetchone()
    counterexamples = json.loads(row[0])
    assert counterexamples.count("dup counter") == 1


def test_add_rule_counterexample_unknown_rule_is_noop(db: sqlite3.Connection) -> None:
    """add_rule_counterexample on a non-existent rule must not raise."""
    add_rule_counterexample(db, "NONEXISTENT", "test")  # must not raise


# ---------------------------------------------------------------------------
# get_low_confidence_rules
# ---------------------------------------------------------------------------


def test_get_low_confidence_rules_filters_correctly(db: sqlite3.Connection) -> None:
    """get_low_confidence_rules must return only rules below the threshold."""
    store_rule(db, _make_rule("R-LOW1", confidence=0.2))
    store_rule(db, _make_rule("R-LOW2", confidence=0.15))
    store_rule(db, _make_rule("R-HIGH", confidence=0.9))
    store_rule(db, _make_rule("R-MED", confidence=0.5))

    low = get_low_confidence_rules(db, threshold=0.3)
    ids = {r.rule_id for r in low}
    assert "R-LOW1" in ids
    assert "R-LOW2" in ids
    assert "R-HIGH" not in ids
    assert "R-MED" not in ids


def test_get_low_confidence_rules_default_threshold(db: sqlite3.Connection) -> None:
    """Default threshold of 0.3 must be used when not specified."""
    store_rule(db, _make_rule("R-BELOW", confidence=0.25))
    store_rule(db, _make_rule("R-ABOVE", confidence=0.35))

    low = get_low_confidence_rules(db)
    ids = {r.rule_id for r in low}
    assert "R-BELOW" in ids
    assert "R-ABOVE" not in ids


def test_get_low_confidence_rules_empty_db(db: sqlite3.Connection) -> None:
    """get_low_confidence_rules on an empty DB must return empty list, not raise."""
    result = get_low_confidence_rules(db)
    assert result == []


def test_get_low_confidence_rules_none_below_threshold(db: sqlite3.Connection) -> None:
    """get_low_confidence_rules returns empty list when nothing qualifies."""
    store_rule(db, _make_rule("R-ALL-HIGH", confidence=0.9))
    result = get_low_confidence_rules(db, threshold=0.3)
    assert result == []


def test_get_low_confidence_rules_sorted_ascending(db: sqlite3.Connection) -> None:
    """get_low_confidence_rules must return results ordered by confidence ascending."""
    store_rule(db, _make_rule("R-C02", confidence=0.2))
    store_rule(db, _make_rule("R-C01", confidence=0.1))
    store_rule(db, _make_rule("R-C025", confidence=0.25))

    low = get_low_confidence_rules(db, threshold=0.3)
    confidences = [r.confidence for r in low]
    assert confidences == sorted(confidences)


# ---------------------------------------------------------------------------
# load_active_rules backward compat with old DB (no EXT2 columns)
# ---------------------------------------------------------------------------


def test_load_active_rules_old_schema_backward_compat(tmp_path: Path) -> None:
    """load_active_rules must work on a DB that has no EXT2 columns."""
    db_path = tmp_path / "old_schema.db"
    # Build an old-style rules table without EXT2 columns
    conn = init_db(db_path)
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
        "INSERT INTO rules (rule_id, created_at, human_rule, status, trigger_count) "
        "VALUES ('R-COMPAT', '2026-01-01T00:00:00+00:00', 'old rule', 'active', 0)"
    )
    conn.commit()

    # load_active_rules should not raise even though EXT2 columns are absent
    rules = load_active_rules(conn, status="active")
    assert len(rules) == 1
    r = rules[0]
    assert r.rule_id == "R-COMPAT"
    # EXT2 fields must fall back to safe defaults
    assert r.exceptions == []
    assert r.counterexamples == []
    assert r.override_count == 0
    assert r.confidence == pytest.approx(0.8, abs=1e-6)
    assert r.last_confirmed_valid is None


# ---------------------------------------------------------------------------
# Integration: override then confirm cycles confidence correctly
# ---------------------------------------------------------------------------


def test_override_then_confirm_cycles(db: sqlite3.Connection) -> None:
    """Overriding then confirming should adjust confidence in the right direction."""
    rule = _make_rule("R-CYCLE", confidence=0.8)
    store_rule(db, rule)

    record_rule_override(db, "R-CYCLE", reason="looked like FP")
    # confidence = 0.7
    record_rule_override(db, "R-CYCLE", reason="confirmed FP again")
    # confidence = 0.6
    record_rule_confirmation(db, "R-CYCLE")
    # confidence = 0.65

    row = db.execute(
        "SELECT confidence, override_count FROM rules WHERE rule_id = 'R-CYCLE'"
    ).fetchone()
    assert abs(row[0] - 0.65) < 1e-9, f"Expected 0.65, got {row[0]}"
    assert row[1] == 2
