"""Tests for saturnday.release.checks.allowlist_manifest (RS-010).

Covers:
- No manifest supplied → SKIPPED with info message
- Valid manifest, all required files present → PASS
- Valid manifest, a required file missing → FAIL with missing_required finding
- Valid manifest, deny_always pattern matched → FAIL with denied_always finding
- Invalid manifest (no version field) → FAIL with manifest_invalid finding
- Glob patterns in required_files → PASS when a file matches the glob
- deny_always with glob pattern matching basename only → FAIL
- Multiple required missing → multiple findings, single FAIL
- deny_always and missing_required both present → FAIL with both finding kinds
- Empty required_files and deny_always lists → PASS (nothing to check)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.checks.allowlist_manifest import CHECK_NAME, RULE_ID, run_check


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inventory(paths: list[str], artefact_type: str = "wheel") -> ArtefactInventory:
    """Build a minimal ArtefactInventory for testing.

    Args:
        paths:         List of relative file paths to include in the inventory.
        artefact_type: Artefact type string (default ``"wheel"``).

    Returns:
        :class:`~saturnday.release._types.ArtefactInventory` with synthetic
        file entries (size=0, sha256="deadbeef").
    """
    return ArtefactInventory(
        artefact_type=artefact_type,
        artefact_path="/tmp/test.whl",
        artefact_sha256="deadbeef" * 8,
        files=[
            ArtefactFile(path=p, size=0, sha256="deadbeef" * 8)
            for p in paths
        ],
    )


def _minimal_manifest(
    required_files: list[str] | None = None,
    deny_always: list[str] | None = None,
) -> dict:
    """Build a minimal valid manifest dict.

    Args:
        required_files: Patterns for the ``required_files`` key.
        deny_always:    Patterns for the ``deny_always`` key.

    Returns:
        Manifest dict with ``version`` set to ``"1.0"``.
    """
    manifest: dict = {"version": "1.0"}
    if required_files is not None:
        manifest["required_files"] = required_files
    if deny_always is not None:
        manifest["deny_always"] = deny_always
    return manifest


# Convenience: a Path that need not exist (this check never reads the filesystem)
_UNPACK_DIR = Path("/tmp/unpack")


# ---------------------------------------------------------------------------
# Test: no manifest → SKIPPED
# ---------------------------------------------------------------------------


def test_no_manifest_returns_skipped() -> None:
    """When manifest=None the check must return SKIPPED, never FAIL."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    result = run_check(inventory, _UNPACK_DIR, manifest=None)

    assert result.status == "SKIPPED"
    assert result.rule_id == RULE_ID
    assert result.name == CHECK_NAME
    assert result.severity == "info"
    assert result.files_checked == 0
    # Must include an explanatory finding so callers can surface the reason
    assert len(result.findings) >= 1
    assert result.findings[0]["kind"] == "no_manifest"
    assert "saturnday-release-manifest.yaml" in result.findings[0]["detail"]


# ---------------------------------------------------------------------------
# Test: valid manifest, all required present → PASS
# ---------------------------------------------------------------------------


def test_all_required_present_returns_pass() -> None:
    """All required_files patterns match → PASS with no findings."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        "saturnday/cli.py",
        "saturnday-1.0.0.dist-info/METADATA",
    ])
    manifest = _minimal_manifest(
        required_files=[
            "saturnday/__init__.py",
            "saturnday/cli.py",
            "saturnday-1.0.0.dist-info/METADATA",
        ]
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "PASS"
    assert result.severity == "error"
    assert result.findings == []
    assert result.files_checked == 3


# ---------------------------------------------------------------------------
# Test: required file missing → FAIL
# ---------------------------------------------------------------------------


def test_missing_required_file_returns_fail() -> None:
    """A required_files pattern that matches nothing produces a FAIL."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    manifest = _minimal_manifest(
        required_files=[
            "saturnday/__init__.py",
            "saturnday/cli.py",  # not in inventory
        ]
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    missing_findings = [f for f in result.findings if f["kind"] == "missing_required"]
    assert len(missing_findings) == 1
    assert "saturnday/cli.py" in missing_findings[0]["file"]
    assert "saturnday/cli.py" in missing_findings[0]["detail"]


# ---------------------------------------------------------------------------
# Test: deny_always matched → FAIL
# ---------------------------------------------------------------------------


def test_deny_always_match_returns_fail() -> None:
    """A file matching a deny_always pattern produces a FAIL."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        ".env",
    ])
    manifest = _minimal_manifest(deny_always=["*.env"])
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    denied_findings = [f for f in result.findings if f["kind"] == "denied_always"]
    assert len(denied_findings) == 1
    assert denied_findings[0]["file"] == ".env"
    assert "deny_always" in denied_findings[0]["detail"]


# ---------------------------------------------------------------------------
# Test: invalid manifest (no version) → FAIL
# ---------------------------------------------------------------------------


def test_invalid_manifest_no_version_returns_fail() -> None:
    """A manifest missing the version field produces a FAIL."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    manifest: dict = {"required_files": ["saturnday/__init__.py"]}
    # No 'version' key

    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    assert len(result.findings) == 1
    assert result.findings[0]["kind"] == "manifest_invalid"
    assert "version" in result.findings[0]["detail"]
    # files_checked is 0 because validation fails before the inventory scan
    assert result.files_checked == 0


# ---------------------------------------------------------------------------
# Test: glob patterns in required_files → PASS when matched
# ---------------------------------------------------------------------------


def test_glob_in_required_files_pass() -> None:
    """Glob patterns in required_files match correctly against inventory paths."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        "saturnday/cli.py",
        "saturnday/run/planner.py",
        "saturnday-1.2.3.dist-info/METADATA",
    ])
    manifest = _minimal_manifest(
        required_files=[
            "saturnday/__init__.py",
            "saturnday-*.dist-info/METADATA",  # glob with wildcard
        ]
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "PASS"
    assert result.findings == []


# ---------------------------------------------------------------------------
# Test: glob pattern in required_files → FAIL when not matched
# ---------------------------------------------------------------------------


def test_glob_in_required_files_fail_when_no_match() -> None:
    """A glob in required_files that matches no file produces missing_required FAIL."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        "saturnday/cli.py",
    ])
    manifest = _minimal_manifest(
        required_files=[
            "saturnday-*.dist-info/METADATA",  # nothing in inventory matches
        ]
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    missing = [f for f in result.findings if f["kind"] == "missing_required"]
    assert len(missing) == 1
    assert "saturnday-*.dist-info/METADATA" in missing[0]["file"]


# ---------------------------------------------------------------------------
# Test: deny_always basename-only glob → FAIL
# ---------------------------------------------------------------------------


def test_deny_always_basename_glob_matches() -> None:
    """deny_always patterns match against the basename, not only the full path."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        "scripts/generate_licence.py",
    ])
    manifest = _minimal_manifest(deny_always=["generate_licence*"])
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    denied = [f for f in result.findings if f["kind"] == "denied_always"]
    assert len(denied) == 1
    assert "generate_licence.py" in denied[0]["file"]


# ---------------------------------------------------------------------------
# Test: multiple missing required → multiple findings
# ---------------------------------------------------------------------------


def test_multiple_missing_required_produces_multiple_findings() -> None:
    """Each unmatched required pattern generates a separate finding."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    manifest = _minimal_manifest(
        required_files=[
            "saturnday/__init__.py",
            "saturnday/cli.py",       # missing
            "saturnday/governance.py", # missing
        ]
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    missing = [f for f in result.findings if f["kind"] == "missing_required"]
    assert len(missing) == 2
    missing_files = {f["file"] for f in missing}
    assert "saturnday/cli.py" in missing_files
    assert "saturnday/governance.py" in missing_files


# ---------------------------------------------------------------------------
# Test: deny_always and missing_required both triggered
# ---------------------------------------------------------------------------


def test_deny_and_missing_both_produce_findings() -> None:
    """Both denied_always and missing_required findings can coexist in one result."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        ".env",
    ])
    manifest = _minimal_manifest(
        required_files=["saturnday/cli.py"],  # missing
        deny_always=["*.env"],                # .env matches
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    kinds = {f["kind"] for f in result.findings}
    assert "missing_required" in kinds
    assert "denied_always" in kinds


# ---------------------------------------------------------------------------
# Test: empty required and deny lists → PASS
# ---------------------------------------------------------------------------


def test_empty_lists_pass() -> None:
    """An otherwise valid manifest with empty required_files and deny_always → PASS."""
    inventory = _make_inventory([
        "saturnday/__init__.py",
        "saturnday/cli.py",
    ])
    manifest = _minimal_manifest(required_files=[], deny_always=[])
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "PASS"
    assert result.findings == []


# ---------------------------------------------------------------------------
# Test: manifest with non-dict type → FAIL (manifest_invalid)
# ---------------------------------------------------------------------------


def test_manifest_not_a_dict_returns_fail() -> None:
    """A manifest that is not a dict produces a manifest_invalid FAIL."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    result = run_check(inventory, _UNPACK_DIR, manifest="not-a-dict")  # type: ignore[arg-type]

    assert result.status == "FAIL"
    assert result.findings[0]["kind"] == "manifest_invalid"
    assert "mapping" in result.findings[0]["detail"].lower()


# ---------------------------------------------------------------------------
# Test: manifest with empty version string → FAIL
# ---------------------------------------------------------------------------


def test_manifest_empty_version_returns_fail() -> None:
    """A manifest whose version is an empty string produces manifest_invalid FAIL."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    manifest: dict = {"version": "   ", "required_files": []}
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "FAIL"
    assert result.findings[0]["kind"] == "manifest_invalid"


# ---------------------------------------------------------------------------
# Test: result fields are always populated correctly on PASS
# ---------------------------------------------------------------------------


def test_result_fields_on_pass() -> None:
    """On PASS, rule_id, name, status, severity, files_checked are canonical."""
    inventory = _make_inventory(["saturnday/__init__.py"])
    manifest = _minimal_manifest(required_files=["saturnday/__init__.py"])
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.rule_id == "REL-004"
    assert result.name == "allowlist_manifest"
    assert result.status == "PASS"
    assert result.severity == "error"
    assert result.files_checked == 1
    assert isinstance(result.elapsed_s, float)
    assert result.elapsed_s >= 0.0


# ---------------------------------------------------------------------------
# Test: npm artefact type also works
# ---------------------------------------------------------------------------


def test_npm_artefact_type_pass() -> None:
    """The check operates uniformly on npm inventories."""
    inventory = _make_inventory(
        paths=["index.js", "package.json"],
        artefact_type="npm",
    )
    manifest = _minimal_manifest(
        required_files=["index.js", "package.json"],
        deny_always=[".npmrc"],
    )
    result = run_check(inventory, _UNPACK_DIR, manifest=manifest)

    assert result.status == "PASS"
    assert result.findings == []
