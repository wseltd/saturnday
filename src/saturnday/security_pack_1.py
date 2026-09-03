"""Security Expansion Pack 1 — SEC-FE-001 through SEC-FE-002.

Implements frontend-specific secret exposure checks that complement the
existing SEC-001 through SEC-OPS-001 checks in review.py.

Rule IDs introduced here:
    SEC-FE-001  Frontend secret exposure (generic secrets in frontend files)
    SEC-FE-002  Payment-provider secret key in frontend code

These checks are intentionally narrow: they target the specific class of
failure where a secret key is placed in a file that will be bundled into a
browser-delivered asset or is otherwise a frontend module.

Existing review.py checks (SEC-001 through SEC-OPS-001) already cover:
- SEC-AUTH-001 (missing auth on sensitive endpoint) → auth_bypass / SEC-002
- SEC-AUTH-002 (missing WebSocket auth) → websocket_auth / SEC-003
- SEC-JWT-001..004 (JWT hardening) → hardcoded_jwt / SEC-001, token_expiry / SEC-012,
  cookie_security_* / SEC-005/011, token_revocation / SEC-007
- SEC-OAUTH-001 (OAuth state) → oauth_flow_integrity / SEC-004
- SEC-CSRF-001 (CSRF) → csrf_state_change / SEC-009
- SEC-RATE-001 (rate limiting) → rate_limit_wiring / SEC-010
- SEC-IDOR-001 (IDOR) → idor_check / SEC-014
- SEC-TRUST-001 (client-trusted values) → client_trusted_logic / SEC-016

The new checks here extend coverage to frontend file patterns and
payment-provider-specific key formats not covered by the generic secret
regex in the existing checks.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontend file classification
# ---------------------------------------------------------------------------

# Files that are frontend/browser-delivered or bundled into client assets.
# Python backend files are excluded — secrets there are caught by SEC-001.
_FRONTEND_EXTENSIONS: frozenset[str] = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".svelte",
})

# Directory name segments that strongly indicate a frontend context even
# for .ts/.js files that might otherwise be server-side.
_FRONTEND_DIRS: tuple[str, ...] = (
    "components", "pages", "views", "ui", "client", "frontend",
    "public", "static", "assets", "web", "app", "src/app",
    "src/pages", "src/components", "src/views", "src/client",
)

# Patterns for env var access that indicate safe server-side usage.
# process.env.* in Next.js API routes is server-side; NEXT_PUBLIC_* is
# client-side but only appropriate for publishable keys.
_ENV_ACCESS_PATTERN = re.compile(
    r"process\.env\.[A-Z_]+",
)

# Config object patterns that indicate the value is read from env, not hardcoded.
_SAFE_ENV_CONFIG = re.compile(
    r"""(process\.env\.|os\.environ|getenv\s*\(|config\[|settings\.)""",
    re.IGNORECASE,
)


def _is_frontend_file(rel_path: str) -> bool:
    """Return True if the file is likely a frontend/browser-delivered module."""
    p = Path(rel_path)
    ext = p.suffix.lower()
    if ext not in _FRONTEND_EXTENSIONS:
        return False
    # .py files are never frontend
    if ext == ".py":
        return False
    # .ts/.js files only count as frontend if in a frontend directory
    # OR if the file is .jsx/.tsx/.vue/.svelte (always frontend)
    if ext in {".jsx", ".tsx", ".vue", ".svelte"}:
        return True
    # For .ts/.js/.mjs/.cjs, use directory heuristic
    norm = rel_path.replace("\\", "/").lower()
    for segment in _FRONTEND_DIRS:
        if f"/{segment}/" in f"/{norm}" or norm.startswith(f"{segment}/"):
            return True
    return False


# ---------------------------------------------------------------------------
# SEC-FE-001: Generic frontend secret exposure
# ---------------------------------------------------------------------------

# Variable name patterns that strongly suggest a secret/credential
_SECRET_VAR_NAME = re.compile(
    r"\b\w*(secret|api_key|apikey|auth_token|access_token|private_key|"
    r"signing_key|admin_token|service_key|server_key|backend_key|"
    r"internal_token)\w*\b",
    re.IGNORECASE,
)

# Common secret string shape heuristics (high-entropy-ish patterns common
# in API keys and tokens — at least 20 chars, mix of alpha+digit or underscores).
# We deliberately avoid matching UUIDs (4-section hex), typical placeholder
# strings, and short test values.
_SECRET_VALUE_PATTERN = re.compile(
    r"""(?:
        # Generic long alphanumeric key (≥20 chars)
        [A-Za-z0-9_\-]{20,}
    )""",
    re.VERBOSE,
)

# Assignment patterns in JS/TS
_JS_ASSIGNMENT = re.compile(
    r"""(?:const|let|var)\s+(\w+)\s*=\s*['"]([^'"]{8,})['"]""",
)
_JS_EXPORT_ASSIGNMENT = re.compile(
    r"""export\s+(?:const|let|var)\s+(\w+)\s*=\s*['"]([^'"]{8,})['"]""",
)
# Object key patterns: { SECRET_KEY: "literal" }
_JS_OBJECT_KEY = re.compile(
    r"""['"]?(\w+)['"]?\s*:\s*['"]([^'"]{8,})['"]""",
)
# env config object literal patterns for process.env fallback with literal
_JS_ENV_FALLBACK = re.compile(
    r"""process\.env\.\w+\s*\|\|\s*['"]([^'"]{8,})['"]""",
)

# Placeholder/test values to skip (avoids false positives on example code)
_PLACEHOLDER_VALUES = {
    "your_api_key_here", "your-secret-here", "replace_me", "changeme",
    "placeholder", "example_key", "test_secret", "dummy_key",
    "your_stripe_secret_key", "xxxxxxxxxxxxxxxxxxxx",
    "insert_your_key_here", "my_secret_key", "my_api_key",
}


def _looks_like_secret_value(value: str) -> bool:
    """Return True if the string value looks like a real secret, not a placeholder."""
    if not value or len(value) < 10:
        return False
    lower = value.lower()
    if lower in _PLACEHOLDER_VALUES:
        return False
    # Skip obvious environment variable references
    if lower.startswith("process.env") or lower.startswith("os.environ"):
        return False
    # Skip template literal markers
    if "${" in value or "#{" in value:
        return False
    # Skip values that look like test fixture boilerplate
    if lower.startswith(("test_", "fake_", "mock_", "stub_")):
        return False
    return bool(_SECRET_VALUE_PATTERN.fullmatch(value))


def check_frontend_secret_exposure(
    repo_path: Path, changed_files: list[str]
) -> dict:
    """SEC-FE-001: Detect likely secret keys or tokens exposed in frontend code.

    Scans frontend/browser-delivered files (.jsx, .tsx, .vue, .svelte, and
    .js/.ts files in frontend directories) for hardcoded secret variable
    assignments and env-fallback patterns with literal values.

    This is additive to SEC-001 (hardcoded_jwt) which covers Python files.
    """
    findings: list[dict] = []

    for rel_path in sorted(changed_files):
        if not _is_frontend_file(rel_path):
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        lines = source.splitlines()

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            # Skip comment lines
            if stripped.startswith("//") or stripped.startswith("#"):
                continue
            if stripped.startswith("*") or stripped.startswith("/*"):
                continue

            # Pattern 1: const/let/var assignment with secret name
            for pattern in (_JS_ASSIGNMENT, _JS_EXPORT_ASSIGNMENT):
                for m in pattern.finditer(line):
                    var_name = m.group(1)
                    value = m.group(2)
                    if _SECRET_VAR_NAME.search(var_name) and _looks_like_secret_value(value):
                        findings.append({
                            "rule_id": "SEC-FE-001",
                            "file": rel_path,
                            "line": lineno,
                            "kind": "frontend_secret_assignment",
                            "detail": (
                                f"Variable '{var_name}' appears to contain a hardcoded secret "
                                f"in a frontend/browser-delivered file."
                            ),
                            "remediation": (
                                "Move secrets to server-side environment variables. "
                                "Only NEXT_PUBLIC_* or equivalent publishable keys belong in frontend code."
                            ),
                            "confidence": "high",
                            "cwe": "CWE-312",
                            "owasp": "A02:2021",
                        })

            # Pattern 2: object key with secret name
            for m in _JS_OBJECT_KEY.finditer(line):
                key_name = m.group(1)
                value = m.group(2)
                if _SECRET_VAR_NAME.search(key_name) and _looks_like_secret_value(value):
                    # Skip if the line also has process.env (safe pattern)
                    if not _SAFE_ENV_CONFIG.search(line):
                        findings.append({
                            "rule_id": "SEC-FE-001",
                            "file": rel_path,
                            "line": lineno,
                            "kind": "frontend_secret_object_key",
                            "detail": (
                                f"Object key '{key_name}' has a hardcoded secret literal "
                                f"in a frontend/browser-delivered file."
                            ),
                            "remediation": (
                                "Read sensitive config from server-side env vars. "
                                "Do not embed secret literals in client-side config objects."
                            ),
                            "confidence": "medium",
                            "cwe": "CWE-312",
                            "owasp": "A02:2021",
                        })

            # Pattern 3: process.env.FOO || "fallback_secret"
            for m in _JS_ENV_FALLBACK.finditer(line):
                fallback = m.group(1)
                if _looks_like_secret_value(fallback):
                    findings.append({
                        "rule_id": "SEC-FE-001",
                        "file": rel_path,
                        "line": lineno,
                        "kind": "frontend_env_fallback_secret",
                        "detail": (
                            "process.env lookup has a hardcoded literal fallback secret "
                            "in frontend code. The fallback will be bundled into client assets."
                        ),
                        "remediation": (
                            "Remove the fallback literal. If the env var is missing at build "
                            "time, the build should fail rather than embed a fallback secret."
                        ),
                        "confidence": "high",
                        "cwe": "CWE-312",
                        "owasp": "A02:2021",
                    })

    return {
        "name": "frontend_secret_exposure",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


# ---------------------------------------------------------------------------
# SEC-FE-002: Payment-provider secret key in frontend code
# ---------------------------------------------------------------------------

# Payment provider secret key patterns.
# These prefixes are published by the providers as indicators of secret keys.
# sk_live_ / sk_test_ = Stripe secret keys (sk_live_ is production, must never be in frontend)
# rk_live_ = Stripe restricted keys
# AC[a-z0-9]{32} = Twilio account SID prefix pattern (SID not quite secret but included)
# SG. = SendGrid API key prefix
# xoxb- / xoxp- = Slack bot/user tokens
# EAAA = Facebook/Meta access token prefix (partial)
_PAYMENT_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "Stripe secret key (sk_live_*)",
        re.compile(r"\bsk_live_[A-Za-z0-9_]{20,}\b"),
        "Stripe sk_live_ keys are secret and must never appear in frontend code or browser-delivered assets.",
    ),
    (
        "Stripe secret key (sk_test_*)",
        re.compile(r"\bsk_test_[A-Za-z0-9_]{20,}\b"),
        "Stripe sk_test_ keys are secret. Even test keys should not be in frontend code.",
    ),
    (
        "Stripe restricted key (rk_live_*)",
        re.compile(r"\brk_live_[A-Za-z0-9_]{20,}\b"),
        "Stripe rk_live_ restricted keys are secret and must not appear in frontend code.",
    ),
    (
        "SendGrid API key",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
        "SendGrid API keys (SG.xxx) are server-side credentials and must not appear in frontend code.",
    ),
    (
        "Slack bot token (xoxb-*)",
        re.compile(r"\bxoxb-[0-9A-Za-z\-]{20,}\b"),
        "Slack bot tokens (xoxb-) are server-side secrets and must not appear in frontend code.",
    ),
    (
        "Slack user token (xoxp-*)",
        re.compile(r"\bxoxp-[0-9A-Za-z\-]{20,}\b"),
        "Slack user tokens (xoxp-) are server-side secrets and must not appear in frontend code.",
    ),
    (
        "Generic live/secret API key literal",
        re.compile(
            r"""(?:secret_key|api_secret|private_key|server_key)\s*[=:]\s*['"][A-Za-z0-9_\-]{20,}['"]""",
            re.IGNORECASE,
        ),
        "A variable named secret_key/api_secret/private_key/server_key has a hardcoded literal in frontend code.",
    ),
]

# Files to scan for SEC-FE-002 — broader than pure frontend because payment keys
# should never be in source regardless of file type (belt-and-suspenders alongside SEC-001).
_ALL_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".vue", ".svelte", ".json", ".env",
})


def check_payment_secret_in_frontend(
    repo_path: Path, changed_files: list[str]
) -> dict:
    """SEC-FE-002: Detect payment-provider secret keys in any source file.

    Scans all source files for Stripe, SendGrid, Slack, and similar
    payment/communication provider secret key patterns using published
    prefix patterns. Flags both frontend and backend occurrences because
    these keys must never appear as literals in version-controlled source.

    Complements SEC-001 (hardcoded_jwt) which covers generic JWT secrets.
    """
    findings: list[dict] = []

    for rel_path in sorted(changed_files):
        p = Path(rel_path)
        if p.suffix.lower() not in _ALL_SOURCE_EXTENSIONS:
            continue
        path = repo_path / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        lines = source.splitlines()

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            # Skip full-line comments
            if stripped.startswith(("//", "#", "*", "/*")):
                continue

            for label, pattern, remediation in _PAYMENT_SECRET_PATTERNS:
                m = pattern.search(line)
                if m:
                    # Skip if surrounded by a known safe env var access pattern
                    if _SAFE_ENV_CONFIG.search(line):
                        continue
                    findings.append({
                        "rule_id": "SEC-FE-002",
                        "file": rel_path,
                        "line": lineno,
                        "kind": "payment_secret_literal",
                        "label": label,
                        "detail": (
                            f"Payment/provider secret key literal detected: {label}. "
                            f"Found in '{rel_path}' line {lineno}."
                        ),
                        "remediation": remediation,
                        "confidence": "high",
                        "cwe": "CWE-798",
                        "owasp": "A02:2021",
                    })
                    # One finding per line per file is enough to avoid noise
                    break

    return {
        "name": "payment_secret_frontend",
        "status": "FAIL" if findings else "PASS",
        "findings": findings,
        "exit_code": 1 if findings else 0,
        "raw_output": "",
        "error": None,
    }


# ---------------------------------------------------------------------------
# Registry: all checks in this pack
# ---------------------------------------------------------------------------

# Callable list consumed by run_review to wire in these checks.
# Each entry is (check_name: str, fn: callable, requires_python: bool).
# requires_python=False means the check runs even when there are no .py files.
SECURITY_PACK_1_CHECKS: list[tuple[str, object, bool]] = [
    ("frontend_secret_exposure", check_frontend_secret_exposure, False),
    ("payment_secret_frontend", check_payment_secret_in_frontend, False),
]
