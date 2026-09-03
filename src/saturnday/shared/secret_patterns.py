"""Shared secret-detection patterns — public API for governance checks.

This module provides the canonical pattern sets used across Saturnday's
security checks (review.py, security_pack_1.py, RS-008 and related rules).
All symbols here are public.  Consumers must NOT import underscore-prefixed
symbols from review.py or security_pack_1.py directly.

Exported names
--------------
SECRET_PATTERNS         Named patterns with compiled regexes for generic
                        high-entropy / credential scanning.
HIGH_ENTROPY_REGEX      Main entropy-based pattern (≥32 contiguous alphanum chars).
PAYMENT_SECRET_PATTERNS Payment-provider-specific patterns (Stripe, SendGrid, …).
ENV_FILE_PATTERNS       File name patterns that indicate env/credential files.
KEY_FILE_PATTERNS       File name patterns for key/certificate files.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# HIGH_ENTROPY_REGEX
# Matches long (≥32 char) alphanumeric runs — the primary signal used by
# _scan_secrets in review.py.  Defined here as the single source of truth.
# ---------------------------------------------------------------------------

HIGH_ENTROPY_REGEX: re.Pattern[str] = re.compile(r"[A-Za-z0-9]{32,}")

# ---------------------------------------------------------------------------
# SECRET_PATTERNS
# A sequence of (label, compiled_regex) pairs.  Each entry represents one
# named pattern family.  Order is significant: more specific patterns first.
# ---------------------------------------------------------------------------

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # --- Provider-prefixed keys ---
    (
        "Stripe secret key (sk_live_*)",
        re.compile(r"\bsk_live_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "Stripe secret key (sk_test_*)",
        re.compile(r"\bsk_test_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "Stripe restricted key (rk_live_*)",
        re.compile(r"\brk_live_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "SendGrid API key",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
    ),
    (
        "Slack bot token (xoxb-*)",
        re.compile(r"\bxoxb-[0-9A-Za-z\-]{20,}\b"),
    ),
    (
        "Slack user token (xoxp-*)",
        re.compile(r"\bxoxp-[0-9A-Za-z\-]{20,}\b"),
    ),
    (
        "Generic live/secret API key literal",
        re.compile(
            r"""(?:secret_key|api_secret|private_key|server_key)\s*[=:]\s*['"][A-Za-z0-9_\-]{20,}['"]""",
            re.IGNORECASE,
        ),
    ),
    # --- High-entropy fallback ---
    (
        "High-entropy alphanumeric string (≥32 chars)",
        HIGH_ENTROPY_REGEX,
    ),
]

# ---------------------------------------------------------------------------
# PAYMENT_SECRET_PATTERNS
# Extended 3-tuple form: (label, compiled_regex, remediation_message).
# Sourced from security_pack_1._PAYMENT_SECRET_PATTERNS.
# ---------------------------------------------------------------------------

PAYMENT_SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "Stripe secret key (sk_live_*)",
        re.compile(r"\bsk_live_[A-Za-z0-9_]{20,}\b"),
        (
            "Stripe sk_live_ keys are secret and must never appear in "
            "frontend code or browser-delivered assets."
        ),
    ),
    (
        "Stripe secret key (sk_test_*)",
        re.compile(r"\bsk_test_[A-Za-z0-9_]{20,}\b"),
        (
            "Stripe sk_test_ keys are secret. Even test keys should not be "
            "in frontend code."
        ),
    ),
    (
        "Stripe restricted key (rk_live_*)",
        re.compile(r"\brk_live_[A-Za-z0-9_]{20,}\b"),
        (
            "Stripe rk_live_ restricted keys are secret and must not appear "
            "in frontend code."
        ),
    ),
    (
        "SendGrid API key",
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
        (
            "SendGrid API keys (SG.xxx) are server-side credentials and must "
            "not appear in frontend code."
        ),
    ),
    (
        "Slack bot token (xoxb-*)",
        re.compile(r"\bxoxb-[0-9A-Za-z\-]{20,}\b"),
        (
            "Slack bot tokens (xoxb-) are server-side secrets and must not "
            "appear in frontend code."
        ),
    ),
    (
        "Slack user token (xoxp-*)",
        re.compile(r"\bxoxp-[0-9A-Za-z\-]{20,}\b"),
        (
            "Slack user tokens (xoxp-) are server-side secrets and must not "
            "appear in frontend code."
        ),
    ),
    (
        "Generic live/secret API key literal",
        re.compile(
            r"""(?:secret_key|api_secret|private_key|server_key)\s*[=:]\s*['"][A-Za-z0-9_\-]{20,}['"]""",
            re.IGNORECASE,
        ),
        (
            "A variable named secret_key/api_secret/private_key/server_key "
            "has a hardcoded literal in frontend code."
        ),
    ),
]

# ---------------------------------------------------------------------------
# ENV_FILE_PATTERNS
# File name patterns (basenames or suffixes) that indicate an
# environment/credential configuration file.  Used to flag committed env files.
# ---------------------------------------------------------------------------

ENV_FILE_PATTERNS: list[str] = [
    ".env",
    ".env.local",
    ".env.development",
    ".env.development.local",
    ".env.test",
    ".env.test.local",
    ".env.production",
    ".env.production.local",
    ".env.staging",
    ".env.staging.local",
    ".env.example",   # template — warn, not block
    ".env.sample",    # template — warn, not block
    ".envrc",         # direnv
    "*.env",          # glob variant for scanners
]

# ---------------------------------------------------------------------------
# KEY_FILE_PATTERNS
# File name patterns (basenames or suffixes) for cryptographic key /
# certificate files.  Committed private keys are always a hard block.
# ---------------------------------------------------------------------------

KEY_FILE_PATTERNS: list[str] = [
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.crt",
    "*.cer",
    "*.der",
    "id_rsa",
    "id_rsa.pub",
    "id_dsa",
    "id_dsa.pub",
    "id_ecdsa",
    "id_ecdsa.pub",
    "id_ed25519",
    "id_ed25519.pub",
    "*.jks",           # Java KeyStore
    "*.keystore",
    "*.pkcs12",
    "server.key",
    "server.crt",
    "ca.key",
    "ca.crt",
]
