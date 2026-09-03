"""Tests for RS-023: npm publishing workflow and fixture.

Covers:
- npm workflow YAML file exists and is valid
- Workflow triggers on workflow_dispatch with required inputs
- Preflight step and verify-pass step are present
- npm publish step uses --provenance
- NPM_TOKEN secret is referenced
- Fixture package.json parses correctly
- npm pack on fixture produces a .tgz (integration, skipped if npm absent)
- release-preflight --type npm runs against packed tarball (integration, npm)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

_WORKFLOW_PATH = (
    Path(__file__).parent.parent.parent
    / ".github"
    / "workflows"
    / "publish-npm.yml"
)

_FIXTURE_PKG_DIR = (
    Path(__file__).parent.parent
    / "fixtures"
    / "sample-npm-package"
)


# ---------------------------------------------------------------------------
# Workflow YAML tests
# ---------------------------------------------------------------------------


def _load_workflow() -> dict:
    if not _YAML_AVAILABLE:
        pytest.skip("PyYAML not installed")
    with open(_WORKFLOW_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestNpmWorkflowFileExists:
    def test_file_exists(self) -> None:
        assert _WORKFLOW_PATH.is_file(), (
            f"Expected workflow at {_WORKFLOW_PATH}"
        )

    def test_file_non_empty(self) -> None:
        assert _WORKFLOW_PATH.stat().st_size > 0


class TestNpmWorkflowStructure:
    def test_parses_as_valid_yaml(self) -> None:
        wf = _load_workflow()
        assert isinstance(wf, dict)

    def _on(self) -> dict:
        """Return the parsed 'on:' value, handling PyYAML bool-True quirk."""
        wf = _load_workflow()
        # PyYAML may parse bare 'on:' as boolean True in some versions.
        return wf.get("on") or wf.get(True) or {}

    def test_has_required_top_level_keys(self) -> None:
        wf = _load_workflow()
        assert "name" in wf, "Workflow must have 'name' key"
        assert "jobs" in wf, "Workflow must have 'jobs' key"
        # 'on' may be parsed as True by PyYAML
        has_on = "on" in wf or True in wf
        assert has_on, "Workflow must have an 'on' trigger key"

    def test_triggers_on_workflow_dispatch(self) -> None:
        on = self._on()
        assert "workflow_dispatch" in on, (
            "npm workflow must trigger on workflow_dispatch"
        )

    def test_workflow_dispatch_has_package_path_input(self) -> None:
        on = self._on()
        wd = on.get("workflow_dispatch") or {}
        inputs = wd.get("inputs") or {}
        assert "package_path" in inputs, (
            "workflow_dispatch must have 'package_path' input"
        )

    def test_workflow_dispatch_has_registry_input(self) -> None:
        on = self._on()
        wd = on.get("workflow_dispatch") or {}
        inputs = wd.get("inputs") or {}
        assert "registry" in inputs, (
            "workflow_dispatch must have 'registry' input"
        )

    def test_id_token_write_permission(self) -> None:
        wf = _load_workflow()
        permissions = wf.get("permissions", {})
        assert permissions.get("id-token") == "write", (
            "npm workflow must have 'id-token: write' for provenance OIDC"
        )

    def test_has_publish_job(self) -> None:
        wf = _load_workflow()
        assert len(wf.get("jobs", {})) > 0


class TestNpmWorkflowSteps:
    """Verify key step content is present in the raw YAML text."""

    def _raw(self) -> str:
        return _WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_npm_pack_present(self) -> None:
        assert "npm pack" in self._raw(), (
            "Workflow must run 'npm pack'"
        )

    def test_release_preflight_present(self) -> None:
        assert "release-preflight" in self._raw(), (
            "Workflow must invoke 'saturnday release-preflight'"
        )

    def test_npm_type_flag_present(self) -> None:
        assert "--type npm" in self._raw(), (
            "release-preflight must pass '--type npm'"
        )

    def test_disposition_verification_present(self) -> None:
        assert "disposition" in self._raw(), (
            "Workflow must verify preflight disposition == PASS"
        )

    def test_npm_publish_command_present(self) -> None:
        assert "npm publish" in self._raw(), (
            "Workflow must run 'npm publish'"
        )

    def test_provenance_flag_present(self) -> None:
        assert "--provenance" in self._raw(), (
            "npm publish must pass '--provenance'"
        )

    def test_npm_token_secret_referenced(self) -> None:
        assert "NPM_TOKEN" in self._raw(), (
            "Workflow must reference the NPM_TOKEN secret"
        )

    def test_release_environment_present(self) -> None:
        assert "release" in self._raw().lower(), (
            "Workflow must reference the 'release' environment"
        )


# ---------------------------------------------------------------------------
# Fixture package tests
# ---------------------------------------------------------------------------


class TestNpmFixturePackage:
    def test_fixture_dir_exists(self) -> None:
        assert _FIXTURE_PKG_DIR.is_dir(), (
            f"Fixture directory missing: {_FIXTURE_PKG_DIR}"
        )

    def test_package_json_exists(self) -> None:
        assert (_FIXTURE_PKG_DIR / "package.json").is_file()

    def test_package_json_is_valid_json(self) -> None:
        data = json.loads((_FIXTURE_PKG_DIR / "package.json").read_text())
        assert isinstance(data, dict)

    def test_package_json_has_name(self) -> None:
        data = json.loads((_FIXTURE_PKG_DIR / "package.json").read_text())
        assert "name" in data
        assert data["name"] == "@saturnday/sample-fixture"

    def test_package_json_has_version(self) -> None:
        data = json.loads((_FIXTURE_PKG_DIR / "package.json").read_text())
        assert "version" in data
        assert data["version"] == "0.0.1"

    def test_package_json_has_files_field(self) -> None:
        data = json.loads((_FIXTURE_PKG_DIR / "package.json").read_text())
        assert "files" in data
        assert isinstance(data["files"], list)

    def test_index_js_exists(self) -> None:
        assert (_FIXTURE_PKG_DIR / "index.js").is_file()

    def test_lib_utils_js_exists(self) -> None:
        assert (_FIXTURE_PKG_DIR / "lib" / "utils.js").is_file()

    def test_index_js_has_hello_export(self) -> None:
        content = (_FIXTURE_PKG_DIR / "index.js").read_text()
        assert "hello" in content

    def test_lib_utils_js_has_add_export(self) -> None:
        content = (_FIXTURE_PKG_DIR / "lib" / "utils.js").read_text()
        assert "add" in content


# ---------------------------------------------------------------------------
# Integration tests (require npm on PATH)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not shutil.which("npm"),
    reason="npm not installed — skipping npm integration tests",
)
class TestNpmPackIntegration:
    """Run npm pack on the fixture package and verify output."""

    def test_npm_pack_produces_tgz(self, tmp_path: Path) -> None:
        """npm pack on the fixture produces a .tgz tarball."""
        import shutil as _shutil

        # Copy fixture to a temp dir to avoid polluting the repo.
        pkg_copy = tmp_path / "pkg"
        _shutil.copytree(str(_FIXTURE_PKG_DIR), str(pkg_copy))

        result = subprocess.run(
            ["npm", "pack", "--pack-destination", str(tmp_path)],
            cwd=str(pkg_copy),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"npm pack failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

        tarballs = list(tmp_path.glob("*.tgz"))
        assert len(tarballs) == 1, (
            f"Expected exactly one .tgz, got: {tarballs}"
        )
        assert tarballs[0].stat().st_size > 0

    def test_release_preflight_npm_type_against_fixture(
        self, tmp_path: Path
    ) -> None:
        """release-preflight --type npm runs against the fixture tarball."""
        import shutil as _shutil
        import sys

        # Copy fixture to temp dir.
        pkg_copy = tmp_path / "pkg"
        _shutil.copytree(str(_FIXTURE_PKG_DIR), str(pkg_copy))

        # Pack into tmp_path.
        pack_result = subprocess.run(
            ["npm", "pack", "--pack-destination", str(tmp_path)],
            cwd=str(pkg_copy),
            capture_output=True,
            text=True,
        )
        assert pack_result.returncode == 0, f"npm pack failed: {pack_result.stderr}"

        tarballs = list(tmp_path.glob("*.tgz"))
        assert tarballs, "No tarball produced by npm pack"
        tarball = tarballs[0]

        evidence_dir = tmp_path / "evidence"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "saturnday",
                "release-preflight",
                "--repo",
                str(pkg_copy),
                "--type",
                "npm",
                "--tarball",
                str(tarball),
                "--output",
                str(evidence_dir),
            ],
            capture_output=True,
            text=True,
        )
        # Exit 0 (PASS) or 1 (WARN/advisory) are both acceptable.
        assert result.returncode in (0, 1), (
            f"release-preflight crashed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_preflight_evidence_written(self, tmp_path: Path) -> None:
        """Evidence JSON is written after a successful preflight run."""
        import shutil as _shutil
        import sys

        pkg_copy = tmp_path / "pkg"
        _shutil.copytree(str(_FIXTURE_PKG_DIR), str(pkg_copy))

        pack_result = subprocess.run(
            ["npm", "pack", "--pack-destination", str(tmp_path)],
            cwd=str(pkg_copy),
            capture_output=True,
            text=True,
        )
        assert pack_result.returncode == 0

        tarballs = list(tmp_path.glob("*.tgz"))
        assert tarballs
        tarball = tarballs[0]

        evidence_dir = tmp_path / "evidence"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "saturnday",
                "release-preflight",
                "--repo",
                str(pkg_copy),
                "--type",
                "npm",
                "--tarball",
                str(tarball),
                "--output",
                str(evidence_dir),
            ],
            capture_output=True,
            text=True,
        )

        evidence_json = evidence_dir / "evidence.json"
        assert evidence_json.is_file(), (
            f"Expected evidence.json at {evidence_json}"
        )

        data = json.loads(evidence_json.read_text(encoding="utf-8"))
        assert "disposition" in data
        assert data["disposition"] in ("PASS", "WARN", "FAIL")
        assert "artefact_type" in data
        assert data["artefact_type"] == "npm"
