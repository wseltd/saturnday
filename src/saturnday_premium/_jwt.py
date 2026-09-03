"""JWT encode/decode for Saturnday premium licence validation.

Uses Ed25519 (EdDSA) asymmetric signing exclusively via PyNaCl.
- Shipped runtime contains ONLY the public verification key.
- Signing requires the private key (not shipped, developer-only).
- Only EdDSA tokens are accepted; all other algorithms are rejected.

Security model: Ed25519 public-key verification. The signing key
never ships in the distributed package. End users cannot forge
valid licence JWTs from shipped package contents.

Do NOT import anything from ``saturnday`` or ``saturnday_premium`` here.
This module must remain independently importable as a pure utility.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "decode_jwt",
    "JWTError",
    "JWTExpiredError",
    "JWTInvalidSignatureError",
    "JWTMalformedError",
    "JwtError",
    "JwtExpiredError",
    "JwtSignatureError",
]

# ---------------------------------------------------------------------------
# Ed25519 public verification key
# ---------------------------------------------------------------------------
# This is safe to ship — it can only VERIFY, not SIGN.
# Generated 2026-03-30 with PyNaCl; corresponding private key is in
# ~/.saturnday/licence-signing.key (developer only, never shipped).
_VERIFY_KEY_B64: str = "eQhbLMRecPZlHQ_cXQfkOMVaTDv7KtNrS_7tDo7OkyY="

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class JWTError(Exception):
    """Base class for all JWT errors raised by this module."""


class JWTExpiredError(JWTError):
    """Raised when the ``exp`` claim is present and has elapsed."""


class JWTInvalidSignatureError(JWTError):
    """Raised when signature verification fails (wrong key or tampered token)."""


class JWTMalformedError(JWTError):
    """Raised when the token cannot be parsed (wrong structure, bad base64, or non-JSON)."""


# Aliases matching the ticket spec naming convention (used in new code paths).
JwtError = JWTError
JwtExpiredError = JWTExpiredError
JwtSignatureError = JWTInvalidSignatureError

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode *data* and strip ``=`` padding.

    Args:
        data: Raw bytes to encode.

    Returns:
        URL-safe base64 string with no trailing ``=`` characters.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Base64url-decode *s*, adding ``=`` padding as required.

    Args:
        s: URL-safe base64 string (with or without padding).

    Returns:
        Decoded bytes.

    Raises:
        JWTMalformedError: If *s* is not valid base64url.
    """
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    try:
        return base64.urlsafe_b64decode(s)
    except Exception as exc:
        raise JWTMalformedError(f"Invalid base64url segment: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API: decode_jwt
# ---------------------------------------------------------------------------


def decode_jwt(token: str) -> dict[str, Any]:
    """Validate and decode a JWT, returning the payload claims.

    Supports EdDSA (Ed25519) only. Tokens with any other algorithm are rejected.

    Dispatch by ``alg`` header:
    - ``EdDSA``: verifies against the embedded public verification key using
      PyNaCl. No secret argument is needed or accepted.

    Validation order:
    1. Split on ``"."`` — must have exactly three parts.
    2. Decode and parse header — ``alg`` must be ``EdDSA``.
    3. Verify signature against the embedded public key.
    4. Decode and parse payload JSON.
    5. If ``exp`` claim is present and ``exp < time.time()``, raise
       :exc:`JWTExpiredError`.
    6. Return the payload dict.

    Args:
        token: Raw JWT string (``header.payload.signature``).

    Returns:
        Decoded payload dict.

    Raises:
        JWTMalformedError: Token has wrong structure, bad base64, non-JSON
            content, or uses an unsupported signing algorithm.
        JWTInvalidSignatureError: Signature verification failed.
        JWTExpiredError: Token ``exp`` claim has elapsed.
        JWTError: PyNaCl not available.
    """
    # Step 1: structural check.
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise JWTMalformedError(
            f"JWT must have exactly 3 dot-separated parts; got {len(parts)}"
        )

    header_b64, payload_b64, signature_b64 = parts

    # Step 2: decode and validate header.
    try:
        header_bytes = _b64url_decode(header_b64)
        header = json.loads(header_bytes.decode("utf-8"))
    except JWTMalformedError:
        raise
    except Exception as exc:
        raise JWTMalformedError(f"Cannot parse JWT header: {exc}") from exc

    alg = header.get("alg", "")

    # Step 3: algorithm dispatch and signature verification.
    if alg == "EdDSA":
        _verify_eddsa(header_b64, payload_b64, signature_b64)
    else:
        raise JWTMalformedError(
            f"Unsupported signing algorithm: {alg!r}. Supported: EdDSA."
        )

    # Step 4: decode payload.
    try:
        payload_bytes = _b64url_decode(payload_b64)
        payload: dict[str, Any] = json.loads(payload_bytes.decode("utf-8"))
    except JWTMalformedError:
        raise
    except Exception as exc:
        raise JWTMalformedError(f"Cannot parse JWT payload: {exc}") from exc

    # Step 5: expiry check.
    exp = payload.get("exp")
    if exp is not None and float(exp) < time.time():
        raise JWTExpiredError("JWT has expired.")

    return payload


def _verify_eddsa(header_b64: str, payload_b64: str, signature_b64: str) -> None:
    """Verify an EdDSA (Ed25519) JWT signature against the embedded public key.

    Args:
        header_b64: Base64url-encoded JWT header.
        payload_b64: Base64url-encoded JWT payload.
        signature_b64: Base64url-encoded signature to verify.

    Raises:
        JWTError: PyNaCl is not installed.
        JWTInvalidSignatureError: Signature does not match.
    """
    try:
        import nacl.exceptions
        import nacl.signing
    except ImportError:
        raise JWTError(
            "PyNaCl is required for EdDSA licence verification: pip install PyNaCl"
        )

    try:
        verify_key_bytes = base64.urlsafe_b64decode(_VERIFY_KEY_B64 + "==")
        verify_key = nacl.signing.VerifyKey(verify_key_bytes)
        message = f"{header_b64}.{payload_b64}".encode("utf-8")
        signature = _b64url_decode(signature_b64)
        verify_key.verify(message, signature)
    except nacl.exceptions.BadSignatureError:
        raise JWTInvalidSignatureError("Ed25519 signature verification failed.")
    except (JWTMalformedError, JWTInvalidSignatureError):
        raise
    except Exception as exc:
        raise JWTInvalidSignatureError(
            f"Ed25519 verification error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Developer-only: encode_jwt
# ---------------------------------------------------------------------------


def encode_jwt(
    payload: dict[str, Any],
    *,
    private_key_b64: str,
) -> str:
    """Create a signed JWT using Ed25519. For developer tooling only — NOT part of shipped public API.

    .. warning::
        This function is **not intended for use from the shipped runtime**.
        Callers must supply an explicit Ed25519 private key. The shipped
        verification path (``decode_jwt``) does not depend on this function.

    Args:
        payload: Arbitrary JSON-serialisable dict of claims. Include an
            ``"exp"`` key (Unix epoch int) to create a token with an expiry.
        private_key_b64: Base64url-encoded Ed25519 private key for EdDSA
            signing. Required.

    Returns:
        A three-part JWT string of the form ``<header>.<payload>.<signature>``.

    Raises:
        RuntimeError: PyNaCl is not available.

    Example::

        token = encode_jwt(payload, private_key_b64=private_key)
    """
    return _encode_eddsa(payload, private_key_b64)


def _encode_eddsa(payload: dict[str, Any], private_key_b64: str) -> str:
    """Sign a JWT payload with an Ed25519 private key.

    Args:
        payload: Claims dict to encode.
        private_key_b64: Base64url-encoded 32-byte Ed25519 private key.

    Returns:
        Signed JWT string.

    Raises:
        RuntimeError: PyNaCl not installed.
    """
    try:
        import nacl.signing
    except ImportError:
        raise RuntimeError(
            "PyNaCl is required for EdDSA signing: pip install PyNaCl"
        )

    header: dict[str, str] = {"alg": "EdDSA", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    message = f"{header_b64}.{payload_b64}".encode("utf-8")

    private_bytes = base64.urlsafe_b64decode(private_key_b64 + "==")
    signing_key = nacl.signing.SigningKey(private_bytes)
    signed = signing_key.sign(message)
    sig_b64 = _b64url_encode(signed.signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"
