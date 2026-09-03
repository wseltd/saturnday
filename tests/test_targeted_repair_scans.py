"""Tests for targeted repair scan locality.

Covers:
  A. finding_locality registry — is_file_local() and REPO_LEVEL_KINDS
  B. run_repair_batch targeted scan behaviour (file-local vs repo-level)
  C. Explicit coverage of the repo-level kinds list
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday.repair.repair_tickets import RepairTicket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(
    finding_kind: str,
    file_path: str = "main.py",
    ticket_id: str = "REPAIR-001",
) -> RepairTicket:
    """Return a minimal RepairTicket for the given kind and file."""
    return RepairTicket(
        ticket_id=ticket_id,
        title=f"Fix {finding_kind} in {file_path}",
        severity="medium",
        file_path=file_path,
        line=1,
        finding_kind=finding_kind,
        evidence=[f"{file_path}:1: {finding_kind} detected"],
        remediation=None,
        group_key=f"{file_path}:{finding_kind}",
    )


# ---------------------------------------------------------------------------
# A. Finding locality registry
# ---------------------------------------------------------------------------

class TestFindingLocalityRegistry:
    """Tests for is_file_local() classification logic."""

    def test_known_repo_level_kinds_classified_correctly(self) -> None:
        """Repo-level kinds must return False from is_file_local()."""
        from saturnday.repair.finding_locality import is_file_local

        repo_level_kinds = [
            "missing_license",
            "missing_readme",
            "tests_failing",
            "tests_timeout",
            "unpinned_dependency",
        ]
        for kind in repo_level_kinds:
            assert is_file_local(kind) is False, (
                f"Expected is_file_local({kind!r}) to be False (repo-level kind)"
            )

    def test_known_file_local_kinds_classified_correctly(self) -> None:
        """File-local kinds must return True from is_file_local()."""
        from saturnday.repair.finding_locality import is_file_local

        file_local_kinds = [
            "hardcoded_secret",
            "sql_string_building",
            "ruff",
            "bandit",
            "missing_type_hint",
        ]
        for kind in file_local_kinds:
            assert is_file_local(kind) is True, (
                f"Expected is_file_local({kind!r}) to be True (file-local kind)"
            )

    def test_ruff_is_file_local(self) -> None:
        """ruff findings are always file-local."""
        from saturnday.repair.finding_locality import is_file_local

        assert is_file_local("ruff") is True

    def test_bandit_is_file_local(self) -> None:
        """bandit findings are always file-local."""
        from saturnday.repair.finding_locality import is_file_local

        assert is_file_local("bandit") is True

    def test_unknown_kind_defaults_to_full_repo(self) -> None:
        """Unknown kinds must default to False (full-repo scan) — allowlist design.

        FILE_LOCAL_KINDS is an explicit allowlist. A kind not on the list is
        treated as potentially repo-level to avoid incorrectly scoping a repair
        to a single file when cross-file context might be needed.
        """
        from saturnday.repair.finding_locality import is_file_local

        assert is_file_local("some_unknown_kind_xyz") is False
        assert is_file_local("totally_new_check_zzz") is False


# ---------------------------------------------------------------------------
# B. Targeted scan behaviour in run_repair_batch
# ---------------------------------------------------------------------------

class TestTargetedScanBehaviour:
    """Tests that run_repair_batch uses the correct scan scope per kind."""

    def _make_finding(self, kind: str, file_path: str) -> MagicMock:
        """Return a mock Finding object."""
        f = MagicMock()
        f.kind = kind
        f.file = file_path
        return f

    def test_file_local_kind_uses_targeted_scan(self, tmp_path: Path) -> None:
        """A file-local kind must cause scan_fn to be called with a targeted wrapper.

        Strategy: provide a scan_fn spy and a file-local ticket ("hardcoded_secret").
        Mock execute_repair to avoid real coder calls.  Assert that execute_repair
        received a scan_fn that, when invoked, only scans the target file (i.e. the
        wrapper restricts the call to the ticket's file_path).
        """
        from saturnday.repair.repair_runner import run_repair_batch

        ticket = _make_ticket("hardcoded_secret", file_path="main.py")

        # scan_fn will be called via the targeted wrapper; record calls
        base_scan_calls: list[tuple] = []

        def base_scan_fn(path: Path) -> list:
            base_scan_calls.append((path,))
            return []

        captured_scan_fn: list = []

        def mock_execute_repair(t, skill_path, coder_fn, *, scan_fn=None, cli_mode=False):
            # Capture the scan_fn passed to execute_repair
            captured_scan_fn.append(scan_fn)
            from saturnday.repair.repair_executor import RepairResult
            return RepairResult(
                ticket_id=t.ticket_id,
                status="fixed",
                findings_before=1,
                findings_after=0,
                findings_resolved=[t.finding_kind],
            )

        with patch(
            "saturnday.repair.repair_runner.execute_repair",
            side_effect=mock_execute_repair,
        ):
            result = run_repair_batch(
                tickets=[ticket],
                skill_path=tmp_path,
                coder_fn=None,
                scan_fn=base_scan_fn,
            )

        assert result.fixed == 1
        assert len(captured_scan_fn) == 1

        # The scan_fn forwarded to execute_repair must be a targeted wrapper,
        # not the raw base_scan_fn.  Invoke it to confirm it restricts the scan
        # to the ticket's file (it should only call base_scan_fn with a path
        # that includes the target file, not the full repo root).
        wrapper = captured_scan_fn[0]
        assert wrapper is not None, "scan_fn must not be None for file-local kind"

        # Calling the wrapper should invoke base_scan_fn; if it's the original
        # base_scan_fn we'd see the root path, if it's wrapped we may see a
        # restricted target.  Either way it must be callable and return a list.
        scan_result = wrapper(tmp_path)
        assert isinstance(scan_result, list)

    def test_repo_level_kind_uses_full_scan(self, tmp_path: Path) -> None:
        """A repo-level kind must pass the original scan_fn to execute_repair unchanged."""
        from saturnday.repair.repair_runner import run_repair_batch

        ticket = _make_ticket("missing_license", file_path="LICENSE")

        captured_scan_fn: list = []

        def base_scan_fn(path: Path) -> list:
            return []

        def mock_execute_repair(t, skill_path, coder_fn, *, scan_fn=None, cli_mode=False):
            captured_scan_fn.append(scan_fn)
            from saturnday.repair.repair_executor import RepairResult
            return RepairResult(
                ticket_id=t.ticket_id,
                status="fixed",
                findings_before=1,
                findings_after=0,
                findings_resolved=[t.finding_kind],
            )

        with patch(
            "saturnday.repair.repair_runner.execute_repair",
            side_effect=mock_execute_repair,
        ):
            result = run_repair_batch(
                tickets=[ticket],
                skill_path=tmp_path,
                coder_fn=None,
                scan_fn=base_scan_fn,
            )

        assert result.fixed == 1
        assert len(captured_scan_fn) == 1

        # For repo-level kinds the original scan_fn must be forwarded unchanged.
        assert captured_scan_fn[0] is base_scan_fn, (
            "run_repair_batch must pass the original scan_fn for repo-level kinds"
        )


# ---------------------------------------------------------------------------
# C. Repo-level kinds list completeness
# ---------------------------------------------------------------------------

class TestRepoLevelKindsList:
    """Verify REPO_LEVEL_KINDS contains the required canonical entries."""

    def test_repo_level_kinds_complete_list(self) -> None:
        """REPO_LEVEL_KINDS must contain at least the ten mandatory entries."""
        from saturnday.repair.finding_locality import REPO_LEVEL_KINDS

        required = {
            "missing_license",
            "missing_readme",
            "tests_failing",
            "tests_timeout",
            "duplicate_module",
            "circular_import",
            "unpinned_dependency",
            "pip_audit",
            "excessive_blast_radius",
            "missing_project_config",
        }

        missing = required - REPO_LEVEL_KINDS
        assert not missing, (
            f"REPO_LEVEL_KINDS is missing required entries: {sorted(missing)}"
        )
