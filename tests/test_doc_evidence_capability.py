"""Tests for SPLIT-014: capability state fields in document evidence pack.

Verifies that write_document_evidence writes all required capability-state
fields to run-summary.json, with correct values when no premium stages are
registered (public-only deployment).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import saturnday.capability_registry as reg
from saturnday.document._types import DocumentRunResult, DocumentSpec
from saturnday.document.evidence_pack import write_document_evidence


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Ensure capability registry is empty before and after every test."""
    reg.clear()
    yield
    reg.clear()


def _minimal_spec() -> DocumentSpec:
    """Return a minimal DocumentSpec for use in tests."""
    return DocumentSpec(
        type="report",
        purpose="test document",
        audience="internal",
        risk_class="low",
        required_sections=["Introduction"],
        claim_policy={},
        approved_sources=[],
        sign_off_roles=[],
    )


def _minimal_result() -> DocumentRunResult:
    """Return a minimal DocumentRunResult with no sections, claims, or findings."""
    return DocumentRunResult(
        document_id="test-doc-001",
        type="report",
        total_sections=0,
        passed=0,
        failed=0,
        provisional=0,
        document_status="PASS",
    )


def _run_and_load_summary(tmp_path: Path) -> dict:
    """Call write_document_evidence and return the parsed run-summary.json."""
    result = _minimal_result()
    spec = _minimal_spec()
    output_dir = tmp_path / "evidence"
    write_document_evidence(result, spec, output_dir)
    summary_path = output_dir / "run-summary.json"
    assert summary_path.is_file(), "run-summary.json was not created"
    return json.loads(summary_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_capability_fields_present_in_summary(tmp_path: Path) -> None:
    """All six new capability fields must appear in run-summary.json."""
    summary = _run_and_load_summary(tmp_path)

    required_keys = [
        "premium_capabilities_enabled",
        "available_premium_hooks",
        "skipped_premium_stages",
        "claim_verification_available",
        "publishability_evaluated",
        "premium_evidence",
    ]
    for key in required_keys:
        assert key in summary, f"Expected field {key!r} missing from run-summary.json"


def test_no_premium_all_booleans_false(tmp_path: Path) -> None:
    """With no premium registered, all boolean capability fields must be False."""
    summary = _run_and_load_summary(tmp_path)

    assert summary["premium_capabilities_enabled"] is False
    assert summary["claim_verification_available"] is False
    assert summary["publishability_evaluated"] is False
    assert summary["premium_evidence"] is False


def test_no_premium_available_hooks_empty(tmp_path: Path) -> None:
    """With no premium registered, available_premium_hooks must be an empty list."""
    summary = _run_and_load_summary(tmp_path)

    assert summary["available_premium_hooks"] == []


def test_no_premium_all_eight_stages_skipped(tmp_path: Path) -> None:
    """With no premium registered, all 8 stages must appear in skipped_premium_stages."""
    from saturnday.shared.evidence_schema import PREMIUM_STAGE_NAMES

    summary = _run_and_load_summary(tmp_path)

    skipped = summary["skipped_premium_stages"]
    assert isinstance(skipped, list)
    assert len(skipped) == 8, (
        f"Expected 8 skipped stages, got {len(skipped)}: {skipped}"
    )
    skipped_names = {entry["stage"] for entry in skipped}
    assert skipped_names == set(PREMIUM_STAGE_NAMES)
    for entry in skipped:
        assert entry["reason"] == "premium_not_available", (
            f"Stage {entry['stage']!r} reason should be 'premium_not_available', "
            f"got {entry['reason']!r}"
        )


def test_premium_registered_reflects_in_summary(tmp_path: Path) -> None:
    """When a premium stage is registered, capability fields reflect it correctly."""
    sentinel = object()
    reg.register("doc_post_global", sentinel)
    reg.register("evidence_appender", sentinel)

    summary = _run_and_load_summary(tmp_path)

    assert summary["premium_capabilities_enabled"] is True
    assert "doc_post_global" in summary["available_premium_hooks"]
    assert "evidence_appender" in summary["available_premium_hooks"]
    assert summary["claim_verification_available"] is True
    assert summary["publishability_evaluated"] is True
    assert summary["premium_evidence"] is True


def test_existing_fields_not_disturbed(tmp_path: Path) -> None:
    """Adding capability fields must not remove or alter pre-existing summary fields."""
    summary = _run_and_load_summary(tmp_path)

    # Core fields that must still be present and correct
    assert summary["document_id"] == "test-doc-001"
    assert summary["type"] == "report"
    assert summary["document_status"] == "PASS"
    assert summary["total_sections"] == 0
    assert summary["passed"] == 0
    assert summary["failed"] == 0
    assert summary["provisional"] == 0
    assert summary["spec_risk_class"] == "low"
    assert summary["spec_sign_off_roles"] == []
    assert "artefact_hash" in summary
    assert "timestamp" in summary
