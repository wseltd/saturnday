"""Capability registry for the Saturnday public/premium split.

Premium registers handlers at import time. Public queries availability
and falls back gracefully.
"""
from __future__ import annotations

import logging
from typing import Any

__all__ = [
    "register",
    "get",
    "is_available",
    "registered_stages",
    "clear",
    "snapshot",
]

_logger = logging.getLogger(__name__)

_registry: dict[str, Any] = {}


def register(name: str, handler: Any) -> None:
    """Register a capability handler under *name*.

    Called by premium packages at import time. Overwrites any previously
    registered handler for the same name (last-import-wins).

    Args:
        name: Canonical stage name (e.g. ``"security_triage"``).
        handler: The callable or object that implements the stage protocol.
    """
    _registry[name] = handler
    _logger.info("capability_registry: registered stage %r (%s)", name, type(handler).__qualname__)


def get(name: str) -> Any | None:
    """Return the registered handler for *name*, or ``None`` if absent.

    Args:
        name: Canonical stage name.

    Returns:
        The registered handler, or ``None``.
    """
    return _registry.get(name)


def is_available(name: str) -> bool:
    """Return ``True`` if a handler is registered for *name*.

    Args:
        name: Canonical stage name.
    """
    return name in _registry


def registered_stages() -> list[str]:
    """Return a sorted list of all currently registered stage names."""
    return sorted(_registry.keys())


def clear() -> None:
    """Remove all registered handlers.

    Intended for test isolation only. Do not call in production code.
    """
    _registry.clear()
    _logger.debug("capability_registry: cleared")


def snapshot() -> dict[str, str]:
    """Return a dict mapping each registered name to its handler's qualname.

    Used by evidence packs to record what was registered without exposing
    handler internals.

    Returns:
        ``{stage_name: type(handler).__qualname__}`` for every registered stage.
    """
    return {name: type(handler).__qualname__ for name, handler in _registry.items()}
