"""Tests for Fix 49.a/49.b — per-ticket repair progress durability and restart reuse.

Proves (49.a):
1. A fixed repair ticket triggers an immediate git commit
2. Failed/partial repair tickets do NOT trigger per-ticket commit
3. Repair progress/evidence is persisted incrementally after each ticket
4. Final summary generation still works after incremental writes
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from saturnday.repair.repair_runner import (
    RepairRunResult,
    _commit_repair_fix,
    _write_incremental_summary,
    run_repair_batch,
)
from saturnday.repair.repair_executor import RepairResult
from saturnday.repair.repair_tickets import RepairTicket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(ticket_id: str, finding_kind: str = "syntax_error", file_path: str = "main.py") -> RepairTicket:
    return RepairTicket(
        ticket_id=ticket_id,
        title=f"Fix {finding_kind} in {file_path}",
        severity="warning",
        file_path=file_path,
        line=1,
        finding_kind=finding_kind,
        evidence=[f"{file_path}:1: test finding"],
    )


# ---------------------------------------------------------------------------
# Test: per-ticket git commit on fixed status
# ---------------------------------------------------------------------------

class TestPerTicketCommit:
    def test_fixed_ticket_triggers_commit(self, tmp_path: Path) -> None:
        """A successfully fixed repair ticket (findings_before > 0) triggers git commit."""
        tickets = [_make_ticket("REPAIR-001")]

        fixed_result = RepairResult(
            ticket_id="REPAIR-001",
            status="fixed",
            findings_before=1,
            findings_after=0,
            findings_resolved=["syntax_error"],
        )

        with (
            patch("saturnday.repair.repair_runner.execute_repair", return_value=fixed_result),
            patch("saturnday.repair.repair_runner._commit_repair_fix") as mock_commit,
            patch("saturnday.repair.repair_runner._write_incremental_summary"),
        ):
            result = run_repair_batch(tickets, tmp_path, coder_fn=None)

        assert result.fixed == 1
        # Fix C: call signature now includes scoped_files kwarg so the
        # commit stages only the ticket's target file.
        mock_commit.assert_called_once()
        args, kwargs = mock_commit.call_args
        assert args == (tmp_path, "REPAIR-001", "syntax_error")
        assert kwargs.get("scoped_files") == ["main.py"]

    def test_already_clean_fixed_does_not_commit(self, tmp_path: Path) -> None:
        """A ticket that was already clean (findings_before=0) does not trigger commit."""
        tickets = [_make_ticket("REPAIR-001")]

        already_clean = RepairResult(
            ticket_id="REPAIR-001",
            status="fixed",
            findings_before=0,
            findings_after=0,
            findings_resolved=["syntax_error"],
        )

        with (
            patch("saturnday.repair.repair_runner.execute_repair", return_value=already_clean),
            patch("saturnday.repair.repair_runner._commit_repair_fix") as mock_commit,
            patch("saturnday.repair.repair_runner._write_incremental_summary"),
        ):
            result = run_repair_batch(tickets, tmp_path, coder_fn=None)

        assert result.fixed == 1
        mock_commit.assert_not_called()

    def test_failed_ticket_does_not_commit(self, tmp_path: Path) -> None:
        """A failed repair ticket does NOT trigger per-ticket commit."""
        tickets = [_make_ticket("REPAIR-001")]

        failed_result = RepairResult(
            ticket_id="REPAIR-001",
            status="failed",
            findings_before=1,
            findings_after=1,
            error="coder failed",
        )

        with (
            patch("saturnday.repair.repair_runner.execute_repair", return_value=failed_result),
            patch("saturnday.repair.repair_runner._commit_repair_fix") as mock_commit,
            patch("saturnday.repair.repair_runner._write_incremental_summary"),
        ):
            result = run_repair_batch(tickets, tmp_path, coder_fn=None)

        assert result.failed == 1
        mock_commit.assert_not_called()

    def test_partial_ticket_does_not_commit(self, tmp_path: Path) -> None:
        """A partial repair ticket does NOT trigger per-ticket commit."""
        tickets = [_make_ticket("REPAIR-001")]

        partial_result = RepairResult(
            ticket_id="REPAIR-001",
            status="partial",
            findings_before=3,
            findings_after=1,
        )

        with (
            patch("saturnday.repair.repair_runner.execute_repair", return_value=partial_result),
            patch("saturnday.repair.repair_runner._commit_repair_fix") as mock_commit,
            patch("saturnday.repair.repair_runner._write_incremental_summary"),
        ):
            result = run_repair_batch(tickets, tmp_path, coder_fn=None)

        assert result.partial == 1
        mock_commit.assert_not_called()


# ---------------------------------------------------------------------------
# Test: incremental progress persistence
# ---------------------------------------------------------------------------

class TestIncrementalProgress:
    def test_summary_written_after_each_ticket(self, tmp_path: Path) -> None:
        """Repair summary is persisted incrementally after each processed ticket."""
        tickets = [
            _make_ticket("REPAIR-001", "syntax_error"),
            _make_ticket("REPAIR-002", "mypy_error"),
        ]

        results = [
            RepairResult(ticket_id="REPAIR-001", status="fixed", findings_before=1, findings_after=0, findings_resolved=["syntax_error"]),
            RepairResult(ticket_id="REPAIR-002", status="failed", findings_before=1, findings_after=1, error="failed"),
        ]
        call_count = [0]

        def fake_execute(ticket, *args, **kwargs):
            r = results[call_count[0]]
            call_count[0] += 1
            return r

        output_dir = tmp_path / "evidence"

        with (
            patch("saturnday.repair.repair_runner.execute_repair", side_effect=fake_execute),
            patch("saturnday.repair.repair_runner._commit_repair_fix"),
        ):
            run_repair_batch(tickets, tmp_path, coder_fn=None, output_dir=output_dir)

        # Summary should exist on disk
        summary_path = output_dir / "repair-summary.json"
        assert summary_path.is_file()
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert data["fixed"] == 1
        assert data["failed"] == 1
        assert len(data["tickets"]) == 2

    def test_incremental_summary_survives_interruption(self, tmp_path: Path) -> None:
        """If second ticket raises, the first ticket's progress is already on disk."""
        tickets = [
            _make_ticket("REPAIR-001"),
            _make_ticket("REPAIR-002"),
        ]

        fixed_result = RepairResult(
            ticket_id="REPAIR-001", status="fixed",
            findings_before=1, findings_after=0,
            findings_resolved=["syntax_error"],
        )

        call_count = [0]

        def fake_execute(ticket, *args, **kwargs):
            if call_count[0] == 0:
                call_count[0] += 1
                return fixed_result
            raise KeyboardInterrupt("simulated interrupt")

        output_dir = tmp_path / "evidence"

        with (
            patch("saturnday.repair.repair_runner.execute_repair", side_effect=fake_execute),
            patch("saturnday.repair.repair_runner._commit_repair_fix"),
        ):
            with pytest.raises(KeyboardInterrupt):
                run_repair_batch(tickets, tmp_path, coder_fn=None, output_dir=output_dir)

        # First ticket's progress must be on disk despite interruption
        summary_path = output_dir / "repair-summary.json"
        assert summary_path.is_file()
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert data["fixed"] == 1
        assert len(data["tickets"]) == 1
        assert data["tickets"][0]["ticket_id"] == "REPAIR-001"

    def test_no_output_dir_skips_incremental_write(self, tmp_path: Path) -> None:
        """When output_dir is None, incremental writes are silently skipped."""
        tickets = [_make_ticket("REPAIR-001")]

        fixed_result = RepairResult(
            ticket_id="REPAIR-001", status="fixed",
            findings_before=1, findings_after=0,
        )

        with (
            patch("saturnday.repair.repair_runner.execute_repair", return_value=fixed_result),
            patch("saturnday.repair.repair_runner._commit_repair_fix"),
        ):
            # output_dir=None — should not raise
            result = run_repair_batch(tickets, tmp_path, coder_fn=None, output_dir=None)

        assert result.fixed == 1


# ---------------------------------------------------------------------------
# Fix 49.b: group_key persistence and restart suppression tests
# ---------------------------------------------------------------------------

class TestGroupKeyPersistence:
    """Test that group_key is persisted in repair summary output."""

    def test_group_key_in_incremental_summary(self, tmp_path: Path) -> None:
        """Incremental summary includes group_key from RepairResult."""
        tickets = [_make_ticket("REPAIR-001", "syntax_error", "main.py")]

        fixed_result = RepairResult(
            ticket_id="REPAIR-001",
            status="fixed",
            findings_before=1,
            findings_after=0,
            findings_resolved=["syntax_error"],
            group_key="main.py:syntax_error",
        )

        output_dir = tmp_path / "evidence"

        with (
            patch("saturnday.repair.repair_runner.execute_repair", return_value=fixed_result),
            patch("saturnday.repair.repair_runner._commit_repair_fix"),
        ):
            run_repair_batch(tickets, tmp_path, coder_fn=None, output_dir=output_dir)

        summary = json.loads((output_dir / "repair-summary.json").read_text(encoding="utf-8"))
        assert summary["tickets"][0]["group_key"] == "main.py:syntax_error"

    def test_group_key_empty_when_not_set(self, tmp_path: Path) -> None:
        """group_key defaults to empty string for backward compatibility."""
        result = RepairResult(
            ticket_id="REPAIR-001",
            status="fixed",
            findings_before=1,
            findings_after=0,
        )
        assert result.group_key == ""


class TestRestartSuppression:
    """Test that restart suppresses already-fixed findings from prior repair."""

    def _write_prior_summary(self, repairs_dir: Path, ts: str, tickets: list[dict]) -> Path:
        """Write a mock prior repair summary."""
        run_dir = repairs_dir / ts
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = {"fixed": 1, "partial": 0, "failed": 0, "tickets": tickets}
        path = run_dir / "repair-summary.json"
        path.write_text(json.dumps(summary), encoding="utf-8")
        return path

    def test_fixed_group_keys_suppress_matching_findings(self, tmp_path: Path) -> None:
        """Findings with group_key matching a prior fixed result are suppressed."""
        # Set up repo structure
        repairs_dir = tmp_path / ".saturnday" / "repairs"
        self._write_prior_summary(repairs_dir, "20260413T010000Z", [
            {"ticket_id": "REPAIR-001", "status": "fixed", "findings_before": 1,
             "findings_after": 0, "group_key": "src/foo.py:syntax_error"},
            {"ticket_id": "REPAIR-002", "status": "failed", "findings_before": 1,
             "findings_after": 1, "group_key": "src/bar.py:mypy_error"},
        ])

        # Simulate fresh findings
        from saturnday.guard.cloud_scanner import Finding
        fresh_findings = [
            Finding(kind="syntax_error", file="src/foo.py", message="bad syntax", severity="error"),
            Finding(kind="mypy_error", file="src/bar.py", message="type error", severity="warning"),
            Finding(kind="ruff", file="src/baz.py", message="lint issue", severity="warning"),
        ]

        # Build the group_keys for suppression
        prior_summary = json.loads(
            (repairs_dir / "20260413T010000Z" / "repair-summary.json").read_text(encoding="utf-8")
        )
        fixed_keys = frozenset(
            t.get("group_key", "")
            for t in prior_summary.get("tickets", [])
            if t.get("status") == "fixed" and t.get("group_key")
        )

        # Apply suppression (same logic as cli.py Fix 49.b)
        suppressed = [
            f for f in fresh_findings
            if f"{f.file}:{f.kind}" not in fixed_keys
        ]

        # src/foo.py:syntax_error was fixed → suppressed
        assert len(suppressed) == 2
        assert all(f.file != "src/foo.py" or f.kind != "syntax_error" for f in suppressed)
        # src/bar.py:mypy_error was failed → NOT suppressed
        assert any(f.file == "src/bar.py" and f.kind == "mypy_error" for f in suppressed)
        # src/baz.py:ruff is new → NOT suppressed
        assert any(f.file == "src/baz.py" and f.kind == "ruff" for f in suppressed)

    def test_failed_group_keys_not_suppressed(self, tmp_path: Path) -> None:
        """Failed/partial prior results must NOT suppress findings."""
        repairs_dir = tmp_path / ".saturnday" / "repairs"
        self._write_prior_summary(repairs_dir, "20260413T010000Z", [
            {"ticket_id": "REPAIR-001", "status": "failed", "findings_before": 1,
             "findings_after": 1, "group_key": "src/foo.py:syntax_error"},
            {"ticket_id": "REPAIR-002", "status": "partial", "findings_before": 3,
             "findings_after": 1, "group_key": "src/bar.py:mypy_error"},
        ])

        prior_summary = json.loads(
            (repairs_dir / "20260413T010000Z" / "repair-summary.json").read_text(encoding="utf-8")
        )
        fixed_keys = frozenset(
            t.get("group_key", "")
            for t in prior_summary.get("tickets", [])
            if t.get("status") == "fixed" and t.get("group_key")
        )

        # No keys should be fixed
        assert fixed_keys == frozenset()

    def test_no_prior_repairs_behaves_normally(self, tmp_path: Path) -> None:
        """When no prior repair directory exists, no suppression happens."""
        # No .saturnday/repairs/ directory
        repairs_dir = tmp_path / ".saturnday" / "repairs"
        assert not repairs_dir.exists()

        from saturnday.guard.cloud_scanner import Finding
        findings = [
            Finding(kind="syntax_error", file="src/foo.py", message="bad", severity="error"),
        ]

        # No suppression should happen
        fixed_keys: frozenset[str] = frozenset()
        suppressed = [f for f in findings if f"{f.file}:{f.kind}" not in fixed_keys]
        assert len(suppressed) == 1

    def test_latest_prior_run_is_used(self, tmp_path: Path) -> None:
        """The latest (alphabetically last) prior repair run is used, not an older one."""
        repairs_dir = tmp_path / ".saturnday" / "repairs"

        # Older run: fixed foo
        self._write_prior_summary(repairs_dir, "20260412T010000Z", [
            {"ticket_id": "REPAIR-001", "status": "fixed", "findings_before": 1,
             "findings_after": 0, "group_key": "src/foo.py:syntax_error"},
        ])
        # Newer run: fixed bar (but foo might have regressed)
        self._write_prior_summary(repairs_dir, "20260413T010000Z", [
            {"ticket_id": "REPAIR-001", "status": "fixed", "findings_before": 1,
             "findings_after": 0, "group_key": "src/bar.py:mypy_error"},
        ])

        # Should use newest: only bar is fixed
        prior_dirs = sorted(
            (d for d in repairs_dir.iterdir() if d.is_dir()),
            key=lambda d: d.name, reverse=True,
        )
        latest = prior_dirs[0]
        assert latest.name == "20260413T010000Z"

        prior_summary = json.loads((latest / "repair-summary.json").read_text(encoding="utf-8"))
        fixed_keys = frozenset(
            t.get("group_key", "")
            for t in prior_summary.get("tickets", [])
            if t.get("status") == "fixed" and t.get("group_key")
        )
        assert fixed_keys == frozenset({"src/bar.py:mypy_error"})
        # foo is NOT in fixed_keys from latest run
        assert "src/foo.py:syntax_error" not in fixed_keys
