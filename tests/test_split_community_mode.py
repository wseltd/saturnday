"""SPLIT-033: Integration tests proving the pipeline works with NO premium capabilities.

Every test starts and ends with an empty registry.  The autouse fixture ensures
teardown even on test failure.

Test matrix:
1. test_evidence_fields_community_mode — build_capability_state / build_skipped_stages shape
2. test_no_premium_imports_in_community_mode — key modules import cleanly
3. test_run_plan_evidence_community_mode — write_run_summary records correct fields
4. test_document_evidence_community_mode — write_document_evidence records correct fields
5. test_approve_command_community_mode — _cmd_approve returns "requires saturnday-premium"
6. test_run_ticket_with_retries_community_mode — _run_ticket_with_retries runs without error
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from saturnday import capability_registry
from saturnday.shared.evidence_schema import (
    PREMIUM_STAGE_NAMES,
    build_capability_state,
    build_skipped_stages,
)


# ---------------------------------------------------------------------------
# Autouse fixture — clear registry before and after every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _empty_registry():  # type: ignore[return]
    """Guarantee an empty capability registry for every test in this module.

    Also blocks ``saturnday_premium`` from being importable so that
    ``build_capability_state()`` returns the community-mode values regardless
    of whether the premium package is installed in the test environment.
    """
    capability_registry.clear()
    # Patch saturnday_premium out of sys.modules so build_capability_state()
    # sees it as not installed.  None as the value makes any ``import``
    # statement raise ImportError.
    with patch.dict(sys.modules, {"saturnday_premium": None}):  # type: ignore[dict-item]
        yield
    capability_registry.clear()


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _assert_all_stages_skipped_not_available(skipped: list[dict]) -> None:
    """Assert every PREMIUM_STAGE_NAME appears with reason 'premium_not_available'."""
    skipped_by_name = {entry["stage"]: entry["reason"] for entry in skipped}
    for name in PREMIUM_STAGE_NAMES:
        assert name in skipped_by_name, (
            f"Stage '{name}' missing from skipped_premium_stages; got {list(skipped_by_name)}"
        )
        assert skipped_by_name[name] == "premium_not_available", (
            f"Stage '{name}' reason should be 'premium_not_available', "
            f"got '{skipped_by_name[name]}'"
        )


# ---------------------------------------------------------------------------
# Test 1: Evidence schema helpers with empty registry
# ---------------------------------------------------------------------------


class TestEvidenceFieldsCommunityMode:
    """build_capability_state and build_skipped_stages behave correctly with empty registry."""

    def test_capability_state_premium_disabled(self) -> None:
        """premium_capabilities_enabled must be False when registry is empty."""
        state = build_capability_state()
        assert state["premium_capabilities_enabled"] is False

    def test_capability_state_package_not_installed(self) -> None:
        """premium_package_installed must be False when saturnday_premium is blocked."""
        state = build_capability_state()
        assert state["premium_package_installed"] is False

    def test_capability_state_entitlement_none(self) -> None:
        """entitlement_valid and entitlement_reason must both be None in community mode."""
        state = build_capability_state()
        assert state["entitlement_valid"] is None
        assert state["entitlement_reason"] is None

    def test_capability_state_entitlement_metadata_none(self) -> None:
        """entitlement_org, entitlement_edition, entitlement_expires must all be None in community mode."""
        state = build_capability_state()
        assert state["entitlement_org"] is None, (
            f"entitlement_org must be None in community mode, got {state['entitlement_org']!r}"
        )
        assert state["entitlement_edition"] is None, (
            f"entitlement_edition must be None in community mode, got {state['entitlement_edition']!r}"
        )
        assert state["entitlement_expires"] is None, (
            f"entitlement_expires must be None in community mode, got {state['entitlement_expires']!r}"
        )

    def test_capability_state_no_hooks(self) -> None:
        """available_premium_hooks must be empty list when registry is empty."""
        state = build_capability_state()
        assert state["available_premium_hooks"] == []

    def test_build_skipped_stages_all_listed(self) -> None:
        """All PREMIUM_STAGE_NAMES appear in skipped_premium_stages."""
        skipped = build_skipped_stages(ran_stages=None)
        assert len(skipped) == len(PREMIUM_STAGE_NAMES)
        _assert_all_stages_skipped_not_available(skipped)

    def test_build_skipped_stages_with_empty_ran_set(self) -> None:
        """Passing an empty ran_stages set is equivalent to None when nothing is registered."""
        skipped_none = build_skipped_stages(ran_stages=None)
        skipped_set = build_skipped_stages(ran_stages=set())
        # Both paths should produce the same output — all stages absent from registry.
        assert skipped_none == skipped_set

    def test_is_available_all_stages_false(self) -> None:
        """capability_registry.is_available returns False for every premium stage name."""
        for name in PREMIUM_STAGE_NAMES:
            assert not capability_registry.is_available(name), (
                f"is_available('{name}') should be False in community mode"
            )

    def test_get_all_stages_none(self) -> None:
        """capability_registry.get returns None for every premium stage name."""
        for name in PREMIUM_STAGE_NAMES:
            assert capability_registry.get(name) is None, (
                f"get('{name}') should be None in community mode"
            )


# ---------------------------------------------------------------------------
# Test 2: Module import hygiene
# ---------------------------------------------------------------------------


class TestNoPremiumImportsCommunityMode:
    """Core modules must import cleanly even when premium stages are not registered."""

    def test_ticket_runner_imports_cleanly(self) -> None:
        """ticket_runner must be importable with an empty registry."""
        import saturnday.ticket_runner as tr
        assert hasattr(tr, "run_plan"), "run_plan missing from ticket_runner"
        assert hasattr(tr, "_run_ticket_with_retries"), "_run_ticket_with_retries missing"

    def test_document_runner_imports_cleanly(self) -> None:
        """document_runner must be importable with an empty registry."""
        import saturnday.document.document_runner as dr
        assert hasattr(dr, "run_document"), "run_document missing from document_runner"

    def test_interactive_imports_cleanly(self) -> None:
        """interactive must be importable with an empty registry."""
        import saturnday.interactive as iv
        # repl_loop is the REPL entry point; guided_run is the goal-based entry point.
        assert hasattr(iv, "repl_loop") or hasattr(iv, "guided_run"), (
            "Neither repl_loop nor guided_run found in interactive; "
            f"found: {[n for n in dir(iv) if not n.startswith('_')]}"
        )

    def test_cli_imports_cleanly(self) -> None:
        """cli must be importable with an empty registry."""
        import saturnday.cli as cli
        assert hasattr(cli, "main"), "main missing from cli"

    def test_planner_imports_cleanly(self) -> None:
        """document planner must be importable with an empty registry."""
        import saturnday.document.planner as planner
        assert hasattr(planner, "generate_document_plan"), (
            "generate_document_plan missing from planner"
        )

    def test_capability_registry_clear_is_idempotent(self) -> None:
        """Clearing an already-empty registry must not raise."""
        capability_registry.clear()
        capability_registry.clear()
        assert capability_registry.registered_stages() == []


# ---------------------------------------------------------------------------
# Test 3: write_run_summary records correct community-mode fields
# ---------------------------------------------------------------------------


class TestRunEvidenceCommunityMode:
    """write_run_summary writes community-mode capability fields correctly."""

    def _make_run_result(self) -> Any:
        from saturnday._types import RunResult
        return RunResult(
            project_id="proj-community-test",
            total_tickets=1,
            passed=1,
            failed=0,
        )

    def test_run_summary_premium_capabilities_disabled(self, tmp_path: Path) -> None:
        """run-summary.json must record premium_capabilities_enabled=false."""
        from saturnday.run.evidence import write_run_summary

        write_run_summary(
            result=self._make_run_result(),
            output_dir=tmp_path,
            ran_stages=set(),
        )

        summary_path = tmp_path / "run-summary.json"
        assert summary_path.exists(), "run-summary.json was not written"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

        assert summary["premium_capabilities_enabled"] is False, (
            "premium_capabilities_enabled must be False in community mode"
        )

    def test_run_summary_available_hooks_empty(self, tmp_path: Path) -> None:
        """run-summary.json must have an empty available_premium_hooks list."""
        from saturnday.run.evidence import write_run_summary

        write_run_summary(
            result=self._make_run_result(),
            output_dir=tmp_path,
            ran_stages=set(),
        )

        summary = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        assert summary["available_premium_hooks"] == [], (
            "available_premium_hooks must be empty in community mode"
        )

    def test_run_summary_all_stages_skipped(self, tmp_path: Path) -> None:
        """All 8 PREMIUM_STAGE_NAMES must appear as premium_not_available in evidence."""
        from saturnday.run.evidence import write_run_summary

        write_run_summary(
            result=self._make_run_result(),
            output_dir=tmp_path,
            ran_stages=set(),
        )

        summary = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        _assert_all_stages_skipped_not_available(summary["skipped_premium_stages"])

    def test_run_summary_per_hook_booleans_false(self, tmp_path: Path) -> None:
        """Per-hook availability booleans must all be False in community mode."""
        from saturnday.run.evidence import write_run_summary

        write_run_summary(
            result=self._make_run_result(),
            output_dir=tmp_path,
            ran_stages=set(),
        )

        summary = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        assert summary["security_triage_available"] is False
        assert summary["memory_available"] is False
        assert summary["impact_analysis_available"] is False
        assert summary["code_reviewer_available"] is False


# ---------------------------------------------------------------------------
# Test 4: write_document_evidence records correct community-mode fields
# ---------------------------------------------------------------------------


class TestDocumentEvidenceCommunityMode:
    """write_document_evidence writes community-mode capability fields correctly."""

    def _make_minimal_doc_result(self) -> Any:
        from saturnday.document._types import DocumentRunResult
        return DocumentRunResult(
            document_id="test-doc-001",
            type="report",
            total_sections=0,
            passed=0,
            failed=0,
            provisional=0,
            document_status="PASS",
        )

    def _make_minimal_doc_spec(self) -> Any:
        from saturnday.document._types import DocumentSpec
        # DocumentSpec requires: type, purpose, audience, risk_class,
        # required_sections, claim_policy, approved_sources, sign_off_roles
        return DocumentSpec(
            type="report",
            purpose="Test document for community mode integration test",
            audience="internal",
            risk_class="low",
            required_sections=[],
            claim_policy={},
            approved_sources=[],
            sign_off_roles=[],
        )

    def test_document_summary_premium_capabilities_disabled(self, tmp_path: Path) -> None:
        """run-summary.json in document evidence must record premium_capabilities_enabled=false."""
        from saturnday.document.evidence_pack import write_document_evidence

        result = self._make_minimal_doc_result()
        spec = self._make_minimal_doc_spec()

        write_document_evidence(result=result, spec=spec, output_dir=tmp_path)

        summary_path = tmp_path / "run-summary.json"
        assert summary_path.exists(), "run-summary.json was not written by write_document_evidence"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))

        assert summary["premium_capabilities_enabled"] is False, (
            "premium_capabilities_enabled must be False in community mode"
        )

    def test_document_summary_available_hooks_empty(self, tmp_path: Path) -> None:
        """available_premium_hooks must be empty in community mode."""
        from saturnday.document.evidence_pack import write_document_evidence

        result = self._make_minimal_doc_result()
        spec = self._make_minimal_doc_spec()
        write_document_evidence(result=result, spec=spec, output_dir=tmp_path)

        summary = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        assert summary.get("available_premium_hooks") == [], (
            "available_premium_hooks must be empty in community mode"
        )

    def test_document_summary_claim_verification_false(self, tmp_path: Path) -> None:
        """claim_verification_available must be False when doc_post_global not registered."""
        from saturnday.document.evidence_pack import write_document_evidence

        result = self._make_minimal_doc_result()
        spec = self._make_minimal_doc_spec()
        write_document_evidence(result=result, spec=spec, output_dir=tmp_path)

        summary = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        assert summary.get("claim_verification_available") is False, (
            "claim_verification_available must be False when doc_post_global not registered"
        )
        assert summary.get("publishability_evaluated") is False, (
            "publishability_evaluated must be False when doc_post_global not registered"
        )

    def test_document_summary_skipped_stages_all_listed(self, tmp_path: Path) -> None:
        """All PREMIUM_STAGE_NAMES must appear in skipped_premium_stages of doc evidence."""
        from saturnday.document.evidence_pack import write_document_evidence

        result = self._make_minimal_doc_result()
        spec = self._make_minimal_doc_spec()
        write_document_evidence(result=result, spec=spec, output_dir=tmp_path)

        summary = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        _assert_all_stages_skipped_not_available(summary["skipped_premium_stages"])


# ---------------------------------------------------------------------------
# Test 5: approve command returns premium message when not registered
# ---------------------------------------------------------------------------


class TestApproveCommandCommunityMode:
    """_cmd_approve must return exit-code 1 with a 'saturnday-premium' message in community mode."""

    def _make_approve_args(self, tmp_path: Path) -> argparse.Namespace:
        return argparse.Namespace(
            document="test-doc-001",
            role="cfo",
            actor="Alice",
            status="approved",
            notes="",
            repo=str(tmp_path),
            verbose=False,
        )

    def test_approve_returns_1_without_premium(self, tmp_path: Path) -> None:
        """_cmd_approve must return 1 when doc_post_global is not registered."""
        from saturnday.cli import _cmd_approve

        args = self._make_approve_args(tmp_path)
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            return_code = _cmd_approve(args)

        assert return_code == 1, (
            f"_cmd_approve should return 1 when premium not available, got {return_code}"
        )

    def test_approve_emits_premium_message(self, tmp_path: Path) -> None:
        """_cmd_approve must write 'Approval workflow requires saturnday-premium.' to stderr."""
        from saturnday.cli import _cmd_approve

        args = self._make_approve_args(tmp_path)
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            _cmd_approve(args)

        stderr_text = stderr_buf.getvalue()
        assert "Approval workflow requires saturnday-premium." in stderr_text, (
            f"Expected exact premium message in stderr, got: {stderr_text!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: _run_ticket_with_retries executes without premium stages
# ---------------------------------------------------------------------------


class TestRunTicketWithRetriesCommunityMode:
    """_run_ticket_with_retries must complete cleanly with all premium stages absent."""

    def _make_ticket(self, ticket_id: str = "T001") -> Any:
        from saturnday._types import TicketSpec, TicketScope
        return TicketSpec(
            ticket_id=ticket_id,
            goal="Add a trivial test helper",
            scope=TicketScope(allowed_globs=["*.py"]),
            acceptance_criteria=["The helper exists"],
        )

    def _make_coder_config(self) -> Any:
        from saturnday._types import CoderConfig
        return CoderConfig(backend="anthropic", model="claude-3-5-sonnet-20241022")

    def _standard_patches(self) -> list:
        """Return the list of patch targets needed for a minimal ticket run."""
        return [
            patch("saturnday.ticket_runner._execute_ticket"),
            patch("saturnday.ticket_runner._git_add"),
            patch("saturnday.ticket_runner._auto_install_deps"),
            patch("saturnday.ticket_runner._run_governance"),
            patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
            patch("saturnday.ticket_runner._check_contracts", return_value=None),
            patch("saturnday.ticket_runner._log_ticket_summary"),
            patch("saturnday.ticket_runner._git_commit"),
            patch("saturnday.ticket_runner._snapshot_project_checks", return_value={}),
            patch("saturnday.ticket_runner.write_ticket_evidence"),
            patch("saturnday.ticket_runner._log_progress"),
            patch("saturnday.ticket_runner._filter_findings_to_files", return_value=[]),
            patch("saturnday.ticket_runner._extract_lesson_from_outcome"),
        ]

    def test_pass_path_no_errors(self, tmp_path: Path) -> None:
        """PASS path must complete without ImportError or AttributeError in community mode."""
        from saturnday.ticket_runner import _run_ticket_with_retries
        from saturnday.project_state import ProjectState

        ticket = self._make_ticket()
        coder_cfg = self._make_coder_config()
        state = ProjectState()
        ran: set[str] = set()

        patches = self._standard_patches()
        with (
            patches[0] as mock_exec,
            patches[1],
            patches[2],
            patches[3] as mock_gov,
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            patches[11],
            patches[12],
        ):
            mock_exec.return_value = ("# trivial helper\ndef helper(): pass", ["helper.py"])
            mock_gov.return_value = ("PASS", [], "evidence/path", [])

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=coder_cfg,
                system_prompt="system prompt",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                ran_stages=ran,
            )

        assert result.disposition == "PASS", (
            f"Expected PASS in community mode, got '{result.disposition}'"
        )
        # No premium stages should have run — ran set remains empty
        assert ran == set(), (
            f"No premium stages should run in community mode, but ran={ran}"
        )

    def test_governance_fail_path_no_errors(self, tmp_path: Path) -> None:
        """CODED_UNGOVERNED path must complete without ImportError in community mode.

        The filter returns findings tied to the changed file so that the ticket
        cannot be silently promoted to PASS (the production branch at line 1407
        only promotes when the filtered set is empty).
        """
        from saturnday.ticket_runner import _run_ticket_with_retries
        from saturnday.project_state import ProjectState

        ticket = self._make_ticket("T002")
        coder_cfg = self._make_coder_config()
        state = ProjectState()
        ran: set[str] = set()

        _gov_findings = [{"rule_id": "SEC-001", "severity": "error", "message": "bad pattern"}]

        patches = self._standard_patches() + [
            patch("saturnday.ticket_runner._git_reset_changes", return_value=None),
        ]
        with (
            patches[0] as mock_exec,
            patches[1],
            patches[2],
            patches[3] as mock_gov,
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8],
            patches[9],
            patches[10],
            patches[11] as mock_filter,  # _filter_findings_to_files
            patches[12],
            patches[13],
        ):
            mock_exec.return_value = ("# bad code", ["bad.py"])
            mock_gov.return_value = ("FAIL", _gov_findings, "evidence/path", [])
            # Return non-empty findings so the "no relevant findings → PASS" branch
            # is not taken.  The test proves the FAIL path runs without ImportError.
            mock_filter.return_value = _gov_findings

            result = _run_ticket_with_retries(
                ticket=ticket,
                repo_path=tmp_path,
                coder_config=coder_cfg,
                system_prompt="system prompt",
                state=state,
                plan_notes="",
                output_dir=tmp_path,
                ran_stages=ran,
                max_retries=0,
            )

        # After exhausting retries on a hard FAIL the ticket is CODED_UNGOVERNED.
        assert result.disposition in {"CODED_UNGOVERNED", "FAIL"}, (
            f"Unexpected disposition on governance failure: '{result.disposition}'"
        )
        assert ran == set(), (
            f"No premium stages should run in community mode, but ran={ran}"
        )
