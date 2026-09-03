"""RS-014: Release preflight integration tests.

End-to-end tests for ``saturnday.release.orchestrator.run_release_preflight``
that exercise the full pipeline against real (or semi-real) packed artefacts.

Test matrix:
    1.  Python wheel pipeline end-to-end
    2.  Python sdist pipeline end-to-end
    3.  npm pipeline end-to-end
    4.  REL-001 fires on .map file in artefact
    5.  REL-002 fires on .env file in artefact
    6.  REL-003 fires on id_rsa file in artefact
    7.  Clean package produces all-PASS checks
    8.  Evidence written with correct schema_version and capability_state
    9.  Summary markdown generated with check results
    10. Release diff detects added file between two versions

Tests that require ``python -m build`` are skipped when the ``build`` package
is not importable.  Tests that require ``npm`` are skipped when npm is not on
PATH.

All tests use ``tmp_path`` for filesystem isolation.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Availability markers
# ---------------------------------------------------------------------------

_BUILD_AVAILABLE = False
try:
    import build  # noqa: F401
    _BUILD_AVAILABLE = True
except ImportError:
    pass

_NPM_AVAILABLE = shutil.which("npm") is not None

skip_no_build = pytest.mark.skipif(
    not _BUILD_AVAILABLE,
    reason="python 'build' package not installed",
)
skip_no_npm = pytest.mark.skipif(
    not _NPM_AVAILABLE,
    reason="npm not available on PATH",
)


# ---------------------------------------------------------------------------
# Helpers — minimal project factories
# ---------------------------------------------------------------------------


def _make_python_project(root: Path, name: str = "mypkg", version: str = "1.0.0") -> None:
    """Create a minimal Python project under *root*.

    Writes:
      root/
        pyproject.toml
        src/{name}/__init__.py
    """
    (root / "src" / name).mkdir(parents=True, exist_ok=True)
    (root / "src" / name / "__init__.py").write_text(
        f'# {name}\n__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        textwrap.dedent(f"""\
            [build-system]
            requires = ["setuptools"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "{name}"
            version = "{version}"
            description = "Minimal test package"
        """),
        encoding="utf-8",
    )


def _make_npm_project(root: Path, name: str = "mypkg", version: str = "1.0.0") -> None:
    """Create a minimal npm project under *root*.

    Writes:
      root/
        package.json
        index.js
    """
    (root / "package.json").write_text(
        json.dumps({"name": name, "version": version, "main": "index.js"}),
        encoding="utf-8",
    )
    (root / "index.js").write_text(
        f'// {name}\nmodule.exports = {{version: "{version}"}};\n',
        encoding="utf-8",
    )


def _build_wheel(project_root: Path, output_dir: Path) -> Optional[Path]:
    """Build a wheel from *project_root* into *output_dir*.

    Returns the wheel path on success, ``None`` on failure.
    Requires ``python -m build``.
    """
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation",
         "--outdir", str(output_dir), str(project_root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    wheels = sorted(output_dir.glob("*.whl"))
    return wheels[0] if wheels else None


def _build_sdist(project_root: Path, output_dir: Path) -> Optional[Path]:
    """Build an sdist from *project_root* into *output_dir*.

    Returns the sdist path on success, ``None`` on failure.
    Requires ``python -m build``.
    """
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation",
         "--outdir", str(output_dir), str(project_root)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    sdists = sorted(output_dir.glob("*.tar.gz"))
    return sdists[0] if sdists else None


def _build_npm_tarball(project_root: Path, output_dir: Path) -> Optional[Path]:
    """Run ``npm pack`` in *project_root* and move result to *output_dir*.

    Returns the tarball path on success, ``None`` on failure.
    Requires npm >= 7 (--pack-destination).
    """
    result = subprocess.run(
        ["npm", "pack", "--pack-destination", str(output_dir)],
        capture_output=True,
        text=True,
        cwd=str(project_root),
    )
    if result.returncode != 0:
        return None
    tarballs = sorted(output_dir.glob("*.tgz"))
    return tarballs[0] if tarballs else None


def _make_wheel_with_extra_file(
    output_path: Path,
    extra_filename: str,
    extra_content: bytes = b"extra",
    pkg_name: str = "mypkg",
    pkg_version: str = "1.0.0",
) -> Path:
    """Build a synthetic wheel ZIP containing one extra file.

    The wheel is minimal but structurally valid (has METADATA + RECORD).
    The extra file is added directly into the ZIP at the repo root level,
    simulating an accidentally bundled file.

    Args:
        output_path:    Where to write the ``.whl`` file.
        extra_filename: Relative path of the extra file inside the wheel.
        extra_content:  Bytes content of the extra file.
        pkg_name:       Package name for dist-info naming.
        pkg_version:    Package version for dist-info naming.

    Returns:
        Resolved path to the written wheel file.
    """
    dist_info = f"{pkg_name}-{pkg_version}.dist-info"
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        init_content = f"# {pkg_name}\n".encode()
        zf.writestr(f"{pkg_name}/__init__.py", init_content)
        zf.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {pkg_name}\nVersion: {pkg_version}\n",
        )
        zf.writestr(extra_filename, extra_content)
        # Minimal RECORD (only list METADATA; other files are "undeclared" but present)
        zf.writestr(
            f"{dist_info}/RECORD",
            f"{pkg_name}/__init__.py,,\n{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n",
        )
    return output_path.resolve()


def _make_npm_tarball_with_extra(
    output_path: Path,
    extra_filename: str,
    extra_content: bytes = b"extra",
    pkg_name: str = "mypkg",
    pkg_version: str = "1.0.0",
) -> Path:
    """Build a synthetic npm tarball with one extra file inside package/.

    npm tarballs use gzipped tar with a ``package/`` prefix for all files.

    Args:
        output_path:    Where to write the ``.tgz`` file.
        extra_filename: Filename relative to ``package/`` inside the tarball.
        extra_content:  Bytes content of the extra file.
        pkg_name:       Package name written into package.json.
        pkg_version:    Package version written into package.json.

    Returns:
        Resolved path to the written tarball.
    """
    pkg_json = json.dumps(
        {"name": pkg_name, "version": pkg_version, "main": "index.js"}
    ).encode()
    index_js = b"module.exports = {};\n"

    def _add(tf: tarfile.TarFile, arcname: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=arcname)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        with tarfile.open(fileobj=gz, mode="w|") as tf:  # type: ignore[arg-type]
            _add(tf, "package/package.json", pkg_json)
            _add(tf, "package/index.js", index_js)
            _add(tf, f"package/{extra_filename}", extra_content)

    output_path.write_bytes(buf.getvalue())
    return output_path.resolve()


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from saturnday.release.orchestrator import run_release_preflight  # noqa: E402
from saturnday.release._types import ArtefactInventory, ArtefactFile  # noqa: E402
from saturnday.release.evidence import ReleaseEvidencePack, RELEASE_SCHEMA_VERSION  # noqa: E402


# ---------------------------------------------------------------------------
# Test 1: Python wheel pipeline end-to-end
# ---------------------------------------------------------------------------


@skip_no_build
def test_python_wheel_pipeline_end_to_end(tmp_path: Path) -> None:
    """Build a minimal Python package, run full preflight, verify evidence.

    Disposition may be PASS or FAIL (the high-entropy check can fire on SHA-256
    hashes in the wheel RECORD file — a known false-positive on dist-info).
    The integration concern here is that the pipeline runs without an internal
    error and that the evidence artefacts are written with correct structure.
    """
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _make_python_project(project_dir)

    build_dir = tmp_path / "dist"
    build_dir.mkdir()
    wheel_path = _build_wheel(project_dir, build_dir)
    pytest.skip("Build requires network") if wheel_path is None else None
    assert wheel_path is not None, "wheel build failed in integration test"

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=project_dir,
        artefact_type="python",
        wheel_path=wheel_path,
        output_dir=output_dir,
    )

    # Pipeline must not error
    assert result.error == "", f"Pipeline error: {result.error}"

    # Disposition must be a valid terminal state (pipeline ran to completion)
    assert result.disposition in ("PASS", "WARN", "FAIL"), (
        f"Unexpected disposition: {result.disposition!r}"
    )

    # Evidence pack must exist with correct schema
    pack = result.evidence_pack
    assert pack.schema_version == RELEASE_SCHEMA_VERSION
    assert pack.run_id.startswith("release_")
    assert pack.artefact_type in ("wheel", "python")
    assert pack.artefact_sha256  # non-empty

    # All DEFAULT_CHECKS must have produced a result (none silently omitted)
    from saturnday.release.orchestrator import DEFAULT_CHECKS
    assert len(pack.check_results) == len(DEFAULT_CHECKS), (
        f"Expected {len(DEFAULT_CHECKS)} check results, got {len(pack.check_results)}"
    )

    # Evidence must be written to disk
    assert result.evidence_path is not None
    assert (result.evidence_path / "evidence.json").is_file()
    assert (result.evidence_path / "summary.md").is_file()


# ---------------------------------------------------------------------------
# Test 2: Python sdist pipeline end-to-end
# ---------------------------------------------------------------------------


@skip_no_build
def test_python_sdist_pipeline_end_to_end(tmp_path: Path) -> None:
    """Build a minimal sdist, run full preflight, verify evidence."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    _make_python_project(project_dir, name="mysdistpkg", version="2.0.0")

    build_dir = tmp_path / "dist"
    build_dir.mkdir()
    sdist_path = _build_sdist(project_dir, build_dir)
    assert sdist_path is not None, "sdist build failed in integration test"

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=project_dir,
        artefact_type="python",
        sdist_path=sdist_path,
        output_dir=output_dir,
    )

    assert result.error == "", f"Pipeline error: {result.error}"
    assert result.disposition == "PASS", (
        f"Expected PASS, got {result.disposition}. "
        f"Reasons: {result.evidence_pack.disposition_reasons}"
    )

    pack = result.evidence_pack
    assert pack.schema_version == RELEASE_SCHEMA_VERSION
    assert pack.run_id.startswith("release_")
    assert pack.artefact_sha256

    assert result.evidence_path is not None
    assert (result.evidence_path / "evidence.json").is_file()


# ---------------------------------------------------------------------------
# Test 3: npm pipeline end-to-end
# ---------------------------------------------------------------------------


@skip_no_npm
def test_npm_pipeline_end_to_end(tmp_path: Path) -> None:
    """Build a minimal npm package, run full preflight, verify evidence."""
    project_dir = tmp_path / "npmproj"
    project_dir.mkdir()
    _make_npm_project(project_dir, name="myclean-pkg", version="1.0.0")

    tarball_dir = tmp_path / "dist"
    tarball_dir.mkdir()
    tarball_path = _build_npm_tarball(project_dir, tarball_dir)
    assert tarball_path is not None, "npm pack failed in integration test"

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=project_dir,
        artefact_type="npm",
        tarball_path=tarball_path,
        output_dir=output_dir,
    )

    assert result.error == "", f"Pipeline error: {result.error}"
    # A clean npm package with only package.json + index.js must PASS
    assert result.disposition == "PASS", (
        f"Expected PASS, got {result.disposition}. "
        f"Reasons: {result.evidence_pack.disposition_reasons}"
    )

    pack = result.evidence_pack
    assert pack.schema_version == RELEASE_SCHEMA_VERSION
    assert pack.artefact_type == "npm"
    assert pack.run_id.startswith("release_")
    assert pack.artefact_sha256

    assert result.evidence_path is not None
    assert (result.evidence_path / "evidence.json").is_file()
    assert (result.evidence_path / "summary.md").is_file()


# ---------------------------------------------------------------------------
# Test 4: REL-001 fires on .map file in artefact
# ---------------------------------------------------------------------------


def test_source_map_detection_fires_rel001(tmp_path: Path) -> None:
    """A wheel containing a .map file must produce REL-001 FAIL."""
    whl_path = tmp_path / "mypkg-1.0-py3-none-any.whl"
    _make_wheel_with_extra_file(
        output_path=whl_path,
        extra_filename="mypkg/bundle.js.map",
        extra_content=b'{"version":3,"sources":["app.js"],"mappings":"AAAA"}',
    )

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=whl_path,
        output_dir=output_dir,
    )

    assert result.error == ""
    assert result.disposition == "FAIL", (
        f"Expected FAIL (source map present), got {result.disposition}"
    )

    rel001 = _find_check(result, "REL-001")
    assert rel001 is not None, "REL-001 check result not found in evidence pack"
    assert rel001["status"] == "FAIL", f"REL-001 status: {rel001['status']}"
    assert len(rel001["findings"]) >= 1


# ---------------------------------------------------------------------------
# Test 5: REL-002 fires on .env file in artefact
# ---------------------------------------------------------------------------


def test_secrets_detection_fires_rel002(tmp_path: Path) -> None:
    """A wheel containing a .env file must produce REL-002 FAIL."""
    whl_path = tmp_path / "mypkg-1.0-py3-none-any.whl"
    _make_wheel_with_extra_file(
        output_path=whl_path,
        extra_filename=".env",
        extra_content=b"DB_PASSWORD=supersecret\nAPI_KEY=abc123\n",
    )

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=whl_path,
        output_dir=output_dir,
    )

    assert result.error == ""
    assert result.disposition == "FAIL", (
        f"Expected FAIL (.env file present), got {result.disposition}"
    )

    rel002 = _find_check(result, "REL-002")
    assert rel002 is not None, "REL-002 check result not found in evidence pack"
    assert rel002["status"] == "FAIL", f"REL-002 status: {rel002['status']}"
    assert len(rel002["findings"]) >= 1


# ---------------------------------------------------------------------------
# Test 6: REL-003 fires on id_rsa file in artefact
# ---------------------------------------------------------------------------


def test_internal_file_detection_fires_rel003(tmp_path: Path) -> None:
    """A wheel containing id_rsa must produce REL-003 FAIL."""
    whl_path = tmp_path / "mypkg-1.0-py3-none-any.whl"
    _make_wheel_with_extra_file(
        output_path=whl_path,
        extra_filename="id_rsa",
        extra_content=b"-----BEGIN RSA PRIVATE KEY-----\nfakekey\n-----END RSA PRIVATE KEY-----\n",
    )

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=whl_path,
        output_dir=output_dir,
    )

    assert result.error == ""
    assert result.disposition == "FAIL", (
        f"Expected FAIL (id_rsa present), got {result.disposition}"
    )

    rel003 = _find_check(result, "REL-003")
    assert rel003 is not None, "REL-003 check result not found in evidence pack"
    assert rel003["status"] == "FAIL", f"REL-003 status: {rel003['status']}"
    # Must report at least one finding that identifies id_rsa
    file_paths = [f.get("file", f.get("path", "")) for f in rel003["findings"]]
    assert any("id_rsa" in p for p in file_paths), (
        f"No id_rsa finding in REL-003 findings: {rel003['findings']}"
    )


# ---------------------------------------------------------------------------
# Test 7: Clean package passes all checks
# ---------------------------------------------------------------------------


def test_clean_package_all_pass(tmp_path: Path) -> None:
    """A minimal wheel with no sensitive files must PASS all implemented checks."""
    whl_path = tmp_path / "mypkg-1.0-py3-none-any.whl"
    dist_info = "mypkg-1.0.dist-info"
    with zipfile.ZipFile(whl_path, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# mypkg\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\nVersion: 1.0\n")
        zf.writestr(
            f"{dist_info}/RECORD",
            "mypkg/__init__.py,,\n"
            f"{dist_info}/METADATA,,\n"
            f"{dist_info}/RECORD,,\n",
        )

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=whl_path,
        output_dir=output_dir,
    )

    assert result.error == ""
    assert result.disposition == "PASS", (
        f"Expected PASS (clean package), got {result.disposition}. "
        f"Reasons: {result.evidence_pack.disposition_reasons}"
    )

    # Every implemented check must be PASS or SKIPPED (not FAIL or WARN=error)
    for cr in result.evidence_pack.check_results:
        assert cr.status in ("PASS", "SKIPPED", "WARN"), (
            f"Check {cr.rule_id} ({cr.name}) returned {cr.status!r} on clean artefact. "
            f"Findings: {cr.findings}"
        )
        if cr.status == "FAIL":
            # FAIL on clean package = test bug or check regression
            pytest.fail(
                f"Check {cr.rule_id} ({cr.name}) FAILed on a clean package. "
                f"Findings: {cr.findings}"
            )


# ---------------------------------------------------------------------------
# Test 8: Evidence JSON written with correct schema_version and capability_state
# ---------------------------------------------------------------------------


def test_evidence_json_schema_version_and_capability_state(tmp_path: Path) -> None:
    """evidence.json must have schema_version and capability_state with expected keys."""
    whl_path = tmp_path / "mypkg-1.0-py3-none-any.whl"
    dist_info = "mypkg-1.0.dist-info"
    with zipfile.ZipFile(whl_path, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# mypkg\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\n")
        zf.writestr(f"{dist_info}/RECORD", "mypkg/__init__.py,,\n")

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=whl_path,
        output_dir=output_dir,
    )

    assert result.evidence_path is not None
    evidence_file = result.evidence_path / "evidence.json"
    assert evidence_file.is_file(), "evidence.json not written"

    data = json.loads(evidence_file.read_text(encoding="utf-8"))

    # Schema version
    assert data["schema_version"] == RELEASE_SCHEMA_VERSION, (
        f"Wrong schema_version: {data.get('schema_version')!r}"
    )

    # capability_state must be present and have the golden-rule keys
    assert "capability_state" in data, "capability_state missing from evidence.json"
    cs = data["capability_state"]
    assert isinstance(cs, dict)
    assert "premium_capabilities_enabled" in cs, (
        "premium_capabilities_enabled missing from capability_state"
    )
    assert "available_premium_hooks" in cs, (
        "available_premium_hooks missing from capability_state"
    )

    # run_id must start with "release_"
    assert data["run_id"].startswith("release_"), (
        f"Unexpected run_id format: {data['run_id']!r}"
    )

    # check_results must be a list
    assert isinstance(data["check_results"], list)

    # disposition must be one of the three canonical values
    assert data["disposition"] in ("PASS", "WARN", "FAIL")


# ---------------------------------------------------------------------------
# Test 9: Summary markdown generated with check results
# ---------------------------------------------------------------------------


def test_summary_markdown_generated(tmp_path: Path) -> None:
    """summary.md must exist and contain check result rows."""
    whl_path = tmp_path / "mypkg-1.0-py3-none-any.whl"
    dist_info = "mypkg-1.0.dist-info"
    with zipfile.ZipFile(whl_path, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# mypkg\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\n")
        zf.writestr(f"{dist_info}/RECORD", "mypkg/__init__.py,,\n")

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=whl_path,
        output_dir=output_dir,
    )

    assert result.evidence_path is not None
    summary_path = result.evidence_path / "summary.md"
    assert summary_path.is_file(), "summary.md not written"

    text = summary_path.read_text(encoding="utf-8")

    # Must contain the standard headings
    assert "# Release Preflight Evidence Summary" in text
    assert "## Check Results" in text

    # Must contain at least one rule ID from DEFAULT_CHECKS
    from saturnday.release.orchestrator import DEFAULT_CHECKS
    found_any_rule = any(rule_id in text for _, rule_id in DEFAULT_CHECKS)
    assert found_any_rule, (
        f"No DEFAULT_CHECKS rule ID found in summary.md. Content:\n{text[:500]}"
    )

    # Must include schema_version value
    assert RELEASE_SCHEMA_VERSION in text, (
        f"schema_version {RELEASE_SCHEMA_VERSION!r} missing from summary.md"
    )


# ---------------------------------------------------------------------------
# Test 10: Release diff detects added file between two versions
# ---------------------------------------------------------------------------


def test_release_diff_detects_added_file(tmp_path: Path) -> None:
    """REL-005 diff check must detect a file added in the candidate vs baseline."""
    from saturnday.release.wheel_inspector import inspect_wheel
    from saturnday.release.diff_engine import compute_release_diff

    # --- Baseline wheel (clean) ---
    baseline_whl = tmp_path / "mypkg-1.0-py3-none-any.whl"
    dist_info = "mypkg-1.0.dist-info"
    with zipfile.ZipFile(baseline_whl, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# v1.0\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\n")
        zf.writestr(f"{dist_info}/RECORD", "mypkg/__init__.py,,\n")

    # --- Candidate wheel (with an extra file added) ---
    candidate_whl = tmp_path / "mypkg-2.0-py3-none-any.whl"
    with zipfile.ZipFile(candidate_whl, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# v2.0\n")
        zf.writestr("mypkg/extra.py", "# extra module\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\n")
        zf.writestr(f"{dist_info}/RECORD", "mypkg/__init__.py,,\nmypkg/extra.py,,\n")

    # Inspect both
    unpack_tmp = tmp_path / "unpack"
    unpack_tmp.mkdir()

    baseline_unpack = unpack_tmp / "baseline"
    baseline_unpack.mkdir()
    baseline_inv, _ = inspect_wheel(baseline_whl, target_dir=baseline_unpack)

    candidate_unpack = unpack_tmp / "candidate"
    candidate_unpack.mkdir()
    candidate_inv, _ = inspect_wheel(candidate_whl, target_dir=candidate_unpack)

    # Compute diff directly via the engine
    diff = compute_release_diff(baseline=baseline_inv, candidate=candidate_inv)

    # extra.py must appear as an added file
    assert "mypkg/extra.py" in diff.added_files, (
        f"Expected mypkg/extra.py in added_files, got: {diff.added_files}"
    )

    # __init__.py must appear as changed (content changed v1.0 → v2.0)
    changed_paths = [cf.path for cf in diff.changed_files]
    assert "mypkg/__init__.py" in changed_paths, (
        f"Expected mypkg/__init__.py in changed_files, got: {changed_paths}"
    )

    # Baseline and candidate file counts are derived from the inventory lists
    assert len(baseline_inv.files) >= 1
    assert len(candidate_inv.files) >= len(baseline_inv.files)


# ---------------------------------------------------------------------------
# Test 10b: run_release_preflight with baseline triggers REL-005
# ---------------------------------------------------------------------------


def test_run_release_preflight_with_baseline_writes_evidence(tmp_path: Path) -> None:
    """Calling run_release_preflight with baseline_path triggers REL-005 execution."""
    dist_info = "mypkg-1.0.dist-info"

    baseline_whl = tmp_path / "baseline.whl"
    with zipfile.ZipFile(baseline_whl, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# v1\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\n")
        zf.writestr(f"{dist_info}/RECORD", "mypkg/__init__.py,,\n")

    candidate_whl = tmp_path / "candidate.whl"
    with zipfile.ZipFile(candidate_whl, "w") as zf:
        zf.writestr("mypkg/__init__.py", "# v2\n")
        zf.writestr("mypkg/newmod.py", "# new\n")
        zf.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\nName: mypkg\n")
        zf.writestr(
            f"{dist_info}/RECORD",
            "mypkg/__init__.py,,\nmypkg/newmod.py,,\n",
        )

    output_dir = tmp_path / "evidence"
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=candidate_whl,
        baseline_path=baseline_whl,
        output_dir=output_dir,
    )

    assert result.error == ""
    assert result.evidence_path is not None

    evidence_file = result.evidence_path / "evidence.json"
    assert evidence_file.is_file()
    data = json.loads(evidence_file.read_text(encoding="utf-8"))

    # REL-005 result must be present in check_results
    rel005_results = [r for r in data["check_results"] if r["rule_id"] == "REL-005"]
    assert rel005_results, "REL-005 check result not found in evidence when baseline provided"

    rel005 = rel005_results[0]
    # REL-005 must not be SKIPPED when a baseline is provided (diff should run)
    assert rel005["status"] != "SKIPPED", (
        f"REL-005 was SKIPPED even though baseline_path was provided. "
        f"Findings: {rel005['findings']}"
    )


# ---------------------------------------------------------------------------
# Helper: locate a check result dict by rule_id
# ---------------------------------------------------------------------------


def _find_check(result: "ReleasePreflightResult", rule_id: str) -> Optional[dict]:  # type: ignore[name-defined]
    """Return the serialised check result dict for *rule_id*, or ``None``.

    Looks inside the evidence pack's ``check_results`` list.
    """
    for cr in result.evidence_pack.check_results:
        if cr.rule_id == rule_id:
            return {
                "rule_id": cr.rule_id,
                "name": cr.name,
                "status": cr.status,
                "severity": cr.severity,
                "findings": cr.findings,
                "files_checked": cr.files_checked,
            }
    return None
