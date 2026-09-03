"""Tests for RS-013: generate_release_report (saturnday.release.report).

Acceptance criteria from the plan:
- generate_release_report(pack: ReleaseEvidencePack) -> str
- Markdown format, machine-parseable headers
- Inventory table truncated at 50 rows with "and N more" footer
- Check results table matches evidence.py summary style
- Diff section present only if baseline was provided
- Report written as release-report.md in evidence output dir (orchestrator responsibility;
  the function itself just returns a string)
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from saturnday.release.evidence import (
    ReleaseCheckResult,
    ReleaseEvidencePack,
    RELEASE_SCHEMA_VERSION,
    generate_release_run_id,
)
from saturnday.release.report import generate_release_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_pack(**overrides: Any) -> ReleaseEvidencePack:
    """Return a minimal ReleaseEvidencePack for testing."""
    base: dict[str, Any] = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "run_id": "release_20260330T120000Z_1234_abcd",
        "artefact_type": "wheel",
        "artefact_path": "/dist/saturnday-1.1.01-py3-none-any.whl",
        "artefact_sha256": "abc123" * 10,
        "inventory": {
            "artefact_type": "wheel",
            "files": [],
        },
        "check_results": [],
        "disposition": "PASS",
        "disposition_reasons": [],
        "baseline_artefact_path": "",
        "release_diff": {},
        "capability_state": {
            "premium_capabilities_enabled": False,
            "premium_package_installed": False,
            "entitlement_valid": None,
            "entitlement_reason": None,
            "entitlement_org": None,
            "entitlement_edition": None,
            "entitlement_expires": None,
            "available_premium_hooks": [],
            "skipped_premium_stages": [],
        },
    }
    base.update(overrides)
    return ReleaseEvidencePack(**base)


def _check_result(
    *,
    name: str = "test_check",
    rule_id: str = "REL-001",
    status: str = "PASS",
    severity: str = "error",
    findings: list | None = None,
    files_checked: int = 10,
    elapsed_s: float = 0.1,
) -> ReleaseCheckResult:
    return ReleaseCheckResult(
        name=name,
        rule_id=rule_id,
        status=status,
        severity=severity,
        findings=findings if findings is not None else [],
        files_checked=files_checked,
        elapsed_s=elapsed_s,
    )


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------


class TestReportReturnsMarkdown:
    """generate_release_report must return a non-empty Markdown string."""

    def test_returns_string(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert isinstance(result, str)

    def test_non_empty(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert len(result) > 0

    def test_has_h1_header(self) -> None:
        """Report must start with an H1 header containing the disposition."""
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert result.startswith("# Release Preflight Report")

    def test_pass_disposition_in_header(self) -> None:
        pack = _minimal_pack(disposition="PASS")
        result = generate_release_report(pack)
        assert "PASS" in result.split("\n")[0]

    def test_fail_disposition_in_header(self) -> None:
        pack = _minimal_pack(disposition="FAIL")
        result = generate_release_report(pack)
        assert "FAIL" in result.split("\n")[0]

    def test_warn_disposition_in_header(self) -> None:
        pack = _minimal_pack(disposition="WARN")
        result = generate_release_report(pack)
        assert "WARN" in result.split("\n")[0]

    def test_ends_with_newline(self) -> None:
        """Report must end with a newline for clean file writing."""
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert result.endswith("\n")


# ---------------------------------------------------------------------------
# Header metadata
# ---------------------------------------------------------------------------


class TestReportHeader:
    """The header table must include artefact type, path, and SHA-256."""

    def test_artefact_type_present(self) -> None:
        pack = _minimal_pack(artefact_type="sdist")
        result = generate_release_report(pack)
        assert "sdist" in result

    def test_artefact_path_present(self) -> None:
        path = "/dist/my-package-1.0.0.tar.gz"
        pack = _minimal_pack(artefact_path=path)
        result = generate_release_report(pack)
        assert path in result

    def test_sha256_present(self) -> None:
        sha = "deadbeef" * 8
        pack = _minimal_pack(artefact_sha256=sha)
        result = generate_release_report(pack)
        assert sha in result

    def test_run_id_present(self) -> None:
        run_id = "release_20260330T120000Z_9999_ff00"
        pack = _minimal_pack(run_id=run_id)
        result = generate_release_report(pack)
        assert run_id in result

    def test_timestamp_present(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        # created_utc is set by __post_init__ — just check the field appears
        assert pack.created_utc in result


# ---------------------------------------------------------------------------
# Disposition reasons
# ---------------------------------------------------------------------------


class TestReportDispositionReasons:
    """Disposition reasons must appear when present; section absent when empty."""

    def test_no_section_when_no_reasons(self) -> None:
        pack = _minimal_pack(disposition="PASS", disposition_reasons=[])
        result = generate_release_report(pack)
        assert "Disposition Reasons" not in result

    def test_reasons_section_appears(self) -> None:
        pack = _minimal_pack(
            disposition="FAIL",
            disposition_reasons=["REL-001 source_map_blocker: FAIL"],
        )
        result = generate_release_report(pack)
        assert "Disposition Reasons" in result
        assert "REL-001" in result

    def test_multiple_reasons_listed(self) -> None:
        reasons = [
            "REL-001 source_map_blocker: FAIL",
            "REL-002 secrets_in_artefact: FAIL",
        ]
        pack = _minimal_pack(disposition="FAIL", disposition_reasons=reasons)
        result = generate_release_report(pack)
        for reason in reasons:
            assert reason in result


# ---------------------------------------------------------------------------
# Inventory section
# ---------------------------------------------------------------------------


class TestReportInventorySection:
    """Inventory section must include file count and total size."""

    def test_inventory_section_present(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert "Artefact Inventory" in result

    def test_file_count_shown(self) -> None:
        files = [
            {"path": f"pkg/mod{i}.py", "size": 100 * i, "sha256": f"abc{i}"}
            for i in range(5)
        ]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        assert "5" in result  # file count

    def test_total_size_shown(self) -> None:
        files = [{"path": "pkg/mod.py", "size": 1024, "sha256": "abc"}]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        # 1024 bytes = 1.0 KB
        assert "1.0 KB" in result

    def test_truncation_at_50_files(self) -> None:
        """Inventory table must truncate at 50 rows with an 'and N more' footer."""
        files = [
            {"path": f"pkg/module_{i:03d}.py", "size": 100, "sha256": f"sha{i}"}
            for i in range(75)
        ]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        assert "and 25 more" in result

    def test_no_truncation_at_exactly_50(self) -> None:
        """Exactly 50 files: no truncation footer."""
        files = [
            {"path": f"pkg/mod_{i}.py", "size": 100, "sha256": f"sha{i}"}
            for i in range(50)
        ]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        assert "more" not in result or "0 more" not in result

    def test_empty_inventory(self) -> None:
        """Empty inventory must not raise and must show 0 file count."""
        pack = _minimal_pack(inventory={"files": []})
        result = generate_release_report(pack)
        assert "0" in result


# ---------------------------------------------------------------------------
# Check results table
# ---------------------------------------------------------------------------


class TestReportCheckResultsTable:
    """Check results table must contain name, rule_id, status, and finding count."""

    def test_check_results_section_present(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert "Check Results" in result

    def test_no_checks_message(self) -> None:
        pack = _minimal_pack(check_results=[])
        result = generate_release_report(pack)
        assert "No checks ran" in result

    def test_rule_id_in_table(self) -> None:
        cr = _check_result(rule_id="REL-001", name="source_map_blocker", status="PASS")
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "REL-001" in result

    def test_check_name_in_table(self) -> None:
        cr = _check_result(name="source_map_blocker")
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "source_map_blocker" in result

    def test_status_in_table(self) -> None:
        cr = _check_result(status="FAIL")
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "FAIL" in result

    def test_finding_count_in_table(self) -> None:
        findings = [{"path": "pkg/bad.js.map", "kind": "source_map"}]
        cr = _check_result(status="FAIL", findings=findings)
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "1" in result

    def test_multiple_checks_all_appear(self) -> None:
        checks = [
            _check_result(rule_id="REL-001", name="source_map_blocker", status="PASS"),
            _check_result(rule_id="REL-002", name="secrets_in_artefact", status="FAIL"),
            _check_result(rule_id="REL-003", name="internal_file_blocker", status="SKIPPED"),
        ]
        pack = _minimal_pack(check_results=checks)
        result = generate_release_report(pack)
        assert "REL-001" in result
        assert "REL-002" in result
        assert "REL-003" in result


# ---------------------------------------------------------------------------
# Detailed findings
# ---------------------------------------------------------------------------


class TestReportFindingsDetail:
    """Findings detail section must appear for checks with findings."""

    def test_no_findings_detail_section_when_all_pass(self) -> None:
        cr = _check_result(status="PASS", findings=[])
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "Findings Detail" not in result

    def test_findings_detail_section_present_when_findings(self) -> None:
        findings = [{"path": "pkg/secret.pem", "kind": "key_file"}]
        cr = _check_result(status="FAIL", findings=findings)
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "Findings Detail" in result

    def test_finding_path_appears(self) -> None:
        findings = [{"path": "pkg/secret.pem", "kind": "key_file"}]
        cr = _check_result(status="FAIL", findings=findings)
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "pkg/secret.pem" in result

    def test_finding_kind_appears(self) -> None:
        findings = [{"path": "x.pem", "kind": "key_file"}]
        cr = _check_result(status="FAIL", findings=findings)
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "key_file" in result

    def test_long_detail_truncated(self) -> None:
        """Detail strings longer than 120 characters must be truncated."""
        long_detail = "x" * 200
        findings = [{"path": "f.py", "detail": long_detail}]
        cr = _check_result(status="FAIL", findings=findings)
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        assert "..." in result
        # The full 200-char string must not appear verbatim.
        assert long_detail not in result

    def test_finding_without_known_keys_uses_fallback(self) -> None:
        """Findings with unknown keys must not raise; fallback representation used."""
        findings = [{"unknown_key": "some_value", "another": 42}]
        cr = _check_result(status="FAIL", findings=findings)
        pack = _minimal_pack(check_results=[cr])
        result = generate_release_report(pack)
        # Should not raise; result is a string.
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Diff section
# ---------------------------------------------------------------------------


class TestReportDiffSection:
    """Diff section appears only when a baseline was provided."""

    def test_no_diff_section_without_baseline(self) -> None:
        pack = _minimal_pack(baseline_artefact_path="", release_diff={})
        result = generate_release_report(pack)
        assert "Release Diff" not in result

    def test_diff_section_present_with_baseline_path(self) -> None:
        pack = _minimal_pack(
            baseline_artefact_path="/dist/old-1.0.0.whl",
            release_diff={},
        )
        result = generate_release_report(pack)
        assert "Release Diff" in result
        assert "/dist/old-1.0.0.whl" in result

    def test_diff_section_present_with_diff_data(self) -> None:
        pack = _minimal_pack(
            baseline_artefact_path="",
            release_diff={"added": ["new_mod.py"], "removed": [], "changed": []},
        )
        result = generate_release_report(pack)
        assert "Release Diff" in result

    def test_added_files_listed(self) -> None:
        pack = _minimal_pack(
            baseline_artefact_path="/dist/old.whl",
            release_diff={"added": ["new_module.py"], "removed": [], "changed": []},
        )
        result = generate_release_report(pack)
        assert "new_module.py" in result

    def test_removed_files_listed(self) -> None:
        pack = _minimal_pack(
            baseline_artefact_path="/dist/old.whl",
            release_diff={"added": [], "removed": ["old_module.py"], "changed": []},
        )
        result = generate_release_report(pack)
        assert "old_module.py" in result

    def test_diff_counts_shown(self) -> None:
        pack = _minimal_pack(
            baseline_artefact_path="/dist/old.whl",
            release_diff={
                "added": ["a.py", "b.py"],
                "removed": ["c.py"],
                "changed": ["d.py", "e.py", "f.py"],
            },
        )
        result = generate_release_report(pack)
        assert "2" in result  # added count
        assert "1" in result  # removed count
        assert "3" in result  # changed count

    def test_large_diff_truncated(self) -> None:
        """Lists exceeding 20 entries are truncated with a 'more' footer."""
        many = [f"file_{i}.py" for i in range(25)]
        pack = _minimal_pack(
            baseline_artefact_path="/dist/old.whl",
            release_diff={"added": many, "removed": [], "changed": []},
        )
        result = generate_release_report(pack)
        assert "and 5 more" in result


# ---------------------------------------------------------------------------
# Capability state
# ---------------------------------------------------------------------------


class TestReportCapabilityState:
    """Capability state section must always appear and reflect premium status."""

    def test_capability_section_present(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert "Capability State" in result

    def test_community_mode_shows_not_installed(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert "Premium installed: False" in result
        assert "Premium enabled: False" in result

    def test_premium_enabled_shown(self) -> None:
        cap_state = {
            "premium_capabilities_enabled": True,
            "premium_package_installed": True,
            "entitlement_valid": True,
            "entitlement_reason": "valid",
            "entitlement_org": "my-org",
            "entitlement_edition": "premium",
            "entitlement_expires": "2027-01-01T00:00:00+00:00",
            "available_premium_hooks": ["security_triage", "release_governance"],
            "skipped_premium_stages": [],
        }
        pack = _minimal_pack(capability_state=cap_state)
        result = generate_release_report(pack)
        assert "Premium installed: True" in result
        assert "Premium enabled: True" in result
        assert "my-org" in result
        assert "release_governance" in result

    def test_no_hooks_shown_when_empty(self) -> None:
        pack = _minimal_pack()
        result = generate_release_report(pack)
        assert "none" in result.lower()


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


class TestFormatHelpers:
    """Format helper functions (_format_size, _status_badge) via the public report."""

    def test_bytes_displayed_for_small_sizes(self) -> None:
        files = [{"path": "tiny.py", "size": 512, "sha256": "abc"}]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        assert "512 B" in result

    def test_kb_displayed_for_kilobyte_sizes(self) -> None:
        files = [{"path": "mod.py", "size": 2048, "sha256": "abc"}]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        assert "KB" in result

    def test_mb_displayed_for_megabyte_sizes(self) -> None:
        files = [{"path": "big.py", "size": 2 * 1024 * 1024, "sha256": "abc"}]
        pack = _minimal_pack(inventory={"files": files})
        result = generate_release_report(pack)
        assert "MB" in result
