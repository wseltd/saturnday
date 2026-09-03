"""npm registry client for package existence verification.

Used by hallucinated_imports_ts and typosquat_ts checks to verify whether
an imported package actually exists on the npm registry.

Design principles:
- Never false-positive on network issues: any non-404 error → assume exists
- Process-lifetime cache: same package never checked twice per process
- Graceful degradation: if registry unreachable, all lookups return exists=True
- No dependencies beyond stdlib (urllib.request)
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Registry endpoint — abbreviated metadata is much smaller than full
_REGISTRY_URL = "https://registry.npmjs.org"
_TIMEOUT_S = 5

# Process-lifetime cache: package_name → NpmPackageInfo
_cache: dict[str, NpmPackageInfo] = {}

# None = not yet tested, True = reachable, False = unreachable
_reachable: bool | None = None


@dataclass(frozen=True)
class NpmPackageInfo:
    """Minimal package metadata from npm registry."""
    name: str
    exists: bool
    description: str = ""
    latest_version: str = ""
    deprecated: bool = False


def registry_reachable() -> bool:
    """Check if the npm registry is reachable. Cached for process lifetime."""
    global _reachable
    if _reachable is not None:
        return _reachable

    try:
        req = urllib.request.Request(
            f"{_REGISTRY_URL}/-/ping",
            method="GET",
        )
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            _reachable = resp.status == 200
    except Exception:
        _reachable = False

    return _reachable


def check_package_exists(name: str, *, timeout_s: float | None = None) -> NpmPackageInfo:
    """Check if a package exists on the npm registry.

    Returns NpmPackageInfo with exists=True/False.

    timeout_s: Override the default HTTP timeout. When called from the scanner,
    this is the remaining per-skill budget. Falls back to _TIMEOUT_S.

    On any error OTHER than 404, assumes the package exists
    (never false-positive on network issues).
    """
    # Check cache first
    if name in _cache:
        return _cache[name]

    # If registry known unreachable, assume exists
    if _reachable is False:
        info = NpmPackageInfo(name=name, exists=True)
        _cache[name] = info
        return info

    effective_timeout = timeout_s if timeout_s is not None else _TIMEOUT_S

    # URL-encode scoped packages: @scope/name → @scope%2Fname
    if name.startswith("@"):
        encoded = urllib.parse.quote(name, safe="@")
    else:
        encoded = urllib.parse.quote(name, safe="")

    url = f"{_REGISTRY_URL}/{encoded}"

    try:
        req = urllib.request.Request(url, method="GET")
        # Use abbreviated metadata — much smaller response
        req.add_header("Accept", "application/vnd.npm.install-v1+json")

        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            data = json.loads(resp.read())
            versions = data.get("versions", {})
            latest_ver = data.get("dist-tags", {}).get("latest", "")
            deprecated = False
            if latest_ver and latest_ver in versions:
                deprecated = bool(versions[latest_ver].get("deprecated"))

            info = NpmPackageInfo(
                name=data.get("name", name),
                exists=True,
                latest_version=latest_ver,
                deprecated=deprecated,
            )
            _cache[name] = info
            return info

    except urllib.error.HTTPError as e:
        if e.code == 404:
            info = NpmPackageInfo(name=name, exists=False)
            _cache[name] = info
            return info
        # Any other HTTP error — assume exists (never false-positive)
        logger.debug("npm registry HTTP %d for %s, assuming exists", e.code, name)
        info = NpmPackageInfo(name=name, exists=True)
        _cache[name] = info
        return info

    except Exception as exc:
        # Network error, timeout, DNS failure — assume exists
        logger.debug("npm registry error for %s: %s, assuming exists", name, exc)
        info = NpmPackageInfo(name=name, exists=True)
        _cache[name] = info
        return info


# Download count cache: package_name → weekly downloads (or -1 if unavailable)
_downloads_cache: dict[str, int] = {}

_DOWNLOADS_API_URL = "https://api.npmjs.org/downloads/point/last-week"


def get_weekly_downloads(name: str, *, timeout_s: float | None = None) -> int:
    """Get weekly download count for a package.

    Returns the download count, or -1 if unavailable.
    Uses the npm downloads API (separate from registry metadata).
    """
    if name in _downloads_cache:
        return _downloads_cache[name]

    if _reachable is False:
        _downloads_cache[name] = -1
        return -1

    effective_timeout = timeout_s if timeout_s is not None else _TIMEOUT_S

    # URL-encode scoped packages
    if name.startswith("@"):
        encoded = urllib.parse.quote(name, safe="@")
    else:
        encoded = urllib.parse.quote(name, safe="")

    url = f"{_DOWNLOADS_API_URL}/{encoded}"

    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            data = json.loads(resp.read())
            downloads = int(data.get("downloads", -1))
            _downloads_cache[name] = downloads
            return downloads
    except Exception as exc:
        logger.debug("npm downloads API error for %s: %s", name, exc)
        _downloads_cache[name] = -1
        return -1


def clear_cache() -> None:
    """Clear the process-lifetime cache. Mainly for testing."""
    global _reachable
    _cache.clear()
    _downloads_cache.clear()
    _reachable = None
