"""Tests for Phase 4 memory retrieval (T014, T015, T016).

Covers:
- T014: Hard filtering of memory items (filter_relevant_items)
- T015: Ranking and injection formatting (rank_and_select, format_lessons_for_injection)
- T016: Validation and staleness (validate_before_injection, run_staleness_cleanup)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from saturnday._types import TicketScope, TicketSpec
from saturnday.run.lessons import MemoryItem, init_memory_db, mark_stale, store_memory_item
from saturnday.run.memory_retrieval import (
    filter_relevant_items,
    format_lessons_for_injection,
    rank_and_select,
    run_staleness_cleanup,
    validate_before_injection,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_conn(tmp_path: Path):
    """In-memory DB for testing (uses a tmp file so we can close cleanly)."""
    db_path = tmp_path / "lessons.db"
    conn = init_memory_db(db_path)
    yield conn
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.isoformat()


def _future(days: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.isoformat()


def _make_item(
    item_id: str = "item-001",
    files_touched: list[str] | None = None,
    risk_tags: list[str] | None = None,
    failure_mode: str = "governance_fail",
    ticket_class: str | None = None,
    outcome: str = "FAIL",
    severity: str = "medium",
    status: str = "active",
    stale_after: str | None = None,
    corrective_rule_text: str | None = None,
    created_at: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        memory_type="lesson",
        created_at=created_at or _now(),
        ticket_id="T-test",
        ticket_goal="Test goal",
        ticket_class=ticket_class,
        outcome=outcome,
        failure_mode=failure_mode,
        severity=severity,
        risk_tags=risk_tags or [],
        files_touched=files_touched or [],
        corrective_rule_text=corrective_rule_text,
        stale_after=stale_after,
        status=status,
    )


def _make_ticket(
    allowed_globs: tuple[str, ...] = ("src/**/*.py",),
    goal: str = "Implement feature X",
    ticket_id: str = "T-001",
) -> TicketSpec:
    return TicketSpec(
        ticket_id=ticket_id,
        goal=goal,
        scope=TicketScope(allowed_globs=allowed_globs),
    )


# ---------------------------------------------------------------------------
# T014: filter_relevant_items
# ---------------------------------------------------------------------------


class TestFilterRelevantItems:
    def test_filter_relevant_by_file_overlap(self, mem_conn):
        """Items whose files_touched overlap with ticket scope globs are included."""
        item_match = _make_item(
            item_id="match",
            files_touched=["src/foo/bar.py"],
        )
        item_no_match = _make_item(
            item_id="no-match",
            files_touched=["unrelated/other.js"],
        )
        store_memory_item(mem_conn, item_match)
        store_memory_item(mem_conn, item_no_match)

        ticket = _make_ticket(allowed_globs=("src/**/*.py",))
        results = filter_relevant_items(mem_conn, ticket)
        ids = [r.item_id for r in results]

        assert "match" in ids

    def test_filter_relevant_by_risk_tags(self, mem_conn):
        """Items with overlapping risk tags are included even without file match."""
        item = _make_item(
            item_id="shell-item",
            files_touched=["scripts/deploy.sh"],
            risk_tags=["shell_exec"],
        )
        store_memory_item(mem_conn, item)

        # Ticket goal mentions 'subprocess' — inferred as shell_exec tag
        ticket = _make_ticket(
            allowed_globs=("src/**/*.py",),
            goal="Refactor the subprocess call to avoid shell=True",
        )
        results = filter_relevant_items(mem_conn, ticket)
        ids = [r.item_id for r in results]
        assert "shell-item" in ids

    def test_filter_relevant_excludes_stale(self, mem_conn):
        """Items with stale status are excluded and their status is NOT changed
        (they are already stale in the DB)."""
        stale_item = _make_item(
            item_id="stale-one",
            files_touched=["src/main.py"],
            status="stale",
        )
        stale_item_obj = MemoryItem(
            **{**stale_item.__dict__, "status": "stale"}
        )
        # Insert directly with stale status
        mem_conn.execute(
            "INSERT INTO memory_items (item_id, memory_type, created_at, "
            "ticket_id, ticket_goal, outcome, failure_mode, severity, "
            "finding_ids, files_touched, symbols_touched, risk_tags, "
            "root_cause, corrective_rule_text, evidence_refs, residual_required, "
            "stale_after, status, superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "stale-one", "lesson", _now(), "T", "goal", "FAIL",
                "governance_fail", "medium",
                "[]", json.dumps(["src/main.py"]), "[]", "[]",
                None, None, "[]", 0, None, "stale", None,
            ),
        )
        mem_conn.commit()

        ticket = _make_ticket(allowed_globs=("src/**/*.py",))
        results = filter_relevant_items(mem_conn, ticket)
        ids = [r.item_id for r in results]
        assert "stale-one" not in ids

    def test_filter_relevant_excludes_past_stale_after(self, mem_conn):
        """Items past their stale_after date are excluded and marked stale."""
        expired = _make_item(
            item_id="expired",
            files_touched=["src/main.py"],
            stale_after=_ago(5),  # 5 days ago
        )
        store_memory_item(mem_conn, expired)

        ticket = _make_ticket(allowed_globs=("src/**/*.py",))
        results = filter_relevant_items(mem_conn, ticket)
        ids = [r.item_id for r in results]
        assert "expired" not in ids

        # Verify marked stale in DB
        row = mem_conn.execute(
            "SELECT status FROM memory_items WHERE item_id = ?", ("expired",)
        ).fetchone()
        assert row[0] == "stale"

    def test_filter_relevant_fallback_on_no_matches(self, mem_conn):
        """When nothing matches hard criteria, fallback returns top-10 active items."""
        for i in range(3):
            item = _make_item(
                item_id=f"fallback-{i}",
                files_touched=["unrelated/other.rb"],
                risk_tags=[],
            )
            store_memory_item(mem_conn, item)

        # Ticket scope has no overlap with any stored items
        ticket = _make_ticket(
            allowed_globs=("completely/different/**",),
            goal="Do something with COBOL",
        )
        results = filter_relevant_items(mem_conn, ticket)
        # Fallback should return available active items
        assert len(results) > 0

    def test_filter_excludes_superseded_status(self, mem_conn):
        """Items with status=superseded are excluded."""
        mem_conn.execute(
            "INSERT INTO memory_items (item_id, memory_type, created_at, "
            "ticket_id, ticket_goal, outcome, failure_mode, severity, "
            "finding_ids, files_touched, symbols_touched, risk_tags, "
            "root_cause, corrective_rule_text, evidence_refs, residual_required, "
            "stale_after, status, superseded_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sup-001", "lesson", _now(), "T", "goal", "FAIL",
                "governance_fail", "medium",
                "[]", json.dumps(["src/main.py"]), "[]", "[]",
                None, None, "[]", 0, None, "superseded", None,
            ),
        )
        mem_conn.commit()

        ticket = _make_ticket(allowed_globs=("src/**/*.py",))
        results = filter_relevant_items(mem_conn, ticket)
        ids = [r.item_id for r in results]
        assert "sup-001" not in ids


# ---------------------------------------------------------------------------
# T015: rank_and_select + format_lessons_for_injection
# ---------------------------------------------------------------------------


class TestRankAndSelect:
    def test_rank_and_select_recency(self, mem_conn):
        """Newer items score higher and appear first."""
        old_item = _make_item(
            item_id="old",
            files_touched=["src/main.py"],
            created_at=_ago(60),
        )
        new_item = _make_item(
            item_id="new",
            files_touched=["src/main.py"],
            created_at=_ago(2),
        )
        ticket = _make_ticket()
        ranked = rank_and_select([old_item, new_item], ticket)
        assert ranked[0].item_id == "new"

    def test_rank_and_select_severity_boost(self, mem_conn):
        """Critical items score higher than medium items of the same recency."""
        medium_item = _make_item(
            item_id="medium",
            severity="medium",
            files_touched=["src/main.py"],
            created_at=_ago(5),
        )
        critical_item = _make_item(
            item_id="critical",
            severity="critical",
            files_touched=["src/main.py"],
            created_at=_ago(5),
        )
        ticket = _make_ticket()
        ranked = rank_and_select([medium_item, critical_item], ticket)
        assert ranked[0].item_id == "critical"

    def test_rank_and_select_limit(self, mem_conn):
        """rank_and_select never returns more than limit items."""
        items = [
            _make_item(item_id=f"i-{i}", files_touched=["src/a.py"])
            for i in range(10)
        ]
        ticket = _make_ticket()
        ranked = rank_and_select(items, ticket, limit=3)
        assert len(ranked) <= 3

    def test_rank_and_select_empty_input(self):
        """Empty input returns empty list."""
        ticket = _make_ticket()
        assert rank_and_select([], ticket) == []

    def test_rank_file_overlap_boosts_score(self):
        """Items with more overlapping files rank higher."""
        many_files = _make_item(
            item_id="many",
            files_touched=["src/foo.py", "src/bar.py"],
            created_at=_ago(10),
        )
        one_file = _make_item(
            item_id="one",
            files_touched=["src/foo.py"],
            created_at=_ago(10),
        )
        ticket = _make_ticket(allowed_globs=("src/*.py",))
        ranked = rank_and_select([one_file, many_files], ticket)
        assert ranked[0].item_id == "many"


class TestFormatLessonsForInjection:
    def test_format_lessons_for_injection_size(self):
        """Output stays under 1500 characters."""
        items = [
            _make_item(
                item_id=f"item-{i}",
                risk_tags=["security"],
                files_touched=[f"src/module_{i}.py"],
                failure_mode="governance_fail",
                corrective_rule_text="Do not hardcode credentials. " * 10,
            )
            for i in range(10)
        ]
        result = format_lessons_for_injection(items)
        assert len(result) <= 1500

    def test_format_lessons_for_injection_empty(self):
        """Empty items returns empty string."""
        assert format_lessons_for_injection([]) == ""

    def test_format_lessons_for_injection_structure(self):
        """Output contains numbered entries with rule text."""
        item = _make_item(
            item_id="fmt-001",
            risk_tags=["shell_exec"],
            files_touched=["scripts/deploy.sh"],
            failure_mode="governance_fail",
            corrective_rule_text="Use array form for subprocess calls",
        )
        result = format_lessons_for_injection([item])
        assert "1." in result
        assert "shell_exec" in result
        assert "Rule:" in result


# ---------------------------------------------------------------------------
# T016: validate_before_injection + run_staleness_cleanup
# ---------------------------------------------------------------------------


class TestValidateBeforeInjection:
    def test_validate_before_injection_missing_file(self, tmp_path):
        """Items whose primary file is gone from the repo are excluded."""
        item = _make_item(
            item_id="gone-file",
            files_touched=["src/deleted_module.py"],
        )
        # repo_path has no such file
        result = validate_before_injection([item], repo_path=tmp_path)
        assert result == []

    def test_validate_keeps_item_with_existing_file(self, tmp_path):
        """Items whose primary file still exists are kept."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "module.py").write_text("# exists")

        item = _make_item(
            item_id="existing-file",
            files_touched=["src/module.py"],
        )
        result = validate_before_injection([item], repo_path=tmp_path)
        assert len(result) == 1
        assert result[0].item_id == "existing-file"

    def test_validate_before_injection_stale_after(self, tmp_path, mem_conn):
        """Items past stale_after are excluded and marked stale in DB."""
        item = _make_item(
            item_id="stale-date",
            files_touched=[],  # no file check needed
            stale_after=_ago(3),
        )
        store_memory_item(mem_conn, item)

        result = validate_before_injection([item], repo_path=tmp_path, conn=mem_conn)
        assert result == []

        # Confirm marked stale in DB
        row = mem_conn.execute(
            "SELECT status FROM memory_items WHERE item_id = ?", ("stale-date",)
        ).fetchone()
        assert row[0] == "stale"

    def test_validate_before_injection_superseded(self, tmp_path, mem_conn):
        """Items superseded by a later PASS on the same file+failure_mode are excluded."""
        # Original FAIL item
        fail_item = _make_item(
            item_id="original-fail",
            files_touched=["src/auth.py"],
            failure_mode="security_violation",
            outcome="FAIL",
            created_at=_ago(10),
        )
        store_memory_item(mem_conn, fail_item)

        # Later PASS item for same file+failure_mode
        pass_item = _make_item(
            item_id="later-pass",
            files_touched=["src/auth.py"],
            failure_mode="security_violation",
            outcome="PASS",
            created_at=_ago(2),
        )
        store_memory_item(mem_conn, pass_item)

        result = validate_before_injection([fail_item], repo_path=tmp_path, conn=mem_conn)
        assert result == []

    def test_validate_empty_files_no_file_check(self, tmp_path):
        """Items with no files_touched are not dropped due to missing file check."""
        item = _make_item(
            item_id="no-files",
            files_touched=[],
            stale_after=None,
        )
        result = validate_before_injection([item], repo_path=tmp_path)
        # primary_file_gone is False when files_touched is empty
        assert len(result) == 1


class TestRunStalenessCleanup:
    def test_run_staleness_cleanup_marks_expired(self, tmp_path, mem_conn):
        """Items past stale_after are marked stale by cleanup."""
        expired = _make_item(
            item_id="exp-cleanup",
            files_touched=[],
            stale_after=_ago(10),
        )
        store_memory_item(mem_conn, expired)

        count = run_staleness_cleanup(mem_conn, tmp_path)
        assert count >= 1

        row = mem_conn.execute(
            "SELECT status FROM memory_items WHERE item_id = ?", ("exp-cleanup",)
        ).fetchone()
        assert row[0] == "stale"

    def test_run_staleness_cleanup_marks_missing_file(self, tmp_path, mem_conn):
        """Items whose primary file is gone are marked stale."""
        item = _make_item(
            item_id="missing-file-cleanup",
            files_touched=["src/gone.py"],
            stale_after=None,
        )
        store_memory_item(mem_conn, item)

        # tmp_path has no src/gone.py
        count = run_staleness_cleanup(mem_conn, tmp_path)
        assert count >= 1

        row = mem_conn.execute(
            "SELECT status FROM memory_items WHERE item_id = ?",
            ("missing-file-cleanup",),
        ).fetchone()
        assert row[0] == "stale"

    def test_run_staleness_cleanup_skips_valid_items(self, tmp_path, mem_conn):
        """Items that are current and whose files exist are not marked stale."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "valid.py").write_text("# valid")

        item = _make_item(
            item_id="valid-item",
            files_touched=["src/valid.py"],
            stale_after=_future(30),
        )
        store_memory_item(mem_conn, item)

        count = run_staleness_cleanup(mem_conn, tmp_path)
        assert count == 0

        row = mem_conn.execute(
            "SELECT status FROM memory_items WHERE item_id = ?", ("valid-item",)
        ).fetchone()
        assert row[0] == "active"

    def test_run_staleness_cleanup_no_items(self, tmp_path, mem_conn):
        """Cleanup on empty DB returns 0 without raising."""
        count = run_staleness_cleanup(mem_conn, tmp_path)
        assert count == 0

    def test_run_staleness_cleanup_returns_int(self, tmp_path, mem_conn):
        """Return value is always an int."""
        result = run_staleness_cleanup(mem_conn, tmp_path)
        assert isinstance(result, int)
