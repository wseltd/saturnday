"""Tests for RunMetrics — compute, write, load, and append behaviour (T002)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saturnday._types import RunResult, TicketResult
from saturnday.run.metrics import (
    RunMetrics,
    compute_run_metrics,
    load_metrics,
    write_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_result(
    project_id: str = "test-proj",
    passed: int = 2,
    failed: int = 1,
    coded_ungoverned: int = 0,
    ticket_results: tuple[TicketResult, ...] = (),
) -> RunResult:
    """Build a minimal RunResult for testing."""
    total = passed + failed + coded_ungoverned + sum(
        1 for tr in ticket_results if tr.disposition == "SKIP"
    )
    if not ticket_results:
        # Build synthetic TicketResults to match counts
        results: list[TicketResult] = []
        for i in range(passed):
            results.append(TicketResult(
                ticket_id=f"T-PASS-{i}",
                disposition="PASS",
                governance_disposition="PASS",
            ))
        for i in range(failed):
            results.append(TicketResult(
                ticket_id=f"T-FAIL-{i}",
                disposition="FAIL",
                governance_disposition="FAIL",
            ))
        for i in range(coded_ungoverned):
            results.append(TicketResult(
                ticket_id=f"T-CU-{i}",
                disposition="CODED_UNGOVERNED",
                governance_disposition="FAIL",
            ))
        ticket_results = tuple(results)
        total = len(ticket_results)

    return RunResult(
        project_id=project_id,
        total_tickets=total,
        passed=passed,
        failed=failed,
        skipped=0,
        coded_ungoverned=coded_ungoverned,
        ticket_results=ticket_results,
    )


# ---------------------------------------------------------------------------
# test_compute_run_metrics_basic
# ---------------------------------------------------------------------------


def test_compute_run_metrics_basic(tmp_path: Path) -> None:
    """compute_run_metrics must produce correct top-level counts from RunResult."""
    run_result = _make_run_result(
        project_id="my-project",
        passed=3,
        failed=1,
        coded_ungoverned=1,
    )
    metrics = compute_run_metrics(run_result, tmp_path)

    assert metrics.run_id == "my-project"
    assert metrics.total_tickets == 5
    assert metrics.passed == 3
    assert metrics.failed == 1
    assert metrics.coded_ungoverned == 1
    assert metrics.rules_generated == 0
    assert metrics.timestamp  # non-empty ISO string


def test_compute_run_metrics_lessons_extracted_counts_fail_and_ungoverned(
    tmp_path: Path,
) -> None:
    """lessons_extracted must equal the count of FAIL + CODED_UNGOVERNED tickets."""
    run_result = _make_run_result(
        passed=2,
        failed=2,
        coded_ungoverned=1,
    )
    metrics = compute_run_metrics(run_result, tmp_path)
    assert metrics.lessons_extracted == 3  # 2 FAIL + 1 CODED_UNGOVERNED


def test_compute_run_metrics_repeated_failure_count(tmp_path: Path) -> None:
    """repeated_failure_count must flag tickets with the same finding kind as prior failures."""
    finding_a = {"kind": "SEC-001", "message": "hardcoded secret"}
    finding_b = {"kind": "SEC-001", "message": "hardcoded secret again"}  # same kind
    finding_c = {"kind": "STYLE-002", "message": "missing docstring"}

    results = (
        TicketResult(
            ticket_id="T1",
            disposition="FAIL",
            governance_disposition="FAIL",
            governance_findings=(finding_a,),
        ),
        TicketResult(
            ticket_id="T2",
            disposition="FAIL",
            governance_disposition="FAIL",
            governance_findings=(finding_b,),  # same kind as T1 → repeated
        ),
        TicketResult(
            ticket_id="T3",
            disposition="FAIL",
            governance_disposition="FAIL",
            governance_findings=(finding_c,),  # different kind → not repeated
        ),
    )
    run_result = RunResult(
        project_id="p",
        total_tickets=3,
        passed=0,
        failed=3,
        skipped=0,
        coded_ungoverned=0,
        ticket_results=results,
    )
    metrics = compute_run_metrics(run_result, tmp_path)
    assert metrics.repeated_failure_count == 1


def test_compute_run_metrics_all_pass_no_lessons(tmp_path: Path) -> None:
    """A clean run with all PASS must produce zero lessons_extracted."""
    run_result = _make_run_result(passed=4, failed=0, coded_ungoverned=0)
    metrics = compute_run_metrics(run_result, tmp_path)
    assert metrics.lessons_extracted == 0
    assert metrics.repeated_failure_count == 0


def test_compute_run_metrics_out_of_scope_detection(tmp_path: Path) -> None:
    """out_of_scope_edit_count must count tickets with scope-related findings."""
    scope_finding = {"kind": "out_of_scope_edit", "message": "file outside allowed_globs"}
    clean_finding = {"kind": "SEC-001", "message": "secret found"}

    results = (
        TicketResult(
            ticket_id="T1",
            disposition="FAIL",
            governance_disposition="FAIL",
            governance_findings=(scope_finding,),
        ),
        TicketResult(
            ticket_id="T2",
            disposition="FAIL",
            governance_disposition="FAIL",
            governance_findings=(clean_finding,),
        ),
        TicketResult(
            ticket_id="T3",
            disposition="FAIL",
            governance_disposition="FAIL",
            governance_findings=(scope_finding, clean_finding),  # has scope finding → count once
        ),
    )
    run_result = RunResult(
        project_id="p",
        total_tickets=3,
        passed=0,
        failed=3,
        skipped=0,
        coded_ungoverned=0,
        ticket_results=results,
    )
    metrics = compute_run_metrics(run_result, tmp_path)
    assert metrics.out_of_scope_edit_count == 2


# ---------------------------------------------------------------------------
# test_write_and_load_metrics
# ---------------------------------------------------------------------------


def test_write_and_load_metrics(tmp_path: Path) -> None:
    """write_metrics then load_metrics must recover an identical record."""
    run_result = _make_run_result(
        project_id="roundtrip-proj",
        passed=5,
        failed=1,
        coded_ungoverned=0,
    )
    metrics = compute_run_metrics(run_result, tmp_path)
    dest = write_metrics(metrics, tmp_path)

    assert dest == tmp_path / "metrics.json"
    assert dest.exists()

    loaded = load_metrics(tmp_path)
    assert len(loaded) == 1
    loaded_m = loaded[0]
    assert loaded_m.run_id == "roundtrip-proj"
    assert loaded_m.passed == 5
    assert loaded_m.failed == 1
    assert loaded_m.rules_generated == 0
    assert loaded_m.timestamp == metrics.timestamp


def test_write_metrics_creates_output_dir(tmp_path: Path) -> None:
    """write_metrics must create the output directory if it does not exist."""
    dest_dir = tmp_path / "new" / "subdir"
    assert not dest_dir.exists()
    run_result = _make_run_result(passed=1, failed=0)
    metrics = compute_run_metrics(run_result, dest_dir)
    write_metrics(metrics, dest_dir)
    assert (dest_dir / "metrics.json").exists()


def test_load_metrics_missing_file_returns_empty(tmp_path: Path) -> None:
    """load_metrics must return an empty list when metrics.json does not exist."""
    result = load_metrics(tmp_path / "no-such-dir")
    assert result == []


def test_load_metrics_malformed_json_returns_empty(tmp_path: Path) -> None:
    """load_metrics must return an empty list when metrics.json is malformed."""
    (tmp_path / "metrics.json").write_text("not valid json", encoding="utf-8")
    result = load_metrics(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# test_metrics_append_not_overwrite
# ---------------------------------------------------------------------------


def test_metrics_append_not_overwrite(tmp_path: Path) -> None:
    """write_metrics called twice must produce a two-element JSON array."""
    run_result_1 = _make_run_result(project_id="proj-alpha", passed=3, failed=0)
    run_result_2 = _make_run_result(project_id="proj-beta", passed=1, failed=2)

    metrics_1 = compute_run_metrics(run_result_1, tmp_path)
    metrics_2 = compute_run_metrics(run_result_2, tmp_path)

    write_metrics(metrics_1, tmp_path)
    write_metrics(metrics_2, tmp_path)

    dest = tmp_path / "metrics.json"
    raw = json.loads(dest.read_text(encoding="utf-8"))

    assert isinstance(raw, list), "metrics.json must be a JSON array"
    assert len(raw) == 2, f"Expected 2 records, got {len(raw)}"
    assert raw[0]["run_id"] == "proj-alpha"
    assert raw[1]["run_id"] == "proj-beta"


def test_metrics_append_three_runs(tmp_path: Path) -> None:
    """write_metrics called three times must accumulate all records."""
    for i in range(3):
        rr = _make_run_result(project_id=f"proj-{i}", passed=i + 1, failed=0)
        m = compute_run_metrics(rr, tmp_path)
        write_metrics(m, tmp_path)

    loaded = load_metrics(tmp_path)
    assert len(loaded) == 3
    assert loaded[0].run_id == "proj-0"
    assert loaded[1].run_id == "proj-1"
    assert loaded[2].run_id == "proj-2"


def test_metrics_json_is_valid_array_from_start(tmp_path: Path) -> None:
    """First write_metrics call must produce a single-element JSON array."""
    run_result = _make_run_result(passed=1, failed=0)
    metrics = compute_run_metrics(run_result, tmp_path)
    write_metrics(metrics, tmp_path)

    raw = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list)
    assert len(raw) == 1


def test_metrics_write_recovers_from_corrupt_file(tmp_path: Path) -> None:
    """write_metrics must start a fresh array if metrics.json is corrupt."""
    (tmp_path / "metrics.json").write_text("{invalid json", encoding="utf-8")
    run_result = _make_run_result(passed=2, failed=0)
    metrics = compute_run_metrics(run_result, tmp_path)
    write_metrics(metrics, tmp_path)  # must not raise

    loaded = load_metrics(tmp_path)
    assert len(loaded) == 1
