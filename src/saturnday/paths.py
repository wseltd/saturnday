"""Canonical resolver for the Saturnday operator-state directory.

Every module that writes shared operator state (SQLite databases, JSON
caches, licence file) resolves its path through :func:`state_dir`.  The
resolver implements a single, documented precedence so users can redirect
state without hunting through multiple env vars:

  1. ``SATURNDAY_STATE_DIR``     — explicit opt-in, highest priority
  2. ``XDG_STATE_HOME/saturnday`` — Freedesktop XDG state base dir
  3. ``~/.saturnday``            — backward-compatible default

Backward compatibility: when the operator sets neither env var, the
resolver returns ``~/.saturnday`` — identical to pre-refactor behaviour.

Usage:

    from saturnday.paths import state_dir
    lessons_db = state_dir() / "lessons.db"

Lazy (call-time) evaluation is mandatory so operator overrides and tests
that monkeypatch ``os.environ`` take effect without reloading modules.
"""
from __future__ import annotations

import os
from pathlib import Path


# One canonical env-var name.  Do NOT add more — the whole point of this
# module is a single documented surface.
_STATE_DIR_ENV = "SATURNDAY_STATE_DIR"
_XDG_STATE_ENV = "XDG_STATE_HOME"


def state_dir() -> Path:
    """Return the resolved operator-state directory.

    Precedence:
      1. ``SATURNDAY_STATE_DIR`` (if non-empty)
      2. ``XDG_STATE_HOME/saturnday`` (if ``XDG_STATE_HOME`` is non-empty)
      3. ``~/.saturnday`` (backward-compatible default)

    The directory is not created — callers that write there should
    ``.mkdir(parents=True, exist_ok=True)`` as they already do.
    """
    explicit = os.environ.get(_STATE_DIR_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()

    xdg = os.environ.get(_XDG_STATE_ENV, "").strip()
    if xdg:
        return Path(xdg).expanduser() / "saturnday"

    return Path.home() / ".saturnday"


__all__ = ["state_dir"]
