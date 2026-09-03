"""Unit tests for eval_harness module.

Covers:
- run_eval_suite: all-pass, mixed, repeated runs, exception capture
- EvalSuiteResult.above_threshold
- compare_eval_results: regression, improvement, neutral
- write_eval_report: valid JSON output
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from saturnday.eval_harness import (
    EvalResult,
    EvalSuiteResult,
    compare_eval_results,
    run_eval_suite,
    write_eval_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _always_pass(scenario: dict) -> EvalResult:
    return EvalResult(scenario=scenario["name"], passed=True, duration_s=0.01)


def _always_fail(scenario: dict) -> EvalResult:
    return EvalResult(scenario=scenario["name"], passed=False, duration_s=0.01)


def _raises(scenario: dict) -> EvalResult:
    raise RuntimeError("boom")


def _scenarios(n: int) -> list[dict]:
    return [{"name": f"s{i}"} for i in range(n)]


def _suite_result(pass_rate: float, suite_name: str = "suite") -> EvalSuiteResult:
    """Build a minimal EvalSuiteResult for comparison tests."""
    total = 10
    passes = round(pass_rate * total)
    return EvalSuiteResult(
        suite_name=suite_name,
        total_runs=total,
        pass_count=passes,
        fail_count=total - passes,
        pass_rate=pass_rate,
        mean_duration_s=0.1,
        threshold=0.8,
    )


# ---------------------------------------------------------------------------
# run_eval_suite
# ---------------------------------------------------------------------------

class TestRunEvalSuite:
    def test_all_pass_returns_pass_rate_1(self):
        result = run_eval_suite("s", _scenarios(3), _always_pass)
        assert result.pass_rate == 1.0
        assert result.pass_count == 3
        assert result.fail_count == 0

    def test_all_fail_returns_pass_rate_0(self):
        result = run_eval_suite("s", _scenarios(4), _always_fail)
        assert result.pass_rate == 0.0
        assert result.pass_count == 0
        assert result.fail_count == 4

    def test_mixed_scenarios_correct_pass_rate(self):
        call_count = [0]

        def _mixed(scenario: dict) -> EvalResult:
            call_count[0] += 1
            passed = call_count[0] % 2 == 1  # odd calls pass
            return EvalResult(scenario=scenario["name"], passed=passed, duration_s=0.0)

        result = run_eval_suite("s", _scenarios(4), _mixed)
        assert result.pass_count == 2
        assert result.fail_count == 2
        assert result.pass_rate == 0.5

    def test_repeated_runs_multiplies_total_runs(self):
        result = run_eval_suite("s", _scenarios(3), _always_pass, runs=4)
        assert result.total_runs == 12  # 3 scenarios * 4 runs

    def test_exception_in_eval_fn_captured_as_failure(self):
        result = run_eval_suite("s", _scenarios(2), _raises)
        assert result.pass_count == 0
        assert result.fail_count == 2
        assert all("error" in r.details for r in result.results)

    def test_suite_name_propagated(self):
        result = run_eval_suite("my_suite", _scenarios(1), _always_pass)
        assert result.suite_name == "my_suite"

    def test_empty_scenarios_returns_zero_pass_rate(self):
        result = run_eval_suite("s", [], _always_pass)
        assert result.pass_rate == 0.0
        assert result.total_runs == 0

    def test_mean_duration_computed(self):
        def _timed(scenario: dict) -> EvalResult:
            return EvalResult(scenario=scenario["name"], passed=True, duration_s=0.2)

        result = run_eval_suite("s", _scenarios(3), _timed)
        assert abs(result.mean_duration_s - 0.2) < 1e-9

    def test_threshold_stored_on_result(self):
        result = run_eval_suite("s", _scenarios(1), _always_pass, threshold=0.95)
        assert result.threshold == 0.95

    def test_runs_default_is_1(self):
        result = run_eval_suite("s", _scenarios(5), _always_pass)
        assert result.total_runs == 5


# ---------------------------------------------------------------------------
# EvalSuiteResult.above_threshold
# ---------------------------------------------------------------------------

class TestAboveThreshold:
    def test_above_threshold_true_when_pass_rate_meets_threshold(self):
        r = _suite_result(0.8)
        r.threshold = 0.8
        assert r.above_threshold is True

    def test_above_threshold_true_when_pass_rate_exceeds_threshold(self):
        r = _suite_result(0.9)
        r.threshold = 0.8
        assert r.above_threshold is True

    def test_above_threshold_false_when_pass_rate_below_threshold(self):
        r = _suite_result(0.7)
        r.threshold = 0.8
        assert r.above_threshold is False

    def test_above_threshold_with_threshold_1_requires_perfect(self):
        r = _suite_result(0.9)
        r.threshold = 1.0
        assert r.above_threshold is False

    def test_above_threshold_with_threshold_0_always_true(self):
        r = _suite_result(0.0)
        r.threshold = 0.0
        assert r.above_threshold is True


# ---------------------------------------------------------------------------
# compare_eval_results
# ---------------------------------------------------------------------------

class TestCompareEvalResults:
    def test_regression_detected_on_large_drop(self):
        baseline = _suite_result(0.9, "baseline")
        candidate = _suite_result(0.8, "candidate")
        cmp = compare_eval_results(baseline, candidate)
        assert cmp["regression_detected"] is True
        assert cmp["verdict"] == "REGRESSION"

    def test_improvement_detected_on_large_gain(self):
        baseline = _suite_result(0.7, "baseline")
        candidate = _suite_result(0.8, "candidate")
        cmp = compare_eval_results(baseline, candidate)
        assert cmp["improvement_detected"] is True
        assert cmp["verdict"] == "IMPROVEMENT"

    def test_neutral_when_within_5pp_band(self):
        baseline = _suite_result(0.80, "baseline")
        candidate = _suite_result(0.83, "candidate")
        cmp = compare_eval_results(baseline, candidate)
        assert cmp["regression_detected"] is False
        assert cmp["improvement_detected"] is False
        assert cmp["verdict"] == "NEUTRAL"

    def test_neutral_when_identical(self):
        baseline = _suite_result(0.85, "baseline")
        candidate = _suite_result(0.85, "candidate")
        cmp = compare_eval_results(baseline, candidate)
        assert cmp["verdict"] == "NEUTRAL"

    def test_regression_exactly_at_boundary_is_not_regression(self):
        """A drop of exactly 0.05 is NOT a regression (must be > 0.05)."""
        baseline = _suite_result(0.85, "baseline")
        candidate = _suite_result(0.80, "candidate")
        cmp = compare_eval_results(baseline, candidate)
        # 0.80 - 0.85 = -0.05, boundary is < -0.05 for regression
        assert cmp["regression_detected"] is False

    def test_delta_keys_present(self):
        baseline = _suite_result(0.8, "b")
        candidate = _suite_result(0.9, "c")
        cmp = compare_eval_results(baseline, candidate)
        assert "pass_rate_delta" in cmp
        assert "duration_delta_s" in cmp
        assert "baseline_pass_rate" in cmp
        assert "candidate_pass_rate" in cmp

    def test_pass_rate_delta_correct_sign(self):
        baseline = _suite_result(0.6, "b")
        candidate = _suite_result(0.8, "c")
        cmp = compare_eval_results(baseline, candidate)
        assert cmp["pass_rate_delta"] > 0

    def test_suite_names_in_comparison(self):
        baseline = _suite_result(0.8, "old")
        candidate = _suite_result(0.9, "new")
        cmp = compare_eval_results(baseline, candidate)
        assert cmp["baseline_suite"] == "old"
        assert cmp["candidate_suite"] == "new"


# ---------------------------------------------------------------------------
# write_eval_report
# ---------------------------------------------------------------------------

class TestWriteEvalReport:
    def test_produces_valid_json(self, tmp_path):
        result = run_eval_suite("test_suite", _scenarios(2), _always_pass)
        output = tmp_path / "report.json"
        write_eval_report(result, output)
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["suite_name"] == "test_suite"

    def test_report_contains_required_keys(self, tmp_path):
        result = run_eval_suite("s", _scenarios(3), _always_pass)
        output = tmp_path / "report.json"
        write_eval_report(result, output)
        data = json.loads(output.read_text(encoding="utf-8"))
        for key in ("suite_name", "total_runs", "pass_count", "fail_count",
                    "pass_rate", "threshold", "above_threshold", "mean_duration_s",
                    "results"):
            assert key in data, f"Missing key: {key}"

    def test_report_results_are_list(self, tmp_path):
        result = run_eval_suite("s", _scenarios(2), _always_pass)
        output = tmp_path / "report.json"
        write_eval_report(result, output)
        data = json.loads(output.read_text(encoding="utf-8"))
        assert isinstance(data["results"], list)
        assert len(data["results"]) == 2

    def test_report_above_threshold_reflects_pass_rate(self, tmp_path):
        result = run_eval_suite("s", _scenarios(5), _always_fail, threshold=0.8)
        output = tmp_path / "report.json"
        write_eval_report(result, output)
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["above_threshold"] is False
        assert data["pass_rate"] == 0.0

    def test_creates_parent_directories(self, tmp_path):
        result = run_eval_suite("s", _scenarios(1), _always_pass)
        output = tmp_path / "deep" / "nested" / "report.json"
        write_eval_report(result, output)
        assert output.is_file()

    def test_returns_path(self, tmp_path):
        result = run_eval_suite("s", _scenarios(1), _always_pass)
        output = tmp_path / "r.json"
        returned = write_eval_report(result, output)
        assert returned == output
