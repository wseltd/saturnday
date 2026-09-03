"""Bounded eval harness for regression and quality proof.

Provides repeated-run evaluation, pass-rate thresholds, A/B comparison,
and statistical regression gating against seeded fixtures.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class EvalResult:
    """Result of a single eval scenario run."""

    scenario: str
    passed: bool
    duration_s: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalSuiteResult:
    """Aggregated results across multiple runs of an eval suite."""

    suite_name: str
    total_runs: int
    pass_count: int
    fail_count: int
    pass_rate: float
    mean_duration_s: float
    results: list[EvalResult] = field(default_factory=list)
    threshold: float = 0.8  # minimum acceptable pass rate

    @property
    def above_threshold(self) -> bool:
        """Return True when the suite pass rate meets or exceeds the threshold."""
        return self.pass_rate >= self.threshold


def run_eval_suite(
    suite_name: str,
    scenarios: list[dict[str, Any]],
    eval_fn: Callable[[dict[str, Any]], EvalResult],
    runs: int = 1,
    threshold: float = 0.8,
) -> EvalSuiteResult:
    """Run an eval suite with repeated runs and threshold checking.

    Each scenario is executed ``runs`` times.  Exceptions raised by
    ``eval_fn`` are captured and recorded as failed :class:`EvalResult`
    entries rather than propagating to the caller.

    Args:
        suite_name: Name for this evaluation suite.
        scenarios: List of scenario configs to evaluate.
        eval_fn: Function that takes a scenario dict and returns
            :class:`EvalResult`.
        runs: Number of times to repeat each scenario (for statistical
            power).
        threshold: Minimum pass rate to consider the suite passing.

    Returns:
        :class:`EvalSuiteResult` with aggregated metrics.
    """
    all_results: list[EvalResult] = []

    for _run_idx in range(runs):
        for scenario in scenarios:
            try:
                result = eval_fn(scenario)
                all_results.append(result)
            except Exception as exc:
                logger.warning(
                    "eval_fn raised for scenario %r: %s",
                    scenario.get("name", "unknown"),
                    exc,
                )
                all_results.append(
                    EvalResult(
                        scenario=scenario.get("name", "unknown"),
                        passed=False,
                        duration_s=0.0,
                        details={"error": str(exc)},
                    )
                )

    pass_count = sum(1 for r in all_results if r.passed)
    fail_count = len(all_results) - pass_count
    pass_rate = pass_count / len(all_results) if all_results else 0.0
    durations = [r.duration_s for r in all_results if r.duration_s > 0]
    mean_duration = statistics.mean(durations) if durations else 0.0

    return EvalSuiteResult(
        suite_name=suite_name,
        total_runs=len(all_results),
        pass_count=pass_count,
        fail_count=fail_count,
        pass_rate=pass_rate,
        mean_duration_s=mean_duration,
        results=all_results,
        threshold=threshold,
    )


def compare_eval_results(
    baseline: EvalSuiteResult,
    candidate: EvalSuiteResult,
) -> dict[str, Any]:
    """A/B comparison between baseline and candidate eval results.

    A pass-rate drop greater than 5 percentage points is classified as a
    regression.  A gain greater than 5 percentage points is classified as
    an improvement.  Changes within that band are neutral.

    Args:
        baseline: Prior or reference :class:`EvalSuiteResult`.
        candidate: New or proposed :class:`EvalSuiteResult`.

    Returns:
        Comparison dict containing delta metrics and a ``verdict`` field
        of ``"REGRESSION"``, ``"IMPROVEMENT"``, or ``"NEUTRAL"``.
    """
    pass_rate_delta = candidate.pass_rate - baseline.pass_rate
    duration_delta = candidate.mean_duration_s - baseline.mean_duration_s

    regression = pass_rate_delta < -0.05   # >5 pp drop is regression
    improvement = pass_rate_delta > 0.05   # >5 pp gain is improvement

    return {
        "baseline_suite": baseline.suite_name,
        "candidate_suite": candidate.suite_name,
        "baseline_pass_rate": baseline.pass_rate,
        "candidate_pass_rate": candidate.pass_rate,
        "pass_rate_delta": pass_rate_delta,
        "baseline_mean_duration_s": baseline.mean_duration_s,
        "candidate_mean_duration_s": candidate.mean_duration_s,
        "duration_delta_s": duration_delta,
        "regression_detected": regression,
        "improvement_detected": improvement,
        "verdict": (
            "REGRESSION" if regression else ("IMPROVEMENT" if improvement else "NEUTRAL")
        ),
    }


def write_eval_report(
    result: EvalSuiteResult,
    output_path: Path,
) -> Path:
    """Write eval results as JSON evidence.

    Args:
        result: Aggregated :class:`EvalSuiteResult` to serialise.
        output_path: Destination file path.  Parent directories are
            created automatically.

    Returns:
        The resolved ``output_path`` after writing.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "suite_name": result.suite_name,
        "total_runs": result.total_runs,
        "pass_count": result.pass_count,
        "fail_count": result.fail_count,
        "pass_rate": result.pass_rate,
        "threshold": result.threshold,
        "above_threshold": result.above_threshold,
        "mean_duration_s": result.mean_duration_s,
        "results": [
            {
                "scenario": r.scenario,
                "passed": r.passed,
                "duration_s": r.duration_s,
                "details": r.details,
            }
            for r in result.results
        ],
    }
    output_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.debug("Eval report written to %s", output_path)
    return output_path
