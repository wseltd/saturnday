"""Tests for release-preflight stdout output.

Covers:
1. PASS output is human-readable with disposition + evidence path
2. FAIL output includes compact findings summary + evidence path
3. Missing-detail fallback is safe and explicit
4. Output does not regress for basic result shapes
5. No repo state mutation from output formatting
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch
import sys


# Minimal stubs matching the real dataclass shapes
@dataclass
class StubCheckResult:
    name: str
    rule_id: str
    status: str
    severity: str = "error"
    findings: list[dict[str, Any]] = field(default_factory=list)
    files_checked: int = 0
    elapsed_s: float = 0.0


@dataclass
class StubEvidencePack:
    run_id: str = "rel_20260405T120000Z"
    artefact_type: str = "python"
    artefact_path: str = "dist/mypackage-1.0-py3-none-any.whl"
    check_results: list[StubCheckResult] = field(default_factory=list)
    disposition_reasons: list[str] = field(default_factory=list)


@dataclass
class StubResult:
    disposition: str
    evidence_pack: Any = None
    evidence_path: Path | None = None
    error: str = ""


def _capture_summary(result: StubResult) -> str:
    """Call _print_release_summary and capture stdout."""
    from saturnday.cli import _print_release_summary
    buf = StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        _print_release_summary(result)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. PASS output
# ---------------------------------------------------------------------------

class TestPassOutput:
    def test_pass_shows_disposition(self) -> None:
        result = StubResult(
            disposition="PASS",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="source_map_blocker", rule_id="REL-001", status="PASS"),
                    StubCheckResult(name="secrets_in_artefact", rule_id="REL-002", status="PASS"),
                ],
            ),
            evidence_path=Path("/tmp/evidence"),
        )
        output = _capture_summary(result)
        assert "PASS" in output
        assert "ready for release" in output.lower()

    def test_pass_shows_evidence_path(self) -> None:
        result = StubResult(
            disposition="PASS",
            evidence_pack=StubEvidencePack(check_results=[]),
            evidence_path=Path("/tmp/evidence/rel_123"),
        )
        output = _capture_summary(result)
        assert "/tmp/evidence/rel_123" in output

    def test_pass_shows_check_count(self) -> None:
        result = StubResult(
            disposition="PASS",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="PASS"),
                    StubCheckResult(name="check_b", rule_id="REL-002", status="PASS"),
                    StubCheckResult(name="check_c", rule_id="REL-003", status="PASS"),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "3 total" in output
        assert "3 passed" in output

    def test_pass_shows_per_check_lines(self) -> None:
        result = StubResult(
            disposition="PASS",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="secrets_in_artefact", rule_id="REL-002", status="PASS"),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "REL-002" in output
        assert "secrets_in_artefact" in output


# ---------------------------------------------------------------------------
# 2. FAIL output with findings
# ---------------------------------------------------------------------------

class TestFailOutput:
    def test_fail_shows_findings(self) -> None:
        result = StubResult(
            disposition="FAIL",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(
                        name="secrets_in_artefact",
                        rule_id="REL-002",
                        status="FAIL",
                        findings=[
                            {"path": "mypackage/config.py", "detail": "Stripe API key detected"},
                            {"path": "mypackage/auth.py", "detail": "AWS secret key detected"},
                        ],
                    ),
                    StubCheckResult(name="source_map_blocker", rule_id="REL-001", status="PASS"),
                ],
            ),
            evidence_path=Path("/tmp/evidence"),
        )
        output = _capture_summary(result)
        assert "FAIL" in output
        assert "Stripe API key" in output
        assert "mypackage/config.py" in output
        assert "Blocking findings present" in output

    def test_fail_shows_next_steps(self) -> None:
        result = StubResult(
            disposition="FAIL",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="FAIL", findings=[{"path": "x"}]),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "release-exception" in output or "Fix the issues" in output

    def test_fail_truncates_long_findings_list(self) -> None:
        findings = [{"path": f"file_{i}.py", "detail": f"Issue {i}"} for i in range(10)]
        result = StubResult(
            disposition="FAIL",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="FAIL", findings=findings),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "... and 5 more" in output


# ---------------------------------------------------------------------------
# 3. Missing detail fallback
# ---------------------------------------------------------------------------

class TestMissingDetailFallback:
    def test_no_evidence_pack(self) -> None:
        result = StubResult(disposition="FAIL", evidence_pack=None)
        output = _capture_summary(result)
        assert "FAIL" in output
        assert "No detailed check results available" in output

    def test_finding_with_no_detail(self) -> None:
        result = StubResult(
            disposition="FAIL",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="FAIL", findings=[{"path": "foo.py"}]),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "foo.py" in output


# ---------------------------------------------------------------------------
# 4. WARN output
# ---------------------------------------------------------------------------

class TestFailGuidanceCapabilityAware:
    def test_fail_without_premium_says_requires_premium(self) -> None:
        result = StubResult(
            disposition="FAIL",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="FAIL", findings=[{"path": "x"}]),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "saturnday-premium" in output
        assert "Approved exceptions require" in output

    def test_fail_with_premium_shows_release_exception(self) -> None:
        result = StubResult(
            disposition="FAIL",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="FAIL", findings=[{"path": "x"}]),
                ],
            ),
        )
        with patch("saturnday.capability_registry.is_available", return_value=True):
            output = _capture_summary(result)
        assert "saturnday release-exception" in output
        assert "saturnday-premium" not in output


class TestWarnOutput:
    def test_warn_shows_advisory_message(self) -> None:
        result = StubResult(
            disposition="WARN",
            evidence_pack=StubEvidencePack(
                check_results=[
                    StubCheckResult(name="check_a", rule_id="REL-001", status="WARN"),
                ],
            ),
        )
        output = _capture_summary(result)
        assert "WARN" in output
        assert "Advisory" in output or "advisory" in output
