"""Run metrics — tracks quality indicators across Saturnday plan executions.

Persists a metrics record to ``.saturnday/metrics.json`` after each run.
Multiple runs append to the array; the file is never overwritten wholesale.

Usage::

    from saturnday.run.metrics import compute_run_metrics, write_metrics, load_metrics
    metrics = compute_run_metrics(run_result, evidence_dir)
    write_metrics(metrics, output_dir)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saturnday._types import RunResult

logger = logging.getLogger(__name__)

__all__ = [
    "RunMetrics",
    "compute_run_metrics",
    "write_metrics",
    "load_metrics",
    "compare_metrics",
    "format_metrics_report",
]

_METRICS_FILENAME = "metrics.json"


@dataclass
class RunMetrics:
    """Quality indicators for a single plan execution.

    Attributes:
        run_id: Project identifier from the plan.
        total_tickets: Total number of tickets in the run.
        passed: Tickets that passed governance.
        failed: Tickets that failed all retries (disposition FAIL).
        coded_ungoverned: Tickets committed with CODED_UNGOVERNED disposition.
        repeated_failure_count: Tickets that failed on the same finding kind
            as a prior ticket in the same run.
        out_of_scope_edit_count: Tickets that touched files outside their
            allowed_globs (detected via governance_findings scope check).
        governance_regression_count: Tickets where project-level checks
            worsened relative to the pre-ticket baseline.
        lessons_extracted: Lessons created during this run (governance_fail
            and post_check_fail outcomes in ticket_results).
        rules_generated: Reserved for future rule-generation tracking.
            Always 0 in Phase 0.
        timestamp: UTC ISO-8601 timestamp when metrics were computed.
    """

    run_id: str
    total_tickets: int
    passed: int
    failed: int
    coded_ungoverned: int
    repeated_failure_count: int
    out_of_scope_edit_count: int
    governance_regression_count: int
    lessons_extracted: int
    rules_generated: int
    timestamp: str


def compute_run_metrics(run_result: "RunResult", evidence_dir: Path) -> RunMetrics:
    """Compute RunMetrics from a completed RunResult and evidence directory.

    Deterministic computation — no LLM calls. Uses only the RunResult fields
    and governance_findings embedded in each TicketResult.

    Args:
        run_result: The completed RunResult from run_plan().
        evidence_dir: Path to the .saturnday output directory (used for
            any evidence file inspection; currently reserved for future use).

    Returns:
        A populated RunMetrics dataclass.
    """
    repeated_failure_count = _count_repeated_failures(run_result)
    out_of_scope_count = _count_out_of_scope_edits(run_result)
    regression_count = _count_governance_regressions(run_result)
    lessons_count = _count_lessons_extracted(run_result)

    return RunMetrics(
        run_id=run_result.project_id,
        total_tickets=run_result.total_tickets,
        passed=run_result.passed,
        failed=run_result.failed,
        coded_ungoverned=run_result.coded_ungoverned,
        repeated_failure_count=repeated_failure_count,
        out_of_scope_edit_count=out_of_scope_count,
        governance_regression_count=regression_count,
        lessons_extracted=lessons_count,
        rules_generated=0,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def write_metrics(metrics: RunMetrics, output_dir: Path) -> Path:
    """Append a RunMetrics record to output_dir/metrics.json.

    If the file already exists and contains a JSON array, the new record is
    appended. If the file is absent or malformed, a fresh single-element
    array is written.

    Args:
        metrics: The metrics record to persist.
        output_dir: Directory where metrics.json lives (typically .saturnday/).

    Returns:
        Path to the written metrics.json file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / _METRICS_FILENAME

    existing: list[dict] = []
    if dest.exists():
        try:
            data = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
            else:
                logger.warning(
                    "metrics.json was not a JSON array — starting fresh: %s", dest
                )
        except json.JSONDecodeError as exc:
            logger.warning("metrics.json unreadable (%s) — starting fresh: %s", exc, dest)

    existing.append(asdict(metrics))
    dest.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    logger.debug("Metrics written to %s (%d total records)", dest, len(existing))
    return dest


def load_metrics(output_dir: Path) -> list[RunMetrics]:
    """Load historical RunMetrics records from output_dir/metrics.json.

    Records that cannot be deserialized (missing fields from older schema
    versions) are skipped with a warning rather than raising.

    Args:
        output_dir: Directory containing metrics.json.

    Returns:
        List of RunMetrics ordered oldest-first (file order). Empty list
        if the file does not exist or cannot be parsed.
    """
    dest = Path(output_dir) / _METRICS_FILENAME
    if not dest.exists():
        return []

    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("Failed to load metrics.json (%s): %s", dest, exc)
        return []

    if not isinstance(data, list):
        logger.warning("metrics.json is not a JSON array: %s", dest)
        return []

    results: list[RunMetrics] = []
    for idx, record in enumerate(data):
        if not isinstance(record, dict):
            logger.warning("Skipping non-dict record at index %d in %s", idx, dest)
            continue
        try:
            results.append(RunMetrics(**record))
        except TypeError as exc:
            logger.warning(
                "Skipping malformed metrics record at index %d (%s): %s", idx, exc, dest
            )
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_repeated_failures(run_result: "RunResult") -> int:
    """Count tickets that failed on the same finding kind as an earlier ticket.

    A ticket is counted if any of its governance_findings shares a 'kind'
    (or 'check_name') value with the findings of a ticket that failed before
    it in the same run.
    """
    seen_kinds: set[str] = set()
    repeated = 0
    for tr in run_result.ticket_results:
        if tr.disposition not in ("FAIL", "CODED_UNGOVERNED"):
            # Still record passing-ticket finding kinds so later failures
            # can be compared — only count as repeated when the ticket itself failed.
            for f in (tr.governance_findings or ()):
                kind = f.get("kind") or f.get("check_name") or f.get("message", "")
                if kind:
                    seen_kinds.add(kind)
            continue

        ticket_kinds: set[str] = set()
        for f in (tr.governance_findings or ()):
            kind = f.get("kind") or f.get("check_name") or f.get("message", "")
            if kind:
                ticket_kinds.add(kind)

        if ticket_kinds & seen_kinds:
            repeated += 1

        seen_kinds.update(ticket_kinds)

    return repeated


def _count_out_of_scope_edits(run_result: "RunResult") -> int:
    """Count tickets that produced out-of-scope edit findings.

    Detects 'scope' or 'out_of_scope' in the check_name or kind field of
    any governance finding, which is the standard naming used by post-checks.
    """
    count = 0
    for tr in run_result.ticket_results:
        for f in (tr.governance_findings or ()):
            kind = (
                f.get("kind", "") or f.get("check_name", "") or ""
            ).lower()
            if "scope" in kind or "out_of_scope" in kind:
                count += 1
                break  # count ticket once regardless of finding count
    return count


def _count_governance_regressions(run_result: "RunResult") -> int:
    """Count tickets where governance disposition was FAIL after an earlier PASS.

    A regression is counted when a ticket that reached the governance check
    (governance_disposition is set) has a FAIL disposition while a prior ticket
    in the same run had a PASS governance disposition.
    """
    had_pass = False
    regressions = 0
    for tr in run_result.ticket_results:
        if tr.governance_disposition == "PASS":
            had_pass = True
        elif tr.governance_disposition == "FAIL" and had_pass:
            regressions += 1
    return regressions


def _count_lessons_extracted(run_result: "RunResult") -> int:
    """Count tickets where a lesson would be recorded (FAIL or CODED_UNGOVERNED)."""
    return sum(
        1
        for tr in run_result.ticket_results
        if tr.disposition in ("FAIL", "CODED_UNGOVERNED")
    )


# ---------------------------------------------------------------------------
# T019: Metrics comparison
# ---------------------------------------------------------------------------


def compare_metrics(
    current: RunMetrics,
    history: list[RunMetrics],
) -> dict:
    """Compare current run metrics against the historical average.

    Computes trends for repeated failure count, governance regression count,
    and cumulative totals across the history window.

    Args:
        current: Metrics for the just-completed run.
        history: Historical RunMetrics records (oldest-first, does NOT include
            current).  May be empty.

    Returns:
        Dict with keys:

        - ``repeated_failure_trend``: ``"increasing"``, ``"decreasing"``,
          or ``"stable"`` comparing current vs historical average.
        - ``regression_trend``: same for governance_regression_count.
        - ``lessons_extracted_total``: cumulative lessons including current.
        - ``rules_generated_total``: cumulative rules including current.
        - ``improvement_score``: ratio of current failures / avg historical
          failures (lower is better). 1.0 when no history.
        - ``current_repeated_failures``: current run value.
        - ``avg_historical_repeated_failures``: mean across history records.
    """
    if not history:
        return {
            "repeated_failure_trend": "stable",
            "regression_trend": "stable",
            "lessons_extracted_total": current.lessons_extracted,
            "rules_generated_total": current.rules_generated,
            "improvement_score": 1.0,
            "current_repeated_failures": current.repeated_failure_count,
            "avg_historical_repeated_failures": 0.0,
        }

    avg_repeated = sum(m.repeated_failure_count for m in history) / len(history)
    avg_regression = sum(m.governance_regression_count for m in history) / len(history)

    def _trend(current_val: float, avg: float) -> str:
        """Classify trend relative to historical average."""
        if avg == 0.0:
            return "stable" if current_val == 0 else "increasing"
        if current_val > avg * 1.1:
            return "increasing"
        if current_val < avg * 0.9:
            return "decreasing"
        return "stable"

    lessons_total = sum(m.lessons_extracted for m in history) + current.lessons_extracted
    rules_total = sum(m.rules_generated for m in history) + current.rules_generated

    improvement_score = (
        current.repeated_failure_count / avg_repeated if avg_repeated > 0 else 1.0
    )

    return {
        "repeated_failure_trend": _trend(current.repeated_failure_count, avg_repeated),
        "regression_trend": _trend(current.governance_regression_count, avg_regression),
        "lessons_extracted_total": lessons_total,
        "rules_generated_total": rules_total,
        "improvement_score": round(improvement_score, 3),
        "current_repeated_failures": current.repeated_failure_count,
        "avg_historical_repeated_failures": round(avg_repeated, 3),
    }


def format_metrics_report(comparison: dict) -> str:
    """Render a comparison dict as a human-readable report string.

    Args:
        comparison: Output of :func:`compare_metrics`.

    Returns:
        Multi-line string suitable for logging or appending to lessons.md.
        Never raises.
    """
    try:
        current_rf = comparison.get("current_repeated_failures", 0)
        avg_rf = comparison.get("avg_historical_repeated_failures", 0.0)
        rf_trend = comparison.get("repeated_failure_trend", "stable")
        reg_trend = comparison.get("regression_trend", "stable")
        score = comparison.get("improvement_score", 1.0)
        lessons_total = comparison.get("lessons_extracted_total", 0)
        rules_total = comparison.get("rules_generated_total", 0)

        # Label the repeated-failure trend
        rf_label = _trend_label(current_rf, avg_rf, rf_trend)

        lines = [
            "## Run Performance",
            f"Repeated failures: {avg_rf:.1f} avg -> {current_rf} current ({rf_label})",
            f"Regression trend: {reg_trend}",
            f"Improvement score: {score:.3f} (lower is better; 1.0 = baseline)",
            f"Cumulative lessons extracted: {lessons_total}",
            f"Cumulative rules generated: {rules_total}",
        ]
        return "\n".join(lines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("format_metrics_report failed: %s", exc)
        return "## Run Performance\n(metrics unavailable)"


def _trend_label(current: float, avg: float, trend: str) -> str:
    """Produce a short label describing the direction and quality of a trend.

    Args:
        current: Current run value.
        avg: Historical average.
        trend: One of ``"increasing"``, ``"decreasing"``, ``"stable"``.

    Returns:
        Label string such as ``"improvement"`` or ``"regression"``.
    """
    if trend == "decreasing":
        return "improvement"
    if trend == "increasing":
        return "regression"
    return "stable"
