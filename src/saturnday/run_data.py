"""Run data collection via SQLite.

Records evidence packs to a local SQLite database for stats and history.
Uses stdlib sqlite3 — no ORM. All db access is wrapped in try/except
so it never breaks the main pipeline.
"""

import sqlite3
from pathlib import Path


# Back-compat alias: the old module-level constant stays importable and
# returns the legacy path.  New code should call ``_default_db_path()``
# (or pass an explicit path) so ``SATURNDAY_STATE_DIR`` / ``XDG_STATE_HOME``
# overrides take effect at call time rather than import time.
DEFAULT_DB_PATH = Path.home() / ".saturnday" / "run_data.db"


def _default_db_path() -> Path:
    """Resolve the run_data.db path at call time via the state-dir resolver."""
    from saturnday.paths import state_dir
    return state_dir() / "run_data.db"


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (or create) the run data database."""
    # Resolve at call time so SATURNDAY_STATE_DIR / XDG_STATE_HOME work
    # without re-importing the module.
    path = db_path or _default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            ended_utc TEXT,
            repo_path TEXT,
            disposition TEXT,
            evidence_dir TEXT,
            duration_s REAL,
            saturnday_version TEXT,
            policy_path TEXT,
            diff_range TEXT,
            model_used TEXT,
            plan_file TEXT
        );

        CREATE TABLE IF NOT EXISTS check_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            check_name TEXT NOT NULL,
            status TEXT NOT NULL,
            severity TEXT NOT NULL,
            finding_count INTEGER DEFAULT 0,
            elapsed_s REAL DEFAULT 0.0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            check_result_id INTEGER NOT NULL REFERENCES check_results(id),
            file_path TEXT,
            line_number INTEGER,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            tier TEXT,
            model TEXT,
            success INTEGER,
            duration_s REAL
        );
    """)
    conn.commit()


def record_evidence_pack(conn: sqlite3.Connection, pack) -> None:
    """Insert run + all check_results + all findings in one transaction."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, mode, created_utc, ended_utc, repo_path, disposition,
                evidence_dir, duration_s, saturnday_version, policy_path,
                diff_range, model_used, plan_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pack.run_id,
                pack.mode,
                pack.created_utc,
                pack.ended_utc,
                pack.repo_path,
                pack.disposition,
                None,  # evidence_dir filled by caller if needed
                None,  # duration_s
                pack.saturnday_version,
                pack.policy_path,
                pack.diff_range,
                pack.model_used,
                pack.plan_file,
            ),
        )

        for cr in pack.check_results:
            cursor.execute(
                """INSERT INTO check_results
                   (run_id, check_name, status, severity, finding_count, elapsed_s, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    pack.run_id,
                    cr.name,
                    cr.status,
                    cr.severity,
                    len(cr.findings),
                    cr.elapsed_s,
                    cr.error,
                ),
            )
            check_result_id = cursor.lastrowid

            for finding in cr.findings:
                cursor.execute(
                    """INSERT INTO findings
                       (check_result_id, file_path, line_number, message)
                       VALUES (?, ?, ?, ?)""",
                    (
                        check_result_id,
                        finding.get("file_path") or finding.get("filename"),
                        finding.get("line_number") or finding.get("line"),
                        finding.get("message") or finding.get("issue_text", ""),
                    ),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


def record_escalation(
    conn: sqlite3.Connection,
    run_id: str,
    tier: str,
    model: str,
    success: bool,
    duration_s: float,
) -> None:
    """Record an escalation event."""
    conn.execute(
        """INSERT INTO escalations (run_id, tier, model, success, duration_s)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, tier, model, int(success), duration_s),
    )
    conn.commit()


def query_run_stats(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """Aggregate stats over the last N days."""
    row = conn.execute(
        """SELECT
             COUNT(*) as total,
             SUM(CASE WHEN disposition = 'PASS' THEN 1 ELSE 0 END) as pass_count,
             SUM(CASE WHEN disposition = 'WARN' THEN 1 ELSE 0 END) as warn_count,
             SUM(CASE WHEN disposition = 'FAIL' THEN 1 ELSE 0 END) as fail_count
           FROM runs
           WHERE created_utc >= datetime('now', ?)""",
        (f"-{days} days",),
    ).fetchone()

    total = row["total"] or 0
    pass_count = row["pass_count"] or 0
    return {
        "total": total,
        "pass_count": pass_count,
        "warn_count": row["warn_count"] or 0,
        "fail_count": row["fail_count"] or 0,
        "pass_rate": (pass_count / total * 100) if total > 0 else 0.0,
        "days": days,
    }


def query_recent_runs(conn: sqlite3.Connection, *, limit: int = 20) -> list[dict]:
    """Return recent runs ordered by creation time."""
    rows = conn.execute(
        """SELECT run_id, mode, created_utc, repo_path, disposition, saturnday_version
           FROM runs ORDER BY created_utc DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_failing_checks(conn: sqlite3.Connection, *, days: int = 30) -> list[dict]:
    """Return most common failing checks over the last N days."""
    rows = conn.execute(
        """SELECT check_name, COUNT(*) as fail_count
           FROM check_results
           WHERE status = 'FAIL'
             AND run_id IN (
               SELECT run_id FROM runs
               WHERE created_utc >= datetime('now', ?)
             )
           GROUP BY check_name
           ORDER BY fail_count DESC""",
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]
