"""Tests for repair CLI: serialization, batch runner, evidence, CLI parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday.repair.repair_tickets import RepairTicket
from saturnday.repair.repair_runner import run_repair_batch, RepairRunResult
from saturnday.repair.repair_executor import RepairResult
from saturnday.repair.repair_evidence import write_repair_evidence


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestRepairTicketSerialization:
    def test_round_trip(self) -> None:
        """to_dict followed by from_dict yields an identical ticket."""
        ticket = RepairTicket(
            ticket_id="REPAIR-001",
            title="Fix shell danger",
            severity="high",
            file_path="run.py",
            line=10,
            finding_kind="shell_danger",
            evidence=["run.py:10: subprocess.call"],
            remediation={"why": "injection risk", "fix": "use subprocess.run"},
            group_key="run.py:shell_danger",
        )
        restored = RepairTicket.from_dict(ticket.to_dict())
        assert restored.ticket_id == ticket.ticket_id
        assert restored.finding_kind == ticket.finding_kind
        assert restored.evidence == ticket.evidence
        assert restored.remediation == ticket.remediation

    def test_round_trip_all_fields(self) -> None:
        """Every field survives a serialisation round-trip."""
        ticket = RepairTicket(
            ticket_id="REPAIR-042",
            title="Fix credential_leak",
            severity="high",
            file_path="secrets.py",
            line=99,
            finding_kind="credential_leak",
            evidence=["secrets.py:99: hardcoded token"],
            remediation={"why": "data breach", "fix": "use env vars", "patch": None},
            group_key="secrets.py:credential_leak",
        )
        restored = RepairTicket.from_dict(ticket.to_dict())
        assert restored.ticket_id == ticket.ticket_id
        assert restored.title == ticket.title
        assert restored.severity == ticket.severity
        assert restored.file_path == ticket.file_path
        assert restored.line == ticket.line
        assert restored.group_key == ticket.group_key

    def test_from_dict_missing_ticket_id_raises(self) -> None:
        """from_dict raises ValueError when ticket_id is absent."""
        with pytest.raises(ValueError, match="ticket_id"):
            RepairTicket.from_dict({"finding_kind": "x", "file_path": "y"})

    def test_from_dict_missing_finding_kind_raises(self) -> None:
        """from_dict raises ValueError when finding_kind is absent."""
        with pytest.raises(ValueError, match="finding_kind"):
            RepairTicket.from_dict({"ticket_id": "R1", "file_path": "y"})

    def test_from_dict_missing_file_path_raises(self) -> None:
        """from_dict raises ValueError when file_path is absent."""
        with pytest.raises(ValueError, match="file_path"):
            RepairTicket.from_dict({"ticket_id": "R1", "finding_kind": "x"})

    def test_from_dict_defaults(self) -> None:
        """from_dict applies safe defaults for optional fields."""
        t = RepairTicket.from_dict(
            {"ticket_id": "R1", "finding_kind": "x", "file_path": "f.py"}
        )
        assert t.title == ""
        assert t.severity == "medium"
        assert t.remediation is None
        assert t.evidence == []
        assert t.group_key == ""
        assert t.line is None

    def test_to_dict_json_serialisable(self) -> None:
        """to_dict output is valid JSON with no non-serialisable values."""
        ticket = RepairTicket(
            ticket_id="REPAIR-001",
            title="Test",
            severity="low",
            file_path="a.py",
            line=None,
            finding_kind="missing_docs",
        )
        payload = json.dumps(ticket.to_dict())
        recovered = json.loads(payload)
        assert recovered["ticket_id"] == "REPAIR-001"
        assert recovered["line"] is None


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


class TestRepairRunner:
    def _make_ticket(self, tid: str = "REPAIR-001") -> RepairTicket:
        return RepairTicket(
            ticket_id=tid,
            title="test",
            severity="high",
            file_path="f.py",
            line=1,
            finding_kind="shell_danger",
        )

    def _fixed_result(self, tid: str) -> RepairResult:
        return RepairResult(
            ticket_id=tid,
            status="fixed",
            findings_before=1,
            findings_after=0,
            findings_resolved=["shell_danger"],
        )

    def _failed_result(self, tid: str) -> RepairResult:
        return RepairResult(
            ticket_id=tid,
            status="failed",
            findings_before=1,
            findings_after=1,
            findings_resolved=[],
        )

    def test_all_fixed(self) -> None:
        """All tickets fixed → fixed==2, failed==0, stopped_early==False."""
        tickets = [self._make_ticket("R1"), self._make_ticket("R2")]

        def mock_execute(ticket: RepairTicket, skill_path: Path, coder_fn, **kwargs) -> RepairResult:
            return self._fixed_result(ticket.ticket_id)

        with patch(
            "saturnday.repair.repair_runner.execute_repair",
            side_effect=mock_execute,
        ):
            result = run_repair_batch(tickets, Path("/tmp"), lambda p, f, r: "")

        assert result.fixed == 2
        assert result.failed == 0
        assert not result.stopped_early
        assert result.stop_reason == ""
        assert len(result.results) == 2

    def test_stop_on_max_failures(self) -> None:
        """3 failures of same kind skips remaining tickets of that kind."""
        tickets = [self._make_ticket(f"R{i}") for i in range(4)]

        def mock_execute(ticket: RepairTicket, skill_path: Path, coder_fn, **kwargs) -> RepairResult:
            return self._failed_result(ticket.ticket_id)

        with patch(
            "saturnday.repair.repair_runner.execute_repair",
            side_effect=mock_execute,
        ):
            result = run_repair_batch(
                tickets, Path("/tmp"), lambda p, f, r: "", max_failures=3
            )

        # Per-kind tracking: after 3 shell_danger failures, ticket 4 is skipped
        assert result.failed == 4
        assert len(result.results) == 4

    def test_zero_tickets(self) -> None:
        """Empty ticket list → zeroed counters, no stop condition."""
        result = run_repair_batch([], Path("/tmp"), None)
        assert result.total_tickets == 0
        assert result.fixed == 0
        assert result.failed == 0
        assert result.partial == 0
        assert not result.stopped_early

    def test_mixed_outcomes_no_stop(self) -> None:
        """Interleaved fixed/failed resets consecutive counter; no early stop."""
        statuses = ["fixed", "failed", "fixed", "failed"]
        tickets = [self._make_ticket(f"R{i}") for i in range(4)]

        def mock_execute(ticket: RepairTicket, skill_path: Path, coder_fn, **kwargs) -> RepairResult:
            idx = int(ticket.ticket_id[1])  # R0 → 0, R1 → 1, ...
            status = statuses[idx]
            if status == "fixed":
                return self._fixed_result(ticket.ticket_id)
            return self._failed_result(ticket.ticket_id)

        with patch(
            "saturnday.repair.repair_runner.execute_repair",
            side_effect=mock_execute,
        ):
            result = run_repair_batch(
                tickets, Path("/tmp"), lambda p, f, r: "", max_failures=3
            )

        assert result.fixed == 2
        assert result.failed == 2
        assert not result.stopped_early  # consecutive never hit 3

    def test_total_tickets_matches_input(self) -> None:
        """RepairRunResult.total_tickets reflects the submitted ticket count."""
        tickets = [self._make_ticket(f"R{i}") for i in range(5)]

        def mock_execute(ticket: RepairTicket, skill_path: Path, coder_fn, **kwargs) -> RepairResult:
            return self._fixed_result(ticket.ticket_id)

        with patch(
            "saturnday.repair.repair_runner.execute_repair",
            side_effect=mock_execute,
        ):
            result = run_repair_batch(tickets, Path("/tmp"), lambda p, f, r: "")

        assert result.total_tickets == 5


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


class TestRepairEvidence:
    def test_writes_both_files(self, tmp_path: Path) -> None:
        """write_repair_evidence creates repair-metadata.json and repair-summary.json."""
        run_result = RepairRunResult(
            results=[
                RepairResult("R1", "fixed", 2, 0, ["shell_danger"]),
                RepairResult("R2", "failed", 1, 1, [], error="coder timeout"),
            ],
            total_tickets=2,
            fixed=1,
            partial=0,
            failed=1,
        )
        paths = write_repair_evidence(
            run_result, tmp_path, backend="codex-cli", skill_path="/tmp/skill"
        )
        assert paths["metadata"].exists()
        assert paths["summary"].exists()

    def test_metadata_fields(self, tmp_path: Path) -> None:
        """repair-metadata.json contains backend, skill_path, and ticket_count."""
        run_result = RepairRunResult(
            results=[RepairResult("R1", "fixed", 1, 0, ["shell_danger"])],
            total_tickets=1,
            fixed=1,
        )
        paths = write_repair_evidence(
            run_result, tmp_path, backend="codex-cli", skill_path="/tmp/skill"
        )
        meta = json.loads(paths["metadata"].read_text())
        assert meta["backend"] == "codex-cli"
        assert meta["ticket_count"] == 1
        assert meta["skill_path"] == "/tmp/skill"
        assert "timestamp" in meta

    def test_summary_fields(self, tmp_path: Path) -> None:
        """repair-summary.json contains fixed, failed, partial, and tickets array."""
        run_result = RepairRunResult(
            results=[
                RepairResult("R1", "fixed", 2, 0, ["shell_danger"]),
                RepairResult("R2", "failed", 1, 1, [], error="coder timeout"),
            ],
            total_tickets=2,
            fixed=1,
            partial=0,
            failed=1,
        )
        paths = write_repair_evidence(
            run_result, tmp_path, backend="codex-cli", skill_path="/tmp/skill"
        )
        summary = json.loads(paths["summary"].read_text())
        assert summary["fixed"] == 1
        assert summary["failed"] == 1
        assert summary["partial"] == 0
        assert len(summary["tickets"]) == 2

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        """write_repair_evidence creates the output directory if it does not exist."""
        nested = tmp_path / "deep" / "nested" / "dir"
        run_result = RepairRunResult(total_tickets=0)
        paths = write_repair_evidence(run_result, nested)
        assert nested.exists()
        assert paths["metadata"].exists()
        assert paths["summary"].exists()

    def test_summary_json_is_valid(self, tmp_path: Path) -> None:
        """repair-summary.json is valid JSON and round-trips without error."""
        run_result = RepairRunResult(
            results=[RepairResult("R1", "fixed", 1, 0, ["shell_danger"])],
            total_tickets=1,
            fixed=1,
        )
        paths = write_repair_evidence(run_result, tmp_path)
        raw = paths["summary"].read_text()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


class TestRepairCLIParsing:
    def test_repair_help(self, capsys) -> None:
        """repair --help exits 0."""
        from saturnday.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["repair", "--help"])
        assert exc.value.code == 0

    def test_repair_dry_run_no_findings(self, tmp_path: Path) -> None:
        """Dry run on a clean skill with no findings returns 0."""
        from saturnday.cli import main

        skill = tmp_path / "clean-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "# Clean\n\nA clean skill.\n\n## Usage\nRun it.\n"
        )
        (skill / "clean.py").write_text("def add(a, b):\n    return a + b\n")
        out = tmp_path / "output"
        result = main(
            ["repair", "--dry-run", "--skill", str(skill), "--output-dir", str(out)]
        )
        assert result == 0

    def test_repair_dry_run_produces_plan(self, tmp_path: Path) -> None:
        """Dry run on a vulnerable skill writes a repair-plan.json to output-dir."""
        from saturnday.cli import main

        skill = tmp_path / "vuln-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "# Vuln Skill\n\nA test.\n\n## Usage\nRun it.\n"
        )
        (skill / "run.py").write_text(
            "import subprocess\n"
            "def execute(cmd):\n"
            "    subprocess.call(cmd, shell=True)\n"
        )
        out = tmp_path / "output"
        result = main(
            ["repair", "--dry-run", "--skill", str(skill), "--output-dir", str(out)]
        )
        assert result == 0
        plan_path = out / "repair-plan.json"
        assert plan_path.exists()
        plan = json.loads(plan_path.read_text())
        assert "tickets" in plan
        assert len(plan["tickets"]) > 0
        assert any(t["finding_kind"] == "shell_danger" for t in plan["tickets"])

    def test_repair_without_backend_fails(self, tmp_path: Path) -> None:
        """Non-dry-run without --backend should return 1."""
        from saturnday.cli import main

        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# S\n\nDesc.\n\n## Usage\nRun.\n")
        (skill / "bad.py").write_text(
            "import subprocess\nsubprocess.call('rm -rf /', shell=True)\n"
        )
        result = main(["repair", "--skill", str(skill)])
        assert result == 1

    def test_repair_cli_is_registered(self) -> None:
        """repair subcommand is present in the top-level parser."""
        from saturnday.cli import build_parser as _build_parser

        parser = _build_parser()
        # Ensure repair is a known subcommand by checking parse does not error
        args = parser.parse_args(
            ["repair", "--skill", "/tmp/skill", "--dry-run"]
        )
        assert args.command == "repair"
        assert args.dry_run is True
