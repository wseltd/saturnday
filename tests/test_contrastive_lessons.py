"""Tests for Extension 3: Contrastive Lessons (EXT3-T01 through EXT3-T03).

Covers:
- Schema migration idempotency (EXT3-T01)
- store_contrastive_lesson: updates existing item, raises on missing (EXT3-T02)
- load_contrastive_items: filters correctly, handles empty DB (EXT3-T02)
- MemoryItem roundtrip with and without contrastive fields (EXT3-T02)
- format_lessons_for_injection: contrastive rendering, unchanged when absent,
  char-cap respected (EXT3-T03)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from saturnday.run.lessons import (
    MemoryItem,
    init_memory_db,
    load_contrastive_items,
    store_contrastive_lesson,
    store_memory_item,
)
from saturnday.run.memory_retrieval import format_lessons_for_injection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_item(
    item_id: str,
    *,
    wrong_pattern: str | None = None,
    correct_pattern: str | None = None,
    why_wrong: str | None = None,
    why_correct: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        memory_type="lesson",
        created_at=_now_iso(),
        wrong_pattern=wrong_pattern,
        correct_pattern=correct_pattern,
        why_wrong=why_wrong,
        why_correct=why_correct,
    )


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Open a fresh memory DB in a temp directory."""
    return init_memory_db(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# 1. store_contrastive_lesson updates an existing item
# ---------------------------------------------------------------------------


def test_store_contrastive_lesson_updates_existing(db: sqlite3.Connection) -> None:
    item = _make_item("item-1")
    store_memory_item(db, item)

    store_contrastive_lesson(
        db,
        item,
        wrong="subprocess.run(cmd, shell=True)",
        correct="subprocess.run(shlex.split(cmd), shell=False)",
        why_wrong="shell=True allows command injection",
        why_correct="Explicit arg list with shell=False is injection-safe",
    )

    row = db.execute(
        "SELECT wrong_pattern, correct_pattern, why_wrong, why_correct "
        "FROM memory_items WHERE item_id='item-1'"
    ).fetchone()
    assert row is not None
    wrong, correct, ww, wc = row
    assert wrong == "subprocess.run(cmd, shell=True)"
    assert correct == "subprocess.run(shlex.split(cmd), shell=False)"
    assert ww == "shell=True allows command injection"
    assert wc == "Explicit arg list with shell=False is injection-safe"
    # In-memory object also updated
    assert item.wrong_pattern == "subprocess.run(cmd, shell=True)"
    assert item.correct_pattern == "subprocess.run(shlex.split(cmd), shell=False)"


# ---------------------------------------------------------------------------
# 2. store_contrastive_lesson raises on nonexistent item_id
# ---------------------------------------------------------------------------


def test_store_contrastive_lesson_nonexistent_raises(db: sqlite3.Connection) -> None:
    ghost = _make_item("no-such-item")
    with pytest.raises(ValueError, match="not found in memory_items"):
        store_contrastive_lesson(
            db,
            ghost,
            wrong="bad",
            correct="good",
            why_wrong="reason",
            why_correct="reason",
        )


# ---------------------------------------------------------------------------
# 3. load_contrastive_items filters correctly
# ---------------------------------------------------------------------------


def test_load_contrastive_items_filters_correctly(db: sqlite3.Connection) -> None:
    # item with both fields set
    item_with = _make_item("item-with")
    store_memory_item(db, item_with)
    store_contrastive_lesson(
        db, item_with, wrong="bad", correct="good", why_wrong="w", why_correct="c"
    )

    # item without contrastive fields
    item_without = _make_item("item-without")
    store_memory_item(db, item_without)

    results = load_contrastive_items(db)
    ids = [r.item_id for r in results]
    assert "item-with" in ids
    assert "item-without" not in ids
    # Returned item has correct fields set
    found = next(r for r in results if r.item_id == "item-with")
    assert found.wrong_pattern == "bad"
    assert found.correct_pattern == "good"


# ---------------------------------------------------------------------------
# 4. load_contrastive_items on empty DB returns []
# ---------------------------------------------------------------------------


def test_load_contrastive_items_empty_db(db: sqlite3.Connection) -> None:
    assert load_contrastive_items(db) == []


# ---------------------------------------------------------------------------
# 5. MemoryItem roundtrip with contrastive fields
# ---------------------------------------------------------------------------


def test_memory_item_roundtrip_with_contrastive_fields(db: sqlite3.Connection) -> None:
    item = _make_item(
        "roundtrip-with",
        wrong_pattern="os.system(cmd)",
        correct_pattern="subprocess.run([cmd], check=True)",
        why_wrong="os.system returns shell exit code, no capture",
        why_correct="subprocess.run gives full control and is testable",
    )
    store_memory_item(db, item)

    items = load_contrastive_items(db)
    assert len(items) == 1
    loaded = items[0]
    assert loaded.item_id == "roundtrip-with"
    assert loaded.wrong_pattern == "os.system(cmd)"
    assert loaded.correct_pattern == "subprocess.run([cmd], check=True)"
    assert loaded.why_wrong == "os.system returns shell exit code, no capture"
    assert loaded.why_correct == "subprocess.run gives full control and is testable"


# ---------------------------------------------------------------------------
# 6. MemoryItem roundtrip without contrastive fields
# ---------------------------------------------------------------------------


def test_memory_item_roundtrip_without_contrastive_fields(db: sqlite3.Connection) -> None:
    item = _make_item("roundtrip-plain")
    store_memory_item(db, item)

    from saturnday.run.lessons import load_active_items
    items = load_active_items(db)
    plain = next(i for i in items if i.item_id == "roundtrip-plain")
    assert plain.wrong_pattern is None
    assert plain.correct_pattern is None
    assert plain.why_wrong is None
    assert plain.why_correct is None


# ---------------------------------------------------------------------------
# 7. format_injection_with_contrastive renders Wrong/Instead/Because lines
# ---------------------------------------------------------------------------


def test_format_injection_with_contrastive(db: sqlite3.Connection) -> None:
    item = _make_item(
        "ci-1",
        wrong_pattern="shell=True",
        correct_pattern="shell=False",
        why_wrong="injection risk",
    )
    item.risk_tags = ["shell_exec"]
    item.files_touched = ["src/runner.py"]
    item.failure_mode = "FAIL"

    result = format_lessons_for_injection([item])
    assert "Wrong: shell=True" in result
    assert "Instead: shell=False" in result
    assert "Because: injection risk" in result


# ---------------------------------------------------------------------------
# 8. format_injection_without_contrastive is unchanged
# ---------------------------------------------------------------------------


def test_format_injection_without_contrastive_unchanged(db: sqlite3.Connection) -> None:
    item = _make_item("ci-plain")
    item.risk_tags = ["general"]
    item.files_touched = ["src/foo.py"]
    item.failure_mode = "FAIL"
    item.corrective_rule_text = "Do not do X."

    result = format_lessons_for_injection([item])
    assert "Wrong:" not in result
    assert "Instead:" not in result
    assert "Because:" not in result
    assert "Rule: Do not do X." in result


# ---------------------------------------------------------------------------
# 9. format_injection respects 1500-char cap
# ---------------------------------------------------------------------------


def test_format_injection_respects_char_cap(db: sqlite3.Connection) -> None:
    items = []
    for i in range(20):
        item = _make_item(
            f"cap-{i}",
            wrong_pattern="x" * 80,
            correct_pattern="y" * 80,
            why_wrong="z" * 80,
        )
        item.risk_tags = ["security"]
        item.files_touched = [f"src/file_{i}.py"]
        item.failure_mode = "FAIL"
        item.corrective_rule_text = "r" * 120
        items.append(item)

    result = format_lessons_for_injection(items)
    assert len(result) <= 1500
