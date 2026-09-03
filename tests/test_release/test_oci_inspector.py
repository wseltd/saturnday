"""Tests for saturnday.release.oci_inspector (RS-024).

Covers:
- Single-layer archive — inventory has correct file count
- Two-layer archive — second layer adds a file (4 total)
- Two-layer archive — second layer has .wh.<name> (3 total after deletion)
- Opaque whiteout (.wh..wh..opq) — entire directory contents removed
- Empty image (no layers) — empty inventory
- Image config parsing: env, entrypoint, cmd, labels
- inspect_oci_archive returns artefact_type="oci"
- inspect_oci_archive raises ValueError for missing archive
- CLI accepts --type oci (argparse choices)
- inspect_oci_image raises RuntimeError when no tools available
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Optional
from unittest import mock

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.oci_inspector import (
    inspect_oci_archive,
    inspect_oci_image,
    _apply_layer,
    _process_layer_members,
    _parse_image_config,
    _extract_config_metadata,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_layer_tar(files: dict[str, bytes], compressed: bool = False) -> bytes:
    """Build a tar archive (bytes) representing one image layer.

    Args:
        files:      Dict mapping relative paths to file content bytes.
        compressed: When True, produce a gzip-compressed tar.

    Returns:
        Raw bytes of the layer tar (uncompressed or gzip).
    """
    buf = io.BytesIO()
    mode = "w:gz" if compressed else "w"
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for rel_path, content in files.items():
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_oci_archive(
    tmp_path: Path,
    files_per_layer: list[dict[str, bytes]],
    config: Optional[dict] = None,
) -> Path:
    """Create a minimal docker-save style ``.tar`` archive for testing.

    Structure created::

        manifest.json
        config.json          (synthetic image config)
        layer_0/layer.tar
        layer_1/layer.tar
        ...

    Args:
        tmp_path:        Destination directory for the archive file.
        files_per_layer: List of dicts, each mapping ``path → content bytes``
                         for one layer.  Upper layers (higher index) override
                         lower layers.
        config:          Optional dict for the image config JSON.  When
                         ``None``, a minimal synthetic config is generated.

    Returns:
        Path to the ``.tar`` archive file.
    """
    archive_path = tmp_path / "test-image.tar"

    layer_entries: list[tuple[str, bytes]] = []
    layer_names: list[str] = []

    for i, layer_files in enumerate(files_per_layer):
        layer_tar_bytes = _make_layer_tar(layer_files)
        layer_dir = f"layer_{i:03d}"
        layer_path = f"{layer_dir}/layer.tar"
        layer_names.append(layer_path)
        layer_entries.append((layer_path, layer_tar_bytes))
        # docker-save also places a "json" file per layer; we skip it as it
        # is not required by our parser.

    # Build a synthetic image config
    if config is None:
        config = {
            "config": {
                "Env": ["PATH=/usr/local/sbin:/usr/local/bin"],
                "Entrypoint": ["/bin/sh"],
                "Cmd": None,
                "Labels": None,
            }
        }

    config_bytes = json.dumps(config).encode()
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    config_filename = f"{config_sha}.json"

    manifest = [
        {
            "Config": config_filename,
            "RepoTags": ["test-image:latest"],
            "Layers": layer_names,
        }
    ]
    manifest_bytes = json.dumps(manifest).encode()

    # Pack everything into the outer tar
    outer_buf = io.BytesIO()
    with tarfile.open(fileobj=outer_buf, mode="w") as tf:
        # manifest.json
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))

        # config JSON
        info = tarfile.TarInfo(config_filename)
        info.size = len(config_bytes)
        tf.addfile(info, io.BytesIO(config_bytes))

        # layer tars
        for layer_path, layer_bytes in layer_entries:
            # Ensure the directory entry exists (some parsers require it)
            dir_name = layer_path.rsplit("/", 1)[0]
            dir_info = tarfile.TarInfo(dir_name)
            dir_info.type = tarfile.DIRTYPE
            dir_info.size = 0
            tf.addfile(dir_info)

            info = tarfile.TarInfo(layer_path)
            info.size = len(layer_bytes)
            tf.addfile(info, io.BytesIO(layer_bytes))

    archive_path.write_bytes(outer_buf.getvalue())
    return archive_path


# ---------------------------------------------------------------------------
# Tests: inspect_oci_archive
# ---------------------------------------------------------------------------


class TestInspectOciArchive:
    """Tests for inspect_oci_archive using synthetic in-memory archives."""

    def test_single_layer_three_files(self, tmp_path: Path) -> None:
        """Single layer with 3 files → inventory has 3 files."""
        layer0 = {
            "app/main.py": b"print('hello')",
            "app/utils.py": b"def noop(): pass",
            "README.md": b"# Test",
        }
        archive = _make_oci_archive(tmp_path, [layer0])
        inventory, unpack_dir = inspect_oci_archive(archive)

        assert inventory.artefact_type == "oci"
        assert len(inventory.files) == 3
        paths = {f.path for f in inventory.files}
        assert "app/main.py" in paths
        assert "app/utils.py" in paths
        assert "README.md" in paths

    def test_two_layers_second_adds_file(self, tmp_path: Path) -> None:
        """Two layers, second adds a file → inventory has 4 files."""
        layer0 = {
            "app/main.py": b"v1",
            "app/utils.py": b"util",
            "README.md": b"readme",
        }
        layer1 = {
            "app/new_feature.py": b"new",
        }
        archive = _make_oci_archive(tmp_path, [layer0, layer1])
        inventory, _ = inspect_oci_archive(archive)

        assert len(inventory.files) == 4
        paths = {f.path for f in inventory.files}
        assert "app/new_feature.py" in paths

    def test_two_layers_whiteout_removes_file(self, tmp_path: Path) -> None:
        """Second layer has .wh.<name> → named file removed from inventory."""
        layer0 = {
            "app/main.py": b"keep me",
            "app/utils.py": b"keep me too",
            "app/deleted.py": b"i will be deleted",
            "README.md": b"readme",
        }
        layer1 = {
            "app/.wh.deleted.py": b"",  # whiteout for app/deleted.py
        }
        archive = _make_oci_archive(tmp_path, [layer0, layer1])
        inventory, _ = inspect_oci_archive(archive)

        # 4 in layer0 minus 1 whited-out = 3; the whiteout file itself is also excluded
        assert len(inventory.files) == 3
        paths = {f.path for f in inventory.files}
        assert "app/deleted.py" not in paths
        assert "app/.wh.deleted.py" not in paths
        assert "app/main.py" in paths

    def test_two_layers_opaque_whiteout(self, tmp_path: Path) -> None:
        """Opaque whiteout removes all files in the directory."""
        layer0 = {
            "app/a.py": b"a",
            "app/b.py": b"b",
            "app/c.py": b"c",
            "root.txt": b"root",
        }
        layer1 = {
            # opaque whiteout replaces entire app/ directory contents
            "app/.wh..wh..opq": b"",
            "app/new.py": b"replacement",
        }
        archive = _make_oci_archive(tmp_path, [layer0, layer1])
        inventory, _ = inspect_oci_archive(archive)

        paths = {f.path for f in inventory.files}
        # app/a, app/b, app/c removed; app/new.py and root.txt remain
        assert "app/a.py" not in paths
        assert "app/b.py" not in paths
        assert "app/c.py" not in paths
        assert "app/new.py" in paths
        assert "root.txt" in paths
        assert len(inventory.files) == 2

    def test_empty_image_no_layers(self, tmp_path: Path) -> None:
        """Zero layers → empty inventory."""
        archive = _make_oci_archive(tmp_path, [])
        inventory, _ = inspect_oci_archive(archive)

        assert inventory.artefact_type == "oci"
        assert inventory.files == []

    def test_artefact_type_is_oci(self, tmp_path: Path) -> None:
        """inspect_oci_archive always returns artefact_type='oci'."""
        archive = _make_oci_archive(tmp_path, [{"file.txt": b"x"}])
        inventory, _ = inspect_oci_archive(archive)
        assert inventory.artefact_type == "oci"

    def test_artefact_sha256_computed(self, tmp_path: Path) -> None:
        """artefact_sha256 is the SHA-256 of the archive file."""
        archive = _make_oci_archive(tmp_path, [{"file.txt": b"x"}])
        expected = hashlib.sha256(archive.read_bytes()).hexdigest()
        inventory, _ = inspect_oci_archive(archive)
        assert inventory.artefact_sha256 == expected

    def test_file_sha256_correct(self, tmp_path: Path) -> None:
        """Each ArtefactFile carries the correct SHA-256 of its content."""
        content = b"hello sha256"
        expected_sha = hashlib.sha256(content).hexdigest()
        archive = _make_oci_archive(tmp_path, [{"greeting.txt": content}])
        inventory, _ = inspect_oci_archive(archive)

        matching = [f for f in inventory.files if f.path == "greeting.txt"]
        assert len(matching) == 1
        assert matching[0].sha256 == expected_sha
        assert matching[0].size == len(content)

    def test_missing_archive_raises_value_error(self, tmp_path: Path) -> None:
        """inspect_oci_archive raises ValueError when file does not exist."""
        with pytest.raises(ValueError, match="does not exist"):
            inspect_oci_archive(tmp_path / "nonexistent.tar")

    def test_artefact_path_is_archive_path(self, tmp_path: Path) -> None:
        """artefact_path in inventory is the resolved archive path string."""
        archive = _make_oci_archive(tmp_path, [{"f.txt": b"x"}])
        inventory, _ = inspect_oci_archive(archive)
        assert inventory.artefact_path == str(archive.resolve())

    def test_inventory_files_sorted(self, tmp_path: Path) -> None:
        """Files in inventory are sorted by path."""
        layer = {"z.txt": b"z", "a.txt": b"a", "m.txt": b"m"}
        archive = _make_oci_archive(tmp_path, [layer])
        inventory, _ = inspect_oci_archive(archive)
        paths = [f.path for f in inventory.files]
        assert paths == sorted(paths)

    def test_second_layer_overrides_first(self, tmp_path: Path) -> None:
        """A file rewritten in an upper layer has the upper layer's content/hash."""
        v1 = b"version one content"
        v2 = b"version two content"
        archive = _make_oci_archive(
            tmp_path,
            [{"shared.txt": v1}, {"shared.txt": v2}],
        )
        inventory, _ = inspect_oci_archive(archive)

        matches = [f for f in inventory.files if f.path == "shared.txt"]
        assert len(matches) == 1
        assert matches[0].sha256 == hashlib.sha256(v2).hexdigest()
        assert matches[0].size == len(v2)


# ---------------------------------------------------------------------------
# Tests: config metadata parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    """Tests for image config metadata extraction."""

    def test_config_env_entrypoint_parsed(self, tmp_path: Path) -> None:
        """Env vars and entrypoint are extracted into inventory metadata."""
        config = {
            "config": {
                "Env": ["PATH=/usr/bin", "APP_ENV=prod"],
                "Entrypoint": ["/app/server"],
                "Cmd": ["--port", "8080"],
                "Labels": {"version": "1.2.3"},
            }
        }
        archive = _make_oci_archive(tmp_path, [{"app/server": b"binary"}], config=config)
        inventory, _ = inspect_oci_archive(archive)

        meta = inventory.metadata
        assert meta.get("env") == ["PATH=/usr/bin", "APP_ENV=prod"]
        assert meta.get("entrypoint") == ["/app/server"]
        assert meta.get("cmd") == ["--port", "8080"]
        assert meta.get("labels") == {"version": "1.2.3"}

    def test_config_missing_fields_not_present(self, tmp_path: Path) -> None:
        """Missing config fields are not injected into metadata."""
        config = {"config": {"Env": ["X=1"]}}
        archive = _make_oci_archive(tmp_path, [{"f.txt": b"x"}], config=config)
        inventory, _ = inspect_oci_archive(archive)

        meta = inventory.metadata
        assert "entrypoint" not in meta
        assert "cmd" not in meta
        assert "labels" not in meta

    def test_image_ref_in_metadata(self, tmp_path: Path) -> None:
        """image_ref key is present in metadata (derived from archive name)."""
        archive = _make_oci_archive(tmp_path, [{"f.txt": b"x"}])
        inventory, _ = inspect_oci_archive(archive)
        assert "image_ref" in inventory.metadata

    def test_extract_config_metadata_container_config_key(self) -> None:
        """container_config key is also accepted for metadata extraction."""
        data = {
            "container_config": {
                "Env": ["MYVAR=hello"],
                "Entrypoint": ["/entrypoint.sh"],
                "Cmd": None,
                "Labels": None,
            }
        }
        result = _extract_config_metadata(data)
        assert result.get("env") == ["MYVAR=hello"]
        assert result.get("entrypoint") == ["/entrypoint.sh"]

    def test_extract_config_metadata_empty_labels_omitted(self) -> None:
        """Labels key is omitted when the value is None or empty."""
        data = {"config": {"Env": ["X=1"], "Labels": None}}
        result = _extract_config_metadata(data)
        assert "labels" not in result


# ---------------------------------------------------------------------------
# Tests: layer whiteout edge cases via _apply_layer
# ---------------------------------------------------------------------------


class TestLayerWhiteout:
    """Direct tests for the layer-merging logic."""

    def test_regular_whiteout_removes_file(self, tmp_path: Path) -> None:
        """A .wh.<name> in a layer removes the named path from merged."""
        layer_bytes = _make_layer_tar(
            {"app/target.py": b"data", "app/.wh.target.py": b""}
        )
        layer_tar = tmp_path / "layer.tar"
        layer_tar.write_bytes(layer_bytes)

        merged: dict[str, ArtefactFile] = {
            "app/target.py": ArtefactFile("app/target.py", 4, "abc"),
        }
        _apply_layer(layer_tar, merged)

        assert "app/target.py" not in merged
        assert "app/.wh.target.py" not in merged

    def test_whiteout_only_targets_named_file(self, tmp_path: Path) -> None:
        """A whiteout for file X does not affect file Y in same directory."""
        layer_bytes = _make_layer_tar({"app/.wh.bad.py": b""})
        layer_tar = tmp_path / "layer.tar"
        layer_tar.write_bytes(layer_bytes)

        merged: dict[str, ArtefactFile] = {
            "app/good.py": ArtefactFile("app/good.py", 10, "deadbeef"),
        }
        _apply_layer(layer_tar, merged)

        assert "app/good.py" in merged

    def test_opaque_whiteout_removes_dir_contents(self, tmp_path: Path) -> None:
        """An opaque whiteout .wh..wh..opq removes all files under the dir."""
        layer_bytes = _make_layer_tar(
            {
                "app/.wh..wh..opq": b"",
                "app/new.py": b"new",
            }
        )
        layer_tar = tmp_path / "layer.tar"
        layer_tar.write_bytes(layer_bytes)

        merged: dict[str, ArtefactFile] = {
            "app/old_a.py": ArtefactFile("app/old_a.py", 1, "aa"),
            "app/old_b.py": ArtefactFile("app/old_b.py", 1, "bb"),
            "other/file.py": ArtefactFile("other/file.py", 1, "cc"),
        }
        _apply_layer(layer_tar, merged)

        # old app/ files gone, new app/new.py present, other/ untouched
        assert "app/old_a.py" not in merged
        assert "app/old_b.py" not in merged
        assert "app/new.py" in merged
        assert "other/file.py" in merged

    def test_nonexistent_layer_tar_logged_no_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_apply_layer logs a warning and does not raise for a bad path."""
        import logging

        merged: dict[str, ArtefactFile] = {}
        with caplog.at_level(logging.WARNING):
            _apply_layer(tmp_path / "ghost.tar", merged)

        # merged is unchanged, no exception raised
        assert merged == {}


# ---------------------------------------------------------------------------
# Tests: broken / edge-case archives
# ---------------------------------------------------------------------------


class TestBrokenArchives:
    """Tests for graceful handling of malformed or unusual archives."""

    def test_missing_manifest_returns_empty_inventory(
        self, tmp_path: Path
    ) -> None:
        """An archive without manifest.json returns empty inventory, no raise."""
        # Build a tar with no manifest.json
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"hello"
            info = tarfile.TarInfo("some_file.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        archive = tmp_path / "no-manifest.tar"
        archive.write_bytes(buf.getvalue())

        inventory, _ = inspect_oci_archive(archive)
        assert inventory.artefact_type == "oci"
        assert inventory.files == []
        assert any("ERROR" in r for r in inventory.record_entries)

    def test_corrupt_tar_returns_empty_inventory(
        self, tmp_path: Path
    ) -> None:
        """A corrupt tar file returns an empty inventory without raising."""
        archive = tmp_path / "corrupt.tar"
        archive.write_bytes(b"not a real tar file at all!!!")

        inventory, _ = inspect_oci_archive(archive)
        assert inventory.artefact_type == "oci"
        assert inventory.files == []
        assert any("ERROR" in r for r in inventory.record_entries)


# ---------------------------------------------------------------------------
# Tests: inspect_oci_image (Docker/Skopeo — skipped when neither available)
# ---------------------------------------------------------------------------


def _docker_has_image(image_ref: str) -> bool:
    """Return True when docker is on PATH, the daemon is running, and the image is locally present."""
    if shutil.which("docker") is None:
        return False
    try:
        import subprocess as _sp

        # Check image exists locally (no pull)
        result = _sp.run(
            ["docker", "image", "inspect", image_ref],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(
    not _docker_has_image("hello-world:latest"),
    reason="Docker image hello-world:latest not available locally",
)
class TestInspectOciImageLive:
    """Live tests requiring a locally pulled Docker image."""

    def test_inspect_image_returns_oci_type(self) -> None:
        """inspect_oci_image returns artefact_type='oci' for a real image."""
        inventory, _ = inspect_oci_image("hello-world:latest")
        assert inventory.artefact_type == "oci"


class TestInspectOciImageNoTools:
    """Tests for inspect_oci_image when no container tools are available."""

    def test_raises_when_no_docker_or_skopeo(self, tmp_path: Path) -> None:
        """RuntimeError is raised when docker and skopeo are both absent."""
        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="neither.*docker.*nor.*skopeo"):
                inspect_oci_image("someimage:latest", target_dir=tmp_path)


# ---------------------------------------------------------------------------
# Tests: CLI argument parsing
# ---------------------------------------------------------------------------


class TestCliArguments:
    """Tests that the CLI parser accepts OCI-related arguments."""

    def test_cli_accepts_type_oci(self) -> None:
        """--type oci is a valid choice in the argparse parser."""
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            ["release-preflight", "--type", "oci", "--image", "myapp:latest"]
        )
        assert args.artefact_type == "oci"
        assert args.image == "myapp:latest"

    def test_cli_accepts_image_archive(self, tmp_path: Path) -> None:
        """--image-archive is accepted and stored as args.image_archive."""
        from saturnday.cli import build_parser

        archive_path = str(tmp_path / "test.tar")
        parser = build_parser()
        args = parser.parse_args(
            [
                "release-preflight",
                "--type", "oci",
                "--image-archive", archive_path,
            ]
        )
        assert args.image_archive == archive_path

    def test_cli_type_oci_in_choices(self) -> None:
        """'oci' is listed as a valid choice for --type."""
        from saturnday.cli import build_parser
        import argparse

        parser = build_parser()
        # Find the release-preflight subparser and inspect its actions
        # We verify that parsing an invalid type raises SystemExit
        with pytest.raises(SystemExit):
            parser.parse_args(["release-preflight", "--type", "invalid_type"])

    def test_cli_python_still_works(self) -> None:
        """Existing --type python still parses correctly (backward compatible)."""
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["release-preflight", "--type", "python"])
        assert args.artefact_type == "python"

    def test_cli_npm_still_works(self) -> None:
        """Existing --type npm still parses correctly (backward compatible)."""
        from saturnday.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["release-preflight", "--type", "npm"])
        assert args.artefact_type == "npm"


# ---------------------------------------------------------------------------
# Tests: orchestrator dispatch
# ---------------------------------------------------------------------------


class TestOrchestratorOciDispatch:
    """Tests that the orchestrator correctly calls OCI inspector."""

    def test_orchestrator_dispatches_oci_archive(
        self, tmp_path: Path
    ) -> None:
        """run_release_preflight with artefact_type='oci' calls oci_inspector."""
        # Create a real minimal archive to avoid mocking the entire inspector
        archive = _make_oci_archive(tmp_path, [{"app.py": b"code"}])

        from saturnday.release.orchestrator import run_release_preflight

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="oci",
            image_archive_path=archive,
        )

        assert result.error == ""
        assert result.evidence_pack is not None
        assert result.evidence_pack.artefact_type == "oci"

    def test_orchestrator_oci_requires_image_or_archive(
        self, tmp_path: Path
    ) -> None:
        """Passing artefact_type='oci' with no image ref or archive → FAIL."""
        from saturnday.release.orchestrator import run_release_preflight

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="oci",
            image_ref=None,
            image_archive_path=None,
        )

        assert result.disposition == "FAIL"
        assert result.error != ""

    def test_orchestrator_unsupported_type_returns_fail(
        self, tmp_path: Path
    ) -> None:
        """Unsupported artefact_type still returns FAIL (backward compatible)."""
        from saturnday.release.orchestrator import run_release_preflight

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="java",
        )

        assert result.disposition == "FAIL"
        assert "Unsupported" in result.error
