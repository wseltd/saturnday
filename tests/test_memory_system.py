"""Tests for the Phase 1 Memory System (v1.1.01).

Covers T003 (typed memory schema), T004 (extraction), T005 (candidate rule
generation), and T006 (lessons.md generation).  All existing test_lessons.py
tests must continue to pass — this file only adds new tests.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.run.lessons import (
    # Phase 1 API
    MemoryItem,
    Rule,
    generate_candidate_rule,
    generate_lessons_markdown,
    increment_rule_trigger,
    init_memory_db,
    load_active_items,
    load_active_rules,
    mark_stale,
    store_memory_item,
    store_rule,
    write_lessons_file,
    # Original API — must still work
    Lesson,
    init_db,
    store_lesson,
    load_lessons,
    format_lessons_for_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    item_id: str = "item-001",
    memory_type: str = "lesson",
    outcome: str = "FAIL",
    severity: str = "medium",
    risk_tags: list[str] | None = None,
    files_touched: list[str] | None = None,
    corrective_rule_text: str | None = None,
    failure_mode: str | None = None,
) -> MemoryItem:
    now = datetime.now(timezone.utc).isoformat()
    return MemoryItem(
        item_id=item_id,
        memory_type=memory_type,
        created_at=now,
        ticket_id="T-test",
        ticket_goal="Implement feature X",
        outcome=outcome,
        failure_mode=failure_mode or outcome,
        severity=severity,
        risk_tags=risk_tags or [],
        files_touched=files_touched or [],
        corrective_rule_text=corrective_rule_text,
    )


def _make_rule(
    rule_id: str = "RULE-001",
    status: str = "active",
    human_rule: str = "Do not use shell=True",
    severity: str = "high",
) -> Rule:
    now = datetime.now(timezone.utc).isoformat()
    return Rule(
        rule_id=rule_id,
        created_at=now,
        human_rule=human_rule,
        severity=severity,
        status=status,
        trigger_count=0,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_db(tmp_path: Path) -> sqlite3.Connection:
    """Open an in-memory Phase 1 DB in a temp directory."""
    return init_memory_db(tmp_path / "lessons.db")


@pytest.fixture()
def old_db(tmp_path: Path) -> sqlite3.Connection:
    """Simulate an existing (old-schema) lessons DB."""
    return init_db(tmp_path / "old_lessons.db")


# ---------------------------------------------------------------------------
# T003: init_memory_db creates all tables
# ---------------------------------------------------------------------------


def test_init_memory_db_creates_tables(tmp_path: Path) -> None:
    """init_memory_db must create lessons, memory_items, and rules tables."""
    conn = init_memory_db(tmp_path / "lessons.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "lessons" in tables, "Original lessons table must exist"
    assert "memory_items" in tables, "memory_items table must exist"
    assert "rules" in tables, "rules table must exist"


def test_init_memory_db_idempotent(tmp_path: Path) -> None:
    """Calling init_memory_db twice on the same path must not raise."""
    path = tmp_path / "lessons.db"
    c1 = init_memory_db(path)
    c1.close()
    c2 = init_memory_db(path)
    c2.close()  # no exception → pass


def test_init_memory_db_on_existing_old_schema(old_db: sqlite3.Connection, tmp_path: Path) -> None:
    """init_memory_db called on a DB that only has the lessons table must add
    the new tables without losing existing lesson rows."""
    store_lesson(old_db, Lesson(
        project_id="proj", ticket_id="T1", failure_type="governance_fail",
        rule="SEC-001", description="old lesson",
    ))
    db_path = tmp_path / "old_lessons.db"

    # Re-open with Phase 1 schema
    conn2 = init_memory_db(db_path)
    # Old lesson still accessible
    lessons = load_lessons(conn2, project_id="proj")
    assert len(lessons) == 1
    assert lessons[0].rule == "SEC-001"
    # New tables created
    tables = {
        row[0]
        for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "memory_items" in tables
    assert "rules" in tables


# ---------------------------------------------------------------------------
# T003: store_memory_item / load_active_items roundtrip
# ---------------------------------------------------------------------------


def test_store_and_load_memory_item(mem_db: sqlite3.Connection) -> None:
    """A stored MemoryItem must be returned by load_active_items."""
    item = _make_item(
        item_id="item-abc",
        memory_type="lesson",
        severity="high",
        risk_tags=["shell_exec", "security"],
        files_touched=["src/foo.py", "tests/test_foo.py"],
        corrective_rule_text="Never call subprocess with shell=True",
    )
    store_memory_item(mem_db, item)

    results = load_active_items(mem_db)
    assert len(results) == 1
    r = results[0]
    assert r.item_id == "item-abc"
    assert r.memory_type == "lesson"
    assert r.severity == "high"
    assert r.risk_tags == ["shell_exec", "security"]
    assert r.files_touched == ["src/foo.py", "tests/test_foo.py"]
    assert r.corrective_rule_text == "Never call subprocess with shell=True"
    assert r.status == "active"


def test_load_active_items_filters_by_type(mem_db: sqlite3.Connection) -> None:
    """load_active_items with memory_type kwarg must filter correctly."""
    store_memory_item(mem_db, _make_item("i1", memory_type="lesson"))
    store_memory_item(mem_db, _make_item("i2", memory_type="rule"))
    store_memory_item(mem_db, _make_item("i3", memory_type="invariant"))

    lessons = load_active_items(mem_db, memory_type="lesson")
    rules = load_active_items(mem_db, memory_type="rule")
    assert len(lessons) == 1 and lessons[0].memory_type == "lesson"
    assert len(rules) == 1 and rules[0].memory_type == "rule"


def test_load_active_items_excludes_non_active(mem_db: sqlite3.Connection) -> None:
    """Items with status != 'active' must not appear in load_active_items."""
    active = _make_item("i-active")
    stale = _make_item("i-stale")
    stale.status = "stale"

    store_memory_item(mem_db, active)
    store_memory_item(mem_db, stale)

    results = load_active_items(mem_db)
    assert len(results) == 1
    assert results[0].item_id == "i-active"


def test_store_memory_item_upsert(mem_db: sqlite3.Connection) -> None:
    """Storing the same item_id twice must update, not duplicate."""
    item = _make_item("dup-item", severity="low")
    store_memory_item(mem_db, item)

    item.severity = "critical"
    item.corrective_rule_text = "updated"
    store_memory_item(mem_db, item)

    results = load_active_items(mem_db)
    assert len(results) == 1
    assert results[0].severity == "critical"
    assert results[0].corrective_rule_text == "updated"


# ---------------------------------------------------------------------------
# T003: mark_stale
# ---------------------------------------------------------------------------


def test_mark_stale(mem_db: sqlite3.Connection) -> None:
    """mark_stale must set status='stale' on the specified item."""
    item = _make_item("stale-target")
    store_memory_item(mem_db, item)

    mark_stale(mem_db, "stale-target")

    results = load_active_items(mem_db)
    assert len(results) == 0  # no longer active

    # Confirm status in DB
    row = mem_db.execute(
        "SELECT status FROM memory_items WHERE item_id = ?", ("stale-target",)
    ).fetchone()
    assert row[0] == "stale"


def test_mark_stale_nonexistent_is_noop(mem_db: sqlite3.Connection) -> None:
    """mark_stale on a non-existent item_id must not raise."""
    mark_stale(mem_db, "does-not-exist")  # should not raise


# ---------------------------------------------------------------------------
# T003: store_rule / load_active_rules roundtrip
# ---------------------------------------------------------------------------


def test_store_and_load_rule(mem_db: sqlite3.Connection) -> None:
    """A stored Rule must be returned by load_active_rules."""
    rule = _make_rule(
        rule_id="RULE-shell-001",
        status="active",
        human_rule="Never pass commands as a string with shell=True",
        severity="high",
    )
    rule.forbidden_patterns = [r"subprocess\.run\(.*shell=True"]
    rule.trigger_conditions = {"risk_tags": ["shell_exec"]}
    rule.trigger_count = 3

    store_rule(mem_db, rule)

    results = load_active_rules(mem_db, status="active")
    assert len(results) == 1
    r = results[0]
    assert r.rule_id == "RULE-shell-001"
    assert r.human_rule == "Never pass commands as a string with shell=True"
    assert r.severity == "high"
    assert r.forbidden_patterns == [r"subprocess\.run\(.*shell=True"]
    assert r.trigger_conditions == {"risk_tags": ["shell_exec"]}
    assert r.trigger_count == 3


def test_load_active_rules_filters_by_status(mem_db: sqlite3.Connection) -> None:
    """load_active_rules must only return rules matching the given status."""
    store_rule(mem_db, _make_rule("R-active", status="active"))
    store_rule(mem_db, _make_rule("R-candidate", status="candidate"))
    store_rule(mem_db, _make_rule("R-enforced", status="enforced"))

    active = load_active_rules(mem_db, status="active")
    candidate = load_active_rules(mem_db, status="candidate")
    enforced = load_active_rules(mem_db, status="enforced")

    assert len(active) == 1 and active[0].rule_id == "R-active"
    assert len(candidate) == 1 and candidate[0].rule_id == "R-candidate"
    assert len(enforced) == 1 and enforced[0].rule_id == "R-enforced"


def test_store_rule_upsert(mem_db: sqlite3.Connection) -> None:
    """Storing the same rule_id twice must update, not duplicate."""
    store_rule(mem_db, _make_rule("R-dup", status="candidate"))
    r2 = _make_rule("R-dup", status="active")
    r2.human_rule = "Updated rule"
    store_rule(mem_db, r2)

    all_candidate = load_active_rules(mem_db, status="candidate")
    all_active = load_active_rules(mem_db, status="active")
    assert len(all_candidate) == 0  # status was updated
    assert len(all_active) == 1
    assert all_active[0].human_rule == "Updated rule"


# ---------------------------------------------------------------------------
# T003: increment_rule_trigger
# ---------------------------------------------------------------------------


def test_increment_rule_trigger(mem_db: sqlite3.Connection) -> None:
    """increment_rule_trigger must increment trigger_count by 1."""
    store_rule(mem_db, _make_rule("R-counter", status="active"))
    increment_rule_trigger(mem_db, "R-counter")
    increment_rule_trigger(mem_db, "R-counter")

    results = load_active_rules(mem_db, status="active")
    assert results[0].trigger_count == 2


def test_increment_rule_trigger_nonexistent_is_noop(mem_db: sqlite3.Connection) -> None:
    """increment_rule_trigger on non-existent rule_id must not raise."""
    increment_rule_trigger(mem_db, "phantom-rule")  # should not raise


# ---------------------------------------------------------------------------
# T004: _extract_lesson_from_outcome (tested via the function in ticket_runner)
# ---------------------------------------------------------------------------


def test_extract_lesson_from_fail_outcome(tmp_path: Path) -> None:
    """_extract_lesson_from_outcome must store a MemoryItem on FAIL."""
    from saturnday import capability_registry
    from saturnday.ticket_runner import _extract_lesson_from_outcome
    from saturnday._types import TicketSpec, TicketScope

    capability_registry.register("memory_provider", MagicMock())
    try:
        conn = init_memory_db(tmp_path / "lessons.db")
        ticket = TicketSpec(
            ticket_id="T-fail-01",
            goal="Implement auth module",
            acceptance_criteria=[],
            scope=TicketScope(allowed_globs=["src/auth.py"]),
        )
        findings = [
            {"message": "security: hardcoded credential found", "severity": "high"},
            {"message": "subprocess shell=True usage", "severity": "high"},
        ]
        _extract_lesson_from_outcome(
            memory_conn=conn,
            ticket=ticket,
            outcome="FAIL",
            attempt=3,
            findings=findings,
            changed_files=["src/auth.py", "tests/test_auth.py"],
            error_message="Governance failed after 3 attempts",
        )

        items = load_active_items(conn, memory_type="lesson")
        assert len(items) == 1
        item = items[0]
        assert item.ticket_id == "T-fail-01"
        assert item.outcome == "FAIL"
        assert "security" in item.risk_tags
        assert "shell_exec" in item.risk_tags
        assert "test_modification" in item.risk_tags
        assert item.severity in ("high", "critical")
        assert "src/auth.py" in item.files_touched
        assert "tests/test_auth.py" in item.files_touched
        assert item.stale_after is not None
    finally:
        capability_registry.clear()


def test_extract_lesson_from_ungoverned_outcome(tmp_path: Path) -> None:
    """_extract_lesson_from_outcome must store a MemoryItem on CODED_UNGOVERNED."""
    from saturnday import capability_registry
    from saturnday.ticket_runner import _extract_lesson_from_outcome
    from saturnday._types import TicketSpec, TicketScope

    capability_registry.register("memory_provider", MagicMock())
    try:
        conn = init_memory_db(tmp_path / "lessons.db")
        ticket = TicketSpec(
            ticket_id="T-ung-02",
            goal="Add Dockerfile",
            acceptance_criteria=[],
            scope=TicketScope(allowed_globs=["Dockerfile"]),
        )
        _extract_lesson_from_outcome(
            memory_conn=conn,
            ticket=ticket,
            outcome="CODED_UNGOVERNED",
            attempt=2,
            findings=[],
            changed_files=["Dockerfile"],
            error_message="Post-checks failed",
        )

        items = load_active_items(conn, memory_type="lesson")
        assert len(items) == 1
        item = items[0]
        assert item.outcome == "CODED_UNGOVERNED"
        assert "config_change" in item.risk_tags
        assert "dockerfile" in "".join(item.files_touched).lower()
    finally:
        capability_registry.clear()


def test_extract_lesson_noop_when_no_memory_conn() -> None:
    """_extract_lesson_from_outcome must be a no-op when memory_conn is None."""
    from saturnday.ticket_runner import _extract_lesson_from_outcome
    from saturnday._types import TicketSpec, TicketScope

    ticket = TicketSpec(
        ticket_id="T-noop",
        goal="something",
        acceptance_criteria=[],
        scope=TicketScope(),
    )
    # Should not raise
    _extract_lesson_from_outcome(
        memory_conn=None,
        ticket=ticket,
        outcome="FAIL",
        attempt=1,
        findings=[],
        changed_files=[],
        error_message="no memory conn",
    )


# ---------------------------------------------------------------------------
# T005: generate_candidate_rule
# ---------------------------------------------------------------------------


def test_generate_candidate_rule_shell_execution() -> None:
    """shell_exec risk tag must generate a candidate rule."""
    item = _make_item(
        "item-shell",
        severity="high",
        risk_tags=["shell_exec"],
        failure_mode="FAIL",
    )
    rule = generate_candidate_rule(item)
    assert rule is not None
    assert rule.status == "candidate"
    assert "shell" in rule.human_rule.lower() or "subprocess" in rule.human_rule.lower()
    assert rule.rule_id.startswith("AUTO-")
    assert len(rule.forbidden_patterns) > 0


def test_generate_candidate_rule_security_finding() -> None:
    """security risk tag must generate a candidate rule."""
    item = _make_item(
        "item-sec",
        severity="critical",
        risk_tags=["security"],
    )
    rule = generate_candidate_rule(item)
    assert rule is not None
    assert rule.status == "candidate"
    assert "secret" in rule.human_rule.lower() or "security" in rule.human_rule.lower()


def test_generate_candidate_rule_deleted_tests() -> None:
    """test_modification risk tag must generate a candidate rule."""
    item = _make_item(
        "item-test-del",
        severity="high",
        risk_tags=["test_modification"],
    )
    rule = generate_candidate_rule(item)
    assert rule is not None
    assert "test" in rule.human_rule.lower()


def test_generate_candidate_rule_none_for_minor() -> None:
    """Low severity item with no severe risk tags must return None."""
    item = _make_item(
        "item-minor",
        severity="low",
        risk_tags=["config_change"],
        failure_mode="FAIL",
    )
    result = generate_candidate_rule(item)
    assert result is None


def test_generate_candidate_rule_none_for_medium_no_tags() -> None:
    """Medium severity item with no qualifying risk tags must return None."""
    item = _make_item(
        "item-medium-plain",
        severity="medium",
        risk_tags=[],
        failure_mode="FAIL",
    )
    result = generate_candidate_rule(item)
    assert result is None


def test_generate_candidate_rule_critical_severity() -> None:
    """Critical severity alone (no risk tags) must generate a rule."""
    item = _make_item(
        "item-critical",
        severity="critical",
        risk_tags=[],
        corrective_rule_text="Do not hardcode secrets",
    )
    rule = generate_candidate_rule(item)
    assert rule is not None
    assert rule.severity == "critical"


def test_generate_candidate_rule_ungoverned_with_tags() -> None:
    """CODED_UNGOVERNED with any risk tag must generate a rule."""
    item = _make_item(
        "item-ung-tags",
        severity="low",
        risk_tags=["dependency_change"],
        failure_mode="CODED_UNGOVERNED",
        outcome="CODED_UNGOVERNED",
    )
    rule = generate_candidate_rule(item)
    assert rule is not None


# ---------------------------------------------------------------------------
# T006: generate_lessons_markdown / write_lessons_file
# ---------------------------------------------------------------------------


def test_generate_lessons_markdown_structure(mem_db: sqlite3.Connection) -> None:
    """generate_lessons_markdown must contain all five required section headers."""
    # Populate each section
    store_memory_item(mem_db, _make_item("i-lesson", memory_type="lesson"))
    store_memory_item(mem_db, _make_item("i-inv", memory_type="invariant"))
    store_memory_item(mem_db, _make_item("i-exc", memory_type="exception"))

    stale_item = _make_item("i-stale", memory_type="lesson")
    stale_item.status = "stale"
    store_memory_item(mem_db, stale_item)

    store_rule(mem_db, _make_rule("R-active", status="active"))
    store_rule(mem_db, _make_rule("R-enforced", status="enforced"))

    output = generate_lessons_markdown(mem_db)

    assert "## Active Rules" in output
    assert "## Do-Not-Touch Areas" in output
    assert "## Recent Lessons" in output
    assert "## Current Exceptions" in output
    assert "## Stale / Superseded Items" in output


def test_generate_lessons_markdown_empty_db(mem_db: sqlite3.Connection) -> None:
    """generate_lessons_markdown on an empty DB must render all sections with (none)."""
    output = generate_lessons_markdown(mem_db)

    assert "## Active Rules" in output
    assert "## Do-Not-Touch Areas" in output
    assert "## Recent Lessons" in output
    assert "## Current Exceptions" in output
    assert "## Stale / Superseded Items" in output
    assert output.count("_(none)_") == 5, (
        f"Expected 5 '(none)' entries, got: {output.count('_(none)_')}\n{output}"
    )


def test_generate_lessons_markdown_active_rules_appear(mem_db: sqlite3.Connection) -> None:
    """Active and enforced rules must appear in the Active Rules section."""
    store_rule(mem_db, _make_rule("R-active-001", status="active", human_rule="No shell=True"))
    store_rule(mem_db, _make_rule("R-enforced-002", status="enforced", human_rule="No secrets"))
    store_rule(mem_db, _make_rule("R-candidate-003", status="candidate", human_rule="Possible issue"))

    output = generate_lessons_markdown(mem_db)

    assert "R-active-001" in output
    assert "R-enforced-002" in output
    # Candidate rules should NOT appear in Active Rules section
    assert "R-candidate-003" not in output


def test_generate_lessons_markdown_lessons_capped_at_10(mem_db: sqlite3.Connection) -> None:
    """Recent Lessons section must contain at most 10 entries."""
    for i in range(15):
        store_memory_item(mem_db, _make_item(f"lesson-{i:03d}", memory_type="lesson"))

    output = generate_lessons_markdown(mem_db)

    # Count bullet entries under Recent Lessons
    lines = output.splitlines()
    in_section = False
    count = 0
    for line in lines:
        if line.startswith("## Recent Lessons"):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.startswith("- "):
            count += 1

    assert count <= 10, f"Expected <=10 lesson entries, got {count}"


def test_write_lessons_file(mem_db: sqlite3.Connection, tmp_path: Path) -> None:
    """write_lessons_file must create lessons.md in the specified directory."""
    store_memory_item(mem_db, _make_item("i-write-test", memory_type="lesson"))
    output_path = write_lessons_file(mem_db, tmp_path / "output")

    assert output_path.exists()
    assert output_path.name == "lessons.md"
    content = output_path.read_text(encoding="utf-8")
    assert "## Recent Lessons" in content
    assert "i-write-test" in content


def test_write_lessons_file_creates_output_dir(tmp_path: Path, mem_db: sqlite3.Connection) -> None:
    """write_lessons_file must create the output directory if it does not exist."""
    deep_dir = tmp_path / "new" / "nested" / "dir"
    assert not deep_dir.exists()
    output_path = write_lessons_file(mem_db, deep_dir)
    assert deep_dir.exists()
    assert output_path.exists()


# ---------------------------------------------------------------------------
# Backward compat: original Lesson API still works alongside Phase 1
# ---------------------------------------------------------------------------


def test_original_lessons_api_unchanged(tmp_path: Path) -> None:
    """Original store_lesson/load_lessons/format_lessons_for_prompt must work
    on a DB opened with init_memory_db."""
    conn = init_memory_db(tmp_path / "lessons.db")

    lesson = Lesson(
        project_id="proj-bc",
        ticket_id="T1",
        failure_type="governance_fail",
        rule="COMPAT-001",
        description="Backward compat test",
    )
    store_lesson(conn, lesson)
    results = load_lessons(conn, project_id="proj-bc")
    assert len(results) == 1
    assert results[0].rule == "COMPAT-001"

    prompt = format_lessons_for_prompt(results)
    assert "COMPAT-001" in prompt
    assert "LESSONS FROM PAST FAILURES" in prompt
