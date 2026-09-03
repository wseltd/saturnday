"""Unit tests for waiver and suppression system."""

from datetime import date, timedelta

import pytest

from saturnday.waivers import (
    Waiver,
    InlineSuppression,
    parse_inline_suppressions,
    load_waivers_from_policy,
    check_waiver,
    validate_waivers,
    MAX_WAIVER_DAYS,
    MAX_RENEWALS,
)


class TestParseInlineSuppressions:
    def test_parses_full_suppression(self):
        content = '# saturnday:ignore SEC-001 reason="test fixture" expires=2026-09-01 owner=onur\nx = 1'
        results = parse_inline_suppressions("test.py", content)
        assert len(results) == 1
        s = results[0]
        assert s.rule_id == "SEC-001"
        assert s.reason == "test fixture"
        assert s.expires == date(2026, 9, 1)
        assert s.owner == "onur"
        assert s.line == 1

    def test_parses_minimal_suppression(self):
        content = "x = 1  # saturnday:ignore SEC-002\n"
        results = parse_inline_suppressions("test.py", content)
        assert len(results) == 1
        assert results[0].rule_id == "SEC-002"
        assert results[0].reason is None

    def test_no_match_on_regular_comments(self):
        content = "# This is a normal comment\nx = 1\n"
        results = parse_inline_suppressions("test.py", content)
        assert len(results) == 0


class TestLoadWaiversFromPolicy:
    def test_loads_valid_waiver(self):
        raw = {"waivers": [{
            "rule_id": "SEC-002",
            "path": "src/health.py",
            "reason": "Public endpoint",
            "owner": "onur",
            "expires": "2026-09-01",
            "approved_by": "security-review",
        }]}
        waivers = load_waivers_from_policy(raw)
        assert len(waivers) == 1
        assert waivers[0].rule_id == "SEC-002"
        assert waivers[0].approved_by == "security-review"

    def test_rejects_wildcard_path(self):
        raw = {"waivers": [{
            "rule_id": "SEC-001",
            "path": "**",
            "reason": "Lazy",
            "owner": "nobody",
            "expires": "2026-09-01",
        }]}
        waivers = load_waivers_from_policy(raw)
        assert len(waivers) == 0  # Wildcard rejected

    def test_skips_missing_expiry(self):
        raw = {"waivers": [{
            "rule_id": "SEC-001",
            "path": "src/foo.py",
            "reason": "Missing expiry",
            "owner": "onur",
        }]}
        waivers = load_waivers_from_policy(raw)
        assert len(waivers) == 0  # No valid expiry


class TestCheckWaiver:
    def test_suppresses_with_valid_waiver(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-002", path="src/health.py",
            reason="Public endpoint", owner="onur",
            expires=date(2026, 9, 1),
        )]
        result = check_waiver("SEC-002", "src/health.py", 10, waivers, [], today=today)
        assert result.suppressed is True

    def test_expired_waiver_not_suppressed(self):
        today = date(2026, 10, 1)
        waivers = [Waiver(
            rule_id="SEC-002", path="src/health.py",
            reason="Public endpoint", owner="onur",
            expires=date(2026, 9, 1),
        )]
        result = check_waiver("SEC-002", "src/health.py", 10, waivers, [], today=today)
        assert result.suppressed is False
        assert any("expired" in e for e in result.errors)

    def test_hard_finding_inline_needs_policy_approval(self):
        today = date(2026, 6, 1)
        inline = [InlineSuppression(
            rule_id="SEC-001", file="src/auth.py", line=5,
            reason="test", expires=date(2026, 9, 1), owner="onur",
        )]
        # No policy waiver with approved_by
        result = check_waiver("SEC-001", "src/auth.py", 5, [], inline, today=today)
        assert result.suppressed is False
        assert any("approved_by" in e for e in result.errors)

    def test_hard_finding_inline_with_policy_approval_works(self):
        today = date(2026, 6, 1)
        inline = [InlineSuppression(
            rule_id="SEC-001", file="src/auth.py", line=5,
            reason="test fixture", expires=date(2026, 9, 1), owner="onur",
        )]
        waivers = [Waiver(
            rule_id="SEC-001", path="src/auth.py",
            reason="test fixture", owner="onur",
            expires=date(2026, 9, 1), approved_by="security-lead",
        )]
        result = check_waiver("SEC-001", "src/auth.py", 5, waivers, inline, today=today)
        assert result.suppressed is True

    def test_renewal_cap_exceeded(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-011", path="src/cookies.py",
            reason="Legacy", owner="onur",
            expires=date(2026, 9, 1), renewal_count=4,
        )]
        result = check_waiver("SEC-011", "src/cookies.py", 10, waivers, [], today=today)
        assert result.suppressed is False
        assert any("renewals" in e for e in result.errors)

    def test_wrong_rule_id_not_suppressed(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-002", path="src/health.py",
            reason="Public", owner="onur",
            expires=date(2026, 9, 1),
        )]
        result = check_waiver("SEC-001", "src/health.py", 10, waivers, [], today=today)
        assert result.suppressed is False


class TestValidateWaivers:
    def test_valid_waiver_no_errors(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-011", path="src/foo.py",
            reason="Legacy code", owner="onur",
            expires=date(2026, 9, 1),
        )]
        errors = validate_waivers(waivers, today=today)
        assert errors == []

    def test_wildcard_rejected(self):
        waivers = [Waiver(
            rule_id="SEC-001", path="**",
            reason="Lazy", owner="onur",
            expires=date(2026, 9, 1),
        )]
        errors = validate_waivers(waivers)
        assert any("Wildcard" in e for e in errors)

    def test_missing_owner(self):
        waivers = [Waiver(
            rule_id="SEC-001", path="src/foo.py",
            reason="Reason", owner="",
            expires=date(2026, 9, 1),
        )]
        errors = validate_waivers(waivers)
        assert any("owner" in e for e in errors)

    def test_missing_reason(self):
        waivers = [Waiver(
            rule_id="SEC-001", path="src/foo.py",
            reason="", owner="onur",
            expires=date(2026, 9, 1),
        )]
        errors = validate_waivers(waivers)
        assert any("reason" in e for e in errors)

    def test_expiry_too_far(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-011", path="src/foo.py",
            reason="Reason", owner="onur",
            expires=today + timedelta(days=MAX_WAIVER_DAYS + 30),
        )]
        errors = validate_waivers(waivers, today=today)
        assert any("too far" in e for e in errors)

    def test_hard_rule_needs_approved_by(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-001", path="src/auth.py",
            reason="Reason", owner="onur",
            expires=date(2026, 9, 1),
            # No approved_by for HARD rule
        )]
        errors = validate_waivers(waivers, today=today)
        assert any("approved_by" in e for e in errors)

    def test_renewal_cap_exceeded(self):
        today = date(2026, 6, 1)
        waivers = [Waiver(
            rule_id="SEC-011", path="src/foo.py",
            reason="Reason", owner="onur",
            expires=date(2026, 9, 1),
            renewal_count=MAX_RENEWALS + 1,
        )]
        errors = validate_waivers(waivers, today=today)
        assert any("renewals" in e for e in errors)
