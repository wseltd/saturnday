"""Closure-proof tests for:

  * Batch 5 — J.2 (Terraform coverage) + I.2 (TS sql_string_building FP)
  * Standalone — state-dir resolver + lessons DB concurrency + source-tree
    isolation
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# J.2 — Terraform coverage
# ---------------------------------------------------------------------------


class TestTerraformCoverage:
    """J.2: .tf / .hcl files must flow through run_full_repo_review so
    _check_terraform actually sees targets."""

    def _make_repo(self, tmp_path: Path, tf_body: str) -> Path:
        repo = tmp_path / "tf-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        # Include a python file too so unrelated checks still run normally.
        (repo / "main.py").write_text("def ok(): return 1\n", encoding="utf-8")
        infra = repo / "infrastructure"
        infra.mkdir()
        (infra / "main.tf").write_text(tf_body, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        return repo

    def test_j2_tf_file_enters_full_repo_review(self, tmp_path: Path) -> None:
        """J.2 ground truth: a .tf file reaches the review pipeline so
        ``_check_terraform`` actually runs.

        The review layer only registers terraform as an executed tool
        when its ``files_checked`` list is non-empty (``review.py`` only
        adds the tool to ``tools`` dict when ``terraform_result["files_checked"]``
        is truthy — see review.py:6903-6906).  Therefore the presence of
        a terraform entry in ``pack.check_results`` is itself the proof
        that ``.tf`` files flowed through the allowlist.  (``files_checked``
        does not propagate through the EvidencePack conversion —
        independent surface issue, not what J.2 is about.)"""
        from saturnday.governance import run_full_repo_review

        # Use a benign .tf file so we isolate the "did it get scanned"
        # question from the "did it fire a finding" question.
        tf_body = 'resource "null_resource" "benign" {}\n'
        repo = self._make_repo(tmp_path, tf_body)

        pack, _ = run_full_repo_review(repo)
        tf_result = next(
            (cr for cr in pack.check_results if cr.name == "terraform"), None
        )
        assert tf_result is not None, (
            "terraform check did not run during full-repo review — .tf was "
            "filtered out of supported_extensions (J.2 regression)"
        )

    def test_j2_seeded_hardcoded_credential_is_detected(self, tmp_path: Path) -> None:
        """End-to-end: a seeded Terraform hardcoded secret surfaces as a
        governance finding on full-repo review."""
        from saturnday.governance import run_full_repo_review

        tf_body = (
            'resource "aws_db_instance" "prod" {\n'
            '  password = "hunter2-definitely-not-a-secret"\n'
            '}\n'
        )
        repo = self._make_repo(tmp_path, tf_body)
        pack, _ = run_full_repo_review(repo)
        tf_result = next(
            (cr for cr in pack.check_results if cr.name == "terraform"), None
        )
        assert tf_result is not None
        assert any(
            f.get("kind") == "tf_hardcoded_credential" for f in tf_result.findings
        ), (
            f"tf_hardcoded_credential not surfaced — findings={tf_result.findings}"
        )

    def test_j2_non_terraform_repo_unaffected(self, tmp_path: Path) -> None:
        """Control: a repo with no .tf files produces no terraform findings
        but also doesn't crash any adjacent check."""
        from saturnday.governance import run_full_repo_review

        repo = tmp_path / "py-only"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        (repo / "main.py").write_text("def ok(): return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

        pack, _ = run_full_repo_review(repo)
        # No terraform findings AND the check either didn't run or saw zero targets.
        tf_result = next(
            (cr for cr in pack.check_results if cr.name == "terraform"), None
        )
        if tf_result is not None:
            assert tf_result.findings == []

    def test_j2_supported_extensions_includes_iac(self) -> None:
        """Canary: the extension allowlist in governance.py must include
        .tf and .hcl.  Protects against future narrowing."""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "saturnday" / "governance.py"
        ).read_text(encoding="utf-8")
        assert '".tf"' in src
        assert '".hcl"' in src


# ---------------------------------------------------------------------------
# I.2 — TS sql_string_building false positive
# ---------------------------------------------------------------------------


class TestSqlStringBuildingFP:
    """I.2: rule must fire on real SQL string-building and must NOT
    fire on HTTP client delete / fetch / axios cases."""

    def _run_check(self, tmp_path: Path, content: str) -> list[dict]:
        from saturnday.review_ts import check_sql_injection_ts

        repo = tmp_path / "ts-repo"
        repo.mkdir()
        f = repo / "foo.ts"
        f.write_text(content, encoding="utf-8")
        res = check_sql_injection_ts(repo, ["foo.ts"])
        return res.get("findings", [])

    # ---- Must NOT fire: HTTP / fetch / axios contexts -----------------------

    def test_i2_http_fetch_delete_does_not_trigger(self, tmp_path: Path) -> None:
        """The exact evaluator-reported false positive: HTTP DELETE via fetch."""
        findings = self._run_check(
            tmp_path,
            "await fetch(`${api}/delete/${id}`, { method: 'DELETE' });\n",
        )
        assert findings == [], f"HTTP DELETE fetch should not fire SEC-015: {findings}"

    def test_i2_axios_delete_does_not_trigger(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "await axios.delete(`${baseUrl}/api/users/${id}`);\n",
        )
        assert findings == [], f"axios.delete should not fire SEC-015: {findings}"

    def test_i2_http_post_with_delete_verb_in_path_does_not_trigger(
        self, tmp_path: Path
    ) -> None:
        findings = self._run_check(
            tmp_path,
            "await http.post(`${base}/delete/${id}`, { method: 'DELETE' });\n",
        )
        assert findings == []

    def test_i2_url_template_with_sql_verb_in_path_does_not_trigger(
        self, tmp_path: Path
    ) -> None:
        findings = self._run_check(
            tmp_path,
            "const url = `/api/v1/select/${id}`;\n",
        )
        assert findings == []

    # ---- Must still fire: real SQL contexts ---------------------------------

    def test_i2_real_sql_delete_from_where_still_fires(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "const q = `DELETE FROM users WHERE id = ${userId}`;\n",
        )
        assert len(findings) == 1, f"real SQL must fire: {findings}"
        assert findings[0]["kind"] == "sql_string_building"

    def test_i2_real_sql_select_from_still_fires(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "db.query(`SELECT * FROM accounts WHERE id = ${id}`);\n",
        )
        assert len(findings) == 1

    def test_i2_real_sql_update_set_still_fires(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "conn.execute(`UPDATE users SET name = ${name} WHERE id = ${id}`);\n",
        )
        assert len(findings) == 1

    def test_i2_real_sql_insert_into_values_still_fires(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "pool.query(`INSERT INTO logs (msg) VALUES (${msg})`);\n",
        )
        assert len(findings) == 1

    def test_i2_tagged_sql_template_still_fires(self, tmp_path: Path) -> None:
        """Tagged template literals like ``sql`...``` are real SQL-builder
        idioms (slonik, postgres.js, pg-template-tag).  Must fire even
        without SQL companion keywords on the line."""
        findings = self._run_check(
            tmp_path,
            "const q = sql`DELETE ${tableRef}`;\n",
        )
        assert len(findings) == 1

    def test_i2_knex_raw_still_fires(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "knex.raw(`DELETE ${ref}`);\n",
        )
        assert len(findings) == 1

    def test_i2_db_raw_still_fires(self, tmp_path: Path) -> None:
        findings = self._run_check(
            tmp_path,
            "db.raw(`DROP ${tbl}`);\n",
        )
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# State-dir resolver — precedence
# ---------------------------------------------------------------------------


class TestStateDirPrecedence:
    def test_no_env_returns_home_saturnday(self, monkeypatch) -> None:
        monkeypatch.delenv("SATURNDAY_STATE_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        from saturnday.paths import state_dir
        assert state_dir() == Path.home() / ".saturnday"

    def test_saturnday_state_dir_wins(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SATURNDAY_STATE_DIR", str(tmp_path / "sstate"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        from saturnday.paths import state_dir
        assert state_dir() == tmp_path / "sstate"

    def test_xdg_state_home_used_when_saturnday_env_absent(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("SATURNDAY_STATE_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        from saturnday.paths import state_dir
        assert state_dir() == tmp_path / "xdg" / "saturnday"

    def test_empty_env_treated_as_unset(self, monkeypatch, tmp_path: Path) -> None:
        """Empty-string env values must NOT win — many shells export
        unset vars as the empty string."""
        monkeypatch.setenv("SATURNDAY_STATE_DIR", "")
        monkeypatch.setenv("XDG_STATE_HOME", "")
        from saturnday.paths import state_dir
        assert state_dir() == Path.home() / ".saturnday"

    def test_tilde_expansion_in_saturnday_state_dir(self, monkeypatch) -> None:
        monkeypatch.setenv("SATURNDAY_STATE_DIR", "~/custom-sat")
        from saturnday.paths import state_dir
        assert state_dir() == Path.home() / "custom-sat"


class TestStateDirUsedByModules:
    """State-writing modules must route through paths.state_dir() at
    call time, not at import time."""

    def test_lessons_default_path_respects_env(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SATURNDAY_STATE_DIR", str(tmp_path / "sstate"))
        from saturnday.run.lessons import _default_db_path
        assert _default_db_path() == tmp_path / "sstate" / "lessons.db"

    def test_run_data_default_path_respects_env(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SATURNDAY_STATE_DIR", str(tmp_path / "sstate"))
        from saturnday.run_data import _default_db_path
        assert _default_db_path() == tmp_path / "sstate" / "run_data.db"

    def test_update_notifier_dir_respects_env(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SATURNDAY_STATE_DIR", str(tmp_path / "sstate"))
        from saturnday.update_notifier import _saturnday_dir
        assert _saturnday_dir() == tmp_path / "sstate"

    def test_backward_compat_no_env_uses_home(self, monkeypatch) -> None:
        monkeypatch.delenv("SATURNDAY_STATE_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        from saturnday.run.lessons import _default_db_path as lessons_default
        from saturnday.run_data import _default_db_path as rundata_default
        from saturnday.update_notifier import _saturnday_dir
        assert lessons_default() == Path.home() / ".saturnday" / "lessons.db"
        assert rundata_default() == Path.home() / ".saturnday" / "run_data.db"
        assert _saturnday_dir() == Path.home() / ".saturnday"


# ---------------------------------------------------------------------------
# Lessons DB concurrency hardening
# ---------------------------------------------------------------------------


class TestLessonsDbConcurrency:
    def test_init_db_uses_wal_mode(self, tmp_path: Path) -> None:
        from saturnday.run.lessons import init_db
        conn = init_db(tmp_path / "lessons.db")
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode_row[0].lower() == "wal"
        conn.close()

    def test_init_db_uses_busy_timeout(self, tmp_path: Path) -> None:
        """SQLite busy timeout must be applied so realistic ticket-write
        contention does not surface as a spurious ``database is locked``.

        ``sqlite3.connect(..., timeout=T)`` maps to ``PRAGMA busy_timeout = T*1000``
        on the underlying connection.  Assert the pragma reports a
        non-trivial value (>= 5s) rather than the default 0."""
        from saturnday.run.lessons import init_db
        conn = init_db(tmp_path / "lessons.db")
        try:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
            assert row is not None
            timeout_ms = row[0]
            # init_db uses timeout=30.0 on connect → 30000 ms.
            assert timeout_ms >= 5000, (
                f"busy_timeout={timeout_ms}ms — expected >= 5000ms "
                f"(connect timeout is not set correctly)"
            )
        finally:
            conn.close()

    def test_concurrent_writes_do_not_crash(self, tmp_path: Path) -> None:
        """Real-world test: two threads insert lessons concurrently.
        With WAL + timeout neither should hard-fail."""
        from saturnday.run.lessons import init_db

        db = tmp_path / "lessons.db"
        init_db(db).close()  # create schema

        errors: list[Exception] = []

        def _worker(tid: int) -> None:
            try:
                c = init_db(db)
                for i in range(20):
                    c.execute(
                        "INSERT INTO lessons (project_id, ticket_id, failure_type, "
                        "rule, description, confidence, created_at, updated_at) "
                        "VALUES (?, ?, 'f', 'r', 'd', 1, 'now', 'now')",
                        (f"p{tid}", f"t{tid}-{i}"),
                    )
                    c.commit()
                c.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, f"concurrent lessons writes crashed: {errors!r}"

        # Verify all 4 * 20 rows landed.
        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        conn.close()
        assert count == 80, f"expected 80 rows, got {count}"


# ---------------------------------------------------------------------------
# Source-tree isolation
# ---------------------------------------------------------------------------


class TestSourceTreeIsolation:
    """Running governance against an external target must not touch the
    Saturnday source checkout.  This is the explicit no-leak proof."""

    def test_external_target_governance_leaves_source_tree_untouched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from saturnday.governance import run_full_repo_review

        src_tree = Path(__file__).resolve().parent.parent
        # Redirect shared state into a temp dir so the test doesn't dirty
        # the operator's real ~/.saturnday either.
        monkeypatch.setenv("SATURNDAY_STATE_DIR", str(tmp_path / "state"))

        target = tmp_path / "target-repo"
        target.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=target, check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=target, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=target, check=True)
        (target / "x.py").write_text("def ok(): return 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=target, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=target, check=True)

        # Snapshot source tree mtimes BEFORE.
        before = {p.relative_to(src_tree): p.stat().st_mtime for p in src_tree.rglob("*")
                  if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                  and ".saturnday" not in p.parts and ".saturnday-baseline.json" not in p.name
                  and ".saturnday-policy.yaml" not in p.name}

        # Run governance against the EXTERNAL target.
        pack, evidence = run_full_repo_review(target)
        assert pack is not None

        # Evidence path must be under the target — never under the source tree.
        ev_resolved = Path(evidence).resolve() if evidence else None
        if ev_resolved is not None:
            src_resolved = src_tree.resolve()
            target_resolved = target.resolve()
            state_resolved = (tmp_path / "state").resolve()
            tmp_resolved = Path("/tmp").resolve()
            assert (
                str(ev_resolved).startswith(str(target_resolved))
                or str(ev_resolved).startswith(str(state_resolved))
                or str(ev_resolved).startswith(str(tmp_resolved))
            ), f"evidence landed outside expected surfaces: {ev_resolved}"
            assert not str(ev_resolved).startswith(str(src_resolved)), (
                f"evidence leaked into saturnday source tree: {ev_resolved}"
            )

        # Compare mtimes — no source-tree file should have been modified.
        after = {p.relative_to(src_tree): p.stat().st_mtime for p in src_tree.rglob("*")
                 if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                 and ".saturnday" not in p.parts and ".saturnday-baseline.json" not in p.name
                 and ".saturnday-policy.yaml" not in p.name}
        changed = [rel for rel, m in after.items()
                   if rel in before and before[rel] != m]
        assert not changed, (
            f"source tree files were modified by a governance run against an "
            f"external target: {changed}"
        )
