"""Non-blocking, cached, offline-safe version update notifier.

Checks PyPI for the latest released version of saturnday and surfaces an
update banner to the user when a newer version is available.

Design principles:
- Fast path: reads only from local disk (the cache file) which adds negligible
  latency (~1ms). No blocking network calls on the main thread when a cache
  exists.
- When no cache exists, a synchronous fetch is attempted so the first-run
  experience shows a banner immediately if an update is available.
- When the cache is stale, a background daemon thread refreshes it while the
  stale value is used for the current invocation.
- Fully offline-safe: any network error is silently swallowed; the command
  proceeds normally and the function returns None.
- First-seen detail: the rich multi-line banner is shown once per version;
  subsequent calls show a compact one-liner instead.
- stdlib only — no ``packaging``, no ``requests``.
- ``~/.saturnday/`` respects the ``HOME`` environment variable so tests can
  redirect all file I/O via ``monkeypatch.setenv("HOME", ...)``.

Cache file: ``~/.saturnday/version_cache.json``
  Schema: ``{"latest_version": "x.y.z", "checked_at": <unix-timestamp-float>}``

Seen file:  ``~/.saturnday/version_seen.json``
  Schema: ``{"seen": ["1.0.0", "1.1.01"]}``
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import urllib.error
import urllib.request
import time
from pathlib import Path
from typing import Any

from .release_notes import (
    format_detailed_banner,
    format_short_banner,
    get_release_note,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PYPI_URL = "https://pypi.org/pypi/saturnday/json"
_PYPI_TIMEOUT_S = 3
_DEFAULT_TTL_HOURS = 4

# ---------------------------------------------------------------------------
# Path helpers — evaluated at call time so HOME monkeypatching works in tests
# ---------------------------------------------------------------------------


def _saturnday_dir() -> Path:
    """Return the Saturnday operator-state directory, resolved at call time.

    Delegates to :func:`saturnday.paths.state_dir` so the same precedence
    applies here as for lessons.db, run_data.db, and the licence file:
    ``SATURNDAY_STATE_DIR`` > ``XDG_STATE_HOME/saturnday`` > ``~/.saturnday``.
    """
    from saturnday.paths import state_dir
    return state_dir()


def _cache_path() -> Path:
    """Return the path to ``version_cache.json``."""
    return _saturnday_dir() / "version_cache.json"


def _seen_path() -> Path:
    """Return the path to ``version_seen.json``."""
    return _saturnday_dir() / "version_seen.json"


# ---------------------------------------------------------------------------
# Internal helpers — filesystem
# ---------------------------------------------------------------------------


def _ensure_saturnday_dir() -> None:
    """Create ``~/.saturnday/`` if it does not exist.

    Silently ignores errors so that a read-only home directory never crashes
    a command invocation.
    """
    try:
        _saturnday_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("Could not create %s: %s", _saturnday_dir(), exc)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON file, returning None on any error.

    Args:
        path: Filesystem path to the JSON file.

    Returns:
        Parsed dictionary, or ``None`` if the file does not exist or is
        malformed.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)  # type: ignore[return-value]
    except FileNotFoundError:
        logger.debug("JSON file not found: %s", path)
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* atomically via a temp-file rename.

    Using a rename guarantees that a concurrent reader never sees a partially
    written file.  Silently ignores errors so background threads never raise.

    Args:
        path: Target file path.
        data: JSON-serialisable dictionary.
    """
    _ensure_saturnday_dir()
    try:
        dir_ = path.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dir_,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)
        logger.debug("Wrote %s atomically.", path)
    except OSError as exc:
        logger.debug("Atomic write to %s failed: %s", path, exc)


# ---------------------------------------------------------------------------
# Cache helpers — public for tests
# ---------------------------------------------------------------------------


def _read_cache() -> dict[str, Any] | None:
    """Return the version cache dictionary, or None if absent/unreadable."""
    return _read_json_file(_cache_path())


def _write_cache(version: str) -> None:
    """Write a fresh cache entry for *version*.

    Sets ``checked_at`` to the current Unix timestamp.

    Args:
        version: Version string to cache as the latest known version.
    """
    _write_json_atomic(
        _cache_path(),
        {
            "latest_version": version,
            "checked_at": time.time(),
        },
    )


def _cache_is_fresh(cache: dict[str, Any], ttl_hours: float = _DEFAULT_TTL_HOURS) -> bool:
    """Return True if the cache was written within its TTL window.

    Args:
        cache: Parsed cache dictionary. Must contain ``checked_at`` as a Unix
            timestamp (float or int).
        ttl_hours: Time-to-live in hours. Defaults to
            :data:`_DEFAULT_TTL_HOURS`.

    Returns:
        ``True`` when the cache is still valid, ``False`` when it has expired
        or when ``checked_at`` cannot be parsed.
    """
    checked_at_raw = cache.get("checked_at")
    if checked_at_raw is None:
        return False
    try:
        checked_at = float(checked_at_raw)
        age_hours = (time.time() - checked_at) / 3600.0
        return age_hours < ttl_hours
    except (ValueError, TypeError) as exc:
        logger.debug("Could not parse checked_at from cache: %s", exc)
        return False


# ---------------------------------------------------------------------------
# First-seen tracking — public for tests
# ---------------------------------------------------------------------------


def _has_seen_detail(version: str) -> bool:
    """Return True if the detailed banner for *version* has been shown before.

    Args:
        version: Version string (e.g. "1.1.01").
    """
    data = _read_json_file(_seen_path())
    if data is None:
        return False
    seen: list[str] = data.get("seen", [])
    return version in seen


def _mark_version_seen(version: str) -> None:
    """Record that the detailed banner for *version* has been shown.

    Reads the current seen file, appends the version if absent, and writes
    back atomically.

    Args:
        version: Version string to mark as seen.
    """
    data = _read_json_file(_seen_path()) or {}
    seen: list[str] = data.get("seen", [])
    if version not in seen:
        seen.append(version)
    _write_json_atomic(_seen_path(), {"seen": seen})


# ---------------------------------------------------------------------------
# PyPI fetch
# ---------------------------------------------------------------------------


def _fetch_latest_version() -> str | None:
    """Fetch the latest saturnday version from PyPI and update the cache.

    May be called synchronously (first run, no cache) or from a background
    thread (stale cache).  Any error is logged at DEBUG level and ``None`` is
    returned — the caller is responsible for falling back gracefully.

    Returns:
        Latest version string (e.g. ``"1.1.01"``), or ``None`` on any error.
    """
    try:
        logger.debug("Fetching latest version from PyPI: %s", _PYPI_URL)
        with urllib.request.urlopen(_PYPI_URL, timeout=_PYPI_TIMEOUT_S) as resp:
            raw = resp.read()
        payload: dict[str, Any] = json.loads(raw)
        version: str = payload["info"]["version"]
        logger.debug("PyPI reports latest version: %s", version)
        _write_cache(version)
        return version
    except urllib.error.URLError as exc:
        logger.debug("PyPI fetch failed (URLError): %s", exc)
        return None
    except (KeyError, json.JSONDecodeError) as exc:
        logger.debug("PyPI response parse error: %s", exc)
        return None
    except OSError as exc:
        # Covers socket.timeout on some Python versions
        logger.debug("PyPI fetch OS error: %s", exc)
        return None
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Unexpected error during PyPI version fetch: %s", exc)
        return None


def _spawn_background_fetch() -> None:
    """Launch :func:`_fetch_latest_version` in a daemon thread.

    The thread is daemonised so it does not prevent process exit.  The result
    is written to the cache file and will be picked up on the next invocation.
    """
    thread = threading.Thread(
        target=_fetch_latest_version,
        name="saturnday-version-check",
        daemon=True,
    )
    thread.start()
    logger.debug("Background version fetch thread started.")


# ---------------------------------------------------------------------------
# Version comparison — stdlib only
# ---------------------------------------------------------------------------


def _parse_version_tuple(version: str) -> tuple[int, ...]:
    """Parse a version string into a comparable integer tuple.

    Non-numeric components are coerced to ``0`` so that strings like
    ``"1.1.01"`` compare correctly.

    Args:
        version: A version string such as ``"1.1.01"`` or ``"2.0.0"``.

    Returns:
        A tuple of integers, e.g. ``(1, 1, 1)`` for ``"1.1.01"``.
    """
    parts: list[int] = []
    for part in version.split("."):
        try:
            parts.append(int(part))
        except (ValueError, TypeError):
            parts.append(0)
    return tuple(parts)


def _version_newer(latest: str, current: str) -> bool:
    """Return True if *latest* is strictly newer than *current*.

    Uses tuple comparison on integer-parsed version components.  Non-numeric
    parts are coerced to ``0`` and never raise.

    Args:
        latest: Candidate newer version string.
        current: Currently installed version string.

    Returns:
        ``True`` when *latest* is strictly greater than *current*, ``False``
        otherwise (including when they are equal).

    Examples::

        >>> _version_newer("1.1.01", "1.0.0")
        True
        >>> _version_newer("1.0.0", "1.1.01")
        False
        >>> _version_newer("1.1.01", "1.1.01")
        False
    """
    try:
        latest_t = _parse_version_tuple(latest)
        current_t = _parse_version_tuple(current)
        return latest_t > current_t
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug(
            "Version comparison failed for latest=%r current=%r: %s",
            latest,
            current,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Banner helpers
# ---------------------------------------------------------------------------


def _format_minimal_banner(version: str, upgrade_cmd: str) -> str:
    """Return a minimal multi-line banner for a version with no registered note.

    Used when a newer version is detected for the first time but no detailed
    :class:`.ReleaseNote` is registered for it.  Produces at least two lines
    so callers can distinguish it from the compact short banner.

    Args:
        version: The newer version that is available.
        upgrade_cmd: Shell command to upgrade.

    Returns:
        A formatted multi-line string, ready to print.
    """
    separator = "-" * 60
    lines = [
        "",
        separator,
        f"  saturnday {version} is available",
        f"  Upgrade: {upgrade_cmd}",
        separator,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_and_notify(
    current_version: str,
    edition: str = "public",
) -> str | None:
    """Check for a newer version and return an update banner string, or None.

    Detailed flow:

    1. Read ``~/.saturnday/version_cache.json``.
    2. If the cache is fresh (within TTL): use the cached ``latest_version``.
    3. If the cache is stale: spawn a background thread to refresh it; use the
       stale value for this invocation.
    4. If there is no cache at all: attempt a synchronous PyPI fetch (first-run
       experience). If that fails (offline), return ``None``.
    5. If ``latest == current`` (or latest is older): return ``None``.
    6. If ``latest > current``:

       a. Check ``~/.saturnday/version_seen.json``.
       b. If not yet shown detailed AND a :class:`.ReleaseNote` exists AND
          :attr:`.ReleaseNote.first_seen_detail_enabled` is ``True``: render
          and return the detailed banner; mark the version as seen.
       c. Otherwise: render and return the short banner.

    Args:
        current_version: The version of saturnday currently installed
            (typically ``__version__``).
        edition: Product edition — ``"public"`` or ``"premium"``. Used to look
            up the correct :class:`.ReleaseNote`. Defaults to ``"public"``.

    Returns:
        A ready-to-print banner string, or ``None`` when no notification
        should be displayed.
    """
    logger.debug(
        "check_and_notify called: current_version=%s edition=%s",
        current_version,
        edition,
    )

    # Step 1-4: resolve latest_version
    latest_version: str | None = None
    cache = _read_cache()

    if cache is not None:
        if _cache_is_fresh(cache):
            latest_version = cache.get("latest_version")
            logger.debug("Using fresh cache: latest_version=%s", latest_version)
        else:
            # Stale cache — use stale value now; refresh asynchronously
            latest_version = cache.get("latest_version")
            logger.debug(
                "Cache is stale; spawning background fetch. "
                "Using stale latest_version=%s for this invocation.",
                latest_version,
            )
            _spawn_background_fetch()
    else:
        # No cache — fetch synchronously so first-run shows a banner
        logger.debug("No cache found; attempting synchronous fetch.")
        latest_version = _fetch_latest_version()

    # Step 5: no version available (offline, first run, network error)
    if not latest_version:
        return None

    # Step 5: already on latest or ahead
    if not _version_newer(latest_version, current_version):
        logger.debug(
            "No update available (latest=%s current=%s).",
            latest_version,
            current_version,
        )
        return None

    # Step 6: newer version available — choose short or detailed banner
    logger.debug(
        "Newer version available: latest=%s current=%s.",
        latest_version,
        current_version,
    )

    note = get_release_note(latest_version, edition)

    if not _has_seen_detail(latest_version):
        # First time this version is seen — show detailed banner if available,
        # otherwise construct a minimal multi-line banner so that the version
        # is surfaced clearly regardless of whether release notes are registered.
        _mark_version_seen(latest_version)
        if note is not None and note.first_seen_detail_enabled:
            return format_detailed_banner(note)
        # Minimal multi-line banner for versions without a registered note
        upgrade_cmd = (
            note.upgrade_command
            if note is not None
            else "python -m pip install -U saturnday"
        )
        return _format_minimal_banner(latest_version, upgrade_cmd)

    # Short banner — version already shown in detail on a prior invocation
    upgrade_cmd = (
        note.upgrade_command
        if note is not None
        else "python -m pip install -U saturnday"
    )
    return format_short_banner(current_version, latest_version, upgrade_cmd)
