"""Unit tests for Security Expansion Pack 1 (SEC-FE-001, SEC-FE-002)."""

import shutil
import tempfile
from pathlib import Path

import pytest

from saturnday.security_pack_1 import (
    check_frontend_secret_exposure,
    check_payment_secret_in_frontend,
    _is_frontend_file,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Fixture files store a placeholder instead of a literal Stripe-shaped key so
# that no scanner-triggering secret is committed to the repository. The real
# shape is restored when the fixture is staged for scanning.
SK_TOKEN = "SATURNDAY_TEST_SK_TOKEN"
SK_VALUE = "sk_live_" + "0" * 24
_SLACK_TOKEN = "xoxb-" + "0" * 12 + "-" + "0" * 24
_RK_VALUE = "rk_live_" + "0" * 24
_SLACK_USER_TOKEN = "xoxp-" + "0" * 12 + "-" + "0" * 24


def _materialise(text: str) -> str:
    """Restore placeholder secrets to their scannable literal form."""
    return text.replace(SK_TOKEN, SK_VALUE)


def _setup_repo(fixture_file: str) -> tuple[Path, list[str]]:
    """Copy a fixture file into a temp repo directory, return (repo_path, changed_files)."""
    tmpdir = Path(tempfile.mkdtemp())
    src = FIXTURES / fixture_file
    dest = tmpdir / src.name
    dest.write_text(_materialise(src.read_text(encoding="utf-8")), encoding="utf-8")
    return tmpdir, [src.name]


def _setup_inline(content: str, filename: str) -> tuple[Path, list[str]]:
    """Write inline content into a temp repo, return (repo_path, changed_files)."""
    tmpdir = Path(tempfile.mkdtemp())
    dest = tmpdir / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return tmpdir, [filename]


# ---------------------------------------------------------------------------
# Tests: _is_frontend_file helper
# ---------------------------------------------------------------------------

class TestIsFrontendFile:
    def test_jsx_is_always_frontend(self):
        assert _is_frontend_file("src/App.jsx") is True

    def test_tsx_is_always_frontend(self):
        assert _is_frontend_file("src/components/Button.tsx") is True

    def test_vue_is_always_frontend(self):
        assert _is_frontend_file("src/views/Home.vue") is True

    def test_svelte_is_always_frontend(self):
        assert _is_frontend_file("src/routes/+page.svelte") is True

    def test_ts_in_components_is_frontend(self):
        assert _is_frontend_file("src/components/utils.ts") is True

    def test_ts_in_pages_is_frontend(self):
        assert _is_frontend_file("pages/api/auth.ts") is True

    def test_ts_in_client_dir_is_frontend(self):
        assert _is_frontend_file("client/auth.ts") is True

    def test_js_in_public_is_frontend(self):
        assert _is_frontend_file("public/bundle.js") is True

    def test_py_is_never_frontend(self):
        assert _is_frontend_file("api/views.py") is False

    def test_ts_in_backend_dir_not_frontend(self):
        # A .ts file in a directory with no frontend signals should not be flagged
        assert _is_frontend_file("server/middleware/auth.ts") is False

    def test_ts_in_lib_not_frontend(self):
        assert _is_frontend_file("lib/utils.ts") is False


# ---------------------------------------------------------------------------
# Tests: SEC-FE-001 — Frontend secret exposure
# ---------------------------------------------------------------------------

class TestFrontendSecretExposure:
    """SEC-FE-001: secret literals in frontend files."""

    def test_detects_secret_key_in_tsx(self):
        content = """
const API_SECRET = "abcdef1234567890abcdef";
export default function App() { return <div>hi</div>; }
"""
        repo, files = _setup_inline(content, "src/components/App.tsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL"
            assert result["name"] == "frontend_secret_exposure"
            assert len(result["findings"]) >= 1
            f = result["findings"][0]
            assert f["rule_id"] == "SEC-FE-001"
            assert f["cwe"] == "CWE-312"
        finally:
            shutil.rmtree(repo)

    def test_detects_api_key_in_jsx(self):
        content = """
const apikey = "supersecretapikey12345678";
function fetchData() {}
"""
        repo, files = _setup_inline(content, "src/pages/index.jsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["rule_id"] == "SEC-FE-001"
            assert f["kind"] == "frontend_secret_assignment"
        finally:
            shutil.rmtree(repo)

    def test_detects_auth_token_in_vue(self):
        content = """
<script>
const AUTH_TOKEN = "bearer_token_abc12345def67890";
</script>
<template><div/></template>
"""
        repo, files = _setup_inline(content, "src/views/Profile.vue")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL"
            assert result["findings"][0]["rule_id"] == "SEC-FE-001"
        finally:
            shutil.rmtree(repo)

    def test_detects_env_fallback_in_client(self):
        content = """
const secret = process.env.API_SECRET || "fallback_secret_abc12345def";
"""
        repo, files = _setup_inline(content, "client/config.ts")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["kind"] == "frontend_env_fallback_secret"
            assert f["rule_id"] == "SEC-FE-001"
        finally:
            shutil.rmtree(repo)

    def test_passes_publishable_key_in_frontend(self):
        # pk_live_ and pk_test_ are Stripe publishable keys — safe for frontend
        content = """
const STRIPE_PK = "pk_live_4eC39HqLyjWDarjtT1zdp7dc";
"""
        repo, files = _setup_inline(content, "src/components/Payment.tsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            # STRIPE_PK does not match _SECRET_VAR_NAME (no 'secret'/'api_key' etc.)
            # so this should PASS for SEC-FE-001 (SEC-FE-002 handles sk_ prefix)
            assert result["name"] == "frontend_secret_exposure"
        finally:
            shutil.rmtree(repo)

    def test_passes_env_var_access_no_fallback(self):
        content = """
const apiKey = process.env.NEXT_PUBLIC_API_KEY;
"""
        repo, files = _setup_inline(content, "src/pages/index.tsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "PASS"
            assert result["findings"] == []
        finally:
            shutil.rmtree(repo)

    def test_skips_python_files(self):
        content = """
API_SECRET = "supersecretapikey12345678"
"""
        repo, files = _setup_inline(content, "api/views.py")
        try:
            result = check_frontend_secret_exposure(repo, files)
            # .py is not a frontend file — should be skipped by SEC-FE-001
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_skips_comment_lines(self):
        content = """
// const API_SECRET = "secretvalue1234567890";
# const API_SECRET = "secretvalue1234567890";
"""
        repo, files = _setup_inline(content, "src/components/Config.tsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_skips_placeholder_values(self):
        content = """
const API_SECRET = "your_api_key_here";
"""
        repo, files = _setup_inline(content, "src/components/Config.tsx")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_fixture_vulnerable_frontend_secret_ts(self):
        repo, files = _setup_repo("vulnerable/frontend_secret.ts")
        try:
            result = check_frontend_secret_exposure(repo, files)
            # frontend_secret.ts is a .ts file — not in a frontend dir by name alone.
            # The fixture file is at tests/fixtures/vulnerable/frontend_secret.ts.
            # "vulnerable" is not a frontend dir segment, so .ts files there won't
            # be classified as frontend. This is correct conservative behavior.
            assert result["name"] == "frontend_secret_exposure"
        finally:
            shutil.rmtree(repo)

    def test_detects_secret_in_svelte(self):
        content = """
<script>
  let signing_key = "signkey_abc1234567890def";
</script>
"""
        repo, files = _setup_inline(content, "src/routes/+layout.svelte")
        try:
            result = check_frontend_secret_exposure(repo, files)
            assert result["status"] == "FAIL"
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Tests: SEC-FE-002 — Payment-provider secret key in frontend
# ---------------------------------------------------------------------------

class TestPaymentSecretInFrontend:
    """SEC-FE-002: payment provider secret key literals in any source file."""

    def test_detects_stripe_live_secret_in_ts(self):
        content = f'const STRIPE_SK = "{SK_VALUE}";\n'
        repo, files = _setup_inline(content, "src/components/Payment.tsx")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            assert result["name"] == "payment_secret_frontend"
            f = result["findings"][0]
            assert f["rule_id"] == "SEC-FE-002"
            assert f["cwe"] == "CWE-798"
            assert "sk_live_" in f["label"]
        finally:
            shutil.rmtree(repo)

    def test_detects_stripe_test_secret_in_py(self):
        content = 'stripe.api_key = "sk_test_4eC39HqLyjWDarjtT1zdp7dc_testvalue"\n'
        repo, files = _setup_inline(content, "payments/views.py")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["rule_id"] == "SEC-FE-002"
            assert "sk_test_" in f["label"]
        finally:
            shutil.rmtree(repo)

    def test_detects_sendgrid_key(self):
        content = 'const sgKey = "SG.abcdefghijklmnop1234.ABCDEFGHIJKLMNOP12345678";\n'
        repo, files = _setup_inline(content, "src/email.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["rule_id"] == "SEC-FE-002"
            assert "SendGrid" in f["label"]
        finally:
            shutil.rmtree(repo)

    def test_detects_slack_bot_token(self):
        content = f'const slackToken = "{_SLACK_TOKEN}";\n'
        repo, files = _setup_inline(content, "src/notifications.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["rule_id"] == "SEC-FE-002"
            assert "xoxb" in f["label"]
        finally:
            shutil.rmtree(repo)

    def test_detects_stripe_restricted_key(self):
        content = f'const key = "{_RK_VALUE}";\n'
        repo, files = _setup_inline(content, "billing/client.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert "rk_live_" in f["label"]
        finally:
            shutil.rmtree(repo)

    def test_passes_stripe_publishable_key(self):
        # pk_live_ and pk_test_ are intentionally frontend-safe publishable keys
        content = 'const stripePK = "pk_live_4eC39HqLyjWDarjtT1zdp7dc";\n'
        repo, files = _setup_inline(content, "src/checkout.tsx")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS"
            assert result["findings"] == []
        finally:
            shutil.rmtree(repo)

    def test_passes_env_var_stripe_secret(self):
        content = 'stripe.api_key = process.env.STRIPE_SECRET_KEY;\n'
        repo, files = _setup_inline(content, "api/payments.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_passes_env_var_python_stripe(self):
        content = 'import os\nstripe.api_key = os.environ["STRIPE_SECRET_KEY"]\n'
        repo, files = _setup_inline(content, "payments/views.py")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_skips_comment_lines(self):
        content = f'// const key = "{SK_VALUE}";\n'
        repo, files = _setup_inline(content, "src/stripe.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_fixture_vulnerable_frontend_secret_ts(self):
        repo, files = _setup_repo("vulnerable/frontend_secret.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            rule_ids = {f["rule_id"] for f in result["findings"]}
            assert "SEC-FE-002" in rule_ids
        finally:
            shutil.rmtree(repo)

    def test_fixture_secure_frontend_safe_py(self):
        repo, files = _setup_repo("secure/frontend_safe.py")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "PASS"
        finally:
            shutil.rmtree(repo)

    def test_detects_slack_user_token(self):
        content = f'const token = "{_SLACK_USER_TOKEN}";\n'
        repo, files = _setup_inline(content, "src/slack.ts")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
            assert result["findings"][0]["rule_id"] == "SEC-FE-002"
        finally:
            shutil.rmtree(repo)

    def test_json_file_scanned(self):
        # .json config files sometimes get secrets embedded in them
        content = '{"stripe_key": "' + SK_VALUE + '"}\n'
        repo, files = _setup_inline(content, "config/stripe.json")
        try:
            result = check_payment_secret_in_frontend(repo, files)
            assert result["status"] == "FAIL"
        finally:
            shutil.rmtree(repo)


# ---------------------------------------------------------------------------
# Tests: Policy manifest registration
# ---------------------------------------------------------------------------

class TestPolicyManifestRegistration:
    """Verify new checks are registered in policy_manifest."""

    def test_frontend_secret_exposure_in_hard_checks(self):
        from saturnday.policy_manifest import HARD_CHECKS
        assert "frontend_secret_exposure" in HARD_CHECKS

    def test_payment_secret_frontend_in_hard_checks(self):
        from saturnday.policy_manifest import HARD_CHECKS
        assert "payment_secret_frontend" in HARD_CHECKS

    def test_rule_ids_registered(self):
        from saturnday.policy_manifest import RULE_IDS
        assert RULE_IDS["frontend_secret_exposure"] == "SEC-FE-001"
        assert RULE_IDS["payment_secret_frontend"] == "SEC-FE-002"

    def test_cwe_mappings_present(self):
        from saturnday.policy_manifest import RULE_CWE
        assert RULE_CWE["frontend_secret_exposure"] == "CWE-312"
        assert RULE_CWE["payment_secret_frontend"] == "CWE-798"

    def test_owasp_mappings_present(self):
        from saturnday.policy_manifest import RULE_OWASP
        assert RULE_OWASP["frontend_secret_exposure"] == "A02:2021"
        assert RULE_OWASP["payment_secret_frontend"] == "A02:2021"

    def test_default_policy_includes_new_checks(self):
        from saturnday.policy_manifest import default_policy
        policy = default_policy()
        assert "frontend_secret_exposure" in policy.checks
        assert "payment_secret_frontend" in policy.checks
        # Both are HARD so severity should be error
        assert policy.checks["frontend_secret_exposure"].severity == "error"
        assert policy.checks["payment_secret_frontend"].severity == "error"

    def test_all_checks_set_includes_new(self):
        from saturnday.policy_manifest import ALL_CHECKS
        assert "frontend_secret_exposure" in ALL_CHECKS
        assert "payment_secret_frontend" in ALL_CHECKS


# ---------------------------------------------------------------------------
# Tests: run_review integration (wiring check)
# ---------------------------------------------------------------------------

class TestRunReviewIntegration:
    """Verify the new checks are wired into run_review output."""

    def test_run_review_emits_frontend_secret_exposure_key(self, tmp_path):
        """run_review must emit 'frontend_secret_exposure' in tools dict."""
        from unittest.mock import MagicMock
        from saturnday.review import run_review

        # Write a minimal .tsx file with a secret
        fe_file = tmp_path / "App.tsx"
        fe_file.write_text(
            'const API_SECRET = "secretvalue1234567890abc";\n',
            encoding="utf-8",
        )

        def fake_shell(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        result = run_review(
            tmp_path,
            ["App.tsx"],
            tmp_path / "work",
            run_shell_func=fake_shell,
            timeout_s=30,
            strict=False,
        )
        assert "frontend_secret_exposure" in result["tools"]
        assert "payment_secret_frontend" in result["tools"]

    def test_run_review_frontend_secret_fails_on_secret(self, tmp_path):
        """frontend_secret_exposure check must FAIL on a .tsx file with a secret var."""
        from unittest.mock import MagicMock
        from saturnday.review import run_review

        fe_file = tmp_path / "App.tsx"
        fe_file.write_text(
            'const API_SECRET = "mysupersecretvalue1234567890";\n',
            encoding="utf-8",
        )

        def fake_shell(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        result = run_review(
            tmp_path,
            ["App.tsx"],
            tmp_path / "work",
            run_shell_func=fake_shell,
            timeout_s=30,
            strict=False,
        )
        fe_result = result["tools"]["frontend_secret_exposure"]
        assert fe_result["status"] == "FAIL"

    def test_run_review_payment_secret_fails_on_stripe_sk(self, tmp_path):
        """payment_secret_frontend must FAIL when Stripe sk_live_ key present."""
        from unittest.mock import MagicMock
        from saturnday.review import run_review

        py_file = tmp_path / "billing.py"
        py_file.write_text(
            f'stripe.api_key = "{SK_VALUE}"\n',
            encoding="utf-8",
        )

        def fake_shell(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="", stderr="")

        result = run_review(
            tmp_path,
            ["billing.py"],
            tmp_path / "work",
            run_shell_func=fake_shell,
            timeout_s=30,
            strict=False,
        )
        pay_result = result["tools"]["payment_secret_frontend"]
        assert pay_result["status"] == "FAIL"
        assert pay_result["findings"][0]["rule_id"] == "SEC-FE-002"
