"""CLI tests for release-preflight and release-approve subcommands (RS-005).

Covers:
- release-preflight --help prints usage
- release-approve --help prints usage
- release-preflight argument parsing (unit — no real build)
- Error: --repo path does not exist
- Error: --wheel path does not exist
- Error: --sdist path does not exist
- Error: --tarball path does not exist
- Error: invalid --type
- Exit code 0 when disposition is PASS (mocked orchestrator)
- Exit code 0 when disposition is WARN (mocked orchestrator)
- Exit code 1 when disposition is FAIL (mocked orchestrator)
- release-approve without premium returns exit code 1
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.cli import build_parser, main


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


def test_release_preflight_help(capsys: pytest.CaptureFixture) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["release-preflight", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "release-preflight" in captured.out
    assert "--wheel" in captured.out
    assert "--sdist" in captured.out
    assert "--tarball" in captured.out
    assert "--type" in captured.out


def test_release_approve_help(capsys: pytest.CaptureFixture) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["release-approve", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "release-approve" in captured.out
    assert "--evidence" in captured.out
    assert "--approver" in captured.out


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_release_preflight_default_args() -> None:
    parser = build_parser()
    args = parser.parse_args(["release-preflight"])
    assert args.command == "release-preflight"
    assert args.repo == "."
    assert args.artefact_type == "python"
    assert args.wheel is None
    assert args.sdist is None
    assert args.tarball is None
    assert args.baseline is None
    assert args.manifest is None
    assert args.output is None
    assert args.verbose is False


def test_release_preflight_npm_type() -> None:
    parser = build_parser()
    args = parser.parse_args(["release-preflight", "--type", "npm"])
    assert args.artefact_type == "npm"


def test_release_preflight_oci_type_valid(capsys: pytest.CaptureFixture) -> None:
    """'oci' is now a valid --type choice (added in RS-024)."""
    parser = build_parser()
    # Should NOT raise; oci is now a recognised artefact type
    args = parser.parse_args(["release-preflight", "--type", "oci"])
    assert args.artefact_type == "oci"


def test_release_preflight_invalid_type(capsys: pytest.CaptureFixture) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["release-preflight", "--type", "java"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Error: bad paths
# ---------------------------------------------------------------------------


def test_release_preflight_bad_repo(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["release-preflight", "--repo", str(tmp_path / "nonexistent")])
    assert code == 2
    captured = capsys.readouterr()
    assert "Error" in captured.err


def test_release_preflight_bad_wheel(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["release-preflight", "--wheel", str(tmp_path / "no.whl"), "--repo", str(tmp_path)])
    assert code == 2
    captured = capsys.readouterr()
    assert "--wheel" in captured.err or "not exist" in captured.err


def test_release_preflight_bad_sdist(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main(["release-preflight", "--sdist", str(tmp_path / "no.tar.gz"), "--repo", str(tmp_path)])
    assert code == 2
    captured = capsys.readouterr()
    assert "--sdist" in captured.err or "not exist" in captured.err


def test_release_preflight_bad_tarball(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    code = main([
        "release-preflight", "--type", "npm",
        "--tarball", str(tmp_path / "no.tgz"),
        "--repo", str(tmp_path),
    ])
    assert code == 2
    captured = capsys.readouterr()
    assert "--tarball" in captured.err or "not exist" in captured.err


# ---------------------------------------------------------------------------
# Disposition exit codes (mocked orchestrator)
# ---------------------------------------------------------------------------


def _make_mock_result(disposition: str) -> MagicMock:
    pack = MagicMock()
    pack.run_id = "release_20260330T000000Z_99_ab12"
    pack.artefact_type = "python"
    pack.artefact_path = "/tmp/dummy.whl"
    pack.check_results = []
    pack.disposition_reasons = []
    pack.capability_state = {"premium_capabilities_enabled": False, "available_premium_hooks": []}

    result = MagicMock()
    result.disposition = disposition
    result.evidence_pack = pack
    result.evidence_path = None
    result.error = ""
    return result


def test_release_preflight_pass_exit_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    # The import of run_release_preflight is done inside _cmd_release_preflight,
    # so we patch the function at its canonical location.
    with patch(
        "saturnday.release.orchestrator.run_release_preflight",
        return_value=_make_mock_result("PASS"),
    ):
        code = main(["release-preflight", "--repo", str(tmp_path)])
    assert code == 0


def test_release_preflight_warn_exit_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    with patch(
        "saturnday.release.orchestrator.run_release_preflight",
        return_value=_make_mock_result("WARN"),
    ):
        code = main(["release-preflight", "--repo", str(tmp_path)])
    assert code == 0


def test_release_preflight_fail_exit_1(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    with patch(
        "saturnday.release.orchestrator.run_release_preflight",
        return_value=_make_mock_result("FAIL"),
    ):
        code = main(["release-preflight", "--repo", str(tmp_path)])
    assert code == 1


# ---------------------------------------------------------------------------
# release-approve without premium
# ---------------------------------------------------------------------------


def test_release_approve_no_premium(capsys: pytest.CaptureFixture) -> None:
    """Without premium registered, release-approve must return 1."""
    with patch("saturnday.capability_registry.get", return_value=None):
        code = main([
            "release-approve",
            "--evidence", "/tmp/some_evidence",
            "--approver", "alice",
        ])
    assert code == 1
    captured = capsys.readouterr()
    assert "premium" in captured.err.lower()
