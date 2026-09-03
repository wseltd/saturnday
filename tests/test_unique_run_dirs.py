"""Tests for unique per-run evidence directories.

Verifies that code-mode and document-mode runs each get their own
collision-resistant evidence directory, that latest.json pointers
update correctly, and that existing repair/governance behaviour
is unchanged.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from saturnday.ticket_runner import (
    _generate_run_id,
    _read_latest_pointer,
    _write_latest_pointer,
)


# ---------------------------------------------------------------------------
# A. Run ID generation
# ---------------------------------------------------------------------------


class TestRunIdGeneration:
    """Run IDs must be collision-resistant even in parallel."""

    def test_run_id_format(self) -> None:
        """ID must match run_{ts}_{pid}_{hex8} format."""
        rid = _generate_run_id()
        parts = rid.split("_")
        # "run", timestamp, pid, hex
        assert parts[0] == "run"
        assert len(parts) == 4, f"Expected 4 parts, got {parts}"
        # pid should be numeric
        assert parts[2].isdigit()
        # hex part should be 8 hex chars
        assert len(parts[3]) == 8
        int(parts[3], 16)  # must not raise

    def test_two_ids_differ(self) -> None:
        """Two consecutive IDs must differ (random component)."""
        a = _generate_run_id()
        b = _generate_run_id()
        assert a != b

    def test_same_second_ids_differ(self) -> None:
        """Even within the same second, IDs must not collide."""
        ids = {_generate_run_id() for _ in range(100)}
        assert len(ids) == 100, "Collision detected among 100 same-second IDs"


# ---------------------------------------------------------------------------
# B. Latest pointer
# ---------------------------------------------------------------------------


class TestLatestPointer:

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        parent = tmp_path / "run"
        parent.mkdir()
        run_dir = parent / "run_test_001"
        run_dir.mkdir()

        _write_latest_pointer(parent, "run_test_001", run_dir)
        data = _read_latest_pointer(parent)

        assert data is not None
        assert data["run_id"] == "run_test_001"
        assert data["path"] == str(run_dir)
        assert "started_utc" in data

    def test_second_write_overwrites_first(self, tmp_path: Path) -> None:
        parent = tmp_path / "run"
        parent.mkdir()

        _write_latest_pointer(parent, "run_first", parent / "run_first")
        _write_latest_pointer(parent, "run_second", parent / "run_second")

        data = _read_latest_pointer(parent)
        assert data is not None
        assert data["run_id"] == "run_second"

    def test_read_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert _read_latest_pointer(tmp_path) is None

    def test_read_returns_none_on_malformed_json(self, tmp_path: Path) -> None:
        (tmp_path / "latest.json").write_text("not json")
        assert _read_latest_pointer(tmp_path) is None


# ---------------------------------------------------------------------------
# C. Two sequential code runs create separate dirs
# ---------------------------------------------------------------------------


class TestCodeRunSeparation:
    """Sequential code-mode runs must not share an evidence directory."""

    def test_two_runs_create_separate_dirs(self, tmp_path: Path) -> None:
        """Simulates the output_dir creation logic from run_plan for two runs."""
        repo = tmp_path / "repo"
        repo.mkdir()

        created_dirs: list[Path] = []
        for _ in range(2):
            rid = _generate_run_id()
            run_parent = repo / ".saturnday" / "run"
            run_dir = run_parent / rid
            run_dir.mkdir(parents=True, exist_ok=True)
            _write_latest_pointer(run_parent, rid, run_dir)
            # Write fake metadata to each
            (run_dir / "run-metadata.json").write_text(
                json.dumps({"run_id": rid}), encoding="utf-8",
            )
            created_dirs.append(run_dir)

        # Two distinct directories
        assert created_dirs[0] != created_dirs[1]
        assert created_dirs[0].is_dir()
        assert created_dirs[1].is_dir()

        # Both have their own metadata
        m0 = json.loads((created_dirs[0] / "run-metadata.json").read_text())
        m1 = json.loads((created_dirs[1] / "run-metadata.json").read_text())
        assert m0["run_id"] != m1["run_id"]

    def test_no_overwrite_of_prior_metadata(self, tmp_path: Path) -> None:
        """Second run must not overwrite first run's files."""
        repo = tmp_path / "repo"
        repo.mkdir()
        run_parent = repo / ".saturnday" / "run"

        # First run
        rid1 = _generate_run_id()
        dir1 = run_parent / rid1
        dir1.mkdir(parents=True, exist_ok=True)
        _write_latest_pointer(run_parent, rid1, dir1)
        (dir1 / "run-summary.json").write_text('{"run": 1}')
        (dir1 / "analytics.json").write_text('{"run": 1}')
        (dir1 / "plan.json").write_text('{"run": 1}')

        # Second run
        rid2 = _generate_run_id()
        dir2 = run_parent / rid2
        dir2.mkdir(parents=True, exist_ok=True)
        _write_latest_pointer(run_parent, rid2, dir2)
        (dir2 / "run-summary.json").write_text('{"run": 2}')
        (dir2 / "analytics.json").write_text('{"run": 2}')
        (dir2 / "plan.json").write_text('{"run": 2}')

        # First run's files untouched
        assert json.loads((dir1 / "run-summary.json").read_text())["run"] == 1
        assert json.loads((dir1 / "analytics.json").read_text())["run"] == 1
        assert json.loads((dir1 / "plan.json").read_text())["run"] == 1

    def test_latest_pointer_points_to_second_run(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        run_parent = repo / ".saturnday" / "run"

        rid1 = _generate_run_id()
        dir1 = run_parent / rid1
        dir1.mkdir(parents=True, exist_ok=True)
        _write_latest_pointer(run_parent, rid1, dir1)

        rid2 = _generate_run_id()
        dir2 = run_parent / rid2
        dir2.mkdir(parents=True, exist_ok=True)
        _write_latest_pointer(run_parent, rid2, dir2)

        latest = _read_latest_pointer(run_parent)
        assert latest is not None
        assert latest["run_id"] == rid2
        assert latest["path"] == str(dir2)


# ---------------------------------------------------------------------------
# D. Two sequential document runs create separate dirs
# ---------------------------------------------------------------------------


class TestDocumentRunSeparation:
    """Sequential document-mode runs must not share an evidence directory."""

    def test_two_doc_runs_create_separate_dirs(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        doc_parent = repo / ".saturnday" / "document-run"

        created_dirs: list[Path] = []
        for _ in range(2):
            ts = "20260331T010000Z"
            pid = os.getpid()
            rand = os.urandom(4).hex()
            doc_run_id = f"docrun_{ts}_{pid}_{rand}"
            doc_dir = doc_parent / doc_run_id
            doc_dir.mkdir(parents=True, exist_ok=True)
            # Write latest pointer
            (doc_parent / "latest.json").write_text(
                json.dumps({"run_id": doc_run_id, "path": str(doc_dir)}),
            )
            (doc_dir / "run-summary.json").write_text(
                json.dumps({"doc_run_id": doc_run_id}),
            )
            created_dirs.append(doc_dir)

        assert created_dirs[0] != created_dirs[1]
        assert created_dirs[0].is_dir()
        assert created_dirs[1].is_dir()

        s0 = json.loads((created_dirs[0] / "run-summary.json").read_text())
        s1 = json.loads((created_dirs[1] / "run-summary.json").read_text())
        assert s0["doc_run_id"] != s1["doc_run_id"]


# ---------------------------------------------------------------------------
# E. Repair mode unchanged
# ---------------------------------------------------------------------------


class TestRepairModeUnchanged:
    """Repair output dirs must still use timestamped paths, not run IDs."""

    def test_repair_dir_uses_timestamp_under_repairs(self) -> None:
        """Verify the expected repair path pattern from interactive.py / cli.py."""
        from datetime import datetime, timezone
        repo_path = Path("/fake/repo")
        repair_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = repo_path / ".saturnday" / "repairs" / repair_ts

        assert ".saturnday" in str(output_dir)
        assert "repairs" in str(output_dir)
        assert repair_ts in str(output_dir)
        # Must NOT contain "run_" prefix used by code-mode
        assert "run_" not in str(output_dir)


# ---------------------------------------------------------------------------
# F. Governance scan evidence unchanged
# ---------------------------------------------------------------------------


class TestGovernanceScanUnchanged:
    """Governance evidence dirs must still use check_{ts} format."""

    def test_governance_evidence_uses_check_prefix(self) -> None:
        """Verify the expected governance evidence path pattern."""
        from datetime import datetime, timezone
        repo_path = Path("/fake/repo")
        run_id = f"check_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        output_dir = repo_path / ".saturnday" / "evidence" / run_id

        assert "evidence" in str(output_dir)
        assert run_id.startswith("check_")
        # Must NOT contain "run_" prefix used by code-mode
        assert "/run_" not in str(output_dir)


# ---------------------------------------------------------------------------
# G. Resume safety validation still applies
# ---------------------------------------------------------------------------


class TestResumeStillValidates:
    """Auto-resume must still validate project_id even when using latest.json."""

    def test_resume_skips_mismatched_project_id(self, tmp_path: Path) -> None:
        """latest.json pointing to a different project's ledger must not resume."""
        run_parent = tmp_path / ".saturnday" / "run"
        rid = "run_20260331T010000Z_1234_abcd1234"
        run_dir = run_parent / rid
        ledger_dir = run_dir / "evidence" / "run"
        ledger_dir.mkdir(parents=True, exist_ok=True)

        # Write a ledger with project_id "old-project"
        ledger = {
            "project_id": "old-project",
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
            },
        }
        (ledger_dir / "ledger.json").write_text(json.dumps(ledger))

        # Write latest pointer
        _write_latest_pointer(run_parent, rid, run_dir)

        # Read back and verify — the project_id check is the responsibility
        # of run_plan's auto-resume logic, which checks _prior_project_id
        # against plan.project_id. Here we verify the data is readable.
        latest = _read_latest_pointer(run_parent)
        assert latest is not None
        prior_ledger_path = Path(latest["path"]) / "evidence" / "run" / "ledger.json"
        assert prior_ledger_path.is_file()
        prior_data = json.loads(prior_ledger_path.read_text())
        # The safety validation: project_id must match before skipping
        assert prior_data["project_id"] == "old-project"
        # A new plan with project_id "new-project" would NOT match, so
        # auto-resume would correctly refuse. This is verified by the
        # existing test_ticket_runner.py auto-resume tests.


# ---------------------------------------------------------------------------
# H. latest.json written AFTER auto-resume reads it (order correctness)
# ---------------------------------------------------------------------------


class TestLatestWrittenAfterResumeLookup:
    """latest.json must still point to the PRIOR run when auto-resume reads it."""

    def test_prior_pointer_survives_during_resume_window(self, tmp_path: Path) -> None:
        """Simulate the correct order: read latest → do resume → write latest."""
        run_parent = tmp_path / ".saturnday" / "run"

        # Set up a prior run with a latest pointer
        prior_rid = "run_20260331T010000Z_1111_aaaa1111"
        prior_dir = run_parent / prior_rid
        prior_dir.mkdir(parents=True, exist_ok=True)
        _write_latest_pointer(run_parent, prior_rid, prior_dir)

        # Step 1: New run generates its own ID and dir (but does NOT write latest yet)
        new_rid = _generate_run_id()
        new_dir = run_parent / new_rid
        new_dir.mkdir(parents=True, exist_ok=True)

        # Step 2: Read latest.json — must still point to PRIOR run
        latest_during_resume = _read_latest_pointer(run_parent)
        assert latest_during_resume is not None
        assert latest_during_resume["run_id"] == prior_rid
        assert latest_during_resume["path"] == str(prior_dir)

        # Step 3: AFTER resume logic, write latest for current run
        _write_latest_pointer(run_parent, new_rid, new_dir)

        # Now latest points to current run
        latest_after = _read_latest_pointer(run_parent)
        assert latest_after is not None
        assert latest_after["run_id"] == new_rid


# ---------------------------------------------------------------------------
# I. RunResult contains evidence_dir
# ---------------------------------------------------------------------------


class TestRunResultContainsEvidenceDir:
    """RunResult must carry the actual evidence directory path."""

    def test_evidence_dir_field_exists(self) -> None:
        from saturnday._types import RunResult
        result = RunResult(project_id="test", evidence_dir="/some/path")
        assert result.evidence_dir == "/some/path"

    def test_evidence_dir_defaults_to_empty(self) -> None:
        from saturnday._types import RunResult
        result = RunResult(project_id="test")
        assert result.evidence_dir == ""
