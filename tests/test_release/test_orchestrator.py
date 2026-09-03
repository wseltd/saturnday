"""Tests for saturnday.release.orchestrator (RS-006).

Covers:
- ReleasePreflightResult structure
- DEFAULT_CHECKS list shape
- run_release_preflight with a mock pre-built wheel (skips real build)
- SKIPPED check result when check module is missing
- Error path: invalid artefact_type
- Error path: invalid wheel path
- Error path: build failure
- Premium release_governance hook called when registered
- Disposition propagation (PASS / WARN / FAIL)
- Evidence pack capability_state always present (golden rule)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.release.orchestrator import (
    DEFAULT_CHECKS,
    ReleasePreflightResult,
    _run_check,
    run_release_preflight,
)
from saturnday.release.evidence import ReleaseCheckResult, RELEASE_SCHEMA_VERSION
from saturnday.release._types import ArtefactFile, ArtefactInventory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def dummy_inventory(tmp_path: Path) -> ArtefactInventory:
    """Minimal ArtefactInventory for unit tests."""
    dummy_whl = tmp_path / "dummy-1.0-py3-none-any.whl"
    # Build a minimal ZIP that looks like a wheel
    import zipfile
    with zipfile.ZipFile(dummy_whl, "w") as zf:
        zf.writestr("dummy/__init__.py", "# dummy\n")
        zf.writestr("dummy-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: dummy\n")
        zf.writestr("dummy-1.0.dist-info/RECORD", "dummy/__init__.py,,\n")

    sha256 = hashlib.sha256(dummy_whl.read_bytes()).hexdigest()
    return ArtefactInventory(
        artefact_type="wheel",
        artefact_path=str(dummy_whl),
        artefact_sha256=sha256,
        files=[
            ArtefactFile(path="dummy/__init__.py", size=8, sha256="ab" * 16),
        ],
    )


# ---------------------------------------------------------------------------
# DEFAULT_CHECKS shape
# ---------------------------------------------------------------------------


def test_default_checks_are_tuples() -> None:
    assert all(len(item) == 2 for item in DEFAULT_CHECKS), (
        "Every entry in DEFAULT_CHECKS must be a 2-tuple (module_name, rule_id)"
    )


def test_default_checks_rule_ids_are_rel() -> None:
    for _, rule_id in DEFAULT_CHECKS:
        assert rule_id.startswith("REL-"), f"rule_id {rule_id!r} should start with REL-"


def test_default_checks_count() -> None:
    assert len(DEFAULT_CHECKS) == 5, "Expected exactly 5 default checks (REL-001 through REL-005)"


# ---------------------------------------------------------------------------
# _run_check: SKIPPED when module absent
# ---------------------------------------------------------------------------


def test_run_check_skipped_when_module_missing(
    dummy_inventory: ArtefactInventory,
    tmp_path: Path,
) -> None:
    result = _run_check(
        check_module_name="nonexistent_check_xyz",
        rule_id="REL-999",
        inventory=dummy_inventory,
        unpack_dir=tmp_path,
        manifest=None,
        baseline_path=None,
    )
    assert result.status == "SKIPPED"
    assert result.rule_id == "REL-999"
    assert result.name == "nonexistent_check_xyz"
    assert result.elapsed_s == 0.0


def test_run_check_warn_when_exception_raised(
    dummy_inventory: ArtefactInventory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check module importable but run_check() raises — should return WARN."""
    import types
    bad_mod = types.ModuleType("saturnday.release.checks.bad_check")

    def _bad_run_check(**_kwargs):
        raise RuntimeError("simulated check failure")

    bad_mod.run_check = _bad_run_check

    import sys
    monkeypatch.setitem(sys.modules, "saturnday.release.checks.bad_check", bad_mod)

    result = _run_check(
        check_module_name="bad_check",
        rule_id="REL-998",
        inventory=dummy_inventory,
        unpack_dir=tmp_path,
        manifest=None,
        baseline_path=None,
    )
    assert result.status == "WARN"
    assert "simulated check failure" in result.findings[0]["message"]


def test_run_check_missing_run_check_fn(
    dummy_inventory: ArtefactInventory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module importable but has no run_check attribute — should return SKIPPED."""
    import types
    empty_mod = types.ModuleType("saturnday.release.checks.empty_check")
    import sys
    monkeypatch.setitem(sys.modules, "saturnday.release.checks.empty_check", empty_mod)

    result = _run_check(
        check_module_name="empty_check",
        rule_id="REL-997",
        inventory=dummy_inventory,
        unpack_dir=tmp_path,
        manifest=None,
        baseline_path=None,
    )
    assert result.status == "SKIPPED"


# ---------------------------------------------------------------------------
# run_release_preflight: invalid artefact_type
# ---------------------------------------------------------------------------


def test_run_release_preflight_invalid_type(tmp_path: Path) -> None:
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="oci",  # unsupported
    )
    assert result.disposition == "FAIL"
    assert result.error
    assert "oci" in result.error.lower() or "Unsupported" in result.error


# ---------------------------------------------------------------------------
# run_release_preflight: non-existent wheel path
# ---------------------------------------------------------------------------


def test_run_release_preflight_bad_wheel_path(tmp_path: Path) -> None:
    result = run_release_preflight(
        repo_path=tmp_path,
        artefact_type="python",
        wheel_path=tmp_path / "nonexistent.whl",
    )
    assert result.disposition == "FAIL"
    assert result.error


# ---------------------------------------------------------------------------
# run_release_preflight: pre-built wheel — all checks SKIPPED (no check modules yet)
# ---------------------------------------------------------------------------


def test_run_release_preflight_prebuilt_wheel_no_errors(
    tmp_path: Path,
    dummy_inventory: ArtefactInventory,
) -> None:
    """A clean dummy inventory produces a PASS disposition with no error.

    As check modules are implemented one by one, individual checks will
    transition from SKIPPED to PASS (or FAIL when findings exist).  This
    test asserts only the invariants that must hold regardless of how many
    check modules are currently implemented:

    - No pipeline error.
    - Overall disposition is PASS (no error-severity failures on a clean artefact).
    - Exactly DEFAULT_CHECKS checks are recorded.
    - Every check status is one of the valid terminal states.
    """
    dummy_whl = Path(dummy_inventory.artefact_path)

    # Mock inspect_wheel to return our pre-built inventory
    with patch("saturnday.release.orchestrator._prepare_python_artefact") as mock_prep:
        unpack_dir = tmp_path / "unpacked"
        unpack_dir.mkdir()
        mock_prep.return_value = (dummy_inventory, unpack_dir, dummy_whl, None)

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="python",
            wheel_path=dummy_whl,
        )

    assert result.error == ""
    assert result.disposition == "PASS"
    assert result.evidence_pack is not None
    assert result.evidence_pack.schema_version == RELEASE_SCHEMA_VERSION
    assert result.evidence_pack.run_id.startswith("release_")
    assert len(result.evidence_pack.check_results) == len(DEFAULT_CHECKS)
    valid_statuses = {"PASS", "SKIPPED", "WARN"}  # no FAIL expected on a clean dummy artefact
    for cr in result.evidence_pack.check_results:
        assert cr.status in valid_statuses, (
            f"Check {cr.rule_id} ({cr.name}) returned unexpected status {cr.status!r} "
            f"on a clean dummy artefact"
        )


# ---------------------------------------------------------------------------
# run_release_preflight: capability_state always present (premium golden rule)
# ---------------------------------------------------------------------------


def test_run_release_preflight_capability_state_always_present(
    tmp_path: Path,
    dummy_inventory: ArtefactInventory,
) -> None:
    dummy_whl = Path(dummy_inventory.artefact_path)
    with patch("saturnday.release.orchestrator._prepare_python_artefact") as mock_prep:
        unpack_dir = tmp_path / "unpacked"
        unpack_dir.mkdir()
        mock_prep.return_value = (dummy_inventory, unpack_dir, dummy_whl, None)

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="python",
            wheel_path=dummy_whl,
        )

    cs = result.evidence_pack.capability_state
    assert isinstance(cs, dict)
    assert "premium_capabilities_enabled" in cs
    assert "available_premium_hooks" in cs


# ---------------------------------------------------------------------------
# run_release_preflight: evidence written to custom output_dir
# ---------------------------------------------------------------------------


def test_run_release_preflight_custom_output_dir(
    tmp_path: Path,
    dummy_inventory: ArtefactInventory,
) -> None:
    dummy_whl = Path(dummy_inventory.artefact_path)
    custom_output = tmp_path / "my_evidence"

    with patch("saturnday.release.orchestrator._prepare_python_artefact") as mock_prep:
        unpack_dir = tmp_path / "unpacked"
        unpack_dir.mkdir()
        mock_prep.return_value = (dummy_inventory, unpack_dir, dummy_whl, None)

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="python",
            wheel_path=dummy_whl,
            output_dir=custom_output,
        )

    assert result.evidence_path == custom_output
    assert (custom_output / "evidence.json").is_file()
    assert (custom_output / "summary.md").is_file()


# ---------------------------------------------------------------------------
# run_release_preflight: FAIL disposition when a check returns error-severity FAIL
# ---------------------------------------------------------------------------


def test_run_release_preflight_fail_disposition(
    tmp_path: Path,
    dummy_inventory: ArtefactInventory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a FAIL check result and verify the overall disposition becomes FAIL."""
    dummy_whl = Path(dummy_inventory.artefact_path)

    # Inject a check module that always fails with severity=error
    import types
    blocking_mod = types.ModuleType("saturnday.release.checks.source_map_blocker")

    def _blocking_run_check(inventory, unpack_dir, manifest):
        return ReleaseCheckResult(
            name="source_map_blocker",
            rule_id="REL-001",
            status="FAIL",
            severity="error",
            findings=[{"path": "dist/bundle.js.map", "message": "source map found"}],
            files_checked=1,
        )

    blocking_mod.run_check = _blocking_run_check
    import sys
    monkeypatch.setitem(sys.modules, "saturnday.release.checks.source_map_blocker", blocking_mod)

    with patch("saturnday.release.orchestrator._prepare_python_artefact") as mock_prep:
        unpack_dir = tmp_path / "unpacked"
        unpack_dir.mkdir()
        mock_prep.return_value = (dummy_inventory, unpack_dir, dummy_whl, None)

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="python",
            wheel_path=dummy_whl,
        )

    assert result.disposition == "FAIL"
    rel001 = next(
        (r for r in result.evidence_pack.check_results if r.rule_id == "REL-001"),
        None,
    )
    assert rel001 is not None
    assert rel001.status == "FAIL"
    assert rel001.severity == "error"


# ---------------------------------------------------------------------------
# run_release_preflight: premium hook called when registered
# ---------------------------------------------------------------------------


def test_run_release_preflight_premium_hook_called(
    tmp_path: Path,
    dummy_inventory: ArtefactInventory,
) -> None:
    dummy_whl = Path(dummy_inventory.artefact_path)
    premium_result = ReleaseCheckResult(
        name="premium_org_policy",
        rule_id="REL-P01",
        status="PASS",
        severity="error",
        findings=[],
    )
    mock_handler = MagicMock()
    # apply_policy returns the input list extended with the premium result.
    mock_handler.apply_policy.side_effect = lambda check_results, policy: check_results + [premium_result]

    with (
        patch("saturnday.release.orchestrator._prepare_python_artefact") as mock_prep,
        patch("saturnday.capability_registry.get", return_value=mock_handler),
    ):
        unpack_dir = tmp_path / "unpacked"
        unpack_dir.mkdir()
        mock_prep.return_value = (dummy_inventory, unpack_dir, dummy_whl, None)

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="python",
            wheel_path=dummy_whl,
        )

    mock_handler.apply_policy.assert_called_once()
    # Premium result appended to check_results
    rule_ids = [r.rule_id for r in result.evidence_pack.check_results]
    assert "REL-P01" in rule_ids


# ---------------------------------------------------------------------------
# run_release_preflight: ReleasePreflightResult fields present
# ---------------------------------------------------------------------------


def test_release_preflight_result_fields(
    tmp_path: Path,
    dummy_inventory: ArtefactInventory,
) -> None:
    dummy_whl = Path(dummy_inventory.artefact_path)
    with patch("saturnday.release.orchestrator._prepare_python_artefact") as mock_prep:
        unpack_dir = tmp_path / "unpacked"
        unpack_dir.mkdir()
        mock_prep.return_value = (dummy_inventory, unpack_dir, dummy_whl, None)

        result = run_release_preflight(
            repo_path=tmp_path,
            artefact_type="python",
            wheel_path=dummy_whl,
        )

    assert hasattr(result, "disposition")
    assert hasattr(result, "evidence_pack")
    assert hasattr(result, "evidence_path")
    assert hasattr(result, "error")
    assert result.error == ""
    assert result.disposition in ("PASS", "WARN", "FAIL")
