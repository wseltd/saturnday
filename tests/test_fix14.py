"""Fix 14: Cost observability and usage accounting.

Tests for the four accounting guarantees:
  14.1 Future runs record usable cost/usage telemetry
  14.2 Local-interactive mode explicitly declares accounting limitations
  14.3 Rough-bracket estimator with clearly labelled certainty classes
  14.4 Structural separation of build cost vs generated-product runtime cost

Coverage requirements:
  1.  Analytics output includes usage_accounting with required structure (14.1)
  2.  Backend, auth_mode, call_count, retry_count are recorded in build_cost (14.1)
  3.  local_interactive explicitly sets api_usage_available=False + limitations (14.2)
  4.  Certainty class is "rough_bracket" for local_interactive (14.3)
  5.  Certainty class is "not_realistically_possible" for unknown backend (14.3)
  6.  Certainty class is "exact" for API backend with usage data (14.3)
  7.  Certainty class is "strong_estimate" for API backend without usage data (14.3)
  8.  build_cost and generated_product_runtime_cost are structurally separate (14.4)
  9.  generated_product_runtime_cost.available is False at build time (14.4)
  10. build_cost and runtime cost are never blended (14.4)
  11. accounting_from_run_result correctly counts calls and retries from RunResult
  12. Unrelated existing analytics behaviour does not regress
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saturnday._types import RunResult, TicketResult
from saturnday.run.cost_accounting import (
    BuildCostAccounting,
    GeneratedProductRuntimeCost,
    UsageAccounting,
    accounting_from_run_result,
    build_usage_accounting,
    classify_accounting_certainty,
)
from saturnday.run.evidence import compute_run_analytics, write_analytics


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_run_result(
    passed: int = 0,
    failed: int = 0,
    ticket_results: tuple[TicketResult, ...] = (),
) -> RunResult:
    return RunResult(
        project_id="test",
        total_tickets=passed + failed,
        passed=passed,
        failed=failed,
        ticket_results=ticket_results,
    )


# ---------------------------------------------------------------------------
# Requirement 1: analytics output includes structured usage_accounting (14.1)
# ---------------------------------------------------------------------------


def test_analytics_includes_usage_accounting() -> None:
    """compute_run_analytics must include a usage_accounting key."""
    result = _make_run_result()
    analytics = compute_run_analytics(result)
    assert "usage_accounting" in analytics
    ua = analytics["usage_accounting"]
    assert "build_cost" in ua
    assert "generated_product_runtime_cost" in ua


def test_usage_accounting_serialisable_to_json(tmp_path: Path) -> None:
    """usage_accounting in analytics.json must be valid JSON."""
    result = _make_run_result(
        passed=1,
        ticket_results=(
            TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
        ),
    )
    analytics = compute_run_analytics(result, backend="claude-cli", auth_mode="local_interactive")
    write_analytics(analytics, tmp_path)
    raw = (tmp_path / "analytics.json").read_text()
    data = json.loads(raw)
    ua = data["usage_accounting"]
    assert "build_cost" in ua
    assert "generated_product_runtime_cost" in ua


# ---------------------------------------------------------------------------
# Requirement 2: backend, auth_mode, call_count, retry_count in build_cost (14.1)
# ---------------------------------------------------------------------------


def test_build_cost_records_backend_and_auth_mode() -> None:
    """build_cost must record backend and auth_mode."""
    result = _make_run_result(
        passed=2,
        ticket_results=(
            TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
            TicketResult(ticket_id="T002", disposition="PASS", attempts=2),
        ),
    )
    analytics = compute_run_analytics(result, backend="anthropic", auth_mode="api_key")
    bc = analytics["usage_accounting"]["build_cost"]
    assert bc["backend"] == "anthropic"
    assert bc["auth_mode"] == "api_key"


def test_build_cost_records_call_and_retry_counts() -> None:
    """build_cost must record total call_count and retry_count from ticket attempts."""
    result = _make_run_result(
        passed=2,
        failed=1,
        ticket_results=(
            TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
            TicketResult(ticket_id="T002", disposition="PASS", attempts=3),  # 2 retries
            TicketResult(ticket_id="T003", disposition="FAIL", attempts=2),  # 1 retry
        ),
    )
    acct = accounting_from_run_result(result, backend="claude-cli", auth_mode="local_interactive")
    assert acct.build_cost.call_count == 6  # 1 + 3 + 2
    assert acct.build_cost.retry_count == 3  # 0 + 2 + 1


# ---------------------------------------------------------------------------
# Requirement 3: local_interactive explicit limitations (14.2)
# ---------------------------------------------------------------------------


def test_local_interactive_api_usage_not_available() -> None:
    """local_interactive must set api_usage_available=False."""
    acct = build_usage_accounting(
        backend="claude-cli",
        auth_mode="local_interactive",
        call_count=5,
        retry_count=1,
    )
    assert acct.build_cost.api_usage_available is False


def test_local_interactive_has_explicit_limitations() -> None:
    """local_interactive must produce a non-empty limitations list."""
    acct = build_usage_accounting(
        backend="claude-cli",
        auth_mode="local_interactive",
        call_count=5,
        retry_count=1,
    )
    assert len(acct.build_cost.limitations) > 0
    combined = " ".join(acct.build_cost.limitations).lower()
    # Must mention the CLI backend limitation
    assert "claude-cli" in combined
    # Must mention subscription or billing
    assert "subscription" in combined or "billing" in combined


def test_local_interactive_does_not_claim_exact_accounting() -> None:
    """local_interactive must NOT produce certainty='exact'."""
    acct = build_usage_accounting(
        backend="codex-cli",
        auth_mode="local_interactive",
        call_count=3,
        retry_count=0,
    )
    assert acct.build_cost.certainty != "exact"


# ---------------------------------------------------------------------------
# Requirement 4: rough_bracket for local_interactive (14.3)
# ---------------------------------------------------------------------------


def test_certainty_rough_bracket_for_local_interactive() -> None:
    """local_interactive backend must produce certainty='rough_bracket'."""
    assert classify_accounting_certainty("local_interactive", "claude-cli") == "rough_bracket"


def test_certainty_rough_bracket_for_local_subscription() -> None:
    """local_subscription backend must produce certainty='rough_bracket'."""
    assert classify_accounting_certainty("local_subscription", "codex-cli") == "rough_bracket"


# ---------------------------------------------------------------------------
# Requirement 5: not_realistically_possible for unknown backend (14.3)
# ---------------------------------------------------------------------------


def test_certainty_not_realistically_possible_no_backend() -> None:
    """Empty backend must produce certainty='not_realistically_possible'."""
    assert classify_accounting_certainty("", "") == "not_realistically_possible"


def test_certainty_not_realistically_possible_unsupported() -> None:
    """Unsupported auth mode must produce certainty='not_realistically_possible'."""
    assert classify_accounting_certainty("unsupported", "unknown-backend") == "not_realistically_possible"


# ---------------------------------------------------------------------------
# Requirement 6: exact for API backend with usage data (14.3)
# ---------------------------------------------------------------------------


def test_certainty_exact_for_api_backend_with_usage_data() -> None:
    """API backend with usage data must produce certainty='exact'."""
    assert classify_accounting_certainty(
        "api_key", "anthropic", api_usage_data={"input_tokens": 1000, "output_tokens": 500}
    ) == "exact"


# ---------------------------------------------------------------------------
# Requirement 7: strong_estimate for API backend without usage data (14.3)
# ---------------------------------------------------------------------------


def test_certainty_strong_estimate_for_api_backend_no_usage() -> None:
    """API backend without usage data must produce certainty='strong_estimate'."""
    assert classify_accounting_certainty("api_key", "openai") == "strong_estimate"
    assert classify_accounting_certainty("ci", "anthropic") == "strong_estimate"


def test_certainty_classes_are_not_conflated() -> None:
    """All four certainty classes must be distinct and non-overlapping."""
    cases = [
        ("local_interactive", "claude-cli", None),    # rough_bracket
        ("", "", None),                                 # not_realistically_possible
        ("api_key", "anthropic", {"tokens": 100}),    # exact
        ("api_key", "openai", None),                   # strong_estimate
    ]
    results = [classify_accounting_certainty(am, b, ud) for am, b, ud in cases]
    assert len(set(results)) == 4, f"Expected 4 distinct classes, got: {results}"


# ---------------------------------------------------------------------------
# Requirement 8 & 9: structural separation (14.4)
# ---------------------------------------------------------------------------


def test_build_cost_and_runtime_cost_are_separate_fields() -> None:
    """usage_accounting must have separate build_cost and generated_product_runtime_cost."""
    acct = build_usage_accounting(
        backend="claude-cli",
        auth_mode="local_interactive",
        call_count=4,
        retry_count=1,
    )
    d = acct.to_dict()
    assert "build_cost" in d
    assert "generated_product_runtime_cost" in d
    # They must be distinct dicts
    assert d["build_cost"] is not d["generated_product_runtime_cost"]


def test_generated_product_runtime_cost_available_false() -> None:
    """generated_product_runtime_cost.available must be False at build time."""
    acct = build_usage_accounting(
        backend="anthropic",
        auth_mode="api_key",
        call_count=2,
        retry_count=0,
        api_usage_data={"input_tokens": 500, "output_tokens": 200},
    )
    assert acct.generated_product_runtime_cost.available is False


# ---------------------------------------------------------------------------
# Requirement 10: no blended totals (14.4)
# ---------------------------------------------------------------------------


def test_no_blended_cost_field_in_accounting() -> None:
    """usage_accounting must not have a top-level blended cost field."""
    acct = build_usage_accounting(
        backend="anthropic",
        auth_mode="api_key",
        call_count=3,
        retry_count=0,
    )
    d = acct.to_dict()
    # These would indicate blending
    forbidden_keys = {"total_cost", "combined_cost", "blended_cost", "total_tokens"}
    assert not forbidden_keys.intersection(d.keys()), (
        f"Blended cost fields found in usage_accounting: "
        f"{forbidden_keys.intersection(d.keys())}"
    )


def test_runtime_cost_note_mentions_separation() -> None:
    """generated_product_runtime_cost.note must mention separation from build cost."""
    grc = GeneratedProductRuntimeCost()
    note_lower = grc.note.lower()
    assert "runtime" in note_lower or "deployment" in note_lower or "build" in note_lower


# ---------------------------------------------------------------------------
# Requirement 11: accounting_from_run_result counts correctly
# ---------------------------------------------------------------------------


def test_accounting_from_run_result_zero_tickets() -> None:
    """Zero-ticket run must produce call_count=0, retry_count=0."""
    result = _make_run_result()
    acct = accounting_from_run_result(result)
    assert acct.build_cost.call_count == 0
    assert acct.build_cost.retry_count == 0


def test_accounting_from_run_result_single_attempt() -> None:
    """Single-attempt tickets contribute 0 retries each."""
    result = _make_run_result(
        passed=3,
        ticket_results=tuple(
            TicketResult(ticket_id=f"T00{i}", disposition="PASS", attempts=1)
            for i in range(3)
        ),
    )
    acct = accounting_from_run_result(result, backend="claude-cli", auth_mode="local_interactive")
    assert acct.build_cost.call_count == 3
    assert acct.build_cost.retry_count == 0


# ---------------------------------------------------------------------------
# Requirement 12: existing analytics behaviour does not regress
# ---------------------------------------------------------------------------


def test_existing_analytics_keys_still_present() -> None:
    """All pre-Fix-14 analytics keys except cost_tracking must still be present."""
    result = _make_run_result(
        passed=2,
        failed=1,
        ticket_results=(
            TicketResult(ticket_id="T001", disposition="PASS", attempts=1),
            TicketResult(ticket_id="T002", disposition="PASS", attempts=2),
            TicketResult(ticket_id="T003", disposition="FAIL", attempts=3, failure_category="timeout"),
        ),
    )
    analytics = compute_run_analytics(result)
    for key in (
        "acceptance_rate", "avg_retries", "failure_category_distribution",
        "total_tickets", "passed", "failed", "skipped",
        "stop_reason", "definition_of_done_met", "backend", "auth_mode",
        "senior_quality_verdict",
    ):
        assert key in analytics, f"Missing key: {key}"


def test_cost_tracking_key_no_longer_present() -> None:
    """The old vague 'cost_tracking: partial' field must no longer exist."""
    result = _make_run_result()
    analytics = compute_run_analytics(result)
    assert "cost_tracking" not in analytics
