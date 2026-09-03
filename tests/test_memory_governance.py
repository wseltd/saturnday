"""Tests for Extension 1: Memory Governance (dedup, contradiction, review).

Each test uses an isolated in-memory SQLite DB via tmp_path to avoid
cross-test state leakage.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from saturnday.run.lessons import (
    MemoryItem,
    Rule,
    detect_contradictions,
    find_duplicates,
    get_items_needing_review,
    init_memory_db,
    mark_reviewed,
    merge_duplicates,
    run_dedup_pass,
    store_memory_item,
    store_rule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _make_item(
    item_id: str,
    *,
    failure_mode: str | None = "timeout",
    root_cause: str | None = "slow_network",
    files_touched: list[str] | None = None,
    severity: str = "medium",
    risk_tags: list[str] | None = None,
    created_at: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        memory_type="lesson",
        created_at=created_at or _now_iso(),
        failure_mode=failure_mode,
        root_cause=root_cause,
        files_touched=files_touched or ["src/foo.py"],
        severity=severity,
        risk_tags=risk_tags or [],
    )


def _make_rule(
    rule_id: str,
    *,
    forbidden_patterns: list[str] | None = None,
    required_patterns: list[str] | None = None,
    file_patterns: list[str] | None = None,
    status: str = "active",
) -> Rule:
    trigger_conditions: dict = {}
    if file_patterns:
        trigger_conditions["file_patterns"] = file_patterns
    return Rule(
        rule_id=rule_id,
        created_at=_now_iso(),
        human_rule=f"test rule {rule_id}",
        forbidden_patterns=forbidden_patterns or [],
        required_patterns=required_patterns or [],
        trigger_conditions=trigger_conditions,
        status=status,
    )


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    return init_memory_db(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# EXT1-T02 — find_duplicates
# ---------------------------------------------------------------------------

class TestFindDuplicates:
    def test_find_duplicates_groups_matching(self, tmp_path: Path) -> None:
        """Two items with same failure_mode, root_cause, overlapping files are grouped."""
        conn = _open_db(tmp_path)
        item_a = _make_item("a1", files_touched=["src/foo.py", "src/bar.py"])
        item_b = _make_item("a2", files_touched=["src/foo.py"])
        store_memory_item(conn, item_a)
        store_memory_item(conn, item_b)

        groups = find_duplicates(conn)
        assert len(groups) == 1
        ids_in_group = {i.item_id for i in groups[0]}
        assert ids_in_group == {"a1", "a2"}

    def test_find_duplicates_no_matches(self, tmp_path: Path) -> None:
        """Items with different failure_modes produce no groups."""
        conn = _open_db(tmp_path)
        item_a = _make_item("b1", failure_mode="timeout")
        item_b = _make_item("b2", failure_mode="auth_error")
        store_memory_item(conn, item_a)
        store_memory_item(conn, item_b)

        groups = find_duplicates(conn)
        assert groups == []

    def test_find_duplicates_different_root_causes_no_group(self, tmp_path: Path) -> None:
        """Items with same failure_mode but different root_cause are not grouped."""
        conn = _open_db(tmp_path)
        item_a = _make_item("c1", failure_mode="timeout", root_cause="slow_network")
        item_b = _make_item("c2", failure_mode="timeout", root_cause="dns_failure")
        store_memory_item(conn, item_a)
        store_memory_item(conn, item_b)

        groups = find_duplicates(conn)
        assert groups == []

    def test_find_duplicates_no_file_overlap_no_group(self, tmp_path: Path) -> None:
        """Same failure_mode + root_cause but no file overlap — no group."""
        conn = _open_db(tmp_path)
        item_a = _make_item("d1", files_touched=["src/foo.py"])
        item_b = _make_item("d2", files_touched=["src/bar.py"])
        store_memory_item(conn, item_a)
        store_memory_item(conn, item_b)

        groups = find_duplicates(conn)
        assert groups == []

    def test_find_duplicates_empty_db(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        assert find_duplicates(conn) == []


# ---------------------------------------------------------------------------
# EXT1-T02 — merge_duplicates
# ---------------------------------------------------------------------------

class TestMergeDuplicates:
    def test_merge_duplicates_keeps_newest(self, tmp_path: Path) -> None:
        """The most recently created item is kept; older is superseded."""
        conn = _open_db(tmp_path)
        older = _make_item("old1", created_at=_ago_iso(10))
        newer = _make_item("new1", created_at=_now_iso())
        store_memory_item(conn, older)
        store_memory_item(conn, newer)

        kept = merge_duplicates(conn, [older, newer])
        assert kept.item_id == "new1"

        # Verify DB state
        row = conn.execute(
            "SELECT status FROM memory_items WHERE item_id='old1'"
        ).fetchone()
        assert row[0] == "superseded"

    def test_merge_duplicates_unions_tags(self, tmp_path: Path) -> None:
        """Kept item receives union of risk_tags from both items."""
        conn = _open_db(tmp_path)
        older = _make_item(
            "old2", created_at=_ago_iso(5), risk_tags=["shell_exec"]
        )
        newer = _make_item(
            "new2", created_at=_now_iso(), risk_tags=["security"]
        )
        store_memory_item(conn, older)
        store_memory_item(conn, newer)

        kept = merge_duplicates(conn, [older, newer])
        assert "shell_exec" in kept.risk_tags
        assert "security" in kept.risk_tags

    def test_merge_duplicates_upgrades_severity(self, tmp_path: Path) -> None:
        """If merged item has higher severity, kept item's severity is upgraded."""
        conn = _open_db(tmp_path)
        older = _make_item("old3", created_at=_ago_iso(5), severity="critical")
        newer = _make_item("new3", created_at=_now_iso(), severity="low")
        store_memory_item(conn, older)
        store_memory_item(conn, newer)

        kept = merge_duplicates(conn, [older, newer])
        assert kept.severity == "critical"

    def test_merge_duplicates_sets_merged_from(self, tmp_path: Path) -> None:
        """merged_from column on kept item lists the merged item_ids."""
        conn = _open_db(tmp_path)
        older = _make_item("old4", created_at=_ago_iso(3))
        newer = _make_item("new4", created_at=_now_iso())
        store_memory_item(conn, older)
        store_memory_item(conn, newer)

        merge_duplicates(conn, [older, newer])

        row = conn.execute(
            "SELECT merged_from FROM memory_items WHERE item_id='new4'"
        ).fetchone()
        merged_from = json.loads(row[0]) if row and row[0] else []
        assert "old4" in merged_from


# ---------------------------------------------------------------------------
# EXT1-T02 — run_dedup_pass
# ---------------------------------------------------------------------------

class TestRunDedupPass:
    def test_run_dedup_pass_returns_count(self, tmp_path: Path) -> None:
        """Returns count of items merged (not groups)."""
        conn = _open_db(tmp_path)
        for i in range(3):
            item = _make_item(
                f"dup{i}",
                files_touched=["src/shared.py"],
                created_at=_ago_iso(10 - i),
            )
            store_memory_item(conn, item)

        count = run_dedup_pass(conn)
        assert count == 2  # 3 items in 1 group → 2 merges

    def test_run_dedup_pass_limit(self, tmp_path: Path) -> None:
        """Limit parameter prevents processing groups once limit is reached.

        Two separate duplicate groups each produce (group_size - 1) merges.
        With limit=1, only the first group is processed and the second is skipped.
        """
        conn = _open_db(tmp_path)
        # Group A: 2 items with failure_mode='err_a', root_cause='rc_a'
        for i in range(2):
            item = _make_item(
                f"grp_a_{i}",
                failure_mode="err_a",
                root_cause="rc_a",
                files_touched=["src/alpha.py"],
                created_at=_ago_iso(100 - i),
            )
            store_memory_item(conn, item)
        # Group B: 2 items with failure_mode='err_b', root_cause='rc_b'
        for i in range(2):
            item = _make_item(
                f"grp_b_{i}",
                failure_mode="err_b",
                root_cause="rc_b",
                files_touched=["src/beta.py"],
                created_at=_ago_iso(100 - i),
            )
            store_memory_item(conn, item)

        # With limit=1, at most 1 merge should occur (first group only)
        count = run_dedup_pass(conn, limit=1)
        assert count <= 1

    def test_run_dedup_pass_no_duplicates_returns_zero(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        item = _make_item("solo1")
        store_memory_item(conn, item)
        assert run_dedup_pass(conn) == 0

    def test_run_dedup_pass_empty_db(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        assert run_dedup_pass(conn) == 0


# ---------------------------------------------------------------------------
# EXT1-T03 — detect_contradictions
# ---------------------------------------------------------------------------

class TestDetectContradictions:
    def test_detect_contradictions(self, tmp_path: Path) -> None:
        """Rule forbidding 'shell=True' contradicts rule requiring 'shell=True'."""
        conn = _open_db(tmp_path)
        rule_a = _make_rule("R-A", forbidden_patterns=["shell=True"])
        rule_b = _make_rule("R-B", required_patterns=["shell=True"])
        store_rule(conn, rule_a)
        store_rule(conn, rule_b)

        contradictions = detect_contradictions(conn)
        assert len(contradictions) >= 1
        conflict = contradictions[0]
        assert conflict["conflict_type"] == "forbidden_vs_required"
        assert "R-A" in (conflict["rule_a"], conflict["rule_b"])
        assert "R-B" in (conflict["rule_a"], conflict["rule_b"])

    def test_detect_contradictions_no_overlap(self, tmp_path: Path) -> None:
        """Rules with disjoint file_patterns produce no contradiction."""
        conn = _open_db(tmp_path)
        rule_a = _make_rule(
            "R-C",
            forbidden_patterns=["shell=True"],
            file_patterns=["src/*.py"],
        )
        rule_b = _make_rule(
            "R-D",
            required_patterns=["shell=True"],
            file_patterns=["tests/*.py"],
        )
        store_rule(conn, rule_a)
        store_rule(conn, rule_b)

        # Disjoint file_patterns — no overlap, no contradiction
        contradictions = detect_contradictions(conn)
        assert contradictions == []

    def test_detect_contradictions_empty_patterns_no_false_positive(
        self, tmp_path: Path
    ) -> None:
        """Empty forbidden/required patterns never produce contradictions."""
        conn = _open_db(tmp_path)
        rule_a = _make_rule("R-E", forbidden_patterns=[], required_patterns=[])
        rule_b = _make_rule("R-F", forbidden_patterns=[], required_patterns=[])
        store_rule(conn, rule_a)
        store_rule(conn, rule_b)

        assert detect_contradictions(conn) == []

    def test_detect_contradictions_empty_db(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        assert detect_contradictions(conn) == []

    def test_detect_contradictions_no_match_different_patterns(
        self, tmp_path: Path
    ) -> None:
        """Rules with non-overlapping patterns produce no contradiction."""
        conn = _open_db(tmp_path)
        rule_a = _make_rule("R-G", forbidden_patterns=["os.system"])
        rule_b = _make_rule("R-H", required_patterns=["subprocess.run"])
        store_rule(conn, rule_a)
        store_rule(conn, rule_b)

        # "os.system" is not a substring of "subprocess.run" and vice versa
        assert detect_contradictions(conn) == []


# ---------------------------------------------------------------------------
# EXT1-T04 — mark_reviewed / get_items_needing_review
# ---------------------------------------------------------------------------

class TestMarkReviewed:
    def test_mark_reviewed(self, tmp_path: Path) -> None:
        """mark_reviewed sets last_reviewed_at and review_status."""
        conn = _open_db(tmp_path)
        item = _make_item("rev1")
        store_memory_item(conn, item)

        mark_reviewed(conn, "rev1", review_status="confirmed")

        row = conn.execute(
            "SELECT review_status, last_reviewed_at FROM memory_items WHERE item_id='rev1'"
        ).fetchone()
        assert row[0] == "confirmed"
        assert row[1] is not None

    def test_mark_reviewed_invalid_status_raises(self, tmp_path: Path) -> None:
        """Invalid review_status raises ValueError."""
        conn = _open_db(tmp_path)
        item = _make_item("rev2")
        store_memory_item(conn, item)

        with pytest.raises(ValueError, match="Invalid review_status"):
            mark_reviewed(conn, "rev2", review_status="invalid_status")

    def test_mark_reviewed_all_valid_statuses(self, tmp_path: Path) -> None:
        """All valid review statuses are accepted without error."""
        conn = _open_db(tmp_path)
        for status in ("confirmed", "disputed", "expired"):
            item = _make_item(f"rev_{status}")
            store_memory_item(conn, item)
            mark_reviewed(conn, f"rev_{status}", review_status=status)
            row = conn.execute(
                "SELECT review_status FROM memory_items WHERE item_id=?",
                (f"rev_{status}",),
            ).fetchone()
            assert row[0] == status


class TestGetItemsNeedingReview:
    def test_get_items_needing_review_unreviewed(self, tmp_path: Path) -> None:
        """Items never reviewed (last_reviewed_at IS NULL) are returned."""
        conn = _open_db(tmp_path)
        item = _make_item("unreview1")
        store_memory_item(conn, item)

        needs_review = get_items_needing_review(conn, days_since_review=30)
        ids = [i.item_id for i in needs_review]
        assert "unreview1" in ids

    def test_get_items_needing_review_stale_review(self, tmp_path: Path) -> None:
        """Items reviewed 60 days ago are returned when threshold is 30 days."""
        conn = _open_db(tmp_path)
        item = _make_item("stale_rev1")
        store_memory_item(conn, item)

        # Manually set last_reviewed_at to 60 days ago
        old_ts = _ago_iso(60)
        conn.execute(
            "UPDATE memory_items SET last_reviewed_at=?, review_status='confirmed' "
            "WHERE item_id='stale_rev1'",
            (old_ts,),
        )
        conn.commit()

        needs_review = get_items_needing_review(conn, days_since_review=30)
        ids = [i.item_id for i in needs_review]
        assert "stale_rev1" in ids

    def test_get_items_needing_review_recent_review_excluded(
        self, tmp_path: Path
    ) -> None:
        """Items reviewed recently are NOT returned."""
        conn = _open_db(tmp_path)
        item = _make_item("fresh_rev1")
        store_memory_item(conn, item)
        mark_reviewed(conn, "fresh_rev1", review_status="confirmed")

        needs_review = get_items_needing_review(conn, days_since_review=30)
        ids = [i.item_id for i in needs_review]
        assert "fresh_rev1" not in ids

    def test_get_items_needing_review_empty_db(self, tmp_path: Path) -> None:
        conn = _open_db(tmp_path)
        assert get_items_needing_review(conn) == []

    def test_get_items_needing_review_severity_ordering(
        self, tmp_path: Path
    ) -> None:
        """Critical items appear before medium items in results."""
        conn = _open_db(tmp_path)
        medium_item = _make_item(
            "sev_med", severity="medium", created_at=_ago_iso(5)
        )
        critical_item = _make_item(
            "sev_crit", severity="critical", created_at=_ago_iso(1)
        )
        store_memory_item(conn, medium_item)
        store_memory_item(conn, critical_item)

        needs_review = get_items_needing_review(conn, days_since_review=30)
        assert len(needs_review) == 2
        assert needs_review[0].item_id == "sev_crit"


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------

class TestMigrationIdempotency:
    def test_migration_idempotent_on_existing_db(self, tmp_path: Path) -> None:
        """Calling init_memory_db twice on the same DB does not error or lose data."""
        db_path = tmp_path / "idem.db"
        conn = init_memory_db(db_path)
        item = _make_item("idem1")
        store_memory_item(conn, item)
        conn.close()

        # Second init — should not raise or drop data
        conn2 = init_memory_db(db_path)
        row = conn2.execute(
            "SELECT item_id FROM memory_items WHERE item_id='idem1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "idem1"

    def test_new_columns_exist_after_init(self, tmp_path: Path) -> None:
        """New governance columns exist on memory_items and rules after init."""
        conn = _open_db(tmp_path)

        # Check memory_items columns
        mi_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(memory_items)").fetchall()
        }
        for col in ("last_reviewed_at", "review_status", "dedup_group_id", "merged_from"):
            assert col in mi_cols, f"Column {col!r} missing from memory_items"

        # Check rules columns
        r_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(rules)").fetchall()
        }
        for col in ("last_reviewed_at", "review_status"):
            assert col in r_cols, f"Column {col!r} missing from rules"
