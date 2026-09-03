"""Proof tests for Security Expansion Pack 1.

These tests demonstrate that Saturnday's checks actually fire on each
targeted failure class. Each test loads a synthetic fixture and calls
the relevant check function, asserting specific findings are produced.

Coverage map:
  SEC-FE-001  check_frontend_secret_exposure    stripe_secret_in_react.jsx
  SEC-FE-001  check_frontend_secret_exposure    stripe_secret_in_vue.vue
  SEC-FE-001  check_frontend_secret_exposure    payment_admin_client_side.tsx
  SEC-FE-002  check_payment_secret_in_frontend  stripe_secret_in_react.jsx
  SEC-FE-002  check_payment_secret_in_frontend  stripe_secret_in_vue.vue
  SEC-FE-002  check_payment_secret_in_frontend  payment_admin_client_side.tsx
  SEC-001     _check_hardcoded_jwt              jwt_fallback_secret.py
  SEC-002     _check_auth_bypass                unguarded_admin_routes.py
  SEC-003     _check_websocket_auth             unguarded_websocket.py
  SEC-005     _check_cookie_security_hard       insecure_refresh_cookie.py
  SEC-007     _check_token_revocation           no_token_revocation.py
  SEC-009     _check_csrf_state_change          csrf_no_protection.py
  SEC-010     _check_rate_limit_wiring          login_no_rate_limit.py
  SEC-012     _check_token_expiry               jwt_no_expiry.py
  SEC-014     _check_idor                       idor_by_id.py
  SEC-016     _check_client_trusted_logic       client_trusted_price.py

Secure counterpart tests (must PASS):
  stripe_secret_server_only.py   — SEC-FE-002 PASS
  guarded_admin_routes.py        — SEC-002 PASS
  jwt_proper_config.py           — SEC-001, SEC-005, SEC-012 PASS

Demo app tests:
  insecure_saas_api.py           — multiple rules fire
  fixed_saas_api.py              — SEC-001, SEC-FE-002, SEC-002, SEC-016 PASS
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "security_pack_1"
DEMO_APPS = FIXTURES / "demo_apps"


def _repo_for(fixture_path: Path) -> tuple[Path, list[str]]:
    """Stage a single fixture file in a temp directory for scanning.

    Returns (repo_path, [relative_path_string]).
    The fixture is copied to repo_root/fixtures/security_pack_1/<filename>
    so relative paths match the original location context (important for
    frontend file classification which checks directory names).
    """
    tmpdir = Path(tempfile.mkdtemp())
    rel = fixture_path.relative_to(FIXTURES.parent.parent)  # relative to tests/
    dest = tmpdir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        fixture_path.read_text(encoding="utf-8").replace(
            "SATURNDAY_TEST_SK_TOKEN", "sk_live_" + "0" * 24
        ),
        encoding="utf-8",
    )
    return tmpdir, [str(rel)]


def _repo_for_demo(fixture_path: Path) -> tuple[Path, list[str]]:
    """Stage a demo app fixture. Relative path is fixtures/security_pack_1/demo_apps/<name>."""
    tmpdir = Path(tempfile.mkdtemp())
    rel = fixture_path.relative_to(FIXTURES.parent.parent)
    dest = tmpdir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        fixture_path.read_text(encoding="utf-8").replace(
            "SATURNDAY_TEST_SK_TOKEN", "sk_live_" + "0" * 24
        ),
        encoding="utf-8",
    )
    return tmpdir, [str(rel)]


# ---------------------------------------------------------------------------
# Layer 1A: SEC-FE-001 — Frontend secret exposure
# ---------------------------------------------------------------------------

class TestSecFe001FrontendSecretExposure:
    """Proof that SEC-FE-001 fires on each targeted frontend fixture."""

    def test_stripe_secret_in_react_fires_sec_fe_001(self):
        """SEC-FE-001: STRIPE_SECRET const in .jsx fires frontend_secret_exposure.

        The fixture has const STRIPE_SECRET = "sk_live_abc123..." which matches
        _SECRET_VAR_NAME (contains 'secret') and the value matches _SECRET_VALUE_PATTERN.
        """
        from saturnday.security_pack_1 import check_frontend_secret_exposure

        repo, files = _repo_for(FIXTURES / "stripe_secret_in_react.jsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for stripe_secret_in_react.jsx, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-001" in rule_ids, (
                f"SEC-FE-001 not in findings: {result['findings']}"
            )
        finally:
            shutil.rmtree(repo)

    def test_stripe_secret_in_vue_fires_sec_fe_001(self):
        """SEC-FE-001: STRIPE_SECRET assignment in .vue SFC triggers check.

        .vue is always classified as frontend regardless of directory.
        """
        from saturnday.security_pack_1 import check_frontend_secret_exposure

        repo, files = _repo_for(FIXTURES / "stripe_secret_in_vue.vue")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for stripe_secret_in_vue.vue, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-001" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_payment_admin_client_side_fires_sec_fe_001(self):
        """SEC-FE-001: STRIPE_SECRET const in .tsx fires frontend_secret_exposure.

        .tsx is always classified as a frontend file.
        """
        from saturnday.security_pack_1 import check_frontend_secret_exposure

        repo, files = _repo_for(FIXTURES / "payment_admin_client_side.tsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for payment_admin_client_side.tsx, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-001" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_stripe_secret_server_only_passes_sec_fe_001(self):
        """SECURE: stripe_secret_server_only.py must PASS SEC-FE-001.

        .py files are never classified as frontend — _is_frontend_file returns False.
        """
        from saturnday.security_pack_1 import check_frontend_secret_exposure

        repo, files = _repo_for(FIXTURES / "stripe_secret_server_only.py")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for secure server-only .py, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1B: SEC-FE-002 — Payment-provider secret key in frontend
# ---------------------------------------------------------------------------

class TestSecFe002PaymentSecretInFrontend:
    """Proof that SEC-FE-002 fires on each targeted payment-secret fixture."""

    def test_stripe_secret_in_react_fires_sec_fe_002(self):
        """SEC-FE-002: sk_live_ key in stripe_secret_in_react.jsx triggers payment check."""
        from saturnday.security_pack_1 import check_payment_secret_in_frontend

        repo, files = _repo_for(FIXTURES / "stripe_secret_in_react.jsx")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for stripe_secret_in_react.jsx SEC-FE-002, got PASS."
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-002" in rule_ids
            labels = {f["label"] for f in result["findings"]}
            assert any("sk_live_" in lbl for lbl in labels), (
                f"Expected sk_live_ label, got: {labels}"
            )
        finally:
            shutil.rmtree(repo)

    def test_stripe_secret_in_vue_fires_sec_fe_002(self):
        """SEC-FE-002: sk_live_ key in .vue SFC triggers payment secret check."""
        from saturnday.security_pack_1 import check_payment_secret_in_frontend

        repo, files = _repo_for(FIXTURES / "stripe_secret_in_vue.vue")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-002" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_payment_admin_client_side_fires_sec_fe_002(self):
        """SEC-FE-002: sk_live_ key in payment_admin_client_side.tsx triggers check."""
        from saturnday.security_pack_1 import check_payment_secret_in_frontend

        repo, files = _repo_for(FIXTURES / "payment_admin_client_side.tsx")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-002" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_stripe_secret_server_only_passes_sec_fe_002(self):
        """SECURE: stripe_secret_server_only.py must PASS SEC-FE-002.

        os.environ is present on the assignment line, so _SAFE_ENV_CONFIG fires
        and the line is skipped.
        """
        from saturnday.security_pack_1 import check_payment_secret_in_frontend

        repo, files = _repo_for(FIXTURES / "stripe_secret_server_only.py")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for server-only stripe config, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1C: SEC-001 — Hardcoded JWT secret (_check_hardcoded_jwt)
# ---------------------------------------------------------------------------

class TestSec001HardcodedJwt:
    """Proof that SEC-001 fires on jwt_fallback_secret.py."""

    def test_jwt_fallback_secret_fires_sec_001(self):
        """SEC-001: os.environ.get('JWT_SECRET', 'default-secret-change-me') fires.

        _check_hardcoded_jwt looks for os.environ.get with a non-None/non-empty
        string fallback when the variable name contains 'secret'.
        """
        from saturnday.review import _check_hardcoded_jwt

        repo, files = _repo_for(FIXTURES / "jwt_fallback_secret.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for jwt_fallback_secret.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-001" in rule_ids
            kinds = {f["kind"] for f in result["findings"]}
            assert "env_fallback_secret" in kinds, (
                f"Expected env_fallback_secret kind, got: {kinds}"
            )
        finally:
            shutil.rmtree(repo)

    def test_jwt_proper_config_passes_sec_001(self):
        """SECURE: jwt_proper_config.py must PASS SEC-001.

        os.environ['JWT_SECRET'] (KeyError style) has no fallback literal.
        """
        from saturnday.review import _check_hardcoded_jwt

        repo, files = _repo_for(FIXTURES / "jwt_proper_config.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for jwt_proper_config.py, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1D: SEC-002 — Auth bypass (_check_auth_bypass)
# ---------------------------------------------------------------------------

class TestSec002AuthBypass:
    """Proof that SEC-002 fires on unguarded admin routes."""

    def test_unguarded_admin_routes_fires_sec_002(self):
        """SEC-002: /admin/users, /admin/delete, /api/payments without @login_required.

        _check_auth_bypass walks the AST for route decorators and checks each
        decorated function for @login_required / @jwt_required / Depends(auth).
        """
        from saturnday.review import _check_auth_bypass

        repo, files = _repo_for(FIXTURES / "unguarded_admin_routes.py")
        try:
            result = _check_auth_bypass(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for unguarded_admin_routes.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-002" in rule_ids
            # At least /admin/users and /admin/delete should fire
            assert len(result["findings"]) >= 2, (
                f"Expected >=2 findings, got {len(result['findings'])}: {result['findings']}"
            )
        finally:
            shutil.rmtree(repo)

    def test_guarded_admin_routes_passes_sec_002(self):
        """SECURE: guarded_admin_routes.py must PASS SEC-002.

        Every route has @login_required applied.
        """
        from saturnday.review import _check_auth_bypass

        repo, files = _repo_for(FIXTURES / "guarded_admin_routes.py")
        try:
            result = _check_auth_bypass(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for guarded_admin_routes.py, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1E: SEC-003 — WebSocket auth (_check_websocket_auth)
# ---------------------------------------------------------------------------

class TestSec003WebsocketAuth:
    """Proof that SEC-003 fires on unguarded WebSocket handler."""

    def test_unguarded_websocket_fires_sec_003(self):
        """SEC-003: socketio on_connect with no verify_token or authenticate() call.

        _check_websocket_auth detects @sio.on decorator and then looks within
        the handler body for auth patterns. None are present in this fixture.
        """
        from saturnday.review import _check_websocket_auth

        repo, files = _repo_for(FIXTURES / "unguarded_websocket.py")
        try:
            result = _check_websocket_auth(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for unguarded_websocket.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-003" in rule_ids
            kinds = {f["kind"] for f in result["findings"]}
            assert "ws_no_auth" in kinds, f"Expected ws_no_auth, got: {kinds}"
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1F: SEC-005 — Cookie security (_check_cookie_security_hard)
# ---------------------------------------------------------------------------

class TestSec005CookieSecurity:
    """Proof that SEC-005 fires on insecure refresh token cookie."""

    def test_insecure_refresh_cookie_fires_sec_005(self):
        """SEC-005: set_cookie('refresh_token', ...) without httponly fires.

        _check_cookie_security_hard looks for set_cookie calls on tokens/sessions
        and verifies httponly=True is present in the context window.
        """
        from saturnday.review import _check_cookie_security_hard

        repo, files = _repo_for(FIXTURES / "insecure_refresh_cookie.py")
        try:
            result = _check_cookie_security_hard(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for insecure_refresh_cookie.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-005" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_jwt_proper_config_passes_sec_005(self):
        """SECURE: jwt_proper_config.py must PASS SEC-005.

        set_cookie has httponly=True, secure=True, samesite='Strict'.
        """
        from saturnday.review import _check_cookie_security_hard

        repo, files = _repo_for(FIXTURES / "jwt_proper_config.py")
        try:
            result = _check_cookie_security_hard(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for jwt_proper_config.py SEC-005, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1G: SEC-007 — Token revocation (_check_token_revocation)
# ---------------------------------------------------------------------------

class TestSec007TokenRevocation:
    """Proof that SEC-007 fires on a logout handler that skips invalidation."""

    def test_no_token_revocation_fires_sec_007(self):
        """SEC-007: logout() that only calls jwt.decode without blocklisting.

        _check_token_revocation detects logout/sign_out function names and checks
        the body for blacklist/blocklist/revoke/invalidate/session-delete patterns.
        None are present in this fixture.
        """
        from saturnday.review import _check_token_revocation

        repo, files = _repo_for(FIXTURES / "no_token_revocation.py")
        try:
            result = _check_token_revocation(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for no_token_revocation.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-007" in rule_ids
            kinds = {f["kind"] for f in result["findings"]}
            assert "missing_token_invalidation" in kinds
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1H: SEC-009 — CSRF (_check_csrf_state_change)
# ---------------------------------------------------------------------------

class TestSec009Csrf:
    """Proof that SEC-009 fires on POST routes without CSRF protection."""

    def test_csrf_no_protection_fires_sec_009(self):
        """SEC-009: @app.post routes with session usage but no CSRFProtect/csrf_token.

        _check_csrf_state_change requires: cookie-auth indicator (session) present
        in the file AND state-changing route AND no CSRF protection token/class.
        """
        from saturnday.review import _check_csrf_state_change

        repo, files = _repo_for(FIXTURES / "csrf_no_protection.py")
        try:
            result = _check_csrf_state_change(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for csrf_no_protection.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-009" in rule_ids
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1I: SEC-010 — Rate limit wiring (_check_rate_limit_wiring)
# ---------------------------------------------------------------------------

class TestSec010RateLimitWiring:
    """Proof that SEC-010 fires when rate limiter is imported but not applied."""

    def test_login_no_rate_limit_fires_sec_010(self):
        """SEC-010: flask_limiter imported, Limiter() created, but no @limiter.limit().

        _check_rate_limit_wiring: if _limiter_import matches and _limiter_applied
        does not match anywhere in the file, finding is emitted.
        """
        from saturnday.review import _check_rate_limit_wiring

        repo, files = _repo_for(FIXTURES / "login_no_rate_limit.py")
        try:
            result = _check_rate_limit_wiring(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for login_no_rate_limit.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-010" in rule_ids
            kinds = {f["kind"] for f in result["findings"]}
            assert "rate_limit_not_applied" in kinds
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1J: SEC-012 — Token expiry (_check_token_expiry)
# ---------------------------------------------------------------------------

class TestSec012TokenExpiry:
    """Proof that SEC-012 fires when jwt.encode is called without exp."""

    def test_jwt_no_expiry_fires_sec_012(self):
        """SEC-012: jwt.encode(payload, secret) with no exp in context window.

        _check_token_expiry scans lines with jwt.encode/create_access_token
        and checks a 4-line context for 'exp', 'expires', 'expiresIn'.
        This fixture has none of those.
        """
        from saturnday.review import _check_token_expiry

        repo, files = _repo_for(FIXTURES / "jwt_no_expiry.py")
        try:
            result = _check_token_expiry(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for jwt_no_expiry.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-012" in rule_ids
            kinds = {f["kind"] for f in result["findings"]}
            assert "missing_token_expiry" in kinds
        finally:
            shutil.rmtree(repo)

    def test_jwt_proper_config_passes_sec_012(self):
        """SECURE: jwt_proper_config.py must PASS SEC-012.

        'exp' claim is explicitly set in the payload dict.
        """
        from saturnday.review import _check_token_expiry

        repo, files = _repo_for(FIXTURES / "jwt_proper_config.py")
        try:
            result = _check_token_expiry(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for jwt_proper_config.py SEC-012, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1K: SEC-014 — IDOR (_check_idor)
# ---------------------------------------------------------------------------

class TestSec014Idor:
    """Proof that SEC-014 fires on route handlers with *_id params and no ownership check."""

    def test_idor_by_id_fires_sec_014(self):
        """SEC-014: get_user(user_id) and get_order(order_id) with no current_user check.

        _check_idor walks the AST for route-decorated functions, extracts *_id
        argument names, and checks the body for current_user / request.user.
        """
        from saturnday.review import _check_idor

        repo, files = _repo_for(FIXTURES / "idor_by_id.py")
        try:
            result = _check_idor(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for idor_by_id.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-014" in rule_ids
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1L: SEC-016 — Client-trusted logic (_check_client_trusted_logic)
# ---------------------------------------------------------------------------

class TestSec016ClientTrustedLogic:
    """Proof that SEC-016 fires when price/amount is read directly from request body."""

    def test_client_trusted_price_fires_sec_016(self):
        """SEC-016: request.json.get('price') and request.json.get('amount') fire check.

        _check_client_trusted_logic scans for request.json/form.get on authority
        fields: price, amount, role, admin, score, balance, etc.
        """
        from saturnday.review import _check_client_trusted_logic

        repo, files = _repo_for(FIXTURES / "client_trusted_price.py")
        try:
            result = _check_client_trusted_logic(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for client_trusted_price.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-016" in rule_ids
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 1M: SEC-004 — OAuth flow integrity (_check_oauth_flow_integrity)
# ---------------------------------------------------------------------------

class TestSec004OauthFlowIntegrity:
    """Proof that SEC-004 fires on OAuth callback with no state validation."""

    def test_oauth_no_state_fires_sec_004(self):
        """SEC-004: oauth_callback() reads code but never validates state.

        _check_oauth_flow_integrity looks for oauth.*callback in source,
        then checks for state validation / generation patterns. None present.
        """
        from saturnday.review import _check_oauth_flow_integrity

        repo, files = _repo_for(FIXTURES / "oauth_no_state.py")
        try:
            result = _check_oauth_flow_integrity(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for oauth_no_state.py, got PASS. "
                f"findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-004" in rule_ids
            kinds = {f["kind"] for f in result["findings"]}
            assert "missing_oauth_state" in kinds, (
                f"Expected missing_oauth_state kind, got: {kinds}"
            )
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 2: Demo app — insecure_saas_api.py (all failures)
# ---------------------------------------------------------------------------

class TestInsecureSaasApi:
    """Proof that multiple rules fire on the combined insecure demo app."""

    def test_insecure_saas_api_fires_sec_001(self):
        """SEC-001: JWT_SECRET = 'hardcoded...' in demo app triggers hardcoded_jwt."""
        from saturnday.review import _check_hardcoded_jwt

        repo, files = _repo_for_demo(DEMO_APPS / "insecure_saas_api.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for insecure_saas_api.py SEC-001. findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-001" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_insecure_saas_api_fires_sec_fe_002(self):
        """SEC-FE-002: sk_live_ in PAYMENT_CONFIG dict triggers payment_secret check."""
        from saturnday.security_pack_1 import check_payment_secret_in_frontend

        repo, files = _repo_for_demo(DEMO_APPS / "insecure_saas_api.py")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for insecure_saas_api.py SEC-FE-002. findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-002" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_insecure_saas_api_fires_sec_002(self):
        """SEC-002: /admin/users route has no auth decorator in demo app."""
        from saturnday.review import _check_auth_bypass

        repo, files = _repo_for_demo(DEMO_APPS / "insecure_saas_api.py")
        try:
            result = _check_auth_bypass(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for insecure_saas_api.py SEC-002. findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-002" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_insecure_saas_api_fires_sec_016(self):
        """SEC-016: request.json.get('price') and .get('amount') in checkout."""
        from saturnday.review import _check_client_trusted_logic

        repo, files = _repo_for_demo(DEMO_APPS / "insecure_saas_api.py")
        try:
            result = _check_client_trusted_logic(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for insecure_saas_api.py SEC-016. findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-016" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_insecure_saas_api_fires_sec_010(self):
        """SEC-010: flask_limiter imported + Limiter() created, no @limiter.limit()."""
        from saturnday.review import _check_rate_limit_wiring

        repo, files = _repo_for_demo(DEMO_APPS / "insecure_saas_api.py")
        try:
            result = _check_rate_limit_wiring(repo, files)
            assert result["status"] == "FAIL", (
                f"Expected FAIL for insecure_saas_api.py SEC-010. findings={result['findings']}"
            )
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-010" in rule_ids
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Layer 3: Demo app — fixed_saas_api.py (must pass key rules)
# ---------------------------------------------------------------------------

class TestFixedSaasApi:
    """Proof that the fixed demo app passes the key rules."""

    def test_fixed_saas_api_passes_sec_001(self):
        """SECURE: JWT_SECRET from os.environ['JWT_SECRET'] — no literal fallback."""
        from saturnday.review import _check_hardcoded_jwt

        repo, files = _repo_for_demo(DEMO_APPS / "fixed_saas_api.py")
        try:
            result = _check_hardcoded_jwt(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for fixed_saas_api.py SEC-001, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)

    def test_fixed_saas_api_passes_sec_fe_002(self):
        """SECURE: stripe key from os.environ — _SAFE_ENV_CONFIG guard fires."""
        from saturnday.security_pack_1 import check_payment_secret_in_frontend

        repo, files = _repo_for_demo(DEMO_APPS / "fixed_saas_api.py")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for fixed_saas_api.py SEC-FE-002, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)

    def test_fixed_saas_api_passes_sec_016(self):
        """SECURE: price looked up from PRODUCT_PRICES dict, not request body."""
        from saturnday.review import _check_client_trusted_logic

        repo, files = _repo_for_demo(DEMO_APPS / "fixed_saas_api.py")
        try:
            result = _check_client_trusted_logic(repo, files)
            assert result["status"] == "PASS", (
                f"Expected PASS for fixed_saas_api.py SEC-016, got FAIL. "
                f"findings={result['findings']}"
            )
        finally:
            shutil.rmtree(repo)
