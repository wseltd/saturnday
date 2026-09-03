"""Structural integration tests for security governance.

Runs the full check suite against a single integrated test app
(tests/fixtures/integration_app.py) that has both vulnerable and
secure patterns. Validates that the governance system catches all
DryRun failure classes end-to-end.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from saturnday.review import run_review

FIXTURES = Path(__file__).parent / "fixtures"
INTEGRATION_APP = FIXTURES / "integration_app.py"


@pytest.fixture
def integration_repo():
    """Set up a temp repo with the integration app."""
    tmpdir = Path(tempfile.mkdtemp())
    shutil.copy2(INTEGRATION_APP, tmpdir / "integration_app.py")
    yield tmpdir, ["integration_app.py"]
    shutil.rmtree(tmpdir)


def _run_checks(repo_path, changed_files):
    """Run review and return tools dict."""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        result = run_review(
            repo_path,
            changed_files,
            tmpdir,
            run_shell_func=lambda *a, **kw: {"exit_code": 0, "stdout": "", "stderr": ""},
            timeout_s=30,
            strict=False,
        )
        return result["tools"]
    finally:
        shutil.rmtree(tmpdir)


class TestRouteInventory:
    """Every sensitive route must have auth. Public routes must not flag."""

    def test_unprotected_routes_detected(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("auth_bypass", {})
        assert result["status"] == "FAIL"
        findings = result["findings"]
        flagged_routes = [f["detail"] for f in findings]
        # Sensitive routes should be flagged
        assert any("/api/users" in d for d in flagged_routes)
        assert any("/api/admin" in d for d in flagged_routes)

    def test_public_routes_not_flagged(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("auth_bypass", {})
        flagged_routes = [f["detail"] for f in result["findings"]]
        assert not any("/health" in d for d in flagged_routes)
        assert not any("/login" in d for d in flagged_routes)


class TestAuthzRegression:
    """Protected endpoints must reject unauthenticated requests."""

    def test_auth_bypass_fires_on_sensitive_routes(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        assert tools["auth_bypass"]["status"] == "FAIL"
        assert len(tools["auth_bypass"]["findings"]) >= 2


class TestIdorNegative:
    """Routes with ID params must have ownership checks."""

    def test_idor_detected_on_user_route(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("idor_check", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "idor_no_ownership" for f in result["findings"])


class TestWebSocketHandshake:
    """Unauthenticated WS connections must be flagged."""

    def test_ws_no_auth_detected(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("websocket_auth", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "ws_no_auth" for f in result["findings"])

    def test_ws_no_origin_detected(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("websocket_auth", {})
        assert any(f["kind"] == "ws_no_origin_check" for f in result["findings"])


class TestTokenInvalidation:
    """Logout and password-reset must invalidate tokens."""

    def test_logout_missing_invalidation(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("token_revocation", {})
        assert result["status"] == "FAIL"
        names = [f["detail"] for f in result["findings"]]
        assert any("logout" in d for d in names)

    def test_reset_missing_invalidation(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("token_revocation", {})
        names = [f["detail"] for f in result["findings"]]
        assert any("reset" in d for d in names)


class TestJwtAlgorithmPinning:
    """JWT decode must reject unpinned algorithms."""

    def test_unpinned_algorithm_detected(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("jwt_verification_policy", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "unpinned_algorithm" for f in result["findings"])


class TestCsrfStateChanging:
    """Cookie-auth POST without CSRF token must be flagged."""

    def test_csrf_missing_on_transfer(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("csrf_state_change", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "csrf_missing" for f in result["findings"])


class TestCookieAttributes:
    """Session cookies must have Secure, HttpOnly, explicit SameSite."""

    def test_samesite_none_no_secure(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("cookie_security_hard", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "samesite_none_no_secure" for f in result["findings"])

    def test_missing_httponly(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("cookie_security_hard", {})
        assert any(f["kind"] == "session_cookie_no_httponly" for f in result["findings"])


class TestRateLimitEnforcement:
    """Rate limiter must be applied, not just imported."""

    def test_rate_limit_not_applied(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("rate_limit_wiring", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "rate_limit_not_applied" for f in result["findings"])


class TestRateLimitBackendQuality:
    """Production rate limiter must use external store."""

    def test_memory_store_detected(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("rate_limit_backend_quality", {})
        assert result["status"] == "FAIL"
        assert any(f["kind"] == "memory_store_rate_limit" for f in result["findings"])


class TestSecurityEventEmission:
    """Auth handlers must emit structured security events."""

    def test_bare_logging_detected(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)
        result = tools.get("security_event_logging", {})
        assert result["status"] == "FAIL"
        kinds = [f["kind"] for f in result["findings"]]
        assert "unstructured_security_logging" in kinds or "missing_security_logging" in kinds


class TestFullCoverageMapping:
    """Every DryRun vulnerability class must be detected by at least one check."""

    def test_all_dryrun_classes_covered(self, integration_repo):
        repo, files = integration_repo
        tools = _run_checks(repo, files)

        # Map each DryRun class to the check(s) that should fire
        coverage = {
            "Insecure JWT handling": ["hardcoded_jwt", "jwt_verification_policy"],
            "Missing brute-force protection": ["rate_limit_wiring"],
            "Token replay exposure": ["token_revocation"],
            "Insecure cookie defaults": ["cookie_security_hard"],
            "WS auth missing": ["websocket_auth"],
            "Broken access control": ["auth_bypass"],
            "OAuth failures": ["oauth_flow_integrity"],
            "XSS": ["xss_check"],
            "Client-trusted logic": ["client_trusted_logic"],
            "Weak randomness": ["weak_randomness"],
            "User enumeration": ["user_enumeration"],
            "Missing token expiry": ["token_expiry"],
            "SQL injection": ["sql_injection"],
            "CSRF": ["csrf_state_change"],
            "Security event blindness": ["security_event_logging"],
        }

        uncovered = []
        for dryrun_class, check_names in coverage.items():
            found = False
            for check_name in check_names:
                result = tools.get(check_name, {})
                if result.get("status") == "FAIL" and len(result.get("findings", [])) > 0:
                    found = True
                    break
            if not found:
                uncovered.append(dryrun_class)

        assert uncovered == [], f"DryRun classes not detected: {uncovered}"
