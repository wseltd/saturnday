"""Tests for saturnday.release.npm_builder (RS-025).

Covers:
- NpmBuildResult dataclass structure
- Missing npm binary detection (mocked subprocess)
- Missing package.json detection (no subprocess)
- Invalid repo_path detection (ValueError)
- Successful npm pack result parsing (integration, marked)
- npm pack --json parse failure degrades gracefully
- Build failure (non-zero exit) returns structured result, no raise
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.release.npm_builder import NpmBuildResult, build_npm_artefact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_npm_project(tmp_path: Path) -> Path:
    """Create a minimal npm project with package.json in a temp directory."""
    pkg = {
        "name": "saturnday-test-fixture",
        "version": "1.0.0",
        "description": "Minimal fixture for RS-025 tests",
        "main": "index.js",
        "license": "MIT",
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (tmp_path / "index.js").write_text(
        "module.exports = { hello: () => 'world' };\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def npm_pack_json_output() -> str:
    """Minimal valid npm pack --json stdout."""
    payload = [
        {
            "filename": "saturnday-test-fixture-1.0.0.tgz",
            "name": "saturnday-test-fixture",
            "version": "1.0.0",
            "files": [
                {"path": "package.json", "size": 123, "mode": 33188},
                {"path": "index.js", "size": 45, "mode": 33188},
            ],
            "entryCount": 2,
            "bundled": [],
        }
    ]
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Dataclass structure
# ---------------------------------------------------------------------------


class TestNpmBuildResultDataclass:
    """NpmBuildResult must carry all required fields."""

    def test_fields_present(self) -> None:
        result = NpmBuildResult(tarball_path=None)
        assert result.tarball_path is None
        assert result.npm_file_list == []
        assert result.pack_json is None
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.elapsed_s == 0.0

    def test_with_tarball_path(self, tmp_path: Path) -> None:
        fake_tgz = tmp_path / "pkg-1.0.0.tgz"
        fake_tgz.touch()
        result = NpmBuildResult(
            tarball_path=fake_tgz,
            npm_file_list=["package.json", "index.js"],
            exit_code=0,
            elapsed_s=1.5,
        )
        assert result.tarball_path == fake_tgz
        assert "index.js" in result.npm_file_list
        assert result.elapsed_s == 1.5


# ---------------------------------------------------------------------------
# Precondition checks (no subprocess involved)
# ---------------------------------------------------------------------------


class TestBuildNpmArtefactPreconditions:
    """Precondition errors must raise ValueError before subprocess."""

    def test_repo_path_does_not_exist(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            build_npm_artefact(repo_path=tmp_path / "nonexistent")

    def test_repo_path_is_file(self, tmp_path: Path) -> None:
        f = tmp_path / "not_a_dir.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="not a directory"):
            build_npm_artefact(repo_path=f)

    def test_missing_package_json_returns_failure(self, tmp_path: Path) -> None:
        """No package.json → returns exit_code=1, no raise."""
        result = build_npm_artefact(repo_path=tmp_path)
        assert result.exit_code == 1
        assert result.tarball_path is None
        assert "package.json" in result.stderr.lower()
        assert result.npm_file_list == []


# ---------------------------------------------------------------------------
# npm binary not found
# ---------------------------------------------------------------------------


class TestNpmNotInstalled:
    """If npm is not on PATH, return a clear error result — no crash."""

    def test_npm_not_found_returns_failure(
        self, minimal_npm_project: Path
    ) -> None:
        with patch("saturnday.release.npm_builder.shutil.which", return_value=None):
            result = build_npm_artefact(repo_path=minimal_npm_project)
        assert result.exit_code == 1
        assert result.tarball_path is None
        assert "npm" in result.stderr.lower()
        assert "not installed" in result.stderr.lower() or "not found" in result.stderr.lower()
        assert result.npm_file_list == []
        assert result.pack_json is None


# ---------------------------------------------------------------------------
# Build failure (non-zero exit)
# ---------------------------------------------------------------------------


class TestBuildFailure:
    """Non-zero exit code from npm must be returned, not raised."""

    def test_non_zero_exit_code_returned(
        self, minimal_npm_project: Path
    ) -> None:
        fake_proc = MagicMock()
        fake_proc.returncode = 2
        fake_proc.stdout = ""
        fake_proc.stderr = "npm ERR! some error"

        with patch("saturnday.release.npm_builder.shutil.which", return_value="/usr/bin/npm"):
            with patch("subprocess.run", return_value=fake_proc):
                result = build_npm_artefact(repo_path=minimal_npm_project)

        assert result.exit_code == 2
        assert result.tarball_path is None
        assert "some error" in result.stderr
        assert result.npm_file_list == []

    def test_timeout_returns_failure(self, minimal_npm_project: Path) -> None:
        with patch("saturnday.release.npm_builder.shutil.which", return_value="/usr/bin/npm"):
            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["npm"], timeout=120),
            ):
                result = build_npm_artefact(repo_path=minimal_npm_project)

        assert result.exit_code == 1
        assert "timed out" in result.stderr.lower()
        assert result.tarball_path is None


# ---------------------------------------------------------------------------
# JSON parse degradation
# ---------------------------------------------------------------------------


class TestPackJsonParsing:
    """Malformed --json output should degrade gracefully."""

    def test_malformed_json_still_returns_success_if_tarball_found(
        self, minimal_npm_project: Path, tmp_path: Path
    ) -> None:
        """If npm pack succeeds but stdout JSON is invalid, file list is empty."""
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        fake_tgz = out_dir / "saturnday-test-fixture-1.0.0.tgz"
        fake_tgz.touch()

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "not valid json {{{"
        fake_proc.stderr = ""

        with patch("saturnday.release.npm_builder.shutil.which", return_value="/usr/bin/npm"):
            with patch("subprocess.run", return_value=fake_proc):
                result = build_npm_artefact(
                    repo_path=minimal_npm_project, output_dir=out_dir
                )

        assert result.exit_code == 0
        assert result.npm_file_list == []
        assert result.pack_json is None
        # tarball found via glob fallback
        assert result.tarball_path == fake_tgz

    def test_json_missing_files_key_returns_empty_list(
        self, minimal_npm_project: Path, tmp_path: Path
    ) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        fake_tgz = out_dir / "pkg-1.0.0.tgz"
        fake_tgz.touch()

        # Valid JSON but 'files' key absent
        pack_data = [{"filename": "pkg-1.0.0.tgz", "name": "pkg", "version": "1.0.0"}]

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = json.dumps(pack_data)
        fake_proc.stderr = ""

        with patch("saturnday.release.npm_builder.shutil.which", return_value="/usr/bin/npm"):
            with patch("subprocess.run", return_value=fake_proc):
                result = build_npm_artefact(
                    repo_path=minimal_npm_project, output_dir=out_dir
                )

        assert result.exit_code == 0
        assert result.npm_file_list == []

    def test_valid_json_extracts_file_list(
        self, minimal_npm_project: Path, tmp_path: Path, npm_pack_json_output: str
    ) -> None:
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        fake_tgz = out_dir / "saturnday-test-fixture-1.0.0.tgz"
        fake_tgz.touch()

        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = npm_pack_json_output
        fake_proc.stderr = ""

        with patch("saturnday.release.npm_builder.shutil.which", return_value="/usr/bin/npm"):
            with patch("subprocess.run", return_value=fake_proc):
                result = build_npm_artefact(
                    repo_path=minimal_npm_project, output_dir=out_dir
                )

        assert result.exit_code == 0
        assert "package.json" in result.npm_file_list
        assert "index.js" in result.npm_file_list
        assert result.pack_json is not None


# ---------------------------------------------------------------------------
# Integration test (requires npm installed)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBuildNpmArtefactIntegration:
    """Integration tests that invoke real npm pack.

    Marked as ``integration`` — skipped unless ``-m integration`` is passed.
    Require npm to be installed.
    """

    def test_real_npm_pack_produces_tarball(
        self, minimal_npm_project: Path, tmp_path: Path
    ) -> None:
        import shutil

        if shutil.which("npm") is None:
            pytest.skip("npm not installed on this machine")

        out_dir = tmp_path / "npm_out"
        result = build_npm_artefact(
            repo_path=minimal_npm_project, output_dir=out_dir
        )

        assert result.exit_code == 0, f"npm pack failed: {result.stderr}"
        assert result.tarball_path is not None
        assert result.tarball_path.exists()
        assert result.tarball_path.suffix == ".tgz"
        assert len(result.npm_file_list) >= 1
        assert "package.json" in result.npm_file_list

    def test_real_npm_pack_elapsed_is_positive(
        self, minimal_npm_project: Path, tmp_path: Path
    ) -> None:
        import shutil

        if shutil.which("npm") is None:
            pytest.skip("npm not installed on this machine")

        result = build_npm_artefact(repo_path=minimal_npm_project)
        assert result.elapsed_s > 0
