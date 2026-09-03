"""Unit tests for security governance TS checks."""

import tempfile
import shutil
from pathlib import Path

import pytest

from saturnday.review_ts import (
    check_hardcoded_jwt_ts,
    check_auth_bypass_ts,
    check_websocket_auth_ts,
    check_oauth_flow_ts,
    check_cookie_security_hard_ts,
    check_weak_randomness_ts,
    check_token_revocation_ts,
    check_jwt_verification_ts,
    check_csrf_state_change_ts,
    check_rate_limit_wiring_ts,
    check_cookie_security_soft_ts,
    check_token_expiry_ts,
    check_xss_check_ts,
    check_idor_check_ts,
    check_sql_injection_ts,
    check_client_trusted_logic_ts,
    check_user_enumeration_ts,
    check_rate_limit_backend_quality_ts,
    check_security_event_logging_ts,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _setup_ts(fixture_file: str) -> tuple[Path, list[str]]:
    """Copy a TS fixture into a temp dir."""
    tmpdir = Path(tempfile.mkdtemp())
    src = FIXTURES / fixture_file
    dest = tmpdir / src.name
    shutil.copy2(src, dest)
    return tmpdir, [src.name]


class TestHardcodedJwtTs:
    def test_detects_hardcoded(self):
        repo, files = _setup_ts("vulnerable/jwt_hardcoded.ts")
        try:
            r = check_hardcoded_jwt_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["rule_id"] == "SEC-001" for f in r["findings"])
        finally:
            shutil.rmtree(repo)

    def test_passes_secure(self):
        repo, files = _setup_ts("secure/jwt_env.ts")
        try:
            r = check_hardcoded_jwt_ts(repo, files)
            assert r["status"] == "PASS"
        finally:
            shutil.rmtree(repo)


class TestAuthBypassTs:
    def test_detects_unprotected(self):
        repo, files = _setup_ts("vulnerable/no_auth_routes.ts")
        try:
            r = check_auth_bypass_ts(repo, files)
            assert r["status"] == "FAIL"
            flagged = [f["detail"] for f in r["findings"]]
            assert any("/api/users" in d for d in flagged)
            assert not any("/health" in d for d in flagged)
        finally:
            shutil.rmtree(repo)


class TestWebsocketAuthTs:
    def test_detects_no_auth(self):
        repo, files = _setup_ts("vulnerable/ws_no_auth.ts")
        try:
            r = check_websocket_auth_ts(repo, files)
            assert r["status"] == "FAIL"
            kinds = [f["kind"] for f in r["findings"]]
            assert "ws_no_auth" in kinds or "ws_no_origin_check" in kinds
        finally:
            shutil.rmtree(repo)


class TestWeakRandomnessTs:
    def test_detects_math_random(self):
        repo, files = _setup_ts("vulnerable/weak_random.ts")
        try:
            r = check_weak_randomness_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "weak_random_in_auth" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestJwtVerificationTs:
    def test_detects_unpinned(self):
        repo, files = _setup_ts("vulnerable/jwt_alg_none.ts")
        try:
            r = check_jwt_verification_ts(repo, files)
            assert r["status"] == "FAIL"
            kinds = [f["kind"] for f in r["findings"]]
            assert "unpinned_algorithm" in kinds
        finally:
            shutil.rmtree(repo)

    def test_detects_alg_none(self):
        repo, files = _setup_ts("vulnerable/jwt_alg_none.ts")
        try:
            r = check_jwt_verification_ts(repo, files)
            assert any(f["kind"] == "alg_none_allowed" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestCsrfStateChangeTs:
    def test_detects_missing_csrf(self):
        repo, files = _setup_ts("vulnerable/csrf_no_token.ts")
        try:
            r = check_csrf_state_change_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "csrf_missing" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestCookieSecurityHardTs:
    def test_detects_insecure(self):
        repo, files = _setup_ts("vulnerable/insecure_cookies.ts")
        try:
            r = check_cookie_security_hard_ts(repo, files)
            assert r["status"] == "FAIL"
            kinds = [f["kind"] for f in r["findings"]]
            assert "samesite_none_no_secure" in kinds or "session_cookie_no_httponly" in kinds
        finally:
            shutil.rmtree(repo)


class TestXssCheckTs:
    def test_detects_dangerous_sinks(self):
        repo, files = _setup_ts("vulnerable/xss_sinks.ts")
        try:
            r = check_xss_check_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "dangerous_xss_sink" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestSqlInjectionTs:
    def test_detects_template_sql(self):
        repo, files = _setup_ts("vulnerable/sql_template.ts")
        try:
            r = check_sql_injection_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "sql_string_building" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestClientTrustedLogicTs:
    def test_detects_client_fields(self):
        repo, files = _setup_ts("vulnerable/client_trust.ts")
        try:
            r = check_client_trusted_logic_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "client_trusted_field" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestUserEnumerationTs:
    def test_detects_differentiated_messages(self):
        repo, files = _setup_ts("vulnerable/user_enum.ts")
        try:
            r = check_user_enumeration_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "user_enumeration" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestTokenRevocationTs:
    def test_detects_missing_invalidation(self):
        repo, files = _setup_ts("vulnerable/token_no_revocation.ts")
        try:
            r = check_token_revocation_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "missing_token_invalidation" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestTokenExpiryTs:
    def test_detects_missing_expiry(self):
        repo, files = _setup_ts("vulnerable/token_no_expiry.ts")
        try:
            r = check_token_expiry_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "missing_token_expiry" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestRateLimitBackendQualityTs:
    def test_detects_memory_store(self):
        repo, files = _setup_ts("vulnerable/rate_limit_memory.ts")
        try:
            r = check_rate_limit_backend_quality_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "memory_store_rate_limit" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestCookieSecuritySoftTs:
    def test_detects_missing_samesite(self):
        repo, files = _setup_ts("vulnerable/insecure_cookies.ts")
        try:
            r = check_cookie_security_soft_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "missing_samesite" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestRateLimitWiringTs:
    def test_detects_unwired_limiter(self):
        repo, files = _setup_ts("vulnerable/rate_limit_unwired.ts")
        try:
            r = check_rate_limit_wiring_ts(repo, files)
            assert r["status"] == "FAIL"
            assert any(f["kind"] == "rate_limit_not_applied" for f in r["findings"])
        finally:
            shutil.rmtree(repo)


class TestSecurityEventLoggingTs:
    def test_detects_bare_console_log(self):
        repo, files = _setup_ts("vulnerable/no_security_logging.ts")
        try:
            r = check_security_event_logging_ts(repo, files)
            assert r["status"] == "FAIL"
            kinds = [f["kind"] for f in r["findings"]]
            assert "unstructured_security_logging" in kinds or "missing_security_logging" in kinds
        finally:
            shutil.rmtree(repo)
