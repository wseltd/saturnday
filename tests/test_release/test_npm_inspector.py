"""Tests for saturnday.release.npm_inspector (RS-026).

Covers:
- unpack_npm_tarball / inspect_npm_tarball function signatures (spec compliance)
- ArtefactInventory structure and field values for npm type
- package/ prefix stripping in unpacked paths
- package.json metadata parsing from inside tarball
- npm_file_list validation: extra files, missing files, exact match
- Corrupt / invalid tarball handling (no raise)
- Nonexistent tarball path (ValueError)
- Integration test with a real npm-style tarball
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.npm_inspector import inspect_npm_tarball, unpack_npm_tarball


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_npm_tarball(dest: Path, files: dict[str, bytes]) -> Path:
    """Create a gzipped tar with npm's 'package/' prefix structure.

    Args:
        dest:  Destination directory for the tarball.
        files: Dict mapping relative paths (e.g. ``"index.js"``) to content.

    Returns:
        Path to the created ``.tgz`` file.
    """
    tgz_path = dest / "test-pkg-1.0.0.tgz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel_path, content in files.items():
            # npm prefixes every entry with 'package/'
            member_name = f"package/{rel_path}"
            info = tarfile.TarInfo(name=member_name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    tgz_path.write_bytes(buf.getvalue())
    return tgz_path


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def pkg_json_content() -> bytes:
    pkg = {
        "name": "test-pkg",
        "version": "1.0.0",
        "description": "Test fixture",
        "main": "index.js",
        "license": "MIT",
        "files": ["index.js"],
    }
    return json.dumps(pkg, indent=2).encode()


@pytest.fixture()
def index_js_content() -> bytes:
    return b"module.exports = { hello: () => 'world' };\n"


@pytest.fixture()
def simple_tarball(
    tmp_path: Path,
    pkg_json_content: bytes,
    index_js_content: bytes,
) -> Path:
    """A minimal npm tarball with package.json and index.js."""
    return _make_npm_tarball(
        dest=tmp_path,
        files={
            "package.json": pkg_json_content,
            "index.js": index_js_content,
        },
    )


@pytest.fixture()
def tarball_with_nested_files(tmp_path: Path, pkg_json_content: bytes) -> Path:
    """A tarball with nested dist/ files to exercise deeper paths."""
    return _make_npm_tarball(
        dest=tmp_path,
        files={
            "package.json": pkg_json_content,
            "dist/index.js": b"'use strict'; exports.main = 1;\n",
            "dist/index.d.ts": b"export declare const main: number;\n",
            "README.md": b"# test-pkg\n",
        },
    )


# ---------------------------------------------------------------------------
# ValueError for nonexistent path
# ---------------------------------------------------------------------------


class TestNonexistentTarball:
    def test_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            inspect_npm_tarball(tmp_path / "missing.tgz")

    def test_unpack_alias_raises_value_error(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        with pytest.raises(ValueError, match="does not exist"):
            unpack_npm_tarball(tmp_path / "missing.tgz", target_dir=target)


# ---------------------------------------------------------------------------
# Basic inventory structure
# ---------------------------------------------------------------------------


class TestBasicInventoryStructure:
    def test_artefact_type_is_npm(self, simple_tarball: Path, tmp_path: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert inventory.artefact_type == "npm"

    def test_artefact_path_matches_tarball(
        self, simple_tarball: Path
    ) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert inventory.artefact_path == str(simple_tarball)

    def test_artefact_sha256_is_hex(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        sha = inventory.artefact_sha256
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

    def test_artefact_sha256_matches_file(self, simple_tarball: Path) -> None:
        expected = _sha256_bytes(simple_tarball.read_bytes())
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert inventory.artefact_sha256 == expected

    def test_files_list_is_populated(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert len(inventory.files) == 2  # package.json + index.js

    def test_files_are_artefact_file_instances(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        for f in inventory.files:
            assert isinstance(f, ArtefactFile)

    def test_file_sha256_is_hex(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        for f in inventory.files:
            assert len(f.sha256) == 64
            assert all(c in "0123456789abcdef" for c in f.sha256)

    def test_file_size_positive(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        for f in inventory.files:
            assert f.size > 0

    def test_returns_unpack_dir_path(self, simple_tarball: Path) -> None:
        _, unpack_dir = inspect_npm_tarball(simple_tarball)
        assert isinstance(unpack_dir, Path)
        assert unpack_dir.exists()


# ---------------------------------------------------------------------------
# package/ prefix stripping
# ---------------------------------------------------------------------------


class TestPackagePrefixStripping:
    """Files should be stored without the 'package/' prefix."""

    def test_package_json_path_has_no_prefix(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        paths = [f.path for f in inventory.files]
        assert "package.json" in paths
        assert not any(p.startswith("package/") for p in paths)

    def test_index_js_path_has_no_prefix(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        paths = [f.path for f in inventory.files]
        assert "index.js" in paths

    def test_nested_paths_stripped_correctly(
        self, tarball_with_nested_files: Path
    ) -> None:
        inventory, _ = inspect_npm_tarball(tarball_with_nested_files)
        paths = [f.path for f in inventory.files]
        assert "dist/index.js" in paths
        assert "dist/index.d.ts" in paths
        assert "README.md" in paths
        assert not any(p.startswith("package/") for p in paths)


# ---------------------------------------------------------------------------
# Metadata parsing (package.json from inside tarball)
# ---------------------------------------------------------------------------


class TestMetadataParsing:
    def test_metadata_has_name(
        self, simple_tarball: Path
    ) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert inventory.metadata.get("name") == "test-pkg"

    def test_metadata_has_version(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert inventory.metadata.get("version") == "1.0.0"

    def test_metadata_has_main(self, simple_tarball: Path) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball)
        assert inventory.metadata.get("main") == "index.js"

    def test_metadata_empty_if_no_package_json(self, tmp_path: Path) -> None:
        """Tarball without package.json returns empty metadata dict."""
        tgz = _make_npm_tarball(
            tmp_path, {"index.js": b"module.exports = {};\n"}
        )
        inventory, _ = inspect_npm_tarball(tgz)
        assert inventory.metadata == {}


# ---------------------------------------------------------------------------
# File content hashing correctness
# ---------------------------------------------------------------------------


class TestFileHashing:
    def test_package_json_hash_correct(
        self, simple_tarball: Path, pkg_json_content: bytes
    ) -> None:
        expected_sha256 = _sha256_bytes(pkg_json_content)
        inventory, _ = inspect_npm_tarball(simple_tarball)
        pkg_file = next(f for f in inventory.files if f.path == "package.json")
        assert pkg_file.sha256 == expected_sha256

    def test_index_js_hash_correct(
        self, simple_tarball: Path, index_js_content: bytes
    ) -> None:
        expected_sha256 = _sha256_bytes(index_js_content)
        inventory, _ = inspect_npm_tarball(simple_tarball)
        js_file = next(f for f in inventory.files if f.path == "index.js")
        assert js_file.sha256 == expected_sha256


# ---------------------------------------------------------------------------
# npm_file_list validation
# ---------------------------------------------------------------------------


class TestNpmFileListValidation:
    def test_exact_match_no_warnings_in_record(
        self, simple_tarball: Path
    ) -> None:
        npm_list = ["package.json", "index.js"]
        inventory, _ = inspect_npm_tarball(
            simple_tarball, npm_file_list=npm_list
        )
        warnings = [
            e for e in inventory.record_entries
            if e.startswith("EXTRA") or e.startswith("MISSING")
        ]
        assert warnings == []

    def test_exact_match_record_entries_contains_npm_list(
        self, simple_tarball: Path
    ) -> None:
        npm_list = ["package.json", "index.js"]
        inventory, _ = inspect_npm_tarball(
            simple_tarball, npm_file_list=npm_list
        )
        for path in npm_list:
            assert path in inventory.record_entries

    def test_extra_file_in_tarball_flagged(self, tmp_path: Path) -> None:
        """File in tarball but not in npm_file_list → EXTRA warning in record_entries."""
        pkg = json.dumps({"name": "x", "version": "1.0.0"}).encode()
        tgz = _make_npm_tarball(
            tmp_path,
            {
                "package.json": pkg,
                "index.js": b"module.exports = {};",
                "secret.txt": b"should not be here",
            },
        )
        npm_list = ["package.json", "index.js"]
        inventory, _ = inspect_npm_tarball(tgz, npm_file_list=npm_list)
        extra_entries = [
            e for e in inventory.record_entries if "EXTRA" in e and "secret.txt" in e
        ]
        assert len(extra_entries) == 1

    def test_missing_file_in_tarball_flagged(self, tmp_path: Path) -> None:
        """File in npm_file_list but not in tarball → MISSING warning in record_entries."""
        pkg = json.dumps({"name": "x", "version": "1.0.0"}).encode()
        tgz = _make_npm_tarball(tmp_path, {"package.json": pkg})
        npm_list = ["package.json", "dist/index.js"]  # dist/index.js not in tarball
        inventory, _ = inspect_npm_tarball(tgz, npm_file_list=npm_list)
        missing_entries = [
            e for e in inventory.record_entries if "MISSING" in e and "dist/index.js" in e
        ]
        assert len(missing_entries) == 1

    def test_no_npm_list_gives_empty_record_entries(
        self, simple_tarball: Path
    ) -> None:
        inventory, _ = inspect_npm_tarball(simple_tarball, npm_file_list=None)
        assert inventory.record_entries == []


# ---------------------------------------------------------------------------
# Corrupt tarball handling
# ---------------------------------------------------------------------------


class TestCorruptTarball:
    def test_corrupt_tarball_returns_empty_inventory_no_raise(
        self, tmp_path: Path
    ) -> None:
        """A corrupt .tgz must return an inventory with error info, not raise."""
        corrupt = tmp_path / "corrupt.tgz"
        corrupt.write_bytes(b"this is not a valid gzip or tar file at all xxxx")

        # Must not raise
        inventory, unpack_dir = inspect_npm_tarball(corrupt)

        assert inventory.artefact_type == "npm"
        assert inventory.files == []
        assert len(inventory.record_entries) >= 1
        assert any("ERROR" in e for e in inventory.record_entries)

    def test_corrupt_tarball_sha256_still_computed(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "corrupt.tgz"
        content = b"garbage content"
        corrupt.write_bytes(content)
        expected_sha256 = _sha256_bytes(content)

        inventory, _ = inspect_npm_tarball(corrupt)
        assert inventory.artefact_sha256 == expected_sha256


# ---------------------------------------------------------------------------
# unpack_npm_tarball spec alias
# ---------------------------------------------------------------------------


class TestUnpackNpmTarballAlias:
    """unpack_npm_tarball is the RS-026 spec function — must match the signature."""

    def test_returns_artefact_inventory(
        self, simple_tarball: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "unpacked"
        target.mkdir()
        inventory = unpack_npm_tarball(simple_tarball, target_dir=target)
        assert isinstance(inventory, ArtefactInventory)
        assert inventory.artefact_type == "npm"

    def test_accepts_npm_file_list_kwarg(
        self, simple_tarball: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "unpacked2"
        target.mkdir()
        inventory = unpack_npm_tarball(
            simple_tarball,
            target_dir=target,
            npm_file_list=["package.json", "index.js"],
        )
        assert "package.json" in inventory.record_entries

    def test_target_dir_created_if_missing(
        self, simple_tarball: Path, tmp_path: Path
    ) -> None:
        target = tmp_path / "new_dir" / "nested"
        # Must not exist yet
        assert not target.exists()
        inventory = unpack_npm_tarball(simple_tarball, target_dir=target)
        assert target.exists()
        assert inventory.artefact_type == "npm"


# ---------------------------------------------------------------------------
# Integration test (requires npm installed + builds real tarball)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestNpmInspectorIntegration:
    """End-to-end: build a real npm tarball and inspect it.

    Requires npm to be installed.  Marked ``integration`` so normal runs skip.
    """

    def test_inspect_real_npm_tarball(self, tmp_path: Path) -> None:
        import shutil

        if shutil.which("npm") is None:
            pytest.skip("npm not installed on this machine")

        from saturnday.release.npm_builder import build_npm_artefact

        # Create minimal npm project
        pkg = {
            "name": "saturnday-integration-test",
            "version": "0.0.1",
            "description": "Integration test fixture",
            "main": "index.js",
            "license": "MIT",
        }
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "package.json").write_text(
            json.dumps(pkg), encoding="utf-8"
        )
        (project_dir / "index.js").write_text(
            "module.exports = {};\n", encoding="utf-8"
        )

        # Build with real npm
        out_dir = tmp_path / "out"
        build_result = build_npm_artefact(repo_path=project_dir, output_dir=out_dir)
        if build_result.exit_code != 0:
            pytest.skip(f"npm pack failed: {build_result.stderr}")

        tarball = build_result.tarball_path
        assert tarball is not None and tarball.exists()

        # Inspect
        inventory, unpack_dir = inspect_npm_tarball(
            tarball, npm_file_list=build_result.npm_file_list
        )

        assert inventory.artefact_type == "npm"
        assert inventory.artefact_path == str(tarball)
        assert len(inventory.artefact_sha256) == 64
        assert len(inventory.files) >= 1

        paths = [f.path for f in inventory.files]
        assert "package.json" in paths
        assert "index.js" in paths
        assert not any(p.startswith("package/") for p in paths)

        assert inventory.metadata.get("name") == "saturnday-integration-test"
        assert inventory.metadata.get("version") == "0.0.1"

        # Validation should report no discrepancies
        warning_entries = [
            e for e in inventory.record_entries
            if e.startswith("EXTRA") or e.startswith("MISSING")
        ]
        assert warning_entries == [], f"Unexpected discrepancies: {warning_entries}"
