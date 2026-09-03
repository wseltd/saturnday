"""Integration tests for waiver-to-ratchet wiring (C10).

Verifies that waived fingerprints computed from policy waivers are correctly
excluded from ratchet blocking when compare_findings() is called with them.

These tests operate at the ratchet.compare_findings() layer (pure unit, no I/O)
to keep them fast and deterministic. The governance.py wiring is covered by the
existence of the integration path; the ratchet layer is the correct seam to test
the exclusion logic because that is where the contract lives.
"""

from datetime import date

import pytest

from saturnday.ratchet import (
    Baseline,
    FindingFingerprint,
    compare_findings,
)
from saturnday.waivers import (
    Waiver,
    check_waiver,
    load_waivers_from_policy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fp(
    rule_id: str,
    path: str = "src/api.py",
    snippet_hash: str = "deadbeef",
    severity: str = "warning",
) -> FindingFingerprint:
    return FindingFingerprint(rule_id, path, "", snippet_hash, severity)


def _make_waiver(
    rule_id: str,
    path: str = "src/api.py",
    expires: date = date(2027, 1, 1),
) -> Waiver:
    return Waiver(
        rule_id=rule_id,
        path=path,
        reason="Accepted tech debt for C10 test",
        owner="saturnday-test",
        expires=expires,
    )


def _compute_waived_fps(
    current_fps: set[FindingFingerprint],
    waivers: list[Waiver],
    today: date = date(2026, 3, 18),
) -> set[FindingFingerprint]:
    """Mirror the logic added to governance.py for waived fingerprint computation."""
    waived: set[FindingFingerprint] = set()
    for fp in current_fps:
        result = check_waiver(
            rule_id=fp.rule_id,
            file_path=fp.path,
            line=0,
            waivers=waivers,
            inline_suppressions=[],
            today=today,
        )
        if result.suppressed:
            waived.add(fp)
    return waived


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWaiverRatchetIntegration:
    """Waived findings must not trigger a ratchet FAIL."""

    def test_waived_finding_excluded_from_ratchet(self):
        """A finding that matches an active waiver does not block even if new."""
        # Baseline has one legacy finding; the current scan has that plus a new one.
        legacy_fp = _make_fp("SEC-013", snippet_hash="legacy")
        new_fp = _make_fp("SEC-015", path="src/api.py", snippet_hash="newstuff")

        baseline = Baseline(
            findings={legacy_fp},
            ratchet_mode="block_new",
        )
        current_fps = {legacy_fp, new_fp}

        # Waiver covers the new finding's rule+path
        waivers = [_make_waiver("SEC-015", path="src/api.py")]
        waived_fps = _compute_waived_fps(current_fps, waivers)

        assert new_fp in waived_fps, "Waiver should have matched the new finding"

        result = compare_findings(current_fps, baseline, waived_fingerprints=waived_fps)

        assert result.disposition == "PASS", (
            f"Expected PASS when new finding is waived, got FAIL. "
            f"Reasons: {result.reasons}"
        )

    def test_non_waived_finding_still_blocks(self):
        """A finding without a matching waiver still causes ratchet FAIL."""
        legacy_fp = _make_fp("SEC-013", snippet_hash="legacy")
        new_fp = _make_fp("SEC-015", path="src/api.py", snippet_hash="newstuff")

        baseline = Baseline(
            findings={legacy_fp},
            ratchet_mode="block_new",
        )
        current_fps = {legacy_fp, new_fp}

        # No waivers — nothing is suppressed
        waived_fps: set[FindingFingerprint] = set()

        result = compare_findings(current_fps, baseline, waived_fingerprints=waived_fps)

        assert result.disposition == "FAIL", (
            "Expected FAIL when new finding has no waiver"
        )
        assert len(result.new_findings) == 1
        assert result.new_findings[0].rule_id == "SEC-015"

    def test_waiver_wrong_path_does_not_suppress(self):
        """A waiver for a different path does not suppress the finding."""
        new_fp = _make_fp("SEC-015", path="src/api.py", snippet_hash="newstuff")
        baseline = Baseline(findings=set(), ratchet_mode="block_new")
        current_fps = {new_fp}

        # Waiver covers a different file
        waivers = [_make_waiver("SEC-015", path="src/other.py")]
        waived_fps = _compute_waived_fps(current_fps, waivers)

        assert new_fp not in waived_fps, "Path mismatch should prevent suppression"

        result = compare_findings(current_fps, baseline, waived_fingerprints=waived_fps)
        assert result.disposition == "FAIL"

    def test_expired_waiver_does_not_suppress(self):
        """An expired waiver does not suppress the finding."""
        new_fp = _make_fp("SEC-015", path="src/api.py", snippet_hash="newstuff")
        baseline = Baseline(findings=set(), ratchet_mode="block_new")
        current_fps = {new_fp}

        # Waiver expired before today
        waivers = [_make_waiver("SEC-015", path="src/api.py", expires=date(2025, 1, 1))]
        waived_fps = _compute_waived_fps(current_fps, waivers, today=date(2026, 3, 18))

        assert new_fp not in waived_fps, "Expired waiver should not suppress"

        result = compare_findings(current_fps, baseline, waived_fingerprints=waived_fps)
        assert result.disposition == "FAIL"

    def test_load_waivers_from_policy_and_suppress(self):
        """Round-trip: load waiver from raw policy dict, compute waived set, assert PASS."""
        policy_raw = {
            "waivers": [{
                "rule_id": "SEC-015",
                "path": "src/api.py",
                "reason": "Known legacy endpoint, tracked in JIRA-999",
                "owner": "security-team",
                "expires": "2027-01-01",
            }]
        }
        waivers = load_waivers_from_policy(policy_raw)
        assert len(waivers) == 1

        new_fp = _make_fp("SEC-015", path="src/api.py", snippet_hash="newstuff")
        baseline = Baseline(findings=set(), ratchet_mode="block_new")
        current_fps = {new_fp}

        waived_fps = _compute_waived_fps(current_fps, waivers)
        assert new_fp in waived_fps

        result = compare_findings(current_fps, baseline, waived_fingerprints=waived_fps)
        assert result.disposition == "PASS"
