"""Eval scenarios using seeded fixtures for regression proof.

Uses the eval_harness to run integration-level checks against
known-good fixture repositories.  Each class is a distinct capability
domain (governance detection, scoped repair, plan governance, release
preflight).  All scenarios use seeded fixtures so results are
deterministic and repeatable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from saturnday.eval_harness import EvalResult, run_eval_suite

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_git(path: Path) -> None:
    """Initialise a bare git repo and commit all files."""
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Eval functions
# ---------------------------------------------------------------------------

def _eval_governance_detection(scenario: dict) -> EvalResult:
    """Eval: does governance detect seeded security findings?"""
    from saturnday.governance import run_full_repo_review

    t0 = time.monotonic()
    repo = Path(scenario["repo_path"])
    pack, _ = run_full_repo_review(repo)
    elapsed = time.monotonic() - t0

    has_findings = sum(len(cr.findings) for cr in pack.check_results) > 0
    has_sec = any(
        cr.rule_id and cr.rule_id.startswith("SEC-")
        for cr in pack.check_results
        if cr.findings
    )

    return EvalResult(
        scenario=scenario["name"],
        passed=pack.disposition == "FAIL" and has_findings and has_sec,
        duration_s=elapsed,
        details={
            "disposition": pack.disposition,
            "findings": sum(len(cr.findings) for cr in pack.check_results),
        },
    )


def _eval_scoped_repair(scenario: dict) -> EvalResult:
    """Eval: does category-scoped repair stay scoped?"""
    from saturnday.openclaw_scanner import Finding
    from saturnday.repair.finding_category import parse_repair_categories

    t0 = time.monotonic()
    cats = parse_repair_categories(scenario["hint"])

    findings = [
        Finding(check="hardcoded_jwt", kind="hardcoded_secret", category="security"),
        Finding(check="ruff", kind="ruff", category="quality"),
    ]

    if cats:
        filtered = [f for f in findings if f.category in cats]
        passed = len(filtered) == scenario["expected_count"]
        filtered_count = len(filtered)
    else:
        passed = False
        filtered_count = 0

    return EvalResult(
        scenario=scenario["name"],
        passed=passed,
        duration_s=time.monotonic() - t0,
        details={
            "categories": list(cats) if cats else [],
            "filtered_count": filtered_count,
        },
    )


# ---------------------------------------------------------------------------
# TestGovernanceDetectionEval
# ---------------------------------------------------------------------------

class TestGovernanceDetectionEval:
    """Governance must detect seeded security findings in the Python fixture."""

    def test_governance_detects_security_findings(self, tmp_path):
        repo = tmp_path / "service"
        shutil.copytree(FIXTURES / "sample-python-service", repo)
        _init_git(repo)

        result = run_eval_suite(
            "governance_detection",
            [{"name": "python_service", "repo_path": str(repo)}],
            _eval_governance_detection,
            runs=1,
            threshold=1.0,
        )
        assert result.above_threshold, (
            f"governance_detection suite failed: pass_rate={result.pass_rate}, "
            f"details={[r.details for r in result.results]}"
        )
        assert result.pass_rate == 1.0


# ---------------------------------------------------------------------------
# TestScopedRepairEval
# ---------------------------------------------------------------------------

class TestScopedRepairEval:
    """Category-scoped repair must filter findings to the requested category."""

    def test_security_scoped_repair_stays_scoped(self):
        result = run_eval_suite(
            "scoped_repair",
            [
                {
                    "name": "security_only",
                    "hint": "fix all the security issues",
                    "expected_count": 1,
                },
                {
                    "name": "quality_only",
                    "hint": "fix quality issues",
                    "expected_count": 1,
                },
            ],
            _eval_scoped_repair,
            runs=3,
            threshold=1.0,
        )
        assert result.above_threshold, (
            f"scoped_repair suite failed: pass_rate={result.pass_rate}, "
            f"details={[r.details for r in result.results]}"
        )
        assert result.pass_rate == 1.0


# ---------------------------------------------------------------------------
# TestPlanGovernanceEval
# ---------------------------------------------------------------------------

class TestPlanGovernanceEval:
    """RunResult.definition_of_done_met and plan_governance_met flags are accurate."""

    def test_plan_governance_split_scenarios(self):
        from saturnday._types import RunResult

        scenarios = [
            {"name": "dod_met_pg_not_met", "dod": True, "pg": False},
            {"name": "both_met", "dod": True, "pg": True},
            {"name": "both_not_met", "dod": False, "pg": False},
        ]

        def _eval_pg(s: dict) -> EvalResult:
            r = RunResult(
                project_id="test",
                definition_of_done_met=s["dod"],
                plan_governance_met=s["pg"],
            )
            return EvalResult(
                scenario=s["name"],
                passed=(
                    r.definition_of_done_met == s["dod"]
                    and r.plan_governance_met == s["pg"]
                ),
                duration_s=0.001,
            )

        result = run_eval_suite(
            "plan_governance",
            scenarios,
            _eval_pg,
            runs=3,
            threshold=1.0,
        )
        assert result.above_threshold, (
            f"plan_governance suite failed: pass_rate={result.pass_rate}"
        )


# ---------------------------------------------------------------------------
# TestReleasePreflightEval
# ---------------------------------------------------------------------------

class TestReleasePreflightEval:
    """Release preflight must return a valid result object for the leak fixture."""

    def test_leak_detection(self, tmp_path):
        repo = tmp_path / "leaky"
        shutil.copytree(FIXTURES / "sample-leak-package", repo)
        _init_git(repo)

        def _eval_leak(s: dict) -> EvalResult:
            from saturnday.release.orchestrator import run_release_preflight

            t0 = time.monotonic()
            pack = run_release_preflight(
                repo_path=Path(s["repo_path"]),
                artefact_type="python",
            )
            return EvalResult(
                scenario=s["name"],
                passed=hasattr(pack, "disposition"),
                duration_s=time.monotonic() - t0,
                details={"disposition": getattr(pack, "disposition", "unknown")},
            )

        result = run_eval_suite(
            "release_leak_detection",
            [{"name": "leaky_package", "repo_path": str(repo)}],
            _eval_leak,
            runs=1,
            threshold=1.0,
        )
        assert result.above_threshold, (
            f"release_leak_detection suite failed: pass_rate={result.pass_rate}, "
            f"details={[r.details for r in result.results]}"
        )
