"""Tests for targeted repair scan optimisation.

Verifies that ``run_repair_batch`` creates a targeted (single-file) scan
function for file-local finding kinds, and preserves the full-repo scan
for repo-level kinds.

All tests are fully isolated: no filesystem I/O, no live governance runs.
The scan_fn and coder_fn are replaced with deterministic stubs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from saturnday.repair.finding_locality import REPO_LEVEL_KINDS, is_file_local
from saturnday.repair.repair_runner import _make_targeted_scan, run_repair_batch
from saturnday.repair.repair_tickets import RepairTicket


# ---------------------------------------------------------------------------
# Minimal Finding stub
# ---------------------------------------------------------------------------

class _Finding:
    """Minimal Finding-like object for test assertions."""

    def __init__(self, kind: str, file: str = "main.py") -> None:
        self.kind = kind
        self.file = file


# ---------------------------------------------------------------------------
# Ticket factory
# ---------------------------------------------------------------------------

def _make_ticket(
    kind: str,
    file_path: str = "main.py",
    ticket_id: str = "T-001",
) -> RepairTicket:
    return RepairTicket(
        ticket_id=ticket_id,
        title=f"Fix {kind}",
        finding_kind=kind,
        file_path=file_path,
        severity="WARNING",
        line=None,
        evidence=[f"Found {kind} in {file_path}"],
        remediation={"why": "Test", "fix": "Fix it."},
    )


# ---------------------------------------------------------------------------
# Tests: _make_targeted_scan — unit tests for the factory
# ---------------------------------------------------------------------------

def test_make_targeted_scan_calls_scan_for_repair_with_target_files() -> None:
    """_make_targeted_scan must produce a function that calls _scan_for_repair
    with the correct target_files keyword argument."""
    captured_kwargs: list[dict] = []

    def _stub_scan_for_repair(path: Path, **kwargs: object) -> tuple:
        captured_kwargs.append(kwargs)
        return [], "repo"

    # Patch the import inside repair_runner._targeted_scan
    with patch("saturnday.interactive._scan_for_repair", side_effect=_stub_scan_for_repair):
        targeted = _make_targeted_scan("src/auth.py")
        result = targeted(Path("/fake/repo"))

    assert result == []
    assert captured_kwargs == [{"target_files": ["src/auth.py"]}]


def test_make_targeted_scan_returns_findings_list() -> None:
    """_make_targeted_scan must return the findings list, not the tuple."""
    fake_finding = _Finding("bare_except")

    with patch(
        "saturnday.interactive._scan_for_repair",
        return_value=([fake_finding], "repo"),
    ):
        targeted = _make_targeted_scan("main.py")
        findings = targeted(Path("/repo"))

    assert findings == [fake_finding]


def test_make_targeted_scan_different_target_files_produce_different_calls() -> None:
    """Two scan functions produced from different target_files must each
    call _scan_for_repair with their respective target."""
    calls: list[list[str]] = []

    def _stub(path: Path, **kwargs: object) -> tuple:
        calls.append(kwargs.get("target_files", []))
        return [], "repo"

    with patch("saturnday.interactive._scan_for_repair", side_effect=_stub):
        fn_a = _make_targeted_scan("src/a.py")
        fn_b = _make_targeted_scan("src/b.py")
        fn_a(Path("/repo"))
        fn_b(Path("/repo"))

    assert calls == [["src/a.py"], ["src/b.py"]]


# ---------------------------------------------------------------------------
# Tests: run_repair_batch — targeted scan used for file-local kinds
# ---------------------------------------------------------------------------

def test_targeted_scan_created_for_file_local_kind(tmp_path: Path) -> None:
    """For a file-local kind, run_repair_batch must call _make_targeted_scan
    and use the result instead of the original scan_fn."""
    ticket = _make_ticket("hardcoded_secret", file_path="src/foo.py")

    # original_scan returns 0 findings so the ticket is immediately "fixed"
    original_scan = MagicMock(return_value=[])
    coder_fn = MagicMock(return_value="# fixed")

    with patch(
        "saturnday.repair.repair_runner._make_targeted_scan",
        wraps=_make_targeted_scan,
    ) as mock_factory:
        # Make the targeted scan also return 0 findings
        with patch("saturnday.interactive._scan_for_repair", return_value=([], "repo")):
            run_repair_batch(
                [ticket],
                tmp_path,
                coder_fn,
                scan_fn=original_scan,
                cli_mode=True,
            )

    mock_factory.assert_called_once_with("src/foo.py")


def test_original_scan_fn_used_for_repo_level_kind(tmp_path: Path) -> None:
    """For a repo-level kind, run_repair_batch must NOT create a targeted
    wrapper and must pass the original scan_fn through to execute_repair."""
    repo_level_kind = "missing_readme"
    assert not is_file_local(repo_level_kind)

    ticket = _make_ticket(repo_level_kind, file_path="README.md")

    original_scan_calls: list[Path] = []

    def _original_scan(path: Path) -> list:
        original_scan_calls.append(path)
        return []  # 0 findings → already fixed

    coder_fn = MagicMock(return_value="# fixed")

    with patch(
        "saturnday.repair.repair_runner._make_targeted_scan",
    ) as mock_factory:
        run_repair_batch(
            [ticket],
            tmp_path,
            coder_fn,
            scan_fn=_original_scan,
            cli_mode=True,
        )
        mock_factory.assert_not_called()

    # Original scan_fn must have been called (pre-scan path)
    assert tmp_path in original_scan_calls


# ---------------------------------------------------------------------------
# Tests: no scan_fn — targeted wrapper never created
# ---------------------------------------------------------------------------

def test_no_targeted_scan_when_scan_fn_is_none(tmp_path: Path) -> None:
    """When scan_fn=None, _make_targeted_scan must not be called."""
    ticket = _make_ticket("bare_except")
    coder_fn = MagicMock(return_value="# fixed")

    with patch("saturnday.repair.repair_runner._make_targeted_scan") as mock_factory:
        run_repair_batch(
            [ticket],
            tmp_path,
            coder_fn,
            scan_fn=None,
            cli_mode=True,
        )
        mock_factory.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: no file_path — targeted wrapper not created
# ---------------------------------------------------------------------------

def test_no_targeted_scan_when_file_path_empty(tmp_path: Path) -> None:
    """When ticket.file_path is empty, fall back to original scan_fn."""
    ticket = _make_ticket("bare_except", file_path="")

    original_scan_calls: list[Path] = []

    def _original_scan(path: Path) -> list:
        original_scan_calls.append(path)
        return []

    coder_fn = MagicMock(return_value="# fixed")

    with patch("saturnday.repair.repair_runner._make_targeted_scan") as mock_factory:
        run_repair_batch(
            [ticket],
            tmp_path,
            coder_fn,
            scan_fn=_original_scan,
            cli_mode=True,
        )
        mock_factory.assert_not_called()

    assert tmp_path in original_scan_calls


# ---------------------------------------------------------------------------
# Tests: run_full_repo_review target_files parameter
# ---------------------------------------------------------------------------

def test_run_full_repo_review_uses_target_files_when_provided(tmp_path: Path) -> None:
    """When target_files is given, run_full_repo_review must skip git ls-files
    and use the provided list instead."""
    (tmp_path / ".git").mkdir()

    captured_review_files: list = []

    def _stub_run_review(repo_path, files, tmpdir, **kwargs):
        captured_review_files.extend(files)
        return {"tool_runs": [], "tools": {}}

    with (
        patch("saturnday.governance.subprocess.run") as mock_subprocess,
        patch("saturnday.governance.run_review", side_effect=_stub_run_review),
        patch("saturnday.governance.write_evidence_dir", return_value=tmp_path),
        patch("saturnday.governance.compute_disposition", return_value=("PASS", [])),
    ):
        from saturnday.governance import run_full_repo_review

        run_full_repo_review(
            tmp_path,
            target_files=["src/auth.py", "src/models.py"],
        )

    # subprocess.run (git ls-files) must NOT have been called
    mock_subprocess.assert_not_called()
    assert "src/auth.py" in captured_review_files
    assert "src/models.py" in captured_review_files


def test_run_full_repo_review_filters_unsupported_extensions_in_target_files(
    tmp_path: Path,
) -> None:
    """target_files entries with unsupported extensions must be filtered out."""
    (tmp_path / ".git").mkdir()

    captured_review_files: list = []

    def _stub_run_review(repo_path, files, tmpdir, **kwargs):
        captured_review_files.extend(files)
        return {"tool_runs": [], "tools": {}}

    with (
        patch("saturnday.governance.subprocess.run"),
        patch("saturnday.governance.run_review", side_effect=_stub_run_review),
        patch("saturnday.governance.write_evidence_dir", return_value=tmp_path),
        patch("saturnday.governance.compute_disposition", return_value=("PASS", [])),
    ):
        from saturnday.governance import run_full_repo_review

        run_full_repo_review(
            tmp_path,
            target_files=["src/auth.py", "README.md", "data.json", "main.ts"],
        )

    assert "src/auth.py" in captured_review_files
    assert "main.ts" in captured_review_files
    assert "README.md" not in captured_review_files
    assert "data.json" not in captured_review_files


def test_run_full_repo_review_uses_git_ls_files_when_no_target_files(
    tmp_path: Path,
) -> None:
    """When target_files=None, git ls-files must be called."""
    (tmp_path / ".git").mkdir()

    ls_mock = MagicMock()
    ls_mock.returncode = 0
    ls_mock.stdout = "src/main.py\n"
    ls_mock.stderr = ""

    captured_review_files: list = []

    def _stub_run_review(repo_path, files, tmpdir, **kwargs):
        captured_review_files.extend(files)
        return {"tool_runs": [], "tools": {}}

    with (
        patch("saturnday.governance.subprocess.run", return_value=ls_mock) as mock_subprocess,
        patch("saturnday.governance.run_review", side_effect=_stub_run_review),
        patch("saturnday.governance.write_evidence_dir", return_value=tmp_path),
        patch("saturnday.governance.compute_disposition", return_value=("PASS", [])),
    ):
        from saturnday.governance import run_full_repo_review

        run_full_repo_review(tmp_path)

    mock_subprocess.assert_called_once()
    assert mock_subprocess.call_args[0][0] == ["git", "ls-files"]
    assert "src/main.py" in captured_review_files


# ---------------------------------------------------------------------------
# Tests: _scan_for_repair passes target_files to run_full_repo_review
# ---------------------------------------------------------------------------

def test_scan_for_repair_passes_target_files(tmp_path: Path) -> None:
    """_scan_for_repair must forward target_files to run_full_repo_review."""
    (tmp_path / ".git").mkdir()

    captured_kwargs: list[dict] = []

    def _stub_full_repo_review(repo_path, **kwargs):
        captured_kwargs.append(kwargs)
        pack = MagicMock()
        pack.check_results = []
        return pack, None

    with patch("saturnday.governance.run_full_repo_review", side_effect=_stub_full_repo_review):
        from saturnday.interactive import _scan_for_repair
        findings, mode = _scan_for_repair(tmp_path, target_files=["lib/utils.py"])

    assert mode == "repo"
    assert captured_kwargs, "run_full_repo_review was not called"
    assert captured_kwargs[0].get("target_files") == ["lib/utils.py"]


def test_scan_for_repair_without_target_files_passes_none(tmp_path: Path) -> None:
    """_scan_for_repair without target_files must call run_full_repo_review
    with target_files=None."""
    (tmp_path / ".git").mkdir()

    captured_kwargs: list[dict] = []

    def _stub_full_repo_review(repo_path, **kwargs):
        captured_kwargs.append(kwargs)
        pack = MagicMock()
        pack.check_results = []
        return pack, None

    with patch("saturnday.governance.run_full_repo_review", side_effect=_stub_full_repo_review):
        from saturnday.interactive import _scan_for_repair
        _scan_for_repair(tmp_path)

    assert captured_kwargs
    tf = captured_kwargs[0].get("target_files")
    assert tf is None
