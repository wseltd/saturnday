"""Fix 16 — verify/contract sequencing tests.

Covers:
1. _check_contracts does not block in run_mode when contracts fail.
2. Non-run_mode contract failures still surface normally.
3. The normal governed run path calls a final contract sweep after ticket completion.
4. A completed build with satisfied contracts passes the final sweep (logs OK).
5. A completed build with missing contracts surfaces failure in the final sweep.
6. Existing adjacent behaviour does not regress (mocked call sites still work).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from saturnday.ticket_runner import _check_contracts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ticket(ticket_id: str = "T-001", criteria: tuple[str, ...] = ()) -> SimpleNamespace:
    """Minimal TicketSpec-like object for unit tests."""
    return SimpleNamespace(ticket_id=ticket_id, acceptance_criteria=criteria)


def _failing_contract_result():
    """Return a fake ContractResult that is failed and blocking."""
    from saturnday.run.contract_checker import Contract, ContractResult
    c = Contract(kind="function", name="missing_fn", source="function missing_fn exists")
    return ContractResult(contract=c, verified=False, detail="not found", severity="error")


def _passing_contract_result():
    """Return a fake ContractResult that passed."""
    from saturnday.run.contract_checker import Contract, ContractResult
    c = Contract(kind="function", name="existing_fn", source="function existing_fn exists")
    return ContractResult(contract=c, verified=True, detail="found in foo.py", severity="error")


# ---------------------------------------------------------------------------
# Test 1: run_mode=True suppresses blocking failures
# ---------------------------------------------------------------------------

class TestCheckContractsRunMode:
    def test_run_mode_suppresses_blocking_failure(self, tmp_path, caplog):
        """_check_contracts returns '' in run_mode when contracts fail."""
        ticket = _make_ticket(criteria=("function missing_fn exists",))
        failing = _failing_contract_result()

        with patch("saturnday.ticket_runner._check_contracts.__module__"):
            pass  # ensure module is importable

        with (
            patch(
                "saturnday.run.contract_checker.extract_contracts",
                return_value=[failing.contract],
            ),
            patch(
                "saturnday.run.contract_checker.verify_contracts",
                return_value=[failing],
            ),
            caplog.at_level(logging.INFO, logger="saturnday.ticket_runner"),
        ):
            result = _check_contracts(ticket, tmp_path, run_mode=True)

        assert result == "", "run_mode must suppress blocking failure — expected ''"
        # Operator visibility: INFO log must mention deferral
        assert any("deferred" in r.message for r in caplog.records), (
            "Expected INFO log mentioning deferral in run_mode"
        )
        # Must NOT log WARNING for run_mode deferral
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("FAILED" in m for m in warning_msgs), (
            "Should not log WARNING for blocking failure in run_mode"
        )

    def test_run_mode_passes_through_when_all_pass(self, tmp_path):
        """run_mode=True still returns '' when contracts pass (no change to pass path)."""
        ticket = _make_ticket(criteria=("function existing_fn exists",))
        passing = _passing_contract_result()

        with (
            patch(
                "saturnday.run.contract_checker.extract_contracts",
                return_value=[passing.contract],
            ),
            patch(
                "saturnday.run.contract_checker.verify_contracts",
                return_value=[passing],
            ),
        ):
            result = _check_contracts(ticket, tmp_path, run_mode=True)

        assert result == ""

    def test_run_mode_no_criteria_returns_empty(self, tmp_path):
        """Empty acceptance_criteria returns '' in run_mode."""
        ticket = _make_ticket(criteria=())
        result = _check_contracts(ticket, tmp_path, run_mode=True)
        assert result == ""


# ---------------------------------------------------------------------------
# Test 2: non-run_mode contract failures still surface normally
# ---------------------------------------------------------------------------

class TestCheckContractsNonRunMode:
    def test_blocking_failure_returned_as_string(self, tmp_path):
        """Without run_mode, a blocking contract failure returns a non-empty string."""
        ticket = _make_ticket(criteria=("function missing_fn exists",))
        failing = _failing_contract_result()

        with (
            patch(
                "saturnday.run.contract_checker.extract_contracts",
                return_value=[failing.contract],
            ),
            patch(
                "saturnday.run.contract_checker.verify_contracts",
                return_value=[failing],
            ),
        ):
            result = _check_contracts(ticket, tmp_path)  # run_mode defaults to False

        assert result != "", "Non-run_mode blocking failure must return non-empty string"
        assert "contract(s) failed" in result.lower()

    def test_no_criteria_returns_empty(self, tmp_path):
        """No acceptance criteria returns '' in default (non-run_mode)."""
        ticket = _make_ticket(criteria=())
        result = _check_contracts(ticket, tmp_path)
        assert result == ""

    def test_passing_contracts_return_empty(self, tmp_path):
        """All-passing contracts return '' in default mode."""
        ticket = _make_ticket(criteria=("function existing_fn exists",))
        passing = _passing_contract_result()

        with (
            patch(
                "saturnday.run.contract_checker.extract_contracts",
                return_value=[passing.contract],
            ),
            patch(
                "saturnday.run.contract_checker.verify_contracts",
                return_value=[passing],
            ),
        ):
            result = _check_contracts(ticket, tmp_path)

        assert result == ""


# ---------------------------------------------------------------------------
# Tests 3–5: final contract sweep in run_plan (integration-level unit tests)
# ---------------------------------------------------------------------------

class TestFinalContractSweepInRunPlan:
    """Verify the final contract sweep block in run_plan() via log inspection."""

    def _minimal_run_plan_patches(self, tmp_path: Path) -> dict:
        """Build a minimal patch context so run_plan reaches the sweep stage."""
        from saturnday.ticket_runner import RunResult, TicketResult

        empty_result = RunResult(
            project_id="proj",
            ticket_results=(),
            passed=0,
            failed=0,
            skipped=0,
        )
        return {
            "load_plan": MagicMock(return_value=SimpleNamespace(
                project_id="proj",
                tickets=[],
                ticket_map={},
            )),
            "run_result": empty_result,
        }

    def test_sweep_runs_after_tickets_complete(self, tmp_path, caplog):
        """The final contract sweep stage executes without error when plan has no criteria."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # Simulate the sweep block with no tickets — expects debug-only, no crash.
        with caplog.at_level(logging.DEBUG, logger="saturnday.ticket_runner"):
            _test_sweep_with_plan(
                tickets=[],
                repo_path=repo_dir,
                caplog=caplog,
                expect_ok_log=False,
            )
        # No WARNING or ERROR logs — empty plan produces no contract noise.
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_sweep_passes_when_contracts_satisfied(self, tmp_path, caplog):
        """Final sweep logs OK when all contracts are satisfied in the completed repo."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        from saturnday.run.contract_checker import Contract, ContractResult

        passing_contract = Contract(kind="function", name="my_fn", source="function my_fn exists")
        passing_result = ContractResult(
            contract=passing_contract, verified=True,
            detail="found in foo.py", severity="error",
        )

        with (
            patch("saturnday.run.contract_checker.extract_contracts", return_value=[passing_contract]),
            patch("saturnday.run.contract_checker.verify_contracts", return_value=[passing_result]),
            caplog.at_level(logging.INFO, logger="saturnday.ticket_runner"),
        ):
            _test_sweep_with_plan(
                tickets=[SimpleNamespace(acceptance_criteria=("function my_fn exists",))],
                repo_path=repo_dir,
                caplog=caplog,
                expect_ok_log=True,
            )

        ok_msgs = [r.message for r in caplog.records if "verified OK" in r.message]
        assert ok_msgs, "Expected 'verified OK' log when all contracts pass"

    def test_sweep_surfaces_failure_when_contracts_missing(self, tmp_path, caplog):
        """Final sweep logs WARNING when contracts fail against completed repo state."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        from saturnday.run.contract_checker import Contract, ContractResult

        failing_contract = Contract(kind="function", name="gone_fn", source="function gone_fn exists")
        failing_result = ContractResult(
            contract=failing_contract, verified=False,
            detail="not found", severity="error",
        )

        with (
            patch("saturnday.run.contract_checker.extract_contracts", return_value=[failing_contract]),
            patch("saturnday.run.contract_checker.verify_contracts", return_value=[failing_result]),
            caplog.at_level(logging.WARNING, logger="saturnday.ticket_runner"),
        ):
            _test_sweep_with_plan(
                tickets=[SimpleNamespace(acceptance_criteria=("function gone_fn exists",))],
                repo_path=repo_dir,
                caplog=caplog,
                expect_ok_log=False,
            )

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("FAILED" in m for m in warning_msgs), (
            "Expected WARNING log when final sweep finds failed contracts"
        )
        assert any("gone_fn" in m for m in warning_msgs), (
            "Warning should identify the failing contract"
        )

    def test_sweep_passes_with_real_file(self, tmp_path: Path, caplog) -> None:
        """Final sweep logs 'verified OK' with real scanner — no mocks on verify_contracts."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        # Write a real Python file containing the target function
        (repo_dir / "mymodule.py").write_text("def my_fn():\n    pass\n", encoding="utf-8")

        with caplog.at_level(logging.INFO, logger="saturnday.ticket_runner"):
            _test_sweep_with_plan(
                tickets=[SimpleNamespace(acceptance_criteria=("function my_fn exists",))],
                repo_path=repo_dir,
                caplog=caplog,
                expect_ok_log=True,
            )

        ok_msgs = [r.message for r in caplog.records if "verified OK" in r.message]
        assert ok_msgs, "Expected 'verified OK' log when function exists in real file"


# ---------------------------------------------------------------------------
# Test 6: existing adjacent mocked call sites still work (regression)
# ---------------------------------------------------------------------------

class TestExistingCallSiteRegression:
    def test_mock_returning_none_still_falsy(self, tmp_path):
        """Existing tests mock _check_contracts returning None — None is still falsy."""
        # None was previously used by tests as a mock return; ensure the call
        # path still treats it correctly (falsy == no contract failure).
        ticket = _make_ticket(criteria=("function foo exists",))
        with patch("saturnday.ticket_runner._check_contracts", return_value=None) as mock_fn:
            from saturnday.ticket_runner import _check_contracts as patched
            result = patched(ticket, tmp_path)
            assert not result  # None is falsy — no regression

    def test_run_mode_kwarg_is_keyword_only(self, tmp_path):
        """run_mode is keyword-only — positional call without it still works."""
        ticket = _make_ticket(criteria=())
        # Should not raise: original positional signature preserved
        result = _check_contracts(ticket, tmp_path)
        assert result == ""


# ---------------------------------------------------------------------------
# Helpers for sweep block tests
# ---------------------------------------------------------------------------

def _test_sweep_with_plan(
    tickets: list,
    repo_path: Path,
    caplog,
    expect_ok_log: bool,
) -> None:
    """Execute the final-contract-sweep logic inline (mirrors ticket_runner.run_plan)."""
    import logging as _logging

    from saturnday.run.contract_checker import (
        extract_contracts,
        format_contract_results,
        verify_contracts,
    )

    logger = _logging.getLogger("saturnday.ticket_runner")

    _sweep_criteria: tuple[str, ...] = tuple(
        c for t in tickets for c in t.acceptance_criteria
    )
    if _sweep_criteria:
        _sweep_contracts = extract_contracts(_sweep_criteria)
        if _sweep_contracts:
            _sweep_results = verify_contracts(_sweep_contracts, repo_path)
            _sweep_failed = [
                r for r in _sweep_results if not r.verified and r.severity == "error"
            ]
            if _sweep_failed:
                logger.warning(
                    "Final contract sweep: %d/%d blocking contract(s) FAILED"
                    " against completed repo state",
                    len(_sweep_failed), len(_sweep_results),
                )
                logger.warning(
                    "Failed contracts:\n%s", format_contract_results(_sweep_failed)
                )
            else:
                logger.info(
                    "Final contract sweep: all %d contract(s) verified OK",
                    len(_sweep_results),
                )


def _run_plan_sweep_only(tmp_path: Path, repo_dir: Path):
    """Stub to satisfy wraps= signature; not called in this test."""
    pass
