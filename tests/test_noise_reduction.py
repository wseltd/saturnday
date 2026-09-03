"""Tests for the three scanner noise-reduction fixes.

Fix 1 — Lockfile exclusion from secrets scanning (review._scan_secrets)
Fix 2 — Test-directory exclusion from token_revocation, oauth_flow,
         security_event_logging TS checks (review_ts)
Fix 3 — Vendor/minified JS exclusion from XSS check (review_ts)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from saturnday.review import _scan_secrets, _LOCKFILE_BASENAMES
from saturnday.review_ts import (
    _is_test_file,
    _is_vendored_or_minified,
    check_token_revocation_ts,
    check_oauth_flow_ts,
    check_security_event_logging_ts,
    check_xss_check_ts,
)

# ---------------------------------------------------------------------------
# Fix 1: Lockfile exclusion from secrets scanning
# ---------------------------------------------------------------------------

# Content with long hex strings that match _SECRET_REGEX and would normally
# produce regex_candidate findings.
_LOCKFILE_TRIGGER_CONTENT = """\
{
  "integrity": "sha512-aabbccdd11223344556677889900aabbccdd11223344556677889900aabbccdd1122334455",
  "resolved": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz",
  "version": "1.0.0"
}
"""

# Same content in a non-lockfile name should still be caught.
_CONFIG_TRIGGER_CONTENT = """\
SECRET_KEY = "aabbccdd11223344556677889900aabbccdd11223344556677889900aabbccdd1122334455"
"""


def _make_temp_repo(files: dict[str, str]) -> Path:
    """Create a temporary directory containing the given files.

    Args:
        files: mapping of relative path to file content.

    Returns:
        Path to the temporary repo root.
    """
    tmp = Path(tempfile.mkdtemp())
    for rel_path, content in files.items():
        dest = tmp / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    return tmp


class TestLockfileConstant:
    """Verify the constant is present and contains the expected entries."""

    def test_constant_exported(self) -> None:
        assert isinstance(_LOCKFILE_BASENAMES, frozenset)

    @pytest.mark.parametrize("name", [
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
    ])
    def test_lockfile_in_constant(self, name: str) -> None:
        assert name in _LOCKFILE_BASENAMES


class TestLockfileExclusionFromSecretsScanning:
    """Lockfiles must not produce findings even with secret-like content."""

    def test_lockfile_excluded_from_secrets_scan(self) -> None:
        """package-lock.json should not produce secrets findings."""
        repo = _make_temp_repo({"package-lock.json": _LOCKFILE_TRIGGER_CONTENT})
        result = _scan_secrets(repo, ["package-lock.json"])
        assert result["status"] == "PASS", (
            f"Expected PASS for package-lock.json but got {result['status']}; "
            f"findings={result['findings']}"
        )
        assert result["findings"] == []

    @pytest.mark.parametrize("lockfile_name", [
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
        "go.sum",
        "npm-shrinkwrap.json",
    ])
    def test_all_lockfiles_excluded(self, lockfile_name: str) -> None:
        """All known lockfile names must be skipped."""
        repo = _make_temp_repo({lockfile_name: _LOCKFILE_TRIGGER_CONTENT})
        result = _scan_secrets(repo, [lockfile_name])
        assert result["findings"] == [], (
            f"{lockfile_name} produced unexpected findings: {result['findings']}"
        )

    def test_lockfile_in_subdirectory_excluded(self) -> None:
        """Lockfile in a subdirectory is excluded by basename check."""
        rel = "packages/core/package-lock.json"
        repo = _make_temp_repo({rel: _LOCKFILE_TRIGGER_CONTENT})
        result = _scan_secrets(repo, [rel])
        assert result["findings"] == [], (
            f"Subdirectory lockfile produced unexpected findings: {result['findings']}"
        )

    def test_real_config_file_still_scanned(self) -> None:
        """config.json with a secret should still be caught."""
        repo = _make_temp_repo({"config.json": _CONFIG_TRIGGER_CONTENT})
        result = _scan_secrets(repo, ["config.json"])
        assert len(result["findings"]) > 0, (
            "Expected findings for config.json with secret-like content, got none"
        )


# ---------------------------------------------------------------------------
# Fix 2: Test-directory exclusion from auth-pattern checks
# ---------------------------------------------------------------------------

# Content that triggers token_revocation (SEC-007): has logout but no invalidation.
_LOGOUT_NO_INVALIDATION = """\
function logout(req, res) {
  // just redirect, no token invalidation
  res.redirect('/');
}
"""

# Content that triggers oauth_flow (SEC-004): has callback but no state validation.
_OAUTH_CALLBACK_NO_STATE = """\
app.get('/callback', (req, res) => {
  const { code } = req.query;
  // missing state validation
  exchangeCode(code);
});
"""

# Content that triggers security_event_logging (SEC-OPS-001): has login but no logging.
_LOGIN_NO_LOGGING = """\
function login(req, res) {
  authenticate(req.body.username, req.body.password);
  res.json({ ok: true });
}
"""


class TestIsTestFile:
    """Unit tests for the _is_test_file path classifier."""

    @pytest.mark.parametrize("path", [
        "test/auth.js",
        "tests/integration/oauth.ts",
        "spec/auth_spec.js",
        "e2e/login.spec.ts",
        "__tests__/logout.test.ts",
        "cypress/integration/auth.js",
        "src/cypress/support/commands.ts",
    ])
    def test_test_paths_are_detected(self, path: str) -> None:
        assert _is_test_file(path) is True, f"Expected {path!r} to be a test file"

    @pytest.mark.parametrize("path", [
        "src/auth/logout.ts",
        "lib/oauth/callback.js",
        "routes/login.ts",
        "controllers/authController.js",
    ])
    def test_app_paths_are_not_test_files(self, path: str) -> None:
        assert _is_test_file(path) is False, f"Expected {path!r} NOT to be a test file"


class TestTokenRevocationSkipsTestFiles:
    """SEC-007 should not fire on test directory files."""

    def test_token_revocation_skips_cypress_spec(self) -> None:
        """Cypress test spec should not trigger token_revocation check."""
        repo = _make_temp_repo(
            {"cypress/integration/logout.spec.js": _LOGOUT_NO_INVALIDATION}
        )
        result = check_token_revocation_ts(
            repo, ["cypress/integration/logout.spec.js"]
        )
        token_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_token_invalidation"
        ]
        assert token_findings == [], (
            f"Expected no findings for cypress spec, got: {token_findings}"
        )

    def test_token_revocation_skips_tests_dir(self) -> None:
        """File in tests/ directory must not trigger token_revocation."""
        repo = _make_temp_repo({"tests/unit/auth.ts": _LOGOUT_NO_INVALIDATION})
        result = check_token_revocation_ts(repo, ["tests/unit/auth.ts"])
        token_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_token_invalidation"
        ]
        assert token_findings == []

    def test_token_revocation_fires_on_app_code(self) -> None:
        """Real app logout handler should still trigger token_revocation."""
        repo = _make_temp_repo(
            {"src/auth/logout.ts": _LOGOUT_NO_INVALIDATION}
        )
        result = check_token_revocation_ts(repo, ["src/auth/logout.ts"])
        token_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_token_invalidation"
        ]
        assert len(token_findings) >= 1, (
            f"Expected missing_token_invalidation for app code, got none. "
            f"All findings: {result['findings']}"
        )


class TestOauthFlowSkipsTestFiles:
    """SEC-004 (missing_oauth_state) should not fire on test directory files."""

    def test_oauth_flow_skips_test_dir(self) -> None:
        """Test file should not trigger oauth_flow check."""
        repo = _make_temp_repo({"test/oauth_test.js": _OAUTH_CALLBACK_NO_STATE})
        result = check_oauth_flow_ts(repo, ["test/oauth_test.js"])
        state_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_oauth_state"
        ]
        assert state_findings == [], (
            f"Expected no findings for test/oauth_test.js, got: {state_findings}"
        )

    def test_oauth_flow_skips_spec_dir(self) -> None:
        """Spec file should not trigger oauth_flow check."""
        repo = _make_temp_repo({"spec/oauth.spec.ts": _OAUTH_CALLBACK_NO_STATE})
        result = check_oauth_flow_ts(repo, ["spec/oauth.spec.ts"])
        state_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_oauth_state"
        ]
        assert state_findings == []

    def test_oauth_flow_fires_on_app_code(self) -> None:
        """Real app OAuth callback should still trigger oauth_flow check."""
        repo = _make_temp_repo({"src/oauth/callback.ts": _OAUTH_CALLBACK_NO_STATE})
        result = check_oauth_flow_ts(repo, ["src/oauth/callback.ts"])
        state_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_oauth_state"
        ]
        assert len(state_findings) >= 1, (
            f"Expected missing_oauth_state for app code, got none. "
            f"All findings: {result['findings']}"
        )


class TestSecurityLoggingSkipsTestFiles:
    """SEC-OPS-001 (missing_security_logging) should not fire on test directory files."""

    def test_security_logging_skips_e2e(self) -> None:
        """e2e test file should not trigger security_event_logging."""
        repo = _make_temp_repo({"e2e/auth_flow.spec.ts": _LOGIN_NO_LOGGING})
        result = check_security_event_logging_ts(repo, ["e2e/auth_flow.spec.ts"])
        logging_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_security_logging"
        ]
        assert logging_findings == [], (
            f"Expected no findings for e2e spec, got: {logging_findings}"
        )

    def test_security_logging_skips_tests_dir(self) -> None:
        """tests/ directory file should not trigger security_event_logging."""
        repo = _make_temp_repo({"tests/auth.test.js": _LOGIN_NO_LOGGING})
        result = check_security_event_logging_ts(repo, ["tests/auth.test.js"])
        logging_findings = [
            f for f in result["findings"] if f.get("kind") == "missing_security_logging"
        ]
        assert logging_findings == []

    def test_security_logging_fires_on_app_code(self) -> None:
        """Real app login handler should still trigger security_event_logging."""
        repo = _make_temp_repo({"src/auth/login.ts": _LOGIN_NO_LOGGING})
        result = check_security_event_logging_ts(repo, ["src/auth/login.ts"])
        # Either missing_security_logging or unstructured_security_logging are both
        # valid signals — either confirms the check fired.
        relevant_findings = [
            f for f in result["findings"]
            if f.get("kind") in ("missing_security_logging", "unstructured_security_logging")
        ]
        assert len(relevant_findings) >= 1, (
            f"Expected security logging finding for app code, got none. "
            f"All findings: {result['findings']}"
        )


# ---------------------------------------------------------------------------
# Fix 3: Vendor/minified JS exclusion from XSS check
# ---------------------------------------------------------------------------

# Content that contains an XSS sink (innerHTML assignment).
_XSS_SINK_CONTENT = """\
function render(data) {
  document.getElementById('output').innerHTML = data.html;
}
"""


class TestIsVendoredOrMinified:
    """Unit tests for the _is_vendored_or_minified path classifier."""

    @pytest.mark.parametrize("path", [
        "vendor/jquery.js",
        "vendors/bootstrap.js",
        "dist/vendor/lodash.js",
        "public/js/lib.min.js",
        "assets/scripts/react.min.mjs",
        "static/bundle.js",
        "static/chunk-abc123.js",
        "build/chunk-0a1b2c.js",
    ])
    def test_vendored_paths_detected(self, path: str) -> None:
        assert _is_vendored_or_minified(path) is True, (
            f"Expected {path!r} to be classified as vendored/minified"
        )

    @pytest.mark.parametrize("path", [
        "src/components/UserProfile.tsx",
        "app/utils/render.js",
        "lib/helpers.ts",
        "frontend/views/dashboard.js",
    ])
    def test_app_paths_not_vendored(self, path: str) -> None:
        assert _is_vendored_or_minified(path) is False, (
            f"Expected {path!r} NOT to be classified as vendored/minified"
        )


class TestXssSkipsVendoredFiles:
    """SEC-013 should not fire on vendored or minified JS files."""

    def test_xss_skips_minified_js(self) -> None:
        """*.min.js should not trigger XSS check."""
        repo = _make_temp_repo({"public/js/vendor.min.js": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["public/js/vendor.min.js"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert xss_findings == [], (
            f"Expected no XSS findings for min.js, got: {xss_findings}"
        )

    def test_xss_skips_min_mjs(self) -> None:
        """*.min.mjs should not trigger XSS check."""
        repo = _make_temp_repo({"dist/esm/react.min.mjs": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["dist/esm/react.min.mjs"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert xss_findings == []

    def test_xss_skips_vendor_dir(self) -> None:
        """vendor/lib.js should not trigger XSS check."""
        repo = _make_temp_repo({"vendor/lib.js": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["vendor/lib.js"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert xss_findings == [], (
            f"Expected no XSS findings for vendor dir, got: {xss_findings}"
        )

    def test_xss_skips_vendors_dir(self) -> None:
        """vendors/ directory should not trigger XSS check."""
        repo = _make_temp_repo({"vendors/charts.js": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["vendors/charts.js"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert xss_findings == []

    def test_xss_skips_bundle_js(self) -> None:
        """bundle.js should not trigger XSS check."""
        repo = _make_temp_repo({"dist/bundle.js": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["dist/bundle.js"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert xss_findings == []

    def test_xss_skips_chunk_js(self) -> None:
        """chunk-*.js should not trigger XSS check."""
        repo = _make_temp_repo({"build/chunk-abc123def456.js": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["build/chunk-abc123def456.js"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert xss_findings == []

    def test_xss_fires_on_app_code(self) -> None:
        """app/component.js with innerHTML should still trigger XSS check."""
        repo = _make_temp_repo({"src/components/UserCard.js": _XSS_SINK_CONTENT})
        result = check_xss_check_ts(repo, ["src/components/UserCard.js"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert len(xss_findings) >= 1, (
            f"Expected dangerous_xss_sink finding for app code, got none. "
            f"All findings: {result['findings']}"
        )

    def test_xss_fires_on_tsx_component(self) -> None:
        """React component with dangerouslySetInnerHTML should still be caught."""
        content = """\
function UserBio({ bio }) {
  return <div dangerouslySetInnerHTML={{ __html: bio }} />;
}
"""
        repo = _make_temp_repo({"src/components/UserBio.tsx": content})
        result = check_xss_check_ts(repo, ["src/components/UserBio.tsx"])
        xss_findings = [
            f for f in result["findings"] if f.get("kind") == "dangerous_xss_sink"
        ]
        assert len(xss_findings) >= 1, (
            f"Expected dangerouslySetInnerHTML finding for React component, got none"
        )
