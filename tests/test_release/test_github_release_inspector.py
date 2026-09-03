"""Tests for RS-028: GitHub Releases asset inspection.

Covers:
- fetch_release_assets parses GitHub API response correctly
- download_release_asset saves file to target_dir
- inspect_github_release orchestrates fetch + download + preflight
- Asset naming check: matching pattern → PASS
- Asset naming check: non-matching name → WARN
- Expected asset set: present → PASS
- Expected asset set: missing wheel → FAIL
- SHA-256 cross-reference: match → PASS
- SHA-256 cross-reference: mismatch → FAIL
- SHA-256 cross-reference: no local build dir → SKIPPED
- Authentication: GITHUB_TOKEN env var
- Authentication: gh CLI fallback (mock subprocess)
- Authentication: unauthenticated (warn + proceed)
- Unknown asset type (.exe) → WARN with cannot-inspect note
- CLI --github-release argument accepted and parsed correctly
- CLI error on malformed OWNER/REPO:TAG
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_asset(
    name: str = "saturnday-1.1.01-py3-none-any.whl",
    size: int = 102400,
    content_type: str = "application/zip",
) -> dict:
    """Build a minimal GitHub API asset dict."""
    return {
        "id": 12345,
        "name": name,
        "size": size,
        "content_type": content_type,
        "state": "uploaded",
        "browser_download_url": f"https://github.com/owner/repo/releases/download/v1.0/{name}",
        "url": f"https://api.github.com/repos/owner/repo/releases/assets/12345",
    }


def _make_release_response(assets: list[dict] | None = None) -> dict:
    """Build a minimal GitHub Releases API response dict."""
    if assets is None:
        assets = [_make_asset()]
    return {
        "id": 99,
        "tag_name": "v1.1.01",
        "name": "v1.1.01",
        "prerelease": False,
        "draft": False,
        "assets": assets,
    }


def _encode_response(data: dict) -> bytes:
    return json.dumps(data).encode("utf-8")


# ---------------------------------------------------------------------------
# fetch_release_assets
# ---------------------------------------------------------------------------


class TestFetchReleaseAssets:
    def test_parses_assets_from_response(self) -> None:
        from saturnday.release.github_releases import fetch_release_assets

        mock_resp = MagicMock()
        mock_resp.read.return_value = _encode_response(
            _make_release_response([_make_asset("wheel.whl"), _make_asset("sdist.tar.gz")])
        )
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assets = fetch_release_assets("owner", "repo", "v1.0")

        assert len(assets) == 2
        names = [a["name"] for a in assets]
        assert "wheel.whl" in names
        assert "sdist.tar.gz" in names

    def test_returns_empty_list_for_no_assets(self) -> None:
        from saturnday.release.github_releases import fetch_release_assets

        mock_resp = MagicMock()
        mock_resp.read.return_value = _encode_response(_make_release_response([]))
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assets = fetch_release_assets("owner", "repo", "v1.0")

        assert assets == []

    def test_asset_dict_has_expected_keys(self) -> None:
        from saturnday.release.github_releases import fetch_release_assets

        asset_data = _make_asset()
        mock_resp = MagicMock()
        mock_resp.read.return_value = _encode_response(_make_release_response([asset_data]))
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            assets = fetch_release_assets("owner", "repo", "v1.0")

        a = assets[0]
        for key in ("name", "size", "browser_download_url", "content_type"):
            assert key in a, f"Asset dict missing key: {key}"

    def test_uses_bearer_token_in_header(self) -> None:
        """Token passed as arg ends up in the Authorization header."""
        from saturnday.release.github_releases import _resolve_token

        # Verify the token resolver returns the explicit token unchanged.
        resolved = _resolve_token("mytoken")
        assert resolved == "mytoken"

        # Verify _github_get passes Authorization header when token is set.
        import urllib.request as _urlreq

        mock_resp = MagicMock()
        mock_resp.read.return_value = _encode_response(_make_release_response())
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        sent_request: list[_urlreq.Request] = []

        def fake_urlopen(req: _urlreq.Request, **kwargs):
            sent_request.append(req)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            from saturnday.release.github_releases import fetch_release_assets
            fetch_release_assets("owner", "repo", "v1.0", token="mytoken")

        assert sent_request, "urlopen was never called"
        req = sent_request[0]
        auth = req.get_header("Authorization")
        assert auth is not None, "Authorization header missing from request"
        assert "mytoken" in auth


# ---------------------------------------------------------------------------
# download_release_asset
# ---------------------------------------------------------------------------


class TestDownloadReleaseAsset:
    def test_saves_file_to_target_dir(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import download_release_asset

        fake_content = b"fake wheel bytes"
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_content
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = download_release_asset(
                "https://github.com/releases/download/v1/wheel.whl",
                tmp_path,
            )

        assert result.is_file()
        assert result.name == "wheel.whl"
        assert result.read_bytes() == fake_content

    def test_filename_derived_from_url(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import download_release_asset

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"data"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = download_release_asset(
                "https://github.com/releases/download/v1.0/saturnday-1.0.tar.gz",
                tmp_path,
            )

        assert result.name == "saturnday-1.0.tar.gz"

    def test_file_content_matches_download(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import download_release_asset

        content = b"unique content bytes 12345"
        mock_resp = MagicMock()
        mock_resp.read.return_value = content
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = download_release_asset(
                "https://example.com/releases/v1/file.whl", tmp_path
            )

        assert result.read_bytes() == content


# ---------------------------------------------------------------------------
# Asset naming check
# ---------------------------------------------------------------------------


class TestAssetNamingCheck:
    def test_matching_pattern_returns_pass(self) -> None:
        from saturnday.release.github_releases import _check_asset_naming

        asset = _make_asset("saturnday-1.1.01-py3-none-any.whl")
        manifest = {"github_release_asset_patterns": ["*.whl", "*.tar.gz"]}
        finding = _check_asset_naming(asset, manifest)
        assert finding["status"] == "PASS"

    def test_non_matching_name_returns_warn(self) -> None:
        from saturnday.release.github_releases import _check_asset_naming

        asset = _make_asset("setup.exe")
        manifest = {"github_release_asset_patterns": ["*.whl", "*.tar.gz"]}
        finding = _check_asset_naming(asset, manifest)
        assert finding["status"] == "WARN"
        assert "setup.exe" in finding["message"]

    def test_no_patterns_configured_returns_pass(self) -> None:
        from saturnday.release.github_releases import _check_asset_naming

        asset = _make_asset("anything.whl")
        finding = _check_asset_naming(asset, manifest=None)
        assert finding["status"] == "PASS"
        assert "skipped" in finding["message"].lower()


# ---------------------------------------------------------------------------
# Expected asset set check
# ---------------------------------------------------------------------------


class TestExpectedAssetSetCheck:
    def test_required_asset_present_returns_pass(self) -> None:
        from saturnday.release.github_releases import _check_expected_assets

        manifest = {"github_release_required_assets": ["*.whl"]}
        findings = _check_expected_assets(["saturnday-1.0-py3.whl", "saturnday-1.0.tar.gz"], manifest)
        statuses = [f["status"] for f in findings]
        assert "PASS" in statuses
        assert "FAIL" not in statuses

    def test_missing_wheel_returns_fail(self) -> None:
        from saturnday.release.github_releases import _check_expected_assets

        manifest = {"github_release_required_assets": ["*.whl", "*.tar.gz"]}
        findings = _check_expected_assets(["saturnday-1.0.tar.gz"], manifest)
        fail_findings = [f for f in findings if f["status"] == "FAIL"]
        assert len(fail_findings) >= 1
        assert any("*.whl" in f.get("message", "") or "*.whl" in f.get("pattern", "") for f in fail_findings)

    def test_no_required_assets_returns_empty(self) -> None:
        from saturnday.release.github_releases import _check_expected_assets

        findings = _check_expected_assets(["wheel.whl"], manifest=None)
        assert findings == []

    def test_both_required_assets_present(self) -> None:
        from saturnday.release.github_releases import _check_expected_assets

        manifest = {"github_release_required_assets": ["*.whl", "*.tar.gz"]}
        findings = _check_expected_assets(
            ["saturnday-1.0-py3.whl", "saturnday-1.0.tar.gz"], manifest
        )
        assert all(f["status"] == "PASS" for f in findings)


# ---------------------------------------------------------------------------
# SHA-256 cross-reference check
# ---------------------------------------------------------------------------


class TestSha256CrossReferenceCheck:
    def test_no_local_build_dir_returns_skipped(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import _check_sha256_cross_reference

        asset_file = tmp_path / "wheel.whl"
        asset_file.write_bytes(b"wheel content")
        finding = _check_sha256_cross_reference(asset_file, "wheel.whl", None)
        assert finding["status"] == "SKIPPED"

    def test_matching_sha256_returns_pass(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import _check_sha256_cross_reference

        content = b"identical content"
        asset_file = tmp_path / "wheel.whl"
        asset_file.write_bytes(content)

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "wheel.whl").write_bytes(content)

        finding = _check_sha256_cross_reference(asset_file, "wheel.whl", local_dir)
        assert finding["status"] == "PASS"
        assert "match" in finding["kind"]

    def test_mismatched_sha256_returns_fail(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import _check_sha256_cross_reference

        asset_file = tmp_path / "wheel.whl"
        asset_file.write_bytes(b"downloaded content A")

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        (local_dir / "wheel.whl").write_bytes(b"local content B")

        finding = _check_sha256_cross_reference(asset_file, "wheel.whl", local_dir)
        assert finding["status"] == "FAIL"
        assert "mismatch" in finding["kind"]

    def test_local_copy_not_found_returns_skipped(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import _check_sha256_cross_reference

        asset_file = tmp_path / "wheel.whl"
        asset_file.write_bytes(b"content")

        local_dir = tmp_path / "local"
        local_dir.mkdir()
        # No wheel.whl in local_dir.

        finding = _check_sha256_cross_reference(asset_file, "wheel.whl", local_dir)
        assert finding["status"] == "SKIPPED"


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------


class TestAuthenticationFallback:
    def test_github_token_env_var_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.release.github_releases import _resolve_token

        monkeypatch.setenv("GITHUB_TOKEN", "env-token-xyz")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        result = _resolve_token(None)
        assert result == "env-token-xyz"

    def test_explicit_token_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.release.github_releases import _resolve_token

        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        result = _resolve_token("explicit-token")
        assert result == "explicit-token"

    def test_gh_cli_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from saturnday.release.github_releases import _resolve_token

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "gh-cli-token\n"

        with patch("subprocess.run", return_value=mock_result):
            result = _resolve_token(None)

        assert result == "gh-cli-token"

    def test_unauthenticated_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from saturnday.release.github_releases import _resolve_token

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = _resolve_token(None)

        assert result is None


# ---------------------------------------------------------------------------
# Unknown asset type
# ---------------------------------------------------------------------------


class TestUnknownAssetType:
    def test_unknown_type_yields_warn_finding(self, tmp_path: Path) -> None:
        """An .exe asset should produce a WARN 'uninspectable_asset_type' finding."""
        from saturnday.release.github_releases import inspect_github_release

        exe_asset = _make_asset("setup.exe", content_type="application/octet-stream")
        release_resp = _make_release_response([exe_asset])

        # Mock the full HTTP stack: API call + download.
        fake_exe_bytes = b"MZ fake exe"
        download_resp = MagicMock()
        download_resp.read.return_value = fake_exe_bytes
        download_resp.__enter__ = lambda s: s
        download_resp.__exit__ = MagicMock(return_value=False)

        api_resp = MagicMock()
        api_resp.read.return_value = _encode_response(release_resp)
        api_resp.__enter__ = lambda s: s
        api_resp.__exit__ = MagicMock(return_value=False)

        responses = [api_resp, download_resp]

        with patch("urllib.request.urlopen", side_effect=responses):
            results = inspect_github_release("owner", "repo", "v1.0", output_dir=tmp_path)

        # Find the .exe asset result.
        exe_result = next(
            (r for r in results if r.get("asset_name") == "setup.exe"), None
        )
        assert exe_result is not None
        kinds = [f.get("kind") for f in exe_result["findings"]]
        assert "uninspectable_asset_type" in kinds

        warn_finding = next(
            f for f in exe_result["findings"]
            if f.get("kind") == "uninspectable_asset_type"
        )
        assert warn_finding["status"] == "WARN"


# ---------------------------------------------------------------------------
# inspect_github_release orchestration
# ---------------------------------------------------------------------------


class TestInspectGithubRelease:
    def test_returns_list(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import inspect_github_release

        mock_resp = MagicMock()
        mock_resp.read.return_value = _encode_response(_make_release_response([]))
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            results = inspect_github_release("owner", "repo", "v1.0", output_dir=tmp_path)

        assert isinstance(results, list)

    def test_each_result_has_expected_keys(self, tmp_path: Path) -> None:
        from saturnday.release.github_releases import inspect_github_release

        asset = _make_asset("saturnday-1.0-py3.whl")
        fake_whl_bytes = b"PK fake wheel"

        api_resp = MagicMock()
        api_resp.read.return_value = _encode_response(_make_release_response([asset]))
        api_resp.__enter__ = lambda s: s
        api_resp.__exit__ = MagicMock(return_value=False)

        dl_resp = MagicMock()
        dl_resp.read.return_value = fake_whl_bytes
        dl_resp.__enter__ = lambda s: s
        dl_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=[api_resp, dl_resp]):
            # Preflight will fail on the fake wheel — that's fine, just check structure.
            results = inspect_github_release("owner", "repo", "v1.0", output_dir=tmp_path)

        for r in results:
            if r.get("asset_name") == "__expected_assets__":
                continue
            for key in ("asset_name", "findings"):
                assert key in r, f"Result missing key '{key}': {r}"
            assert isinstance(r["findings"], list)


# ---------------------------------------------------------------------------
# CLI argument tests
# ---------------------------------------------------------------------------


class TestCliGithubReleaseArgument:
    def test_github_release_flag_accepted_by_parser(self) -> None:
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "release-preflight",
                "--github-release",
                "honouralexwill/saturnday:v1.1.01",
            ]
        )
        assert args.github_release == "honouralexwill/saturnday:v1.1.01"

    def test_github_release_default_is_none(self) -> None:
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["release-preflight", "--repo", "."])
        assert getattr(args, "github_release", None) is None

    def test_github_token_flag_accepted(self) -> None:
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "release-preflight",
                "--github-release",
                "owner/repo:v1",
                "--github-token",
                "ghp_test",
            ]
        )
        assert args.github_token == "ghp_test"

    def test_local_build_dir_flag_accepted(self) -> None:
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "release-preflight",
                "--github-release",
                "owner/repo:v1",
                "--local-build-dir",
                "/tmp/dist",
            ]
        )
        assert args.local_build_dir == "/tmp/dist"

    def test_malformed_github_release_spec_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Malformed OWNER/REPO:TAG should exit with code 2."""
        from saturnday.cli import main

        ret = main(["release-preflight", "--github-release", "not-valid"])
        assert ret == 2
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_missing_repo_in_spec_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from saturnday.cli import main

        ret = main(["release-preflight", "--github-release", "owner:v1"])
        assert ret == 2
