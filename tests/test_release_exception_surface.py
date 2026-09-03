"""Tests for Fix 10: release exception operator surface.

Covers:
1. release-exception list shows records correctly
2. release-exception list has clear empty state
3. active vs expired classification correct
4. evidence list shows exception count for release bundles
5. status shows compact exception summary
6. read-only inspection does not mutate state
7. unrelated recording behaviour does not regress
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import sys

from saturnday.release_exception_list import (
    count_exceptions,
    format_exception_list,
    is_expired,
    load_exception_records,
)
from saturnday.evidence_list import gather_evidence, format_evidence_list
from saturnday.status import gather_status, format_status
from saturnday.cli import main


def _capture(args: list[str]) -> tuple[int, str, str]:
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    try:
        code = main(args)
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_out, old_err
    return code, out, err


def _write_exception(evidence_dir: Path, rule: str, approver: str, reason: str, expiry: str = "") -> None:
    import uuid
    exc_dir = evidence_dir / "release" / "exceptions"
    exc_dir.mkdir(parents=True, exist_ok=True)
    eid = str(uuid.uuid4())
    record = {
        "exception_id": eid,
        "rule_ids": [rule],
        "file_patterns": [],
        "approver": approver,
        "reason": reason,
        "expiry": expiry,
        "artefact_sha256": "",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    (exc_dir / f"{eid}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def _make_release_evidence(repo: Path, run_id: str = "release_20260406T120000Z_abc") -> Path:
    d = repo / ".saturnday" / "evidence" / "release" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence.json").write_text('{"disposition": "FAIL"}', encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 1. release-exception list shows records
# ---------------------------------------------------------------------------

class TestExceptionList:
    def test_shows_records(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_exception(ev_dir, "REL-005", "Jane", "No baseline")
        _write_exception(ev_dir, "REL-002", "Bob", "Known false positive")

        code, out, err = _capture(["release-exception", "list", "--evidence", str(ev_dir)])
        assert code == 0
        assert "2 total" in out
        assert "REL-005" in out
        assert "REL-002" in out
        assert "Jane" in out
        assert "Bob" in out
        assert "ACTIVE" in out

    def test_shows_reason(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_exception(ev_dir, "REL-001", "Alice", "First release, expected")

        code, out, err = _capture(["release-exception", "list", "--evidence", str(ev_dir)])
        assert "First release, expected" in out


# ---------------------------------------------------------------------------
# 2. Empty state
# ---------------------------------------------------------------------------

class TestExceptionListEmpty:
    def test_no_exceptions(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        code, out, err = _capture(["release-exception", "list", "--evidence", str(ev_dir)])
        assert code == 0
        assert "No release exceptions" in out


# ---------------------------------------------------------------------------
# 3. Active vs expired
# ---------------------------------------------------------------------------

class TestActiveVsExpired:
    def test_active_exception(self, tmp_path: Path) -> None:
        ev_dir = tmp_path
        _write_exception(ev_dir, "REL-001", "A", "reason")
        records = load_exception_records(ev_dir)
        assert len(records) == 1
        assert not is_expired(records[0])

    def test_expired_exception(self, tmp_path: Path) -> None:
        ev_dir = tmp_path
        past = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
        _write_exception(ev_dir, "REL-001", "A", "reason", expiry=past)
        records = load_exception_records(ev_dir)
        assert len(records) == 1
        assert is_expired(records[0])

    def test_format_shows_expired_label(self, tmp_path: Path) -> None:
        ev_dir = tmp_path
        past = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
        _write_exception(ev_dir, "REL-001", "A", "reason", expiry=past)
        records = load_exception_records(ev_dir)
        output = format_exception_list(records)
        assert "EXPIRED" in output

    def test_count_separates_active_and_expired(self, tmp_path: Path) -> None:
        ev_dir = tmp_path
        _write_exception(ev_dir, "REL-001", "A", "active one")
        past = (datetime.now(tz=timezone.utc) - timedelta(days=1)).isoformat()
        _write_exception(ev_dir, "REL-002", "B", "expired one", expiry=past)
        counts = count_exceptions(ev_dir)
        assert counts["total"] == 2
        assert counts["active"] == 1
        assert counts["expired"] == 1


# ---------------------------------------------------------------------------
# 4. Evidence list shows exception count
# ---------------------------------------------------------------------------

class TestEvidenceListExceptionCount:
    def test_shows_exception_count(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_exception(ev_dir, "REL-005", "Jane", "reason")
        _write_exception(ev_dir, "REL-002", "Bob", "reason")

        bundles = gather_evidence(tmp_path)
        release_bundles = [b for b in bundles if b["category"] == "release"]
        assert len(release_bundles) == 1
        assert release_bundles[0]["exception_count"] is not None
        assert release_bundles[0]["exception_count"]["total"] == 2

    def test_formatted_shows_exception_info(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_exception(ev_dir, "REL-005", "Jane", "reason")

        bundles = gather_evidence(tmp_path)
        output = format_evidence_list(bundles)
        assert "1 active" in output
        assert "exception" in output.lower()


# ---------------------------------------------------------------------------
# 5. Status shows exception summary
# ---------------------------------------------------------------------------

class TestStatusExceptionSummary:
    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_uses_latest_bundle_only(self, _mock, tmp_path: Path) -> None:
        """Status must reflect only the latest release bundle, not historical totals."""
        old_dir = _make_release_evidence(tmp_path, run_id="release_20260401T000000Z_old")
        _write_exception(old_dir, "REL-001", "OldApprover", "old reason")
        _write_exception(old_dir, "REL-002", "OldApprover", "old reason 2")

        new_dir = _make_release_evidence(tmp_path, run_id="release_20260406T000000Z_new")
        _write_exception(new_dir, "REL-005", "NewApprover", "new reason")

        status = gather_status(tmp_path)
        exc = status.get("release_exception_summary")
        assert exc is not None
        assert exc["total"] == 1, "Must show only latest bundle's exceptions, not 3"
        assert exc["release_bundle"] == "release_20260406T000000Z_new"

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_older_bundles_ignored(self, _mock, tmp_path: Path) -> None:
        """When latest bundle has no exceptions, summary should be absent even if older bundles do."""
        old_dir = _make_release_evidence(tmp_path, run_id="release_20260401T000000Z_old")
        _write_exception(old_dir, "REL-001", "A", "reason")

        _make_release_evidence(tmp_path, run_id="release_20260406T000000Z_new")
        # New bundle has no exceptions

        status = gather_status(tmp_path)
        assert status.get("release_exception_summary") is None

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_formatted_shows_latest_label(self, _mock, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path, run_id="release_20260406T120000Z_abc")
        _write_exception(ev_dir, "REL-005", "Jane", "reason")

        output = format_status(gather_status(tmp_path))
        assert "Release exceptions" in output
        assert "latest:" in output
        assert "release_20260406T120000Z_abc" in output

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_no_exceptions(self, _mock, tmp_path: Path) -> None:
        output = format_status(gather_status(tmp_path))
        assert "Release exceptions: none" in output


# ---------------------------------------------------------------------------
# 6. Read-only — no mutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_list_does_not_mutate(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_exception(ev_dir, "REL-001", "A", "reason")

        before = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                before.add(os.path.join(root, f))

        _capture(["release-exception", "list", "--evidence", str(ev_dir)])

        after = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                after.add(os.path.join(root, f))

        assert before == after
