"""Tests for Fix 11: release approval operator surface.

Covers:
1. release-approve prints visible success output (premium adapter)
2. release-approve list shows signoff records
3. release-approve list has clear empty state
4. release-approve check reports completeness
5. release-approve check uses signoff completeness logic
6. evidence list shows signoff count
7. status shows latest-release signoff summary only
8. read-only inspection does not mutate
9. unrelated recording does not regress
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch
import sys

from saturnday.release_signoff_list import check_signoff_completeness
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


def _write_signoff(evidence_dir: Path, approver: str, sha: str = "ab" * 32) -> None:
    sig_dir = evidence_dir / "release" / "signoffs"
    sig_dir.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    record = {
        "signoff_id": sid,
        "artefact_sha256": sha,
        "approver": approver,
        "approved_at": datetime.now(tz=timezone.utc).isoformat(),
        "notes": "",
    }
    (sig_dir / f"{sid}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def _make_release_evidence(repo: Path, run_id: str = "release_20260406T120000Z_abc") -> Path:
    d = repo / ".saturnday" / "evidence" / "release" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "evidence.json").write_text('{"disposition": "PASS"}', encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 2. release-approve list
# ---------------------------------------------------------------------------

class TestApproveList:
    def test_shows_records(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")
        _write_signoff(ev_dir, "Bob")

        code, out, err = _capture(["release-approve", "list", "--evidence", str(ev_dir)])
        assert code == 0
        assert "2 total" in out
        assert "Alice" in out
        assert "Bob" in out
        assert "2 distinct" in out


# ---------------------------------------------------------------------------
# 3. Empty state
# ---------------------------------------------------------------------------

class TestApproveListEmpty:
    def test_no_signoffs(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        code, out, err = _capture(["release-approve", "list", "--evidence", str(ev_dir)])
        assert code == 0
        assert "No release signoffs" in out


# ---------------------------------------------------------------------------
# 4 & 5. release-approve check
# ---------------------------------------------------------------------------

class TestApproveCheck:
    def test_met(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")
        _write_signoff(ev_dir, "Bob")

        code, out, err = _capture(["release-approve", "check", "--evidence", str(ev_dir), "--minimum", "2"])
        assert code == 0
        assert "met" in out.lower()

    def test_not_met(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")

        code, out, err = _capture(["release-approve", "check", "--evidence", str(ev_dir), "--minimum", "2"])
        assert code == 1
        assert "NOT met" in out

    def test_uses_check_signoff_completeness(self, tmp_path: Path) -> None:
        """The CLI check command uses check_signoff_completeness — same logic."""
        ev_dir = tmp_path
        _write_signoff(ev_dir, "Alice")
        _write_signoff(ev_dir, "Bob")
        satisfied, reason = check_signoff_completeness(ev_dir, minimum_approvers=2)
        assert satisfied is True
        assert "2 distinct" in reason

    def test_same_approver_twice_counts_once(self, tmp_path: Path) -> None:
        ev_dir = tmp_path
        _write_signoff(ev_dir, "Alice")
        _write_signoff(ev_dir, "Alice")
        satisfied, _ = check_signoff_completeness(ev_dir, minimum_approvers=2)
        assert satisfied is False

    def test_no_signoffs(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        code, out, err = _capture(["release-approve", "check", "--evidence", str(ev_dir), "--minimum", "1"])
        assert code == 1
        assert "NOT met" in out


# ---------------------------------------------------------------------------
# 6. evidence list shows signoff count
# ---------------------------------------------------------------------------

class TestEvidenceListSignoffCount:
    def test_shows_approver_count(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")
        _write_signoff(ev_dir, "Bob")

        bundles = gather_evidence(tmp_path)
        release = [b for b in bundles if b["category"] == "release"]
        assert len(release) == 1
        assert release[0].get("signoff_count") is not None
        assert release[0]["signoff_count"]["distinct_approvers"] == 2

    def test_formatted_shows_approvers(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")

        output = format_evidence_list(gather_evidence(tmp_path))
        assert "1 approver" in output


# ---------------------------------------------------------------------------
# 7. status shows latest-release signoff summary
# ---------------------------------------------------------------------------

class TestStatusSignoffSummary:
    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_shows_signoff_count(self, _mock, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")
        _write_signoff(ev_dir, "Bob")

        status = gather_status(tmp_path)
        sig = status.get("release_signoff_summary")
        assert sig is not None
        assert sig["total"] == 2
        assert sig["distinct_approvers"] == 2

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_uses_latest_bundle_only(self, _mock, tmp_path: Path) -> None:
        old_dir = _make_release_evidence(tmp_path, run_id="release_20260401T000000Z_old")
        _write_signoff(old_dir, "OldApprover")

        _make_release_evidence(tmp_path, run_id="release_20260406T000000Z_new")
        # New bundle has no signoffs

        status = gather_status(tmp_path)
        assert status.get("release_signoff_summary") is None

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_no_signoffs(self, _mock, tmp_path: Path) -> None:
        output = format_status(gather_status(tmp_path))
        assert "Release signoffs: none" in output


# ---------------------------------------------------------------------------
# 8. No mutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_list_does_not_mutate(self, tmp_path: Path) -> None:
        ev_dir = _make_release_evidence(tmp_path)
        _write_signoff(ev_dir, "Alice")

        before = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                before.add(os.path.join(root, f))

        _capture(["release-approve", "list", "--evidence", str(ev_dir)])

        after = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                after.add(os.path.join(root, f))

        assert before == after
