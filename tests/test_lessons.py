"""Tests for the lessons database (U7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from saturnday.run.lessons import (
    Lesson,
    format_lessons_for_prompt,
    init_db,
    load_lessons,
    store_lesson,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Open an in-memory lessons DB in a temp directory."""
    return init_db(tmp_path / "lessons.db")


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_creates_table(tmp_path: Path) -> None:
    """init_db must create the lessons table and the rule index."""
    conn = init_db(tmp_path / "subdir" / "lessons.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "lessons" in tables


def test_init_db_idempotent(tmp_path: Path) -> None:
    """Calling init_db twice on the same path must not raise."""
    path = tmp_path / "lessons.db"
    c1 = init_db(path)
    c1.close()
    c2 = init_db(path)
    c2.close()  # no exception → test passes


def test_init_db_creates_parent_dirs(tmp_path: Path) -> None:
    """init_db must create missing parent directories."""
    deep_path = tmp_path / "a" / "b" / "c" / "lessons.db"
    conn = init_db(deep_path)
    assert deep_path.exists()
    conn.close()


# ---------------------------------------------------------------------------
# store_lesson + load_lessons roundtrip
# ---------------------------------------------------------------------------


def test_store_and_load_roundtrip(db: sqlite3.Connection) -> None:
    """A stored lesson must be returned by load_lessons."""
    lesson = Lesson(
        project_id="proj-x",
        ticket_id="T1",
        failure_type="governance_fail",
        rule="SEC-001",
        description="Hardcoded secret detected in api_key field",
    )
    store_lesson(db, lesson)
    results = load_lessons(db, project_id="proj-x")
    assert len(results) == 1
    assert results[0].rule == "SEC-001"
    assert results[0].ticket_id == "T1"
    assert results[0].failure_type == "governance_fail"
    assert results[0].description == "Hardcoded secret detected in api_key field"
    assert results[0].confidence == 1


def test_load_lessons_empty_db(db: sqlite3.Connection) -> None:
    """load_lessons on an empty DB must return an empty list."""
    assert load_lessons(db) == []


def test_load_lessons_filters_by_project(db: sqlite3.Connection) -> None:
    """load_lessons must only return lessons for the requested project_id."""
    store_lesson(db, Lesson(
        project_id="proj-a", ticket_id="T1", failure_type="governance_fail",
        rule="SEC-001", description="desc-a",
    ))
    store_lesson(db, Lesson(
        project_id="proj-b", ticket_id="T2", failure_type="post_check_fail",
        rule="STYLE-002", description="desc-b",
    ))
    results_a = load_lessons(db, project_id="proj-a")
    results_b = load_lessons(db, project_id="proj-b")
    assert len(results_a) == 1 and results_a[0].project_id == "proj-a"
    assert len(results_b) == 1 and results_b[0].project_id == "proj-b"


def test_load_lessons_all_projects(db: sqlite3.Connection) -> None:
    """load_lessons with no project_id filter returns all lessons."""
    store_lesson(db, Lesson(
        project_id="proj-a", ticket_id="T1", failure_type="governance_fail",
        rule="R1", description="d1",
    ))
    store_lesson(db, Lesson(
        project_id="proj-b", ticket_id="T2", failure_type="governance_fail",
        rule="R2", description="d2",
    ))
    assert len(load_lessons(db)) == 2


# ---------------------------------------------------------------------------
# Confidence increment on duplicate rule
# ---------------------------------------------------------------------------


def test_confidence_increments_on_duplicate_rule(db: sqlite3.Connection) -> None:
    """Storing the same rule for the same project must increment confidence."""
    lesson = Lesson(
        project_id="proj-x",
        ticket_id="T1",
        failure_type="governance_fail",
        rule="SEC-001",
        description="First occurrence",
    )
    store_lesson(db, lesson)
    # Store again — same project_id + rule
    lesson2 = Lesson(
        project_id="proj-x",
        ticket_id="T2",
        failure_type="governance_fail",
        rule="SEC-001",
        description="Second occurrence — updated description",
    )
    store_lesson(db, lesson2)

    results = load_lessons(db, project_id="proj-x")
    assert len(results) == 1  # deduplication
    assert results[0].confidence == 2
    assert results[0].description == "Second occurrence — updated description"


def test_different_rules_do_not_merge(db: sqlite3.Connection) -> None:
    """Two different rules for the same project must create two records."""
    store_lesson(db, Lesson(
        project_id="proj-x", ticket_id="T1", failure_type="governance_fail",
        rule="SEC-001", description="first",
    ))
    store_lesson(db, Lesson(
        project_id="proj-x", ticket_id="T2", failure_type="governance_fail",
        rule="SEC-002", description="second",
    ))
    results = load_lessons(db, project_id="proj-x")
    assert len(results) == 2


def test_same_rule_different_projects_do_not_merge(db: sqlite3.Connection) -> None:
    """The same rule across different projects must not merge."""
    for pid in ("proj-a", "proj-b"):
        store_lesson(db, Lesson(
            project_id=pid, ticket_id="T1", failure_type="governance_fail",
            rule="SEC-001", description="desc",
        ))
    # Trigger second occurrence for proj-a only
    store_lesson(db, Lesson(
        project_id="proj-a", ticket_id="T2", failure_type="governance_fail",
        rule="SEC-001", description="desc2",
    ))
    a_lessons = load_lessons(db, project_id="proj-a")
    b_lessons = load_lessons(db, project_id="proj-b")
    assert a_lessons[0].confidence == 2
    assert b_lessons[0].confidence == 1


def test_min_confidence_filter(db: sqlite3.Connection) -> None:
    """load_lessons with min_confidence must exclude low-confidence lessons."""
    store_lesson(db, Lesson(
        project_id="p", ticket_id="T1", failure_type="governance_fail",
        rule="SEC-001", description="once",
    ))
    # Store twice for SEC-002
    for _ in range(2):
        store_lesson(db, Lesson(
            project_id="p", ticket_id="T2", failure_type="governance_fail",
            rule="SEC-002", description="repeated",
        ))
    all_results = load_lessons(db, project_id="p")
    high_conf = load_lessons(db, project_id="p", min_confidence=2)
    assert len(all_results) == 2
    assert len(high_conf) == 1
    assert high_conf[0].rule == "SEC-002"


def test_load_lessons_sorted_by_confidence_desc(db: sqlite3.Connection) -> None:
    """Results must be sorted highest confidence first."""
    for i, rule in enumerate(("R1", "R2", "R3")):
        for _ in range(3 - i):  # R1=3x, R2=2x, R3=1x
            store_lesson(db, Lesson(
                project_id="p", ticket_id="T", failure_type="governance_fail",
                rule=rule, description="desc",
            ))
    results = load_lessons(db, project_id="p")
    confidences = [r.confidence for r in results]
    assert confidences == sorted(confidences, reverse=True)


# ---------------------------------------------------------------------------
# format_lessons_for_prompt
# ---------------------------------------------------------------------------


def test_format_lessons_for_prompt_empty() -> None:
    """Empty lessons list must return empty string."""
    assert format_lessons_for_prompt([]) == ""


def test_format_lessons_for_prompt_nonempty() -> None:
    """Non-empty lessons must produce a header and one bullet per lesson."""
    lessons = [
        Lesson(
            project_id="p", ticket_id="T1", failure_type="governance_fail",
            rule="SEC-001", description="Hardcoded secret", confidence=3,
        ),
        Lesson(
            project_id="p", ticket_id="T2", failure_type="post_check_fail",
            rule="STYLE-002", description="Missing docstring", confidence=1,
        ),
    ]
    output = format_lessons_for_prompt(lessons)
    assert output.startswith("LESSONS FROM PAST FAILURES")
    assert "SEC-001" in output
    assert "STYLE-002" in output
    assert "(seen 3x)" in output
    assert "(seen 1x)" in output
    assert "[governance_fail]" in output
    assert "[post_check_fail]" in output


def test_format_lessons_for_prompt_caps_at_10() -> None:
    """format_lessons_for_prompt must include at most 10 lessons."""
    lessons = [
        Lesson(
            project_id="p", ticket_id="T", failure_type="governance_fail",
            rule=f"RULE-{i:02d}", description=f"desc {i}", confidence=1,
        )
        for i in range(15)
    ]
    output = format_lessons_for_prompt(lessons)
    # Count bullet lines (each starts with "- [")
    bullets = [line for line in output.splitlines() if line.startswith("- [")]
    assert len(bullets) == 10
