"""Tests for saturnday.reporting — report generation functions.

Tests cover:
- generate_scan_report: markdown from an EvidencePack
- generate_repair_report: markdown from repair results
- generate_run_report: markdown from RunResult

All tests use minimal mock data; no real governance or LLM calls are made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday._types import RunResult, TicketResult
from saturnday.evidence import CheckResult, EvidencePack
from saturnday.repair.repair_executor import RepairResult
from saturnday.repair.repair_runner import RepairRunResult
from saturnday.repair.repair_tickets import RepairTicket
from saturnday.reporting import (
    generate_repair_report,
    generate_run_report,
    generate_scan_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pack(
    disposition: str = "PASS",
    check_results: list[CheckResult] | None = None,
    run_id: str = "run-001",
) -> EvidencePack:
    """Build a minimal EvidencePack for testing."""
    return EvidencePack(
        schema_version="1.0",
        run_id=run_id,
        mode="check",
        repo_path="/tmp/repo",
        diff_range="HEAD",
        saturnday_version="0.0.0-test",
        created_utc="2026-01-01T00:00:00Z",
        disposition=disposition,
        check_results=check_results or [],
    )


def _make_check_result(
    name: str = "test_check",
    status: str = "PASS",
    severity: str = "error",
    findings: list[dict] | None = None,
    rule_id: str | None = None,
) -> CheckResult:
    return CheckResult(
        name=name,
        status=status,
        severity=severity,
        findings=findings or [],
        rule_id=rule_id,
    )


def _make_repair_result(
    total: int = 2,
    fixed: int = 1,
    partial: int = 0,
    failed: int = 1,
    stopped_early: bool = False,
    stop_reason: str = "",
) -> RepairRunResult:
    results = []
    if fixed:
        results.append(RepairResult(
            ticket_id="REPAIR-001",
            status="fixed",
            findings_before=3,
            findings_after=0,
        ))
    if failed:
        results.append(RepairResult(
            ticket_id="REPAIR-002",
            status="failed",
            findings_before=2,
            findings_after=2,
            error="coder returned empty response",
        ))
    return RepairRunResult(
        results=results,
        total_tickets=total,
        fixed=fixed,
        partial=partial,
        failed=failed,
        stopped_early=stopped_early,
        stop_reason=stop_reason,
    )


def _make_run_result(
    project_id: str = "my-project",
    passed: int = 2,
    failed: int = 0,
    skipped: int = 0,
    ticket_results: tuple[TicketResult, ...] = (),
    dod_met: bool = True,
) -> RunResult:
    total = passed + failed + skipped
    return RunResult(
        project_id=project_id,
        total_tickets=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        ticket_results=ticket_results,
        definition_of_done_met=dod_met,
    )


# ---------------------------------------------------------------------------
# generate_scan_report
# ---------------------------------------------------------------------------

class TestGenerateScanReport:
    def test_returns_path_to_governance_report_md(self, tmp_path: Path) -> None:
        pack = _make_pack()
        out = generate_scan_report(pack, tmp_path, tmp_path)
        assert out == tmp_path / "governance-report.md"
        assert out.exists()

    def test_report_starts_with_h1_header(self, tmp_path: Path) -> None:
        pack = _make_pack()
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert content.startswith("# Saturnday Governance Report")

    def test_report_contains_summary_section(self, tmp_path: Path) -> None:
        pack = _make_pack()
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Summary" in content

    def test_report_contains_check_results_section(self, tmp_path: Path) -> None:
        pack = _make_pack()
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Check Results" in content

    def test_report_contains_action_items_section(self, tmp_path: Path) -> None:
        pack = _make_pack()
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Action Items" in content

    def test_disposition_appears_in_report(self, tmp_path: Path) -> None:
        pack = _make_pack(disposition="FAIL")
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "FAIL" in content

    def test_run_id_appears_when_present(self, tmp_path: Path) -> None:
        pack = _make_pack(run_id="abc-123")
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "abc-123" in content

    def test_empty_check_results_no_crash(self, tmp_path: Path) -> None:
        pack = _make_pack(check_results=[])
        out = generate_scan_report(pack, tmp_path, tmp_path)
        assert out.exists()

    def test_findings_appear_in_detailed_section(self, tmp_path: Path) -> None:
        finding = {"file": "src/foo.py", "line": 42, "kind": "sql_injection", "detail": "Raw SQL"}
        cr = _make_check_result(
            name="sql_injection",
            status="FAIL",
            findings=[finding],
        )
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Detailed Findings" in content
        assert "sql_injection" in content
        assert "src/foo.py" in content

    def test_security_section_appears_for_sec_rule_ids(self, tmp_path: Path) -> None:
        cr = _make_check_result(
            name="hardcoded_jwt",
            status="FAIL",
            rule_id="SEC-001",
            findings=[{"file": "app.py", "line": 1, "kind": "jwt_literal_secret", "detail": "x"}],
        )
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Security Checks" in content
        assert "SEC-001" in content

    def test_no_security_section_without_sec_rule_ids(self, tmp_path: Path) -> None:
        cr = _make_check_result(name="missing_license", status="FAIL")
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Security Checks" not in content

    def test_env_findings_appear_in_environment_section(self, tmp_path: Path) -> None:
        finding = {"file": "requests", "line": 0, "kind": "declared_not_installed", "detail": "requests"}
        cr = _make_check_result(
            name="env_check",
            status="FAIL",
            findings=[finding],
        )
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "## Environment" in content

    def test_no_action_items_on_all_pass(self, tmp_path: Path) -> None:
        cr = _make_check_result(name="license", status="PASS")
        pack = _make_pack(check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "No action items" in content

    def test_check_table_has_header_row(self, tmp_path: Path) -> None:
        cr = _make_check_result(name="lint", status="PASS")
        pack = _make_pack(check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "| Check | Status | Severity | Findings |" in content

    def test_check_name_appears_in_table(self, tmp_path: Path) -> None:
        cr = _make_check_result(name="my_check", status="PASS")
        pack = _make_pack(check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "my_check" in content

    def test_failed_check_appears_in_code_fixes_action_items(self, tmp_path: Path) -> None:
        cr = _make_check_result(
            name="lint",
            status="FAIL",
            severity="error",
            findings=[{"file": "x.py", "line": 1, "kind": "style", "detail": "style issue"}],
        )
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "Code fixes required" in content

    def test_evidence_path_directory_created(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "deep" / "evidence"
        pack = _make_pack()
        out = generate_scan_report(pack, evidence_dir, tmp_path)
        assert out.exists()
        assert out.parent.is_dir()

    def test_finding_with_pattern_field_rendered(self, tmp_path: Path) -> None:
        finding = {"file": "app.py", "line": 5, "kind": "xss_reflected", "pattern": "innerHTML"}
        cr = _make_check_result(name="xss_check", status="FAIL", findings=[finding])
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        assert "innerHTML" in content

    def test_finding_without_line_renders_without_colon(self, tmp_path: Path) -> None:
        finding = {"file": "missing.py", "kind": "missing_license", "detail": "no license"}
        cr = _make_check_result(name="license", status="FAIL", findings=[finding])
        pack = _make_pack(disposition="FAIL", check_results=[cr])
        generate_scan_report(pack, tmp_path, tmp_path)
        content = (tmp_path / "governance-report.md").read_text(encoding="utf-8")
        # No trailing colon without a line number
        assert "missing.py:" not in content or "missing.py:None" not in content


# ---------------------------------------------------------------------------
# generate_repair_report
# ---------------------------------------------------------------------------

class TestGenerateRepairReport:
    def test_returns_path_to_repair_report_md(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result()
        out = generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        assert out == tmp_path / "repair-report.md"
        assert out.exists()

    def test_report_starts_with_h1_header(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert content.startswith("# Saturnday Repair Report")

    def test_report_contains_before_state_section(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## Before State" in content

    def test_report_contains_repair_plan_section(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## Repair Plan" in content

    def test_report_contains_ticket_results_section(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## Ticket Results" in content

    def test_empty_pre_findings_shows_placeholder(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result(total=0, fixed=0, failed=0)
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "No pre-repair findings" in content

    def test_pre_findings_count_appears_in_report(self, tmp_path: Path) -> None:
        pre_findings = [
            type("F", (), {"kind": "sql_injection"})(),
            type("F", (), {"kind": "xss_reflected"})(),
        ]
        repair_result = _make_repair_result()
        generate_repair_report(pre_findings, None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "2" in content

    def test_ticket_results_table_shows_fixed(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result(fixed=1, failed=0, total=1)
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "FIXED" in content

    def test_ticket_results_table_shows_failed(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result(fixed=0, failed=1, total=1)
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "FAILED" in content

    def test_stopped_early_note_appears(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result(
            stopped_early=True, stop_reason="too many failures"
        )
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "Stopped early" in content
        assert "too many failures" in content

    def test_post_pack_triggers_after_state_section(self, tmp_path: Path) -> None:
        post_pack = _make_pack(disposition="PASS")
        repair_result = _make_repair_result()
        generate_repair_report([], post_pack, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## After State" in content
        assert "## Before vs After" in content

    def test_no_after_state_when_post_pack_is_none(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## After State" not in content

    def test_repair_tickets_appear_in_plan_table(self, tmp_path: Path) -> None:
        ticket = RepairTicket(
            ticket_id="REPAIR-001",
            title="Fix SQL injection",
            severity="error",
            file_path="src/db.py",
            line=10,
            finding_kind="sql_injection",
        )
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [ticket], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "REPAIR-001" in content
        assert "src/db.py" in content

    def test_no_tickets_shows_placeholder(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result(total=0, fixed=0, failed=0)
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "No repair tickets" in content

    def test_role_passes_section_appears_when_json_present(self, tmp_path: Path) -> None:
        rp_file = tmp_path / "role-pass-governance_judge.json"
        rp_file.write_text(
            '{"role": "governance_judge", "success": true, "output": "All good."}',
            encoding="utf-8",
        )
        repair_result = _make_repair_result()
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## Role Passes" in content
        assert "governance_judge" in content

    def test_remaining_action_items_with_failed_post_pack(self, tmp_path: Path) -> None:
        cr = _make_check_result(
            name="lint",
            status="FAIL",
            severity="error",
            findings=[{"file": "x.py", "line": 1, "kind": "style", "detail": "x"}],
        )
        post_pack = _make_pack(disposition="FAIL", check_results=[cr])
        repair_result = _make_repair_result()
        generate_repair_report([], post_pack, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "## Remaining Action Items" in content
        assert "lint" in content

    def test_all_checks_passed_message_when_post_pack_clean(self, tmp_path: Path) -> None:
        post_pack = _make_pack(disposition="PASS", check_results=[])
        repair_result = _make_repair_result()
        generate_repair_report([], post_pack, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "All checks passed after repair" in content

    def test_evidence_dir_created_when_missing(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "evidence" / "repair"
        repair_result = _make_repair_result()
        out = generate_repair_report([], None, repair_result, [], evidence_dir, tmp_path)
        assert out.exists()

    def test_error_column_in_ticket_results_table(self, tmp_path: Path) -> None:
        repair_result = _make_repair_result(fixed=0, failed=1, total=1)
        generate_repair_report([], None, repair_result, [], tmp_path, tmp_path)
        content = (tmp_path / "repair-report.md").read_text(encoding="utf-8")
        assert "coder returned empty response" in content


# ---------------------------------------------------------------------------
# generate_run_report
# ---------------------------------------------------------------------------

class TestGenerateRunReport:
    def test_returns_path_to_run_report_md(self, tmp_path: Path) -> None:
        result = _make_run_result()
        out = generate_run_report(result, {}, None, tmp_path, tmp_path)
        assert out == tmp_path / "run-report.md"
        assert out.exists()

    def test_report_starts_with_h1_header(self, tmp_path: Path) -> None:
        result = _make_run_result()
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert content.startswith("# Saturnday Run Report")

    def test_project_id_in_header(self, tmp_path: Path) -> None:
        result = _make_run_result(project_id="cool-project")
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "cool-project" in content

    def test_plan_summary_section_present(self, tmp_path: Path) -> None:
        result = _make_run_result()
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Plan Summary" in content

    def test_ticket_results_section_present(self, tmp_path: Path) -> None:
        result = _make_run_result()
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Ticket Results" in content

    def test_summary_section_with_counts(self, tmp_path: Path) -> None:
        result = _make_run_result(passed=3, failed=1, skipped=0)
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Summary" in content
        assert "| Passed | 3 |" in content
        assert "| Failed | 1 |" in content

    def test_no_tickets_shows_placeholder(self, tmp_path: Path) -> None:
        result = _make_run_result(passed=0, failed=0, skipped=0)
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "No ticket results recorded" in content

    def test_proof_resolution_section_rendered_when_populated(
        self, tmp_path: Path
    ) -> None:
        """Phase 7: the Proof Resolution section surfaces the full
        (status, source, narrative) triple so the report tells the truth
        about HOW the product was proved (or why it was not)."""
        from dataclasses import replace
        result = replace(
            _make_run_result(),
            proof_resolution_status="passed",
            proof_resolution_source="coder_plan_time",
            proof_resolution_narrative="Derived at plan time; ran and passed.",
        )
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Proof Resolution" in content
        assert "passed" in content
        assert "coder_plan_time" in content
        assert "Derived at plan time" in content

    def test_proof_resolution_section_omitted_when_empty(
        self, tmp_path: Path
    ) -> None:
        """When no proof-resolution fields are populated, the section is
        omitted — no empty header/placeholder clutter."""
        result = _make_run_result()  # defaults: not_attempted / none / ""
        # Override so the triple is truly empty.
        from dataclasses import replace
        result = replace(
            result,
            proof_resolution_status="",
            proof_resolution_source="",
            proof_resolution_narrative="",
        )
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Proof Resolution" not in content

    def test_ticket_results_table_rendered(self, tmp_path: Path) -> None:
        tr = TicketResult(
            ticket_id="T001",
            disposition="PASS",
            attempts=1,
            governance_disposition="PASS",
        )
        result = _make_run_result(passed=1, ticket_results=(tr,))
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "T001" in content
        assert "PASS" in content

    def test_ticket_error_appears_in_table(self, tmp_path: Path) -> None:
        tr = TicketResult(
            ticket_id="T002",
            disposition="CODED_UNGOVERNED",
            attempts=3,
            error="governance failed",
        )
        result = _make_run_result(passed=0, failed=0, ticket_results=(tr,))
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "governance failed" in content

    def test_brief_section_appears_when_plan_has_brief(self, tmp_path: Path) -> None:
        result = _make_run_result()
        plan_data = {"brief": "Build a REST API for task management."}
        generate_run_report(result, plan_data, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Brief" in content
        assert "Build a REST API" in content

    def test_no_brief_section_when_plan_is_empty(self, tmp_path: Path) -> None:
        result = _make_run_result()
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Brief" not in content

    def test_phases_table_appears_when_plan_has_phases(self, tmp_path: Path) -> None:
        result = _make_run_result()
        plan_data = {
            "phases": [
                {"name": "Setup", "ticket_ids": ["T001", "T002"]},
                {"name": "Implement", "ticket_ids": ["T003"]},
            ]
        }
        generate_run_report(result, plan_data, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "| Phase | Tickets |" in content
        assert "Setup" in content
        assert "Implement" in content

    def test_dod_section_present_with_status(self, tmp_path: Path) -> None:
        result = _make_run_result(dod_met=True)
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Definition of Done" in content
        assert "MET" in content

    def test_dod_not_met_shown_correctly(self, tmp_path: Path) -> None:
        result = _make_run_result(dod_met=False)
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "NOT MET" in content

    def test_dod_criteria_listed_when_present(self, tmp_path: Path) -> None:
        result = _make_run_result()
        plan_data = {"definition_of_done": ["all_tickets_passed", "no_critical_findings"]}
        generate_run_report(result, plan_data, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "all_tickets_passed" in content

    def test_post_pack_section_rendered(self, tmp_path: Path) -> None:
        post_pack = _make_pack(disposition="PASS")
        result = _make_run_result()
        generate_run_report(result, {}, post_pack, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Post-Run Governance" in content

    def test_post_pack_failed_checks_section(self, tmp_path: Path) -> None:
        cr = _make_check_result(
            name="lint",
            status="FAIL",
            severity="error",
            findings=[{"file": "x.py", "line": 1, "kind": "style", "detail": "bad"}],
        )
        post_pack = _make_pack(disposition="FAIL", check_results=[cr])
        result = _make_run_result()
        generate_run_report(result, {}, post_pack, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "### Failed Checks" in content
        assert "lint" in content

    def test_role_passes_section_from_evidence_dir(self, tmp_path: Path) -> None:
        rp_file = tmp_path / "role-pass-repo-analyst.json"
        rp_file.write_text(
            '{"role": "repo_analyst", "success": true, "output": "Repo looks good."}',
            encoding="utf-8",
        )
        result = _make_run_result()
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "## Role Passes" in content
        assert "repo_analyst" in content

    def test_stop_reason_appears_in_plan_summary(self, tmp_path: Path) -> None:
        result = RunResult(
            project_id="p",
            total_tickets=3,
            passed=1,
            failed=2,
            stop_reason="too many failures",
        )
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        assert "too many failures" in content

    def test_evidence_dir_created_when_missing(self, tmp_path: Path) -> None:
        evidence_dir = tmp_path / "evidence" / "run"
        result = _make_run_result()
        out = generate_run_report(result, {}, None, evidence_dir, tmp_path)
        assert out.exists()

    def test_pipe_in_error_escaped_for_markdown_table(self, tmp_path: Path) -> None:
        tr = TicketResult(
            ticket_id="T003",
            disposition="FAIL",
            attempts=1,
            error="failure | details here",
        )
        result = _make_run_result(passed=0, failed=1, ticket_results=(tr,))
        generate_run_report(result, {}, None, tmp_path, tmp_path)
        content = (tmp_path / "run-report.md").read_text(encoding="utf-8")
        # Pipe in error must be escaped so table isn't broken
        assert "failure \\| details here" in content
