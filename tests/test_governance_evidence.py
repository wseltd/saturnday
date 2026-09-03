"""Tests for C3: governance evidence_path wired into cloud-core evidence.

Covers:
- _run_governance returns a 3-tuple (disposition, findings, evidence_path)
- TicketEvidence has governance_evidence_path field with default ""
- TicketResult has governance_evidence_path field with default ""
- write_run_summary includes governance_evidence_path in per-ticket entries
- write_ticket_evidence serialises governance_evidence_path to JSON
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import RunResult, TicketResult
from saturnday.run.evidence import (
    TicketEvidence,
    write_run_summary,
    write_ticket_evidence,
)


# ---------------------------------------------------------------------------
# TicketEvidence field
# ---------------------------------------------------------------------------

class TestTicketEvidenceField:
    def test_default_is_empty_string(self) -> None:
        ev = TicketEvidence(ticket_id="T001", attempt=1)
        assert ev.governance_evidence_path == ""

    def test_explicit_value_stored(self) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            governance_evidence_path="/tmp/gov/evidence.json",
        )
        assert ev.governance_evidence_path == "/tmp/gov/evidence.json"

    def test_serialised_to_json(self, tmp_path: Path) -> None:
        ev = TicketEvidence(
            ticket_id="T001",
            attempt=1,
            governance_evidence_path="/some/path/evidence.json",
        )
        out = write_ticket_evidence(ev, tmp_path)
        data = json.loads(out.read_text())
        assert data["governance_evidence_path"] == "/some/path/evidence.json"

    def test_empty_path_serialised(self, tmp_path: Path) -> None:
        ev = TicketEvidence(ticket_id="T001", attempt=1)
        out = write_ticket_evidence(ev, tmp_path)
        data = json.loads(out.read_text())
        assert data["governance_evidence_path"] == ""


# ---------------------------------------------------------------------------
# TicketResult field
# ---------------------------------------------------------------------------

class TestTicketResultField:
    def test_default_is_empty_string(self) -> None:
        tr = TicketResult(ticket_id="T001", disposition="PASS")
        assert tr.governance_evidence_path == ""

    def test_explicit_value_stored(self) -> None:
        tr = TicketResult(
            ticket_id="T001",
            disposition="PASS",
            governance_evidence_path="/run/gov/evidence.json",
        )
        assert tr.governance_evidence_path == "/run/gov/evidence.json"


# ---------------------------------------------------------------------------
# write_run_summary includes governance_evidence_path
# ---------------------------------------------------------------------------

class TestWriteRunSummaryGovernanceEvidencePath:
    def test_governance_evidence_path_in_output(self, tmp_path: Path) -> None:
        result = RunResult(
            project_id="proj-x",
            total_tickets=1,
            passed=1,
            ticket_results=(
                TicketResult(
                    ticket_id="T001",
                    disposition="PASS",
                    attempts=1,
                    governance_evidence_path="/evidence/T001.json",
                ),
            ),
        )
        path = write_run_summary(result, tmp_path)
        data = json.loads(path.read_text())
        assert len(data["ticket_results"]) == 1
        entry = data["ticket_results"][0]
        assert "governance_evidence_path" in entry
        assert entry["governance_evidence_path"] == "/evidence/T001.json"

    def test_governance_evidence_path_empty_when_not_set(self, tmp_path: Path) -> None:
        result = RunResult(
            project_id="proj-y",
            total_tickets=1,
            failed=1,
            ticket_results=(
                TicketResult(ticket_id="T002", disposition="FAIL", attempts=3),
            ),
        )
        path = write_run_summary(result, tmp_path)
        data = json.loads(path.read_text())
        entry = data["ticket_results"][0]
        assert entry["governance_evidence_path"] == ""

    def test_governance_evidence_path_present_for_all_tickets(self, tmp_path: Path) -> None:
        result = RunResult(
            project_id="proj-z",
            total_tickets=2,
            passed=1,
            failed=1,
            ticket_results=(
                TicketResult(
                    ticket_id="T001",
                    disposition="PASS",
                    governance_evidence_path="/ev/T001.json",
                ),
                TicketResult(ticket_id="T002", disposition="FAIL"),
            ),
        )
        path = write_run_summary(result, tmp_path)
        data = json.loads(path.read_text())
        for entry in data["ticket_results"]:
            assert "governance_evidence_path" in entry


# ---------------------------------------------------------------------------
# _run_governance returns 3-tuple
# ---------------------------------------------------------------------------

class TestRunGovernanceReturnsTuple:
    """Test _run_governance with a mocked saturnday package."""

    def _make_mock_pack(
        self,
        disposition: str = "PASS",
        findings: list[dict] | None = None,
    ) -> MagicMock:
        pack = MagicMock()
        pack.disposition = disposition
        check = MagicMock()
        check.findings = findings or []
        pack.check_results = [check]
        return pack

    def test_returns_three_tuple(self, tmp_path: Path) -> None:
        """_run_governance must return (disposition, findings, evidence_path)."""
        from saturnday.ticket_runner import _run_governance

        mock_pack = self._make_mock_pack("PASS")
        mock_evidence_path = tmp_path / "evidence.json"

        # Build a fake saturnday module tree
        fake_saturnday = types.ModuleType("saturnday")
        fake_governance_mod = types.ModuleType("saturnday.governance")
        fake_governance_mod.run_governance_check = MagicMock(  # type: ignore[attr-defined]
            return_value=(mock_pack, mock_evidence_path)
        )
        fake_saturnday.governance = fake_governance_mod  # type: ignore[attr-defined]

        with patch.dict(
            "sys.modules",
            {
                "saturnday": fake_saturnday,
                "saturnday.governance": fake_governance_mod,
            },
        ):
            result = _run_governance(tmp_path)

        assert isinstance(result, tuple)
        assert len(result) == 4
        disposition, findings, evidence_path, _reasons = result
        assert disposition == "PASS"
        assert isinstance(findings, list)
        assert evidence_path == str(mock_evidence_path)

    def test_evidence_path_is_string(self, tmp_path: Path) -> None:
        """evidence_path element must always be a str, not a Path."""
        from saturnday.ticket_runner import _run_governance

        mock_pack = self._make_mock_pack("FAIL", [{"path": "foo.py", "message": "bad"}])
        mock_evidence_path = tmp_path / "subdir" / "evidence.json"

        fake_saturnday = types.ModuleType("saturnday")
        fake_governance_mod = types.ModuleType("saturnday.governance")
        fake_governance_mod.run_governance_check = MagicMock(  # type: ignore[attr-defined]
            return_value=(mock_pack, mock_evidence_path)
        )
        fake_saturnday.governance = fake_governance_mod  # type: ignore[attr-defined]

        with patch.dict(
            "sys.modules",
            {
                "saturnday": fake_saturnday,
                "saturnday.governance": fake_governance_mod,
            },
        ):
            _, _, evidence_path, _ = _run_governance(tmp_path)

        assert isinstance(evidence_path, str)
        assert "evidence.json" in evidence_path

    def test_findings_collected_from_check_results(self, tmp_path: Path) -> None:
        """findings list must aggregate from all check_results."""
        from saturnday.ticket_runner import _run_governance

        mock_pack = MagicMock()
        mock_pack.disposition = "FAIL"
        check_a = MagicMock()
        check_a.findings = [{"path": "a.py", "message": "issue-a"}]
        check_b = MagicMock()
        check_b.findings = [{"path": "b.py", "message": "issue-b"}]
        mock_pack.check_results = [check_a, check_b]

        fake_saturnday = types.ModuleType("saturnday")
        fake_governance_mod = types.ModuleType("saturnday.governance")
        fake_governance_mod.run_governance_check = MagicMock(  # type: ignore[attr-defined]
            return_value=(mock_pack, tmp_path / "ev.json")
        )
        fake_saturnday.governance = fake_governance_mod  # type: ignore[attr-defined]

        with patch.dict(
            "sys.modules",
            {
                "saturnday": fake_saturnday,
                "saturnday.governance": fake_governance_mod,
            },
        ):
            disposition, findings, _, _ = _run_governance(tmp_path)

        assert disposition == "FAIL"
        assert len(findings) == 2
        messages = {f["message"] for f in findings}
        assert messages == {"issue-a", "issue-b"}
