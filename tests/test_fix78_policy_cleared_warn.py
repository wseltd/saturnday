"""Fix 78 — narrow policy-cleared WARN gate.

The runner success gate must accept exactly one WARN shape: a WARN whose
post-policy reasons explicitly carry the policy-cleared marker
``["all_findings_expected"]`` (set in ``_apply_policy_filtering`` by the
Fix 65 follow-up when ``expected_findings`` covers every error finding).

All other WARN shapes (generic per-check reason dicts, empty reasons, mixed
reasons, FAIL, anything that is not exactly the single-element marker list)
must remain non-success.  This test pins that boundary so any future widening
is caught explicitly.
"""

from __future__ import annotations

from saturnday.ticket_runner import _is_policy_cleared_warn


# ---------------------------------------------------------------------------
# True cases (the only success-gating WARN shape)
# ---------------------------------------------------------------------------


def test_warn_with_policy_cleared_marker_is_success() -> None:
    assert _is_policy_cleared_warn("WARN", ["all_findings_expected"]) is True


# ---------------------------------------------------------------------------
# False cases — must remain non-success
# ---------------------------------------------------------------------------


def test_pass_is_not_policy_cleared_warn() -> None:
    """PASS already takes the success branch; the helper should not also fire."""
    assert _is_policy_cleared_warn("PASS", ["all_findings_expected"]) is False


def test_fail_is_not_policy_cleared_warn() -> None:
    assert _is_policy_cleared_warn("FAIL", ["all_findings_expected"]) is False


def test_warn_with_empty_reasons_is_not_success() -> None:
    """Empty reasons (no policy interaction) must not pass the gate."""
    assert _is_policy_cleared_warn("WARN", []) is False


def test_warn_with_compute_disposition_dict_reasons_is_not_success() -> None:
    """The list-of-dicts reasons emitted by compute_disposition for a soft-check
    FAIL (Fix 67c — variant A scenario) must not be treated as policy-cleared."""
    reasons = [{"check": "read_without_write_surface", "status": "FAIL",
                "severity": "warning", "finding_count": 1}]
    assert _is_policy_cleared_warn("WARN", reasons) is False


def test_warn_with_marker_plus_extra_is_not_success() -> None:
    """Marker must be the SOLE entry — adding anything else widens the signal
    beyond what _apply_policy_filtering emits and must not pass."""
    assert _is_policy_cleared_warn("WARN", ["all_findings_expected", "extra"]) is False


def test_warn_with_different_marker_string_is_not_success() -> None:
    """Only the exact marker string counts."""
    assert _is_policy_cleared_warn("WARN", ["all_expected"]) is False
    assert _is_policy_cleared_warn("WARN", ["expected_findings"]) is False


def test_warn_with_non_list_reasons_is_not_success() -> None:
    """Defensive: a stray non-list reasons surface must not silently pass."""
    assert _is_policy_cleared_warn("WARN", None) is False  # type: ignore[arg-type]
    assert _is_policy_cleared_warn("WARN", "all_findings_expected") is False  # type: ignore[arg-type]
    assert _is_policy_cleared_warn("WARN", {"all_findings_expected": True}) is False  # type: ignore[arg-type]
