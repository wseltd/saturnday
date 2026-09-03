"""GitHub Releases asset inspection for release-security checks.

RS-028: Fetches release assets from the GitHub REST API, downloads them to a
temporary directory, and runs the existing release-preflight check pipeline
(REL-001 through REL-005) against each inspectable asset.

Additional GitHub-Release-specific checks:

- ``REL-GH-001`` — asset naming convention (configurable via manifest).
- ``REL-GH-002`` — expected asset set validation (must-have globs).
- ``REL-GH-003`` — SHA-256 cross-reference against a local build (optional).

Authentication order:
1. ``token`` argument.
2. ``GITHUB_TOKEN`` environment variable.
3. ``gh auth token`` subprocess.
4. Unauthenticated (rate-limited; WARN logged).

All HTTP calls go through :func:`_github_get` which uses
``urllib.request.urlopen`` so the module has no third-party dependencies.

Note: For testing, patch ``urllib.request.urlopen`` with
``unittest.mock.patch``.  No real GitHub API calls are made in unit tests.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "fetch_release_assets",
    "download_release_asset",
    "inspect_github_release",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GitHub API constants
# ---------------------------------------------------------------------------

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# ---------------------------------------------------------------------------
# Inspectable artefact extensions (mapped to artefact_type)
# ---------------------------------------------------------------------------

_INSPECTABLE: dict[str, str] = {
    ".whl": "python",
    ".tar.gz": "python",
    ".tgz": "npm",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_token(token: str | None) -> str | None:
    """Resolve a GitHub authentication token.

    Priority:
    1. *token* argument if non-empty.
    2. ``GITHUB_TOKEN`` environment variable.
    3. ``gh auth token`` subprocess.
    4. ``None`` (unauthenticated).
    """
    if token:
        return token

    env_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env_token:
        return env_token

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    _logger.warning(
        "No GitHub token found — proceeding unauthenticated. "
        "Rate limits apply for public repos. Set GITHUB_TOKEN to authenticate."
    )
    return None


def _github_get(
    url: str,
    token: str | None,
    accept: str = _DEFAULT_ACCEPT,
) -> Any:
    """Perform an authenticated GET request to the GitHub API.

    Args:
        url:    Full URL to request.
        token:  Optional Bearer token.
        accept: ``Accept`` header value.

    Returns:
        Parsed JSON response (dict or list).

    Raises:
        urllib.error.HTTPError: On HTTP 4xx/5xx.
        ValueError: On non-JSON response.
    """
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", _API_VERSION)
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req) as resp:  # noqa: S310 — controlled URL
        raw = resp.read()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Non-JSON response from {url}: {exc}") from exc


def _download_bytes(
    url: str,
    token: str | None,
) -> bytes:
    """Download raw bytes from *url* with optional Bearer auth."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"Refusing to download from non-HTTP(S) URL: {url!r}"
        )
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/octet-stream")

    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return resp.read()


def _sha256_of_bytes(data: bytes) -> str:
    """Return lowercase hex SHA-256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def _asset_artefact_type(name: str) -> str | None:
    """Return the ``artefact_type`` string for a filename, or ``None``."""
    lower = name.lower()
    for ext, atype in _INSPECTABLE.items():
        if lower.endswith(ext):
            return atype
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_release_assets(
    owner: str,
    repo: str,
    tag: str,
    token: str | None = None,
) -> list[dict]:
    """Fetch release asset metadata from the GitHub API.

    Uses ``GET /repos/{owner}/{repo}/releases/tags/{tag}``.

    Args:
        owner: GitHub repository owner (user or org name).
        repo:  GitHub repository name.
        tag:   Release tag (e.g. ``"v1.1.01"``).
        token: Optional GitHub token.  Falls back to env / ``gh auth token``.

    Returns:
        List of asset dicts.  Each dict contains at minimum:
        ``id``, ``name``, ``size``, ``browser_download_url``,
        ``content_type``, ``state``.

    Raises:
        urllib.error.HTTPError: On API error (e.g. 404 release not found).
        ValueError: On unexpected API response structure.
    """
    resolved = _resolve_token(token)
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/releases/tags/{tag}"
    _logger.debug("Fetching release metadata: %s", url)

    data = _github_get(url, resolved)

    if not isinstance(data, dict):
        raise ValueError(f"Expected dict from {url}, got {type(data).__name__}")

    assets: list[dict] = data.get("assets", [])
    _logger.info(
        "GitHub release %s/%s@%s has %d asset(s)", owner, repo, tag, len(assets)
    )
    return assets


def download_release_asset(
    download_url: str,
    target_dir: Path,
    token: str | None = None,
) -> Path:
    """Download a release asset to *target_dir*.

    Args:
        download_url: ``browser_download_url`` from the asset metadata.
        target_dir:   Directory to save the file into.
        token:        Optional GitHub token for private repos.

    Returns:
        Local :class:`~pathlib.Path` of the downloaded file.

    Raises:
        OSError: If the file cannot be written.
        urllib.error.URLError: On download failure.
    """
    resolved = _resolve_token(token)
    filename = download_url.rstrip("/").split("/")[-1]
    target = Path(target_dir) / filename

    _logger.debug("Downloading asset %s → %s", download_url, target)
    t0 = time.monotonic()
    data = _download_bytes(download_url, resolved)
    elapsed = time.monotonic() - t0

    target.write_bytes(data)
    _logger.info(
        "Downloaded %s (%d bytes, %.2fs)", filename, len(data), elapsed
    )
    return target


# ---------------------------------------------------------------------------
# Per-asset check helpers
# ---------------------------------------------------------------------------


def _check_asset_naming(
    asset: dict,
    manifest: dict | None,
    rule_id: str = "REL-GH-001",
) -> dict:
    """Check whether the asset name matches the configured naming patterns.

    Args:
        asset:    GitHub asset metadata dict.
        manifest: Release manifest dict from ``.saturnday-release-manifest.yaml``.
        rule_id:  Check rule ID (for test overrides).

    Returns:
        Finding dict with keys: ``kind``, ``status``, ``message``,
        ``asset_name``, ``check_id``.
    """
    name: str = asset.get("name", "")
    patterns: list[str] = []
    if manifest:
        raw = manifest.get("github_release_asset_patterns", [])
        if isinstance(raw, list):
            patterns = [str(p) for p in raw]

    if not patterns:
        return {
            "kind": "asset_naming_skipped",
            "status": "PASS",
            "message": "No github_release_asset_patterns configured — naming check skipped",
            "asset_name": name,
            "check_id": rule_id,
        }

    matched = any(fnmatch.fnmatch(name, pat) for pat in patterns)
    if matched:
        return {
            "kind": "asset_naming_pass",
            "status": "PASS",
            "message": f"Asset '{name}' matches naming pattern",
            "asset_name": name,
            "check_id": rule_id,
        }
    return {
        "kind": "asset_naming_violation",
        "status": "WARN",
        "message": (
            f"Asset '{name}' does not match any configured naming pattern: "
            f"{patterns}"
        ),
        "asset_name": name,
        "check_id": rule_id,
        "patterns": patterns,
    }


def _check_expected_assets(
    asset_names: list[str],
    manifest: dict | None,
    rule_id: str = "REL-GH-002",
) -> list[dict]:
    """Check that the expected asset set is present.

    Args:
        asset_names: List of asset filenames in the GitHub release.
        manifest:    Release manifest dict.
        rule_id:     Check rule ID.

    Returns:
        List of finding dicts (one per required glob that is missing).
    """
    required: list[str] = []
    if manifest:
        raw = manifest.get("github_release_required_assets", [])
        if isinstance(raw, list):
            required = [str(p) for p in raw]

    if not required:
        return []

    findings: list[dict] = []
    for pattern in required:
        if any(fnmatch.fnmatch(n, pattern) for n in asset_names):
            findings.append(
                {
                    "kind": "required_asset_present",
                    "status": "PASS",
                    "message": f"Required asset pattern '{pattern}' satisfied",
                    "pattern": pattern,
                    "check_id": rule_id,
                }
            )
        else:
            findings.append(
                {
                    "kind": "required_asset_missing",
                    "status": "FAIL",
                    "message": (
                        f"Required asset pattern '{pattern}' has no matching "
                        f"asset in the GitHub release"
                    ),
                    "pattern": pattern,
                    "check_id": rule_id,
                }
            )
    return findings


def _check_sha256_cross_reference(
    asset_path: Path,
    asset_name: str,
    local_build_dir: Path | None,
    rule_id: str = "REL-GH-003",
) -> dict:
    """Compare the downloaded asset SHA-256 against a local build.

    Args:
        asset_path:     Path to the downloaded asset.
        asset_name:     Original filename (for locating the local copy).
        local_build_dir: Directory containing locally-built artefacts.
        rule_id:        Check rule ID.

    Returns:
        Finding dict.  Status is SKIPPED when *local_build_dir* is None.
    """
    if local_build_dir is None:
        return {
            "kind": "sha256_cross_reference_skipped",
            "status": "SKIPPED",
            "message": "No local build directory provided — SHA-256 cross-reference skipped",
            "check_id": rule_id,
        }

    local_copy = Path(local_build_dir) / asset_name
    if not local_copy.is_file():
        return {
            "kind": "sha256_cross_reference_skipped",
            "status": "SKIPPED",
            "message": f"Local copy not found at {local_copy} — SHA-256 cross-reference skipped",
            "check_id": rule_id,
        }

    downloaded_sha = _sha256_of_bytes(asset_path.read_bytes())
    local_sha = _sha256_of_bytes(local_copy.read_bytes())

    if downloaded_sha == local_sha:
        return {
            "kind": "sha256_cross_reference_match",
            "status": "PASS",
            "message": f"SHA-256 of downloaded '{asset_name}' matches local build",
            "sha256": downloaded_sha,
            "check_id": rule_id,
        }
    return {
        "kind": "sha256_cross_reference_mismatch",
        "status": "FAIL",
        "message": (
            f"SHA-256 mismatch for '{asset_name}': "
            f"downloaded={downloaded_sha} local={local_sha}"
        ),
        "downloaded_sha256": downloaded_sha,
        "local_sha256": local_sha,
        "check_id": rule_id,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def inspect_github_release(
    owner: str,
    repo: str,
    tag: str,
    token: str | None = None,
    manifest: dict | None = None,
    output_dir: Optional[Path] = None,
    local_build_dir: Optional[Path] = None,
) -> list[dict]:
    """Fetch, download, and inspect all assets in a GitHub Release.

    For each asset:
    - Runs asset naming check (REL-GH-001).
    - Downloads the asset.
    - If the asset is an inspectable artefact type (.whl, .tar.gz, .tgz),
      runs the full release-preflight pipeline and captures results.
    - Otherwise records a WARN finding (cannot inspect unknown type).
    - Runs SHA-256 cross-reference check if *local_build_dir* is provided.

    Expected-asset-set check (REL-GH-002) is performed against the full
    asset list before downloading any files.

    Args:
        owner:          GitHub repository owner.
        repo:           GitHub repository name.
        tag:            Release tag.
        token:          Optional GitHub token.
        manifest:       Parsed release manifest dict (allowlist/policy).
        output_dir:     Directory to write per-asset evidence.  When ``None``,
                        a temporary directory is used and cleaned up.
        local_build_dir: Optional path to locally-built artefacts for
                         SHA-256 cross-reference (REL-GH-003).

    Returns:
        List of per-asset result dicts.  Each dict has:
        ``asset_name``, ``asset_size``, ``artefact_type`` (or ``None``),
        ``findings``, ``preflight_disposition`` (or ``None``),
        ``downloaded_path`` (str).
    """
    resolved_token = _resolve_token(token)

    # ------------------------------------------------------------------
    # Step 1: Fetch asset metadata.
    # ------------------------------------------------------------------
    try:
        assets = fetch_release_assets(owner, repo, tag, token=resolved_token)
    except urllib.error.HTTPError as exc:
        _logger.error(
            "GitHub API error fetching %s/%s@%s: %s", owner, repo, tag, exc
        )
        return [
            {
                "asset_name": None,
                "error": str(exc),
                "findings": [
                    {
                        "kind": "api_error",
                        "status": "FAIL",
                        "message": f"GitHub API error: {exc}",
                    }
                ],
            }
        ]
    except Exception as exc:
        _logger.error("Unexpected error fetching release assets: %s", exc)
        return [
            {
                "asset_name": None,
                "error": str(exc),
                "findings": [
                    {
                        "kind": "fetch_error",
                        "status": "FAIL",
                        "message": f"Failed to fetch release assets: {exc}",
                    }
                ],
            }
        ]

    # ------------------------------------------------------------------
    # Step 2: Expected-asset-set check (REL-GH-002).
    # ------------------------------------------------------------------
    asset_names = [a.get("name", "") for a in assets]
    expected_findings = _check_expected_assets(asset_names, manifest)

    # ------------------------------------------------------------------
    # Step 3: Download and inspect each asset.
    # ------------------------------------------------------------------
    use_temp = output_dir is None
    tmp_ctx: Any = None
    if use_temp:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="saturnday_gh_release_")
        dl_dir = Path(tmp_ctx.name)
    else:
        dl_dir = Path(output_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []

    try:
        for asset in assets:
            name: str = asset.get("name", "")
            size: int = asset.get("size", 0)
            download_url: str = (
                asset.get("browser_download_url") or asset.get("url") or ""
            )
            asset_result: dict = {
                "asset_name": name,
                "asset_size": size,
                "artefact_type": _asset_artefact_type(name),
                "findings": [],
                "preflight_disposition": None,
                "downloaded_path": "",
            }

            # --- Naming check ---
            naming_finding = _check_asset_naming(asset, manifest)
            asset_result["findings"].append(naming_finding)

            # --- Download ---
            if not download_url:
                asset_result["findings"].append(
                    {
                        "kind": "download_skipped",
                        "status": "WARN",
                        "message": f"No download URL for asset '{name}' — skipped",
                    }
                )
                results.append(asset_result)
                continue

            try:
                local_path = download_release_asset(
                    download_url, dl_dir, token=resolved_token
                )
                asset_result["downloaded_path"] = str(local_path)
            except Exception as exc:
                _logger.warning("Failed to download asset '%s': %s", name, exc)
                asset_result["findings"].append(
                    {
                        "kind": "download_error",
                        "status": "WARN",
                        "message": f"Download failed for '{name}': {exc}",
                    }
                )
                results.append(asset_result)
                continue

            # --- Inspect if known artefact type ---
            atype = _asset_artefact_type(name)
            if atype is not None:
                try:
                    from saturnday.release.orchestrator import run_release_preflight

                    preflight_output = dl_dir / f"evidence_{name}"
                    preflight_output.mkdir(parents=True, exist_ok=True)

                    kwargs: dict[str, Any] = {
                        "repo_path": dl_dir,
                        "artefact_type": atype,
                        "output_dir": preflight_output,
                    }
                    if atype == "python":
                        if name.endswith(".whl"):
                            kwargs["wheel_path"] = local_path
                        else:
                            kwargs["sdist_path"] = local_path
                    elif atype == "npm":
                        kwargs["tarball_path"] = local_path

                    pf_result = run_release_preflight(**kwargs)
                    asset_result["preflight_disposition"] = pf_result.disposition
                    if pf_result.evidence_pack:
                        pack = pf_result.evidence_pack
                        for cr in getattr(pack, "check_results", []):
                            for f in getattr(cr, "findings", []):
                                asset_result["findings"].append(f)
                except Exception as exc:
                    _logger.warning(
                        "Preflight failed for '%s': %s", name, exc
                    )
                    asset_result["findings"].append(
                        {
                            "kind": "preflight_error",
                            "status": "WARN",
                            "message": f"Preflight raised an exception for '{name}': {exc}",
                        }
                    )
            else:
                asset_result["findings"].append(
                    {
                        "kind": "uninspectable_asset_type",
                        "status": "WARN",
                        "message": (
                            f"Asset '{name}' has an unrecognised artefact type — "
                            f"cannot inspect content (note presence for review)"
                        ),
                    }
                )

            # --- SHA-256 cross-reference (REL-GH-003) ---
            sha_finding = _check_sha256_cross_reference(
                local_path, name, local_build_dir
            )
            asset_result["findings"].append(sha_finding)

            results.append(asset_result)

    finally:
        if use_temp and tmp_ctx is not None:
            try:
                tmp_ctx.cleanup()
            except Exception:
                pass

    # Attach expected-asset findings to results as a synthetic entry.
    if expected_findings:
        results.append(
            {
                "asset_name": "__expected_assets__",
                "asset_size": 0,
                "artefact_type": None,
                "findings": expected_findings,
                "preflight_disposition": None,
                "downloaded_path": "",
            }
        )

    return results
