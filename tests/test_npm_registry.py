"""Tests for npm registry client."""

from unittest.mock import patch, MagicMock
import urllib.error

import pytest

from saturnday.npm_registry import (
    NpmPackageInfo,
    check_package_exists,
    clear_cache,
    get_weekly_downloads,
    registry_reachable,
)


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Clear registry cache before each test."""
    clear_cache()
    yield
    clear_cache()


class TestCheckPackageExists:
    def test_404_returns_not_exists(self):
        """A 404 from the registry means the package does not exist."""
        err = urllib.error.HTTPError(
            "https://registry.npmjs.org/fake-pkg",
            404, "Not Found", {}, None,
        )
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=err):
            with patch("saturnday.npm_registry._reachable", True):
                info = check_package_exists("fake-pkg")
        assert info.exists is False
        assert info.name == "fake-pkg"

    def test_200_returns_exists(self):
        """A successful response means the package exists."""
        resp_data = b'{"name": "express", "dist-tags": {"latest": "4.18.0"}, "versions": {"4.18.0": {}}}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("saturnday.npm_registry.urllib.request.urlopen", return_value=mock_resp):
            with patch("saturnday.npm_registry._reachable", True):
                info = check_package_exists("express")
        assert info.exists is True
        assert info.name == "express"
        assert info.latest_version == "4.18.0"

    def test_500_assumes_exists(self):
        """Non-404 HTTP errors should assume the package exists (never false-positive)."""
        err = urllib.error.HTTPError(
            "https://registry.npmjs.org/some-pkg",
            500, "Internal Server Error", {}, None,
        )
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=err):
            with patch("saturnday.npm_registry._reachable", True):
                info = check_package_exists("some-pkg")
        assert info.exists is True

    def test_network_error_assumes_exists(self):
        """Network errors should assume the package exists."""
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=ConnectionError("timeout")):
            with patch("saturnday.npm_registry._reachable", True):
                info = check_package_exists("some-pkg")
        assert info.exists is True

    def test_cache_hit(self):
        """Second lookup for same package should use cache."""
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=err) as mock_open:
            with patch("saturnday.npm_registry._reachable", True):
                info1 = check_package_exists("cached-pkg")
                info2 = check_package_exists("cached-pkg")
        assert mock_open.call_count == 1  # Only one actual request
        assert info1 is info2
        assert info1.exists is False

    def test_unreachable_registry_assumes_exists(self):
        """If registry is known unreachable, all packages assumed to exist."""
        with patch("saturnday.npm_registry._reachable", False):
            info = check_package_exists("any-package")
        assert info.exists is True

    def test_scoped_package_url_encoding(self):
        """Scoped packages must be URL-encoded (@scope%2Fname)."""
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=err) as mock_open:
            with patch("saturnday.npm_registry._reachable", True):
                check_package_exists("@fake/nonexistent")
        # Verify the URL was properly encoded
        call_args = mock_open.call_args
        req = call_args[0][0]
        assert "%2F" in req.full_url or "%2f" in req.full_url

    def test_deprecated_package_detected(self):
        """Deprecated packages should have deprecated=True."""
        resp_data = b'{"name": "old-pkg", "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"deprecated": "Use new-pkg instead"}}}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("saturnday.npm_registry.urllib.request.urlopen", return_value=mock_resp):
            with patch("saturnday.npm_registry._reachable", True):
                info = check_package_exists("old-pkg")
        assert info.exists is True
        assert info.deprecated is True


class TestRegistryReachable:
    def test_reachable_cached(self):
        """Registry reachability is cached."""
        with patch("saturnday.npm_registry._reachable", True):
            assert registry_reachable() is True

    def test_unreachable_on_error(self):
        with patch("saturnday.npm_registry._reachable", None):
            with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=ConnectionError):
                result = registry_reachable()
        assert result is False


class TestGetWeeklyDownloads:
    def test_returns_download_count(self):
        """Successful API response returns the download count."""
        resp_data = b'{"downloads": 5000000, "package": "express"}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("saturnday.npm_registry.urllib.request.urlopen", return_value=mock_resp):
            with patch("saturnday.npm_registry._reachable", True):
                count = get_weekly_downloads("express")
        assert count == 5_000_000

    def test_error_returns_negative_one(self):
        """Network error returns -1 (unavailable)."""
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=ConnectionError):
            with patch("saturnday.npm_registry._reachable", True):
                count = get_weekly_downloads("some-pkg")
        assert count == -1

    def test_cache_hit(self):
        """Second call for same package uses cache."""
        resp_data = b'{"downloads": 1000, "package": "tiny-pkg"}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("saturnday.npm_registry.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            with patch("saturnday.npm_registry._reachable", True):
                c1 = get_weekly_downloads("tiny-pkg")
                c2 = get_weekly_downloads("tiny-pkg")
        assert mock_open.call_count == 1
        assert c1 == c2 == 1000

    def test_unreachable_returns_negative_one(self):
        """If registry known unreachable, return -1."""
        with patch("saturnday.npm_registry._reachable", False):
            count = get_weekly_downloads("any-pkg")
        assert count == -1

    def test_timeout_s_parameter_accepted(self):
        """timeout_s parameter should be accepted without error."""
        with patch("saturnday.npm_registry._reachable", False):
            count = get_weekly_downloads("any-pkg", timeout_s=2.0)
        assert count == -1


class TestClearCache:
    def test_clear_resets_state(self):
        """clear_cache should reset both cache and reachability."""
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=err):
            with patch("saturnday.npm_registry._reachable", True):
                check_package_exists("test-pkg")

        clear_cache()

        # After clearing, the package should need to be looked up again
        with patch("saturnday.npm_registry.urllib.request.urlopen", side_effect=err) as mock_open:
            with patch("saturnday.npm_registry._reachable", True):
                check_package_exists("test-pkg")
        assert mock_open.call_count == 1

    def test_clear_resets_downloads_cache(self):
        """clear_cache should also clear the downloads cache."""
        resp_data = b'{"downloads": 500, "package": "test-pkg"}'
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("saturnday.npm_registry.urllib.request.urlopen", return_value=mock_resp):
            with patch("saturnday.npm_registry._reachable", True):
                get_weekly_downloads("test-pkg")

        clear_cache()

        with patch("saturnday.npm_registry.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            with patch("saturnday.npm_registry._reachable", True):
                get_weekly_downloads("test-pkg")
        assert mock_open.call_count == 1
