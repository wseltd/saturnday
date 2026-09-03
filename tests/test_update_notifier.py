"""Tests for saturnday.update_notifier and saturnday.release_notes.

TDD: these tests define the contract for modules that do not yet exist.
All scenarios documented in the ticket are covered.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.release_notes import (
    ReleaseNote,
    format_detailed_banner,
    format_short_banner,
    get_release_note,
)
from saturnday.update_notifier import (
    _fetch_latest_version,
    _has_seen_detail,
    _mark_version_seen,
    _read_cache,
    _version_newer,
    _write_cache,
    check_and_notify,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PYPI_RESPONSE = json.dumps(
    {"info": {"version": "1.1.02"}}
).encode()

_PYPI_RESPONSE_CURRENT = json.dumps(
    {"info": {"version": "1.1.01"}}
).encode()


def _make_cache(tmp_path: Path, version: str, *, stale: bool = False) -> Path:
    """Write a version_cache.json under tmp_path/.saturnday/.

    stale=True sets checked_at far in the past (beyond any reasonable TTL).
    """
    saturnday_dir = tmp_path / ".saturnday"
    saturnday_dir.mkdir(parents=True, exist_ok=True)
    cache_file = saturnday_dir / "version_cache.json"
    checked_at = time.time() - (7 * 24 * 3600) if stale else time.time()
    cache_file.write_text(
        json.dumps({"latest_version": version, "checked_at": checked_at})
    )
    return cache_file


def _make_seen(tmp_path: Path, version: str) -> Path:
    """Write a version_seen.json marking *version* as already shown."""
    saturnday_dir = tmp_path / ".saturnday"
    saturnday_dir.mkdir(parents=True, exist_ok=True)
    seen_file = saturnday_dir / "version_seen.json"
    seen_file.write_text(json.dumps({"seen": [version]}))
    return seen_file


# ---------------------------------------------------------------------------
# Scenario 1 — Newer version available, first time seen
# ---------------------------------------------------------------------------


class TestNewerVersionFirstSeen:
    def test_returns_detailed_banner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_and_notify returns a DETAILED banner when newer and not yet seen."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02")

        result = check_and_notify("1.1.01", "public")

        assert result is not None, "Expected a banner, got None"
        # Detailed banner must include more than a single-line upgrade prompt
        # (highlights or fixes section)
        assert len(result.strip().splitlines()) > 1

    def test_version_marked_seen_after_notify(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """After showing the detailed banner, version_seen.json must record the version."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02")

        check_and_notify("1.1.01", "public")

        seen_file = tmp_path / ".saturnday" / "version_seen.json"
        assert seen_file.exists(), "version_seen.json was not created"
        data = json.loads(seen_file.read_text())
        assert "1.1.02" in data.get("seen", []), "Latest version not recorded as seen"


# ---------------------------------------------------------------------------
# Scenario 2 — Newer version available, already seen
# ---------------------------------------------------------------------------


class TestNewerVersionAlreadySeen:
    def test_returns_short_banner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_and_notify returns a SHORT (single-line) banner when already seen."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02")
        _make_seen(tmp_path, "1.1.02")

        result = check_and_notify("1.1.01", "public")

        assert result is not None, "Expected a short banner, got None"
        # Short banner is compact — one logical line or clearly shorter than detailed
        lines = [ln for ln in result.strip().splitlines() if ln.strip()]
        assert len(lines) <= 3, f"Expected short banner, got {len(lines)} lines: {result!r}"

    def test_short_banner_contains_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Short banner must reference the newer version."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02")
        _make_seen(tmp_path, "1.1.02")

        result = check_and_notify("1.1.01", "public")

        assert result is not None
        assert "1.1.02" in result


# ---------------------------------------------------------------------------
# Scenario 3 — Already up to date
# ---------------------------------------------------------------------------


class TestAlreadyUpToDate:
    def test_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_and_notify returns None when current == latest."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.01")

        result = check_and_notify("1.1.01", "public")

        assert result is None

    def test_returns_none_when_current_is_newer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """check_and_notify returns None when running a pre-release ahead of PyPI."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.01")

        result = check_and_notify("1.1.02", "public")

        assert result is None


# ---------------------------------------------------------------------------
# Scenario 4 — Offline / timeout with no cache
# ---------------------------------------------------------------------------


class TestOfflineNoCache:
    def test_returns_none_no_crash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No cache + URLError → returns None silently, no exception raised."""
        from urllib.error import URLError

        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
            result = check_and_notify("1.1.01", "public")

        assert result is None

    def test_returns_none_on_timeout_exception(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """socket.timeout also handled gracefully."""
        import socket

        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            result = check_and_notify("1.1.01", "public")

        assert result is None


# ---------------------------------------------------------------------------
# Scenario 5 — Stale cache, offline → stale value still used
# ---------------------------------------------------------------------------


class TestStaleCacheOffline:
    def test_stale_cache_used_when_network_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When cache is expired and network fails, the stale cached value is used."""
        from urllib.error import URLError

        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02", stale=True)

        with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
            result = check_and_notify("1.1.01", "public")

        # Stale but newer version should still produce a banner
        assert result is not None, "Expected stale-cache banner, got None"

    def test_stale_cache_same_version_still_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stale cache with same version + network failure → None (no banner needed)."""
        from urllib.error import URLError

        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.01", stale=True)

        with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
            result = check_and_notify("1.1.01", "public")

        assert result is None


# ---------------------------------------------------------------------------
# Scenario 6 — Fresh cache → no network call made
# ---------------------------------------------------------------------------


class TestFreshCacheNoNetworkCall:
    def test_no_network_call_when_cache_fresh(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh (within TTL) cache must not trigger any HTTP request."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02")  # fresh by default (checked_at = now)

        with patch("urllib.request.urlopen") as mock_url:
            check_and_notify("1.1.01", "public")
            mock_url.assert_not_called()

    def test_fresh_cache_value_is_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh cache with a newer version produces a banner without a network call."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_cache(tmp_path, "1.1.02")

        with patch("urllib.request.urlopen") as mock_url:
            result = check_and_notify("1.1.01", "public")
            mock_url.assert_not_called()

        assert result is not None


# ---------------------------------------------------------------------------
# Scenario 7 — Version comparison
# ---------------------------------------------------------------------------


class TestVersionComparison:
    def test_newer_patch(self) -> None:
        assert _version_newer("1.1.02", "1.1.01") is True

    def test_same_version(self) -> None:
        assert _version_newer("1.1.01", "1.1.01") is False

    def test_older_version(self) -> None:
        assert _version_newer("1.0.99", "1.1.01") is False

    def test_newer_major(self) -> None:
        assert _version_newer("2.0.0", "1.1.01") is True

    def test_newer_minor(self) -> None:
        assert _version_newer("1.2.0", "1.1.01") is True

    def test_older_major(self) -> None:
        assert _version_newer("0.9.0", "1.1.01") is False

    def test_non_semver_returns_false(self) -> None:
        """Malformed version strings must not raise — return False (safe default)."""
        assert _version_newer("not-a-version", "1.1.01") is False


# ---------------------------------------------------------------------------
# Scenario 8 — Release notes lookup
# ---------------------------------------------------------------------------


class TestReleaseNotesLookup:
    def test_known_version_returns_release_note(self) -> None:
        note = get_release_note("1.1.1", "public")
        assert note is not None
        assert isinstance(note, ReleaseNote)

    def test_unknown_version_returns_none(self) -> None:
        note = get_release_note("99.99.99", "public")
        assert note is None

    def test_release_note_has_required_fields(self) -> None:
        note = get_release_note("1.1.1", "public")
        assert note is not None
        assert hasattr(note, "version")
        assert hasattr(note, "highlights")
        assert hasattr(note, "fixes")
        assert note.version == "1.1.1"

    def test_release_note_highlights_is_list(self) -> None:
        note = get_release_note("1.1.1", "public")
        assert note is not None
        assert isinstance(note.highlights, list)

    def test_release_note_fixes_is_list(self) -> None:
        note = get_release_note("1.1.1", "public")
        assert note is not None
        assert isinstance(note.fixes, list)


# ---------------------------------------------------------------------------
# Scenario 9 — Banner formatting
# ---------------------------------------------------------------------------


class TestBannerFormatting:
    def test_format_short_banner_contains_versions(self) -> None:
        result = format_short_banner("1.1.01", "1.1.02", "pip install --upgrade saturnday")
        assert "1.1.01" in result
        assert "1.1.02" in result

    def test_format_short_banner_contains_upgrade_cmd(self) -> None:
        cmd = "pip install --upgrade saturnday"
        result = format_short_banner("1.1.01", "1.1.02", cmd)
        assert cmd in result

    def test_format_short_banner_is_compact(self) -> None:
        result = format_short_banner("1.1.01", "1.1.02", "pip install --upgrade saturnday")
        lines = [ln for ln in result.strip().splitlines() if ln.strip()]
        assert len(lines) <= 3, f"Short banner too long: {result!r}"

    def test_format_detailed_banner_includes_highlights(self) -> None:
        note = ReleaseNote(
            version="1.1.02",
            highlights=["New feature A", "Improvement B"],
            fixes=["Fixed crash in X"],
        )
        result = format_detailed_banner(note)
        assert "New feature A" in result
        assert "Improvement B" in result

    def test_format_detailed_banner_includes_fixes(self) -> None:
        note = ReleaseNote(
            version="1.1.02",
            highlights=["Feature"],
            fixes=["Fix for bug Y"],
        )
        result = format_detailed_banner(note)
        assert "Fix for bug Y" in result

    def test_format_detailed_banner_includes_version(self) -> None:
        note = ReleaseNote(
            version="1.1.02",
            highlights=["Something"],
            fixes=[],
        )
        result = format_detailed_banner(note)
        assert "1.1.02" in result

    def test_format_detailed_banner_is_longer_than_short(self) -> None:
        note = ReleaseNote(
            version="1.1.02",
            highlights=["Feature A", "Feature B"],
            fixes=["Bug fix C"],
        )
        detailed = format_detailed_banner(note)
        short = format_short_banner("1.1.01", "1.1.02", "pip install --upgrade saturnday")
        assert len(detailed) > len(short)


# ---------------------------------------------------------------------------
# Scenario 10 — Cache missing, fresh fetch succeeds → cache written
# ---------------------------------------------------------------------------


class TestCacheMissingFreshFetch:
    def _mock_urlopen(self, version: str = "1.1.02"):
        """Return a mock context manager that yields a file-like with PyPI JSON."""
        response_body = json.dumps({"info": {"version": version}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_cache_written_after_successful_fetch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """After a live fetch, version_cache.json must be created."""
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen("1.1.02")):
            check_and_notify("1.1.01", "public")

        cache_file = tmp_path / ".saturnday" / "version_cache.json"
        assert cache_file.exists(), "version_cache.json was not written after fetch"

    def test_cache_contains_fetched_version(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The written cache must contain the version returned by PyPI."""
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen("1.1.02")):
            check_and_notify("1.1.01", "public")

        cache_file = tmp_path / ".saturnday" / "version_cache.json"
        data = json.loads(cache_file.read_text())
        assert data.get("latest_version") == "1.1.02"

    def test_cache_contains_checked_at_timestamp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Written cache must include a checked_at timestamp for TTL calculations."""
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen("1.1.02")):
            check_and_notify("1.1.01", "public")

        cache_file = tmp_path / ".saturnday" / "version_cache.json"
        data = json.loads(cache_file.read_text())
        assert "checked_at" in data
        assert isinstance(data["checked_at"], (int, float))

    def test_returns_banner_after_fetch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When fetch returns a newer version, a banner is returned."""
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen("1.1.02")):
            result = check_and_notify("1.1.01", "public")

        assert result is not None


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestReadWriteCache:
    def test_write_then_read_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".saturnday").mkdir(parents=True, exist_ok=True)

        _write_cache("1.1.02")
        result = _read_cache()

        assert result is not None
        assert result.get("latest_version") == "1.1.02"
        assert "checked_at" in result

    def test_read_cache_returns_none_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        # No .saturnday dir created

        result = _read_cache()

        assert result is None


class TestSeenVersionTracking:
    def test_mark_then_check(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".saturnday").mkdir(parents=True, exist_ok=True)

        assert _has_seen_detail("1.1.02") is False
        _mark_version_seen("1.1.02")
        assert _has_seen_detail("1.1.02") is True

    def test_unseen_version_returns_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))

        assert _has_seen_detail("9.9.99") is False

    def test_multiple_versions_tracked_independently(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".saturnday").mkdir(parents=True, exist_ok=True)

        _mark_version_seen("1.1.02")

        assert _has_seen_detail("1.1.02") is True
        assert _has_seen_detail("1.1.03") is False


class TestFetchLatestVersion:
    def test_returns_version_string_on_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        response_body = json.dumps({"info": {"version": "1.1.02"}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            version = _fetch_latest_version()

        assert version == "1.1.02"

    def test_returns_none_on_url_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.error import URLError

        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
            version = _fetch_latest_version()

        assert version is None

    def test_returns_none_on_malformed_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not-json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            version = _fetch_latest_version()

        assert version is None
