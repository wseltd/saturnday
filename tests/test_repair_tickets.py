"""Tests for saturnday.guard.repair_tickets."""

from __future__ import annotations

from saturnday.guard.cloud_scanner import Finding
from saturnday.repair.repair_tickets import generate_repair_tickets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    *,
    kind: str = "hardcoded_secret",
    file: str = "run.py",
    line: int | None = 1,
    severity: str = "high",
    message: str = "Dangerous call",
    remediation: dict | None = None,
) -> Finding:
    return Finding(
        check=kind,
        severity=severity,
        file=file,
        line=line,
        message=message,
        kind=kind,
        remediation=remediation,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGenerateRepairTickets:
    def test_single_finding_single_ticket(self) -> None:
        """1 finding → 1 ticket."""
        findings = [_finding()]
        tickets = generate_repair_tickets(findings)
        assert len(tickets) == 1
        assert tickets[0].ticket_id == "REPAIR-001"

    def test_same_file_same_kind_grouped(self) -> None:
        """3 findings of the same file and kind → 1 ticket with 3 evidence entries."""
        findings = [
            _finding(file="run.py", kind="hardcoded_secret", line=1),
            _finding(file="run.py", kind="hardcoded_secret", line=2),
            _finding(file="run.py", kind="hardcoded_secret", line=3),
        ]
        tickets = generate_repair_tickets(findings)
        assert len(tickets) == 1
        assert len(tickets[0].evidence) == 3

    def test_different_files_separate_tickets(self) -> None:
        """Findings in 2 different files → 2 tickets."""
        findings = [
            _finding(file="run.py", kind="hardcoded_secret", line=1),
            _finding(file="helper.py", kind="hardcoded_secret", line=5),
        ]
        tickets = generate_repair_tickets(findings)
        assert len(tickets) == 2
        file_paths = {t.file_path for t in tickets}
        assert "run.py" in file_paths
        assert "helper.py" in file_paths

    def test_large_group_splits(self) -> None:
        """15 findings of the same file/kind → 2 tickets (10 + 5)."""
        findings = [
            _finding(file="run.py", kind="hardcoded_secret", line=i)
            for i in range(1, 16)
        ]
        tickets = generate_repair_tickets(findings, max_per_ticket=10)
        assert len(tickets) == 2
        assert len(tickets[0].evidence) == 10
        assert len(tickets[1].evidence) == 5

    def test_ticket_ids_sequential(self) -> None:
        """Ticket IDs are REPAIR-001, REPAIR-002, … in order."""
        findings = [
            _finding(file="a.py", kind="hardcoded_secret", line=1),
            _finding(file="b.py", kind="hardcoded_secret", line=2),
            _finding(file="c.py", kind="jwt_literal_secret", line=3),
        ]
        tickets = generate_repair_tickets(findings)
        ids = [t.ticket_id for t in tickets]
        assert ids == ["REPAIR-001", "REPAIR-002", "REPAIR-003"]

    def test_traceability_fields_preserved(self) -> None:
        """severity, file_path, and finding_kind are faithfully copied."""
        findings = [
            _finding(
                file="run.py",
                kind="jwt_literal_secret",
                severity="high",
                line=10,
            )
        ]
        tickets = generate_repair_tickets(findings)
        t = tickets[0]
        assert t.severity == "high"
        assert t.file_path == "run.py"
        assert t.finding_kind == "jwt_literal_secret"
        assert t.line == 10

    def test_group_by_check(self) -> None:
        """group_by='check' groups findings from different files under one ticket."""
        findings = [
            _finding(file="a.py", kind="hardcoded_secret", line=1),
            _finding(file="b.py", kind="hardcoded_secret", line=2),
            _finding(file="c.py", kind="hardcoded_secret", line=3),
        ]
        tickets = generate_repair_tickets(findings, group_by="check")
        assert len(tickets) == 1
        assert len(tickets[0].evidence) == 3
        assert "across repo" in tickets[0].title

    def test_remediation_attached(self) -> None:
        """Finding with remediation dict → ticket has remediation populated."""
        rem = {"why": "why it matters", "fix": "how to fix", "patch": None}
        findings = [_finding(remediation=rem)]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].remediation == rem

    def test_no_remediation_when_absent(self) -> None:
        """Finding without remediation → ticket.remediation is None."""
        findings = [_finding(remediation=None)]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].remediation is None

    def test_evidence_format_with_line(self) -> None:
        """Evidence strings include 'file:line: message' when line is present."""
        findings = [_finding(file="run.py", line=42, message="Bad call")]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].evidence[0] == "run.py:42: Bad call"

    def test_evidence_format_without_line(self) -> None:
        """Evidence strings use 'file: message' when line is None."""
        findings = [_finding(file="SKILL.md", line=None, message="Missing heading")]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].evidence[0] == "SKILL.md: Missing heading"

    def test_empty_findings_returns_empty(self) -> None:
        """No findings → no tickets."""
        tickets = generate_repair_tickets([])
        assert tickets == []

    def test_group_key_populated(self) -> None:
        """group_key is set on each ticket."""
        findings = [_finding(file="run.py", kind="hardcoded_secret")]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].group_key == "run.py:hardcoded_secret"

    def test_group_key_check_mode(self) -> None:
        """group_key is the kind string in check mode."""
        findings = [_finding(file="run.py", kind="hardcoded_secret")]
        tickets = generate_repair_tickets(findings, group_by="check")
        assert tickets[0].group_key == "hardcoded_secret"
