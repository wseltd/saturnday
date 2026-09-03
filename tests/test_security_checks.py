"""Unit tests for security governance checks (SEC-001 through SEC-018 + SEC-OPS-001)."""

import tempfile
import shutil
from pathlib import Path

import pytest

from saturnday.review import (
    _check_hardcoded_jwt,
    _check_weak_randomness,
    _check_jwt_verification_policy,
    _check_cookie_security_hard,
    _check_cookie_security_soft,
    _check_token_revocation,
    _check_token_expiry,
    _check_csrf_state_change,
    _check_oauth_flow_integrity,
    _check_auth_bypass,
    _check_websocket_auth,
    _check_rate_limit_wiring,
    _check_rate_limit_backend_quality,
    _check_xss,
    _check_sql_injection,
    _check_user_enumeration,
    _check_client_trusted_logic,
    _check_idor,
    _check_security_event_logging,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _setup_repo(fixture_file: str) -> tuple[Path, list[str]]:
    """Copy a fixture file into a temp repo directory, return (repo_path, changed_files)."""
    tmpdir = Path(tempfile.mkdtemp())
    src = FIXTURES / fixture_file
    dest = tmpdir / src.name
    shutil.copy2(src, dest)
    return tmpdir, [src.name]


class TestHardcodedJwt:
    """SEC-001: hardcoded_jwt"""

    def test_detects_hardcoded_secret_assignment(self):
        repo, files = _setup_repo("vulnerable/jwt_hardcoded.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            assert result["status"] == "FAIL"
            assert result["name"] == "hardcoded_jwt"
            assert len(result["findings"]) >= 3  # JWT_SECRET, SECRET_KEY, SIGNING_KEY
            for f in result["findings"]:
                assert f["rule_id"] == "SEC-001"
                assert f["cwe"] == "CWE-798"
        finally:
            shutil.rmtree(repo)

    def test_detects_env_fallback_default(self):
        repo, files = _setup_repo("vulnerable/jwt_hardcoded.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            fallback_findings = [f for f in result["findings"] if f["kind"] == "env_fallback_secret"]
            assert len(fallback_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_detects_jwt_encode_literal(self):
        repo, files = _setup_repo("vulnerable/jwt_hardcoded.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            jwt_findings = [f for f in result["findings"] if f["kind"] == "jwt_literal_secret"]
            assert len(jwt_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_passes_secure_jwt_env(self):
        repo, files = _setup_repo("secure/jwt_env.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0
        finally:
            shutil.rmtree(repo)


class TestWeakRandomness:
    """SEC-006: weak_randomness"""

    def test_detects_random_in_auth_context(self):
        repo, files = _setup_repo("vulnerable/weak_random.py")
        try:
            result = _check_weak_randomness(repo, files)
            assert result["status"] == "FAIL"
            assert result["name"] == "weak_randomness"
            assert len(result["findings"]) >= 2
            for f in result["findings"]:
                assert f["rule_id"] == "SEC-006"
                assert f["cwe"] == "CWE-338"
        finally:
            shutil.rmtree(repo)

    def test_detects_timestamp_seed(self):
        repo, files = _setup_repo("vulnerable/weak_random.py")
        try:
            result = _check_weak_randomness(repo, files)
            seed_findings = [f for f in result["findings"] if f["kind"] == "timestamp_seed"]
            assert len(seed_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_passes_secure_random(self):
        repo, files = _setup_repo("secure/strong_random.py")
        try:
            result = _check_weak_randomness(repo, files)
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0
        finally:
            shutil.rmtree(repo)


class TestJwtVerificationPolicy:
    """SEC-008: jwt_verification_policy"""

    def test_detects_unpinned_algorithm(self):
        repo, files = _setup_repo("vulnerable/jwt_alg_none.py")
        try:
            result = _check_jwt_verification_policy(repo, files)
            assert result["status"] == "FAIL"
            assert result["name"] == "jwt_verification_policy"
            unpinned = [f for f in result["findings"] if f["kind"] == "unpinned_algorithm"]
            assert len(unpinned) >= 1
            assert unpinned[0]["rule_id"] == "SEC-008"
        finally:
            shutil.rmtree(repo)

    def test_detects_alg_none(self):
        repo, files = _setup_repo("vulnerable/jwt_alg_none.py")
        try:
            result = _check_jwt_verification_policy(repo, files)
            none_findings = [f for f in result["findings"] if f["kind"] == "alg_none_allowed"]
            assert len(none_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_passes_pinned_algorithm(self):
        repo, files = _setup_repo("secure/jwt_pinned_alg.py")
        try:
            result = _check_jwt_verification_policy(repo, files)
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0
        finally:
            shutil.rmtree(repo)


class TestCookieSecurityHard:
    """SEC-005: cookie_security_hard"""

    def test_detects_samesite_none_no_secure(self):
        repo, files = _setup_repo("vulnerable/insecure_cookies.py")
        try:
            result = _check_cookie_security_hard(repo, files)
            assert result["status"] == "FAIL"
            samesite_findings = [f for f in result["findings"] if f["kind"] == "samesite_none_no_secure"]
            assert len(samesite_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_detects_session_cookie_no_httponly(self):
        repo, files = _setup_repo("vulnerable/insecure_cookies.py")
        try:
            result = _check_cookie_security_hard(repo, files)
            httponly_findings = [f for f in result["findings"] if f["kind"] == "session_cookie_no_httponly"]
            assert len(httponly_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_passes_secure_cookies(self):
        repo, files = _setup_repo("secure/secure_cookies.py")
        try:
            result = _check_cookie_security_hard(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_passes_host_prefix_cookie(self):
        repo, files = _setup_repo("tricky/host_prefix_cookie.py")
        try:
            result = _check_cookie_security_hard(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)


class TestCookieSecuritySoft:
    """SEC-011: cookie_security_soft"""

    def test_detects_missing_samesite(self):
        repo, files = _setup_repo("vulnerable/insecure_cookies.py")
        try:
            result = _check_cookie_security_soft(repo, files)
            missing = [f for f in result["findings"] if f["kind"] == "missing_samesite"]
            assert len(missing) >= 1
        finally:
            shutil.rmtree(repo)

    def test_detects_broad_domain(self):
        repo, files = _setup_repo("vulnerable/insecure_cookies.py")
        try:
            result = _check_cookie_security_soft(repo, files)
            broad = [f for f in result["findings"] if f["kind"] == "broad_domain"]
            assert len(broad) >= 1
        finally:
            shutil.rmtree(repo)


class TestTokenRevocation:
    """SEC-007: token_revocation"""

    def test_detects_logout_without_invalidation(self):
        repo, files = _setup_repo("vulnerable/token_no_revocation.py")
        try:
            result = _check_token_revocation(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "missing_token_invalidation" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestTokenExpiry:
    """SEC-012: token_expiry"""

    def test_detects_missing_expiry(self):
        repo, files = _setup_repo("vulnerable/token_no_expiry.py")
        try:
            result = _check_token_expiry(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "missing_token_expiry" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestCsrfStateChange:
    """SEC-009: csrf_state_change"""

    def test_detects_csrf_missing(self):
        repo, files = _setup_repo("vulnerable/csrf_no_token.py")
        try:
            result = _check_csrf_state_change(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "csrf_missing" for f in result["findings"])
        finally:
            shutil.rmtree(repo)

    def test_passes_with_csrf_protection(self):
        repo, files = _setup_repo("secure/csrf_double_submit.py")
        try:
            result = _check_csrf_state_change(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_samesite_lax_still_fires(self):
        repo, files = _setup_repo("tricky/samesite_lax_no_csrf.py")
        try:
            result = _check_csrf_state_change(repo, files)
            assert result["status"] == "FAIL"
        finally:
            shutil.rmtree(repo)


class TestOauthFlowIntegrity:
    """SEC-004: oauth_flow_integrity"""

    def test_detects_missing_state(self):
        repo, files = _setup_repo("vulnerable/oauth_no_state.py")
        try:
            result = _check_oauth_flow_integrity(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "missing_oauth_state" for f in result["findings"])
        finally:
            shutil.rmtree(repo)

    def test_detects_redirect_from_input(self):
        repo, files = _setup_repo("vulnerable/oauth_no_state.py")
        try:
            result = _check_oauth_flow_integrity(repo, files)
            redirect_findings = [f for f in result["findings"] if f["kind"] == "redirect_from_input"]
            assert len(redirect_findings) >= 1
        finally:
            shutil.rmtree(repo)

    def test_passes_secure_oauth(self):
        repo, files = _setup_repo("secure/oauth_pkce.py")
        try:
            result = _check_oauth_flow_integrity(repo, files)
            # Should pass or have only low-confidence findings
            hard_findings = [f for f in result["findings"] if f["confidence"] == "high"]
            assert len(hard_findings) == 0
        finally:
            shutil.rmtree(repo)


class TestAuthBypass:
    """SEC-002: auth_bypass"""

    def test_detects_unprotected_route(self):
        repo, files = _setup_repo("vulnerable/no_auth_routes.py")
        try:
            result = _check_auth_bypass(repo, files)
            assert result["status"] == "FAIL"
            route_findings = [f for f in result["findings"] if f["kind"] == "route_no_auth"]
            assert len(route_findings) >= 1
            # /health should NOT be flagged
            flagged_routes = [f["detail"] for f in route_findings]
            assert not any("/health" in d for d in flagged_routes)
        finally:
            shutil.rmtree(repo)

    def test_passes_with_auth_decorator(self):
        repo, files = _setup_repo("secure/auth_routes.py")
        try:
            result = _check_auth_bypass(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_detects_fastapi_depends_auth(self):
        repo, files = _setup_repo("tricky/fastapi_depends_auth.py")
        try:
            result = _check_auth_bypass(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)


class TestWebsocketAuth:
    """SEC-003: websocket_auth"""

    def test_detects_ws_no_auth(self):
        repo, files = _setup_repo("vulnerable/ws_no_auth.py")
        try:
            result = _check_websocket_auth(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "ws_no_auth" for f in result["findings"])
        finally:
            shutil.rmtree(repo)

    def test_passes_ws_with_auth(self):
        repo, files = _setup_repo("secure/ws_full_auth.py")
        try:
            result = _check_websocket_auth(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)


class TestRateLimitWiring:
    """SEC-010: rate_limit_wiring"""

    def test_detects_unconnected_limiter(self):
        repo, files = _setup_repo("vulnerable/rate_limit_memory.py")
        try:
            result = _check_rate_limit_wiring(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "rate_limit_not_applied" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestRateLimitBackendQuality:
    """SEC-018: rate_limit_backend_quality"""

    def test_detects_memory_store(self):
        repo, files = _setup_repo("vulnerable/rate_limit_memory.py")
        try:
            result = _check_rate_limit_backend_quality(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "memory_store_rate_limit" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestXssCheck:
    """SEC-013: xss_check"""

    def test_detects_markup(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            vuln = tmpdir / "xss.py"
            vuln.write_text('html = Markup(f"<b>{user_input}</b>")\n')
            result = _check_xss(tmpdir, ["xss.py"])
            assert result["status"] == "FAIL"
        finally:
            shutil.rmtree(tmpdir)


class TestSqlInjection:
    """SEC-015: sql_injection"""

    def test_detects_fstring_sql(self):
        tmpdir = Path(tempfile.mkdtemp())
        try:
            vuln = tmpdir / "sql.py"
            vuln.write_text('query = f"SELECT * FROM users WHERE id = {user_id}"\n')
            result = _check_sql_injection(tmpdir, ["sql.py"])
            assert result["status"] == "FAIL"
        finally:
            shutil.rmtree(tmpdir)


class TestUserEnumeration:
    """SEC-017: user_enumeration"""

    def test_detects_differentiated_messages(self):
        repo, files = _setup_repo("vulnerable/user_enum.py")
        try:
            result = _check_user_enumeration(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "user_enumeration" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestClientTrustedLogic:
    """SEC-016: client_trusted_logic"""

    def test_detects_client_score(self):
        repo, files = _setup_repo("vulnerable/client_trust.py")
        try:
            result = _check_client_trusted_logic(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "client_trusted_field" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestIdorCheck:
    """SEC-014: idor_check"""

    def test_detects_id_without_ownership(self):
        repo, files = _setup_repo("vulnerable/no_auth_routes.py")
        try:
            result = _check_idor(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "idor_no_ownership" for f in result["findings"])
        finally:
            shutil.rmtree(repo)


class TestSecurityEventLogging:
    """SEC-OPS-001: security_event_logging"""

    def test_detects_bare_logging(self):
        repo, files = _setup_repo("vulnerable/no_security_logging.py")
        try:
            result = _check_security_event_logging(repo, files)
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "unstructured_security_logging" for f in result["findings"])
        finally:
            shutil.rmtree(repo)

    def test_passes_structured_logging(self):
        repo, files = _setup_repo("secure/structured_security_log.py")
        try:
            result = _check_security_event_logging(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)
