"""Saturnday Premium — additive premium capabilities for governed execution.

Bootstraps on import: checks entitlement and registers premium stages with the
capability registry if the licence is valid.
"""
from __future__ import annotations

__version__ = "1.1.01"

import logging

_logger = logging.getLogger(__name__)

from saturnday_premium._entitlement import check_entitlement, EntitlementState, is_entitled  # noqa: E402

_entitlement_state: EntitlementState | None = None


def entitlement_state() -> EntitlementState:
    """Return the cached entitlement state from package init.

    Returns:
        The ``EntitlementState`` that was recorded when this package was first
        imported.  If ``_bootstrap()`` has not run yet (unusual), runs it now.
    """
    global _entitlement_state
    if _entitlement_state is None:
        _entitlement_state = check_entitlement()
    return _entitlement_state


def _bootstrap() -> None:
    """Check entitlement and register premium stages if entitled.

    Called once at module import time.  Safe to call again (idempotent via the
    capability registry's duplicate-registration guard).

    If entitlement is invalid, logs a warning and returns without registering
    any stages — the public package continues in community mode.
    """
    global _entitlement_state
    _entitlement_state = check_entitlement()

    if not _entitlement_state.valid:
        _logger.warning(
            "saturnday-premium: not entitled (%s); premium stages will not register",
            _entitlement_state.reason,
        )
        return

    try:
        from saturnday.premium_registration import register_premium_stages
    except ImportError as exc:  # public package not installed — should not happen
        _logger.error("saturnday-premium: cannot import register_premium_stages — %s", exc)
        return

    register_premium_stages()
    _logger.info(
        "saturnday-premium: bootstrap complete, entitled (%s)",
        _entitlement_state.reason,
    )


_bootstrap()

__all__ = [
    "__version__",
    "is_entitled",
    "entitlement_state",
    "EntitlementState",
    "_bootstrap",
]
