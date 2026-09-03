"""Tests for Fix 9: approval and sign-off operator surface.

Covers:
1. approve-list shows records grouped by document
2. approve-list has clear empty state
3. approve-check reports complete sign-off
4. approve-check reports missing roles
5. approve-check reports rejection blocking
6. approve-check handles unknown/empty document
7. status shows approval summary
8. unrelated approval semantics do not regress
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import sys

from saturnday.cli import main
from saturnday.status import gather_status, format_status


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


def _write_approvals(repo: Path, records: list[dict]) -> None:
    path = repo / ".saturnday" / "document" / "approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def _write_policy(repo: Path, roles: list[str]) -> None:
    import yaml
    path = repo / ".saturnday-policy.yaml"
    path.write_text(yaml.dump({
        "schema_version": "1.0.0",
        "document_sign_off_roles": roles,
    }), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. approve-list shows records
# ---------------------------------------------------------------------------

class TestApproveList:
    def test_shows_records(self, tmp_path: Path) -> None:
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "Alice", "status": "approved", "timestamp": "2026-04-06T12:00:00Z", "notes": ""},
            {"document_id": "doc-1", "role": "legal", "actor": "Bob", "status": "pending", "timestamp": "2026-04-06T13:00:00Z", "notes": "waiting"},
            {"document_id": "doc-2", "role": "cfo", "actor": "Charlie", "status": "rejected", "timestamp": "2026-04-06T14:00:00Z", "notes": "budget issue"},
        ])
        code, out, err = _capture(["approve", "list", "--repo", str(tmp_path)])
        assert code == 0
        assert "3" in out  # 3 records
        assert "2 document" in out
        assert "Alice" in out
        assert "Bob" in out
        assert "Charlie" in out
        assert "doc-1" in out
        assert "doc-2" in out

    def test_shows_notes(self, tmp_path: Path) -> None:
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "Alice", "status": "approved", "timestamp": "", "notes": "LGTM"},
        ])
        code, out, err = _capture(["approve", "list", "--repo", str(tmp_path)])
        assert "LGTM" in out


# ---------------------------------------------------------------------------
# 2. approve-list empty state
# ---------------------------------------------------------------------------

class TestApproveListEmpty:
    def test_no_records(self, tmp_path: Path) -> None:
        code, out, err = _capture(["approve", "list", "--repo", str(tmp_path)])
        assert code == 0
        assert "No approval records" in out


# ---------------------------------------------------------------------------
# 3. approve-check complete sign-off
# ---------------------------------------------------------------------------

class TestApproveCheckComplete:
    def test_all_roles_satisfied(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, ["eng", "legal"])
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "Alice", "status": "approved", "timestamp": "", "notes": ""},
            {"document_id": "doc-1", "role": "legal", "actor": "Bob", "status": "approved", "timestamp": "", "notes": ""},
        ])
        code, out, err = _capture(["approve", "check", "--document", "doc-1", "--repo", str(tmp_path)])
        assert code == 0
        assert "COMPLETE" in out


# ---------------------------------------------------------------------------
# 4. approve-check missing roles
# ---------------------------------------------------------------------------

class TestApproveCheckIncomplete:
    def test_missing_role(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, ["eng", "legal", "cfo"])
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "Alice", "status": "approved", "timestamp": "", "notes": ""},
        ])
        code, out, err = _capture(["approve", "check", "--document", "doc-1", "--repo", str(tmp_path)])
        assert code == 0
        assert "INCOMPLETE" in out
        assert "legal" in out
        assert "cfo" in out


# ---------------------------------------------------------------------------
# 5. approve-check rejection blocking
# ---------------------------------------------------------------------------

class TestApproveCheckRejection:
    def test_rejection_blocks(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, ["eng", "legal"])
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "Alice", "status": "approved", "timestamp": "", "notes": ""},
            {"document_id": "doc-1", "role": "legal", "actor": "Bob", "status": "rejected", "timestamp": "", "notes": "nope"},
        ])
        code, out, err = _capture(["approve", "check", "--document", "doc-1", "--repo", str(tmp_path)])
        assert code == 0
        assert "BLOCKED" in out
        assert "legal" in out


# ---------------------------------------------------------------------------
# 6. approve-check unknown document
# ---------------------------------------------------------------------------

class TestApproveCheckUnknown:
    def test_no_approvals_for_doc(self, tmp_path: Path) -> None:
        code, out, err = _capture(["approve", "check", "--document", "nonexistent", "--repo", str(tmp_path)])
        assert code == 0
        assert "No approvals found" in out


# ---------------------------------------------------------------------------
# 7. status shows approval summary
# ---------------------------------------------------------------------------

class TestStatusApprovalSummary:
    def test_shows_count(self, tmp_path: Path) -> None:
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "A", "status": "approved"},
            {"document_id": "doc-2", "role": "legal", "actor": "B", "status": "approved"},
        ])
        status = gather_status(tmp_path)
        assert status.get("approval_summary") is not None
        assert status["approval_summary"]["total_records"] == 2
        assert status["approval_summary"]["document_count"] == 2

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_formatted_shows_approval_line(self, _mock, tmp_path: Path) -> None:
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "A", "status": "approved"},
        ])
        output = format_status(gather_status(tmp_path))
        assert "1 record" in output
        assert "approve", "list" in output

    @patch("saturnday.capability_registry.is_available", return_value=False)
    def test_no_approvals(self, _mock, tmp_path: Path) -> None:
        output = format_status(gather_status(tmp_path))
        assert "Approvals: none" in output


# ---------------------------------------------------------------------------
# 8. Unrelated semantics do not regress
# ---------------------------------------------------------------------------

class TestNoRegression:
    def test_approve_list_does_not_mutate(self, tmp_path: Path) -> None:
        import os
        _write_approvals(tmp_path, [
            {"document_id": "doc-1", "role": "eng", "actor": "A", "status": "approved"},
        ])
        before = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                before.add(os.path.join(root, f))
        _capture(["approve", "list", "--repo", str(tmp_path)])
        after = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                after.add(os.path.join(root, f))
        assert before == after
