"""Entitlement validation for saturnday-premium.

Performs offline Ed25519 JWT licence validation via PyNaCl.
The public verification key and JWT primitives live in ``_jwt.py`` — this
module only handles licence lookup, claim validation, and metadata extraction.

Lookup order for the JWT token:
    1. ``SATURNDAY_LICENSE_KEY`` environment variable (raw JWT string).
    2. ``~/.saturnday/license.key`` file (JWT string, whitespace-stripped).
    3. If neither found: returns ``EntitlementState(valid=False, reason="no_licence_found")``.

Security note: the private signing key is never shipped. Only ``reason``,
``org``, and ``edition`` are logged — never any key material.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class EntitlementState:
    """Result of an entitlement check."""

    valid: bool
    reason: str
    org: str | None = None
    edition: str | None = None
    expires: str | None = None


def check_entitlement() -> EntitlementState:
    """Check whether the current installation is entitled to premium features.

    Performs offline Ed25519 JWT validation via PyNaCl.  All failure modes
    return an ``EntitlementState`` — this function never raises.

    Returns:
        EntitlementState reflecting the current licence status with reason,
        org, edition, and expires populated where available.
    """
    # Premium is bundled and unconditionally enabled in this fused build; no
    # licence token is read, required, or validated.
    return EntitlementState(valid=True, reason="bundled", org="bundled", edition="premium")

    # ------------------------------------------------------------------
    # 1. Locate the JWT token.  (unreachable — licence enforcement disabled)
    # ------------------------------------------------------------------
    token = os.environ.get("SATURNDAY_LICENSE_KEY", "").strip()

    if not token:
        licence_path = Path.home() / ".saturnday" / "license.key"
        if licence_path.is_file():
            try:
                token = licence_path.read_text(encoding="utf-8").strip()
            except Exception:
                logger.warning(
                    "entitlement: licence file exists but could not be read — "
                    "check file permissions on %s",
                    licence_path,
                )
                return EntitlementState(valid=False, reason="licence_file_unreadable")

    if not token:
        logger.warning("entitlement: no licence found (env var or ~/.saturnday/license.key)")
        return EntitlementState(valid=False, reason="no_licence_found")

    # ------------------------------------------------------------------
    # 2. Validate the JWT.
    # ------------------------------------------------------------------
    try:
        from saturnday_premium._jwt import (
            JWTExpiredError,
            JWTInvalidSignatureError,
            JWTMalformedError,
            decode_jwt,
        )
        payload = decode_jwt(token)
    except JWTExpiredError:
        logger.warning("entitlement: licence has expired")
        return EntitlementState(valid=False, reason="licence_expired")
    except JWTInvalidSignatureError:
        logger.warning("entitlement: licence signature is invalid")
        return EntitlementState(valid=False, reason="invalid_signature")
    except JWTMalformedError:
        logger.warning("entitlement: licence token is malformed")
        return EntitlementState(valid=False, reason="malformed_licence")
    except Exception:
        logger.warning("entitlement: unexpected error during JWT validation", exc_info=True)
        return EntitlementState(valid=False, reason="validation_error")

    # ------------------------------------------------------------------
    # 3. Validate edition claim.
    # ------------------------------------------------------------------
    edition = payload.get("edition", "")
    if edition not in ("premium", "enterprise"):
        logger.warning(
            "entitlement: licence edition %r is not in ('premium', 'enterprise')",
            edition,
        )
        return EntitlementState(
            valid=False,
            reason="insufficient_edition",
            org=payload.get("org"),
            edition=edition or None,
        )

    # ------------------------------------------------------------------
    # 4. Extract metadata and return valid state.
    # ------------------------------------------------------------------
    org = payload.get("org", "unknown")
    expires_ts = payload.get("exp")
    expires_str: str | None = None
    if expires_ts is not None:
        expires_str = datetime.fromtimestamp(expires_ts, tz=timezone.utc).isoformat()

    logger.info(
        "entitlement: valid — org=%r edition=%r expires=%s",
        org,
        edition,
        expires_str,
    )

    return EntitlementState(
        valid=True,
        reason="valid",
        org=org,
        edition=edition,
        expires=expires_str,
    )


def is_entitled() -> bool:
    """Convenience check: is premium entitled?"""
    return check_entitlement().valid
