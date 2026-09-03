"""Tests for run_data.py — SQLite run data collection."""

import sqlite3
from pathlib import Path

import pytest

from saturnday.evidence import CheckResult, EvidencePack, SCHEMA_VERSION
from saturnday.run_data import (
    get_db,
    init_db,
    query_failing_checks,
    query_recent_runs,
    query_run_stats,
    record_escalation,
    record_evidence_pack,
)


def _make_pack(**overrides):
    # Use a timestamp close to "now" so it stays inside the default 30-day
    # aggregation window used by ``query_run_stats`` / ``query_failing_checks``.
    from datetime import datetime, timezone
    _today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    defaults = dict(
        schema_version=SCHEMA_VERSION,
        run_id="test-run-001",
        mode="check",
        repo_path="/tmp/repo",
        diff_range="HEAD~1..HEAD",
        saturnday_version="0.1.0",
        created_utc=_today,
        ended_utc=_today,
        check_results=[],
        disposition="PASS",
        disposition_reasons=[],
    )
    defaults.update(overrides)
    return EvidencePack(**defaults)


@pytest.fixture
def db(tmp_path):
    conn = get_db(tmp_path / "test.db")
    init_db(conn)
    yield conn
    conn.close()


class TestInitDb:
    def test_creates_tables(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {row["name"] for row in tables}
        assert "runs" in table_names
        assert "check_results" in table_names
        assert "findings" in table_names
        assert "escalations" in table_names

    def test_idempotent(self, db):
        init_db(db)  # second call should not error
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) >= 4


class TestRecordEvidencePack:
    def test_basic_insert(self, db):
        pack = _make_pack()
        record_evidence_pack(db, pack)
        row = db.execute("SELECT * FROM runs WHERE run_id = ?", ("test-run-001",)).fetchone()
        assert row is not None
        assert row["mode"] == "check"
        assert row["disposition"] == "PASS"

    def test_with_check_results(self, db):
        pack = _make_pack(
            check_results=[
                CheckResult(name="syntax", status="PASS", severity="error"),
                CheckResult(
                    name="ruff", status="FAIL", severity="warning",
                    findings=[{"message": "lint issue", "file_path": "a.py", "line_number": 10}],
                ),
            ],
        )
        record_evidence_pack(db, pack)

        checks = db.execute(
            "SELECT * FROM check_results WHERE run_id = ? ORDER BY check_name",
            ("test-run-001",),
        ).fetchall()
        assert len(checks) == 2

        ruff_check = [c for c in checks if c["check_name"] == "ruff"][0]
        assert ruff_check["status"] == "FAIL"
        assert ruff_check["finding_count"] == 1

        findings = db.execute(
            "SELECT * FROM findings WHERE check_result_id = ?",
            (ruff_check["id"],),
        ).fetchall()
        assert len(findings) == 1
        assert findings[0]["file_path"] == "a.py"
        assert findings[0]["line_number"] == 10

    def test_upsert_on_duplicate_run_id(self, db):
        pack1 = _make_pack(disposition="PASS")
        record_evidence_pack(db, pack1)
        pack2 = _make_pack(disposition="FAIL")
        record_evidence_pack(db, pack2)
        row = db.execute("SELECT disposition FROM runs WHERE run_id = ?", ("test-run-001",)).fetchone()
        assert row["disposition"] == "FAIL"

    def test_empty_findings(self, db):
        pack = _make_pack(
            check_results=[
                CheckResult(name="syntax", status="PASS", severity="error", findings=[]),
            ],
        )
        record_evidence_pack(db, pack)
        findings = db.execute("SELECT * FROM findings").fetchall()
        assert len(findings) == 0


class TestRecordEscalation:
    def test_basic_escalation(self, db):
        pack = _make_pack()
        record_evidence_pack(db, pack)
        record_escalation(db, "test-run-001", "fixer", "llama-70b", True, 5.0)
        rows = db.execute("SELECT * FROM escalations").fetchall()
        assert len(rows) == 1
        assert rows[0]["tier"] == "fixer"
        assert rows[0]["success"] == 1


class TestQueryRunStats:
    def test_empty_db(self, db):
        stats = query_run_stats(db)
        assert stats["total"] == 0
        assert stats["pass_rate"] == 0.0

    def test_with_runs(self, db):
        for i, disp in enumerate(["PASS", "PASS", "FAIL", "WARN"]):
            pack = _make_pack(run_id=f"run-{i}", disposition=disp)
            record_evidence_pack(db, pack)
        stats = query_run_stats(db)
        assert stats["total"] == 4
        assert stats["pass_count"] == 2
        assert stats["fail_count"] == 1
        assert stats["warn_count"] == 1
        assert stats["pass_rate"] == 50.0


class TestQueryRecentRuns:
    def test_empty_db(self, db):
        runs = query_recent_runs(db)
        assert runs == []

    def test_returns_recent(self, db):
        for i in range(5):
            pack = _make_pack(
                run_id=f"run-{i}",
                created_utc=f"2026-03-0{i+1}T12:00:00Z",
            )
            record_evidence_pack(db, pack)
        runs = query_recent_runs(db, limit=3)
        assert len(runs) == 3
        # Most recent first
        assert runs[0]["run_id"] == "run-4"

    def test_returns_dict(self, db):
        pack = _make_pack()
        record_evidence_pack(db, pack)
        runs = query_recent_runs(db, limit=1)
        assert isinstance(runs[0], dict)
        assert "run_id" in runs[0]


class TestQueryFailingChecks:
    def test_empty_db(self, db):
        failing = query_failing_checks(db)
        assert failing == []

    def test_counts_failures(self, db):
        for i in range(3):
            pack = _make_pack(
                run_id=f"run-{i}",
                check_results=[
                    CheckResult(name="syntax", status="FAIL", severity="error"),
                    CheckResult(name="ruff", status="PASS", severity="warning"),
                ],
            )
            record_evidence_pack(db, pack)
        # Add one more with ruff failing
        pack = _make_pack(
            run_id="run-3",
            check_results=[
                CheckResult(name="syntax", status="PASS", severity="error"),
                CheckResult(name="ruff", status="FAIL", severity="warning"),
            ],
        )
        record_evidence_pack(db, pack)

        failing = query_failing_checks(db)
        assert len(failing) >= 1
        syntax_row = next(r for r in failing if r["check_name"] == "syntax")
        assert syntax_row["fail_count"] == 3


class TestGetDb:
    def test_custom_path(self, tmp_path):
        db_path = tmp_path / "custom" / "data.db"
        conn = get_db(db_path)
        init_db(conn)
        assert db_path.exists()
        conn.close()

    def test_wal_mode(self, tmp_path):
        conn = get_db(tmp_path / "test.db")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()
