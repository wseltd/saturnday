"""Tests for RS-015: release governance interface protocol + evidence schema additions.

Acceptance criteria:
- ReleaseGovernanceExt protocol defined with @runtime_checkable
- Methods have clear type signatures
- release_governance added to PREMIUM_STAGE_NAMES
- EVIDENCE_DIR_RELEASE added to evidence_schema.py
- Existing evidence build_capability_state and build_skipped_stages pick up the new stage
- release-approve CLI subcommand stub registered in cli.py, premium-gated
- Without premium: release-approve prints "requires premium" message and exits 1
"""
from __future__ import annotations

import argparse
import io
import sys
from typing import Any
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# RS-015: ReleaseGovernanceExt protocol
# ---------------------------------------------------------------------------


class TestReleaseGovernanceExtProtocol:
    """ReleaseGovernanceExt must be a runtime-checkable Protocol with correct methods."""

    def test_protocol_importable(self) -> None:
        from saturnday.interfaces.release import ReleaseGovernanceExt
        assert ReleaseGovernanceExt is not None

    def test_stage_name_importable(self) -> None:
        from saturnday.interfaces.release import STAGE_NAME
        assert STAGE_NAME == "release_governance"

    def test_protocol_is_runtime_checkable(self) -> None:
        """isinstance() checks must work without raising TypeError."""
        from saturnday.interfaces.release import ReleaseGovernanceExt

        class ConformingImpl:
            def apply_policy(self, check_results: list, policy: dict) -> list:
                return check_results

            def approve(self, args: Any) -> int:
                return 0

            def record_exception(self, exception: dict, evidence_dir: Any) -> None:
                pass

        obj = ConformingImpl()
        # Must not raise TypeError.
        result = isinstance(obj, ReleaseGovernanceExt)
        assert result is True

    def test_non_conforming_class_fails_isinstance(self) -> None:
        """A class missing the required methods must fail isinstance()."""
        from saturnday.interfaces.release import ReleaseGovernanceExt

        class PartialImpl:
            def apply_policy(self, check_results: list, policy: dict) -> list:
                return check_results
            # Missing approve and record_exception

        obj = PartialImpl()
        assert not isinstance(obj, ReleaseGovernanceExt)

    def test_plain_object_fails_isinstance(self) -> None:
        from saturnday.interfaces.release import ReleaseGovernanceExt
        assert not isinstance(object(), ReleaseGovernanceExt)

    def test_apply_policy_method_exists(self) -> None:
        """The apply_policy method must be defined on the protocol."""
        from saturnday.interfaces.release import ReleaseGovernanceExt
        assert hasattr(ReleaseGovernanceExt, "apply_policy")

    def test_approve_method_exists(self) -> None:
        from saturnday.interfaces.release import ReleaseGovernanceExt
        assert hasattr(ReleaseGovernanceExt, "approve")

    def test_record_exception_method_exists(self) -> None:
        from saturnday.interfaces.release import ReleaseGovernanceExt
        assert hasattr(ReleaseGovernanceExt, "record_exception")


class TestReleaseGovernanceExtExportedFromInterfaces:
    """ReleaseGovernanceExt must be exported from the top-level interfaces package."""

    def test_exported_via_init(self) -> None:
        from saturnday.interfaces import ReleaseGovernanceExt
        assert ReleaseGovernanceExt is not None

    def test_in_all_list(self) -> None:
        import saturnday.interfaces as ifaces
        assert "ReleaseGovernanceExt" in ifaces.__all__

    def test_stage_name_from_release_module(self) -> None:
        from saturnday.interfaces.release import STAGE_NAME
        assert STAGE_NAME == "release_governance"


# ---------------------------------------------------------------------------
# RS-015 Part A: evidence_schema.py additions
# ---------------------------------------------------------------------------


class TestEvidenceSchemaDirRelease:
    """EVIDENCE_DIR_RELEASE must be added to evidence_schema.py."""

    def test_constant_importable(self) -> None:
        from saturnday.shared.evidence_schema import EVIDENCE_DIR_RELEASE
        assert EVIDENCE_DIR_RELEASE is not None

    def test_constant_value(self) -> None:
        from saturnday.shared.evidence_schema import EVIDENCE_DIR_RELEASE
        assert EVIDENCE_DIR_RELEASE == "evidence/release"

    def test_constant_is_string(self) -> None:
        from saturnday.shared.evidence_schema import EVIDENCE_DIR_RELEASE
        assert isinstance(EVIDENCE_DIR_RELEASE, str)

    def test_constant_distinct_from_others(self) -> None:
        """EVIDENCE_DIR_RELEASE must differ from all existing directory constants."""
        from saturnday.shared.evidence_schema import (
            EVIDENCE_DIR_GUARD,
            EVIDENCE_DIR_REPAIR,
            EVIDENCE_DIR_RUN,
            EVIDENCE_DIR_DOCUMENT,
            EVIDENCE_DIR_RELEASE,
        )
        all_dirs = {
            EVIDENCE_DIR_GUARD,
            EVIDENCE_DIR_RUN,
            EVIDENCE_DIR_REPAIR,
            EVIDENCE_DIR_DOCUMENT,
            EVIDENCE_DIR_RELEASE,
        }
        assert len(all_dirs) == 5, (
            "All five evidence directory constants must be distinct"
        )

    def test_constant_in_all(self) -> None:
        import saturnday.shared.evidence_schema as schema
        assert "EVIDENCE_DIR_RELEASE" in schema.__all__


class TestPremiumStageNamesIncludesReleaseGovernance:
    """release_governance must be in PREMIUM_STAGE_NAMES after RS-015."""

    def test_release_governance_in_stage_names(self) -> None:
        from saturnday.shared.evidence_schema import PREMIUM_STAGE_NAMES
        assert "release_governance" in PREMIUM_STAGE_NAMES

    def test_stage_names_has_eight_entries(self) -> None:
        from saturnday.shared.evidence_schema import PREMIUM_STAGE_NAMES
        assert len(PREMIUM_STAGE_NAMES) == 8, (
            f"Expected 8 premium stage names, got {len(PREMIUM_STAGE_NAMES)}: "
            f"{PREMIUM_STAGE_NAMES}"
        )

    def test_all_original_stages_still_present(self) -> None:
        from saturnday.shared.evidence_schema import PREMIUM_STAGE_NAMES
        original_stages = {
            "security_triage",
            "memory_provider",
            "spec_verifier",
            "impact_analysis",
            "code_reviewer",
            "doc_post_global",
            "evidence_appender",
        }
        for stage in original_stages:
            assert stage in PREMIUM_STAGE_NAMES, (
                f"Original stage {stage!r} missing from PREMIUM_STAGE_NAMES"
            )


class TestBuildSkippedStagesPicksUpReleaseGovernance:
    """build_skipped_stages must include release_governance when not registered."""

    @pytest.fixture(autouse=True)
    def _isolate(self) -> object:
        import saturnday.capability_registry as reg
        reg.clear()
        yield
        reg.clear()

    def test_release_governance_appears_as_not_available(self) -> None:
        from saturnday.shared.evidence_schema import build_skipped_stages

        skipped = build_skipped_stages(ran_stages=None)
        stage_names = {e["stage"] for e in skipped}
        assert "release_governance" in stage_names, (
            "release_governance must appear in skipped list when not registered"
        )

    def test_release_governance_reason_is_not_available(self) -> None:
        from saturnday.shared.evidence_schema import build_skipped_stages

        skipped = build_skipped_stages(ran_stages=None)
        by_stage = {e["stage"]: e["reason"] for e in skipped}
        assert by_stage.get("release_governance") == "premium_not_available", (
            "release_governance reason must be 'premium_not_available' when not registered"
        )

    def test_release_governance_not_skipped_when_registered(self) -> None:
        import saturnday.capability_registry as reg
        from saturnday.shared.evidence_schema import build_skipped_stages

        # Register a sentinel object as the release_governance handler.
        sentinel = object()
        reg.register("release_governance", sentinel)

        skipped = build_skipped_stages(ran_stages={"release_governance"})
        stage_names = {e["stage"] for e in skipped}
        assert "release_governance" not in stage_names, (
            "release_governance must NOT appear in skipped when registered and ran"
        )

    def test_release_governance_registered_but_not_executed(self) -> None:
        import saturnday.capability_registry as reg
        from saturnday.shared.evidence_schema import build_skipped_stages

        sentinel = object()
        reg.register("release_governance", sentinel)

        # Registered but not in ran_stages.
        skipped = build_skipped_stages(ran_stages=set())
        by_stage = {e["stage"]: e["reason"] for e in skipped}
        assert by_stage.get("release_governance") == "registered_but_not_executed", (
            "release_governance must be 'registered_but_not_executed' when "
            "registered but not in ran_stages"
        )


# ---------------------------------------------------------------------------
# RS-015 Part C: release-approve CLI stub
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _clear_registry():
    """Ensure registry is empty before and after each CLI test."""
    import saturnday.capability_registry as reg
    reg.clear()
    yield
    reg.clear()


class TestReleaseApproveCLIStub:
    """release-approve CLI stub must exist and be premium-gated."""

    def test_release_approve_subcommand_registered(self) -> None:
        """saturnday release-approve must be a registered subparser."""
        from saturnday.cli import build_parser

        parser = build_parser()
        # The subparser action collects all known choices.
        subparsers_action = next(
            a for a in parser._actions
            if hasattr(a, "_name_parser_map")
        )
        assert "release-approve" in subparsers_action._name_parser_map, (
            "release-approve must be a registered CLI subcommand"
        )

    @pytest.mark.usefixtures("_clear_registry")
    def test_release_approve_returns_1_without_premium(self, tmp_path) -> None:
        """release-approve must return exit code 1 when premium is not registered."""
        from saturnday.cli import main

        stderr_buf = io.StringIO()
        with (
            patch.dict(sys.modules, {"saturnday_premium": None}),
            patch("sys.stderr", stderr_buf),
            patch("sys.argv", [
                "saturnday",
                "release-approve",
                "--evidence", str(tmp_path),
                "--approver", "alice",
            ]),
        ):
            try:
                rc = main()
            except SystemExit as exc:
                rc = exc.code

        assert rc == 1, (
            f"release-approve must return 1 without premium, got {rc!r}"
        )

    @pytest.mark.usefixtures("_clear_registry")
    def test_release_approve_emits_premium_message(self, tmp_path) -> None:
        """release-approve must print a 'requires' message to stderr when not premium."""
        from saturnday.cli import main

        stderr_buf = io.StringIO()
        with (
            patch.dict(sys.modules, {"saturnday_premium": None}),
            patch("sys.stderr", stderr_buf),
            patch("sys.argv", [
                "saturnday",
                "release-approve",
                "--evidence", str(tmp_path),
                "--approver", "alice",
            ]),
        ):
            try:
                main()
            except SystemExit:
                pass

        stderr_text = stderr_buf.getvalue()
        assert "premium" in stderr_text.lower(), (
            f"Expected 'premium' in stderr output, got: {stderr_text!r}"
        )

    def test_release_approve_help_text_accessible(self) -> None:
        """release-approve --help must not raise SystemExit with error."""
        from saturnday.cli import build_parser

        parser = build_parser()
        subparsers_action = next(
            a for a in parser._actions
            if hasattr(a, "_name_parser_map")
        )
        # Getting the sub-parser for release-approve must not raise.
        sub = subparsers_action._name_parser_map.get("release-approve")
        assert sub is not None
        # The subparser must have a formatted help string.
        help_text = sub.format_help()
        assert len(help_text) > 0


# ---------------------------------------------------------------------------
# Integration: ReleaseGovernanceExt registered in capability_registry
# ---------------------------------------------------------------------------


class TestReleaseGovernanceRegistration:
    """Registering a conforming handler must make it available via the registry."""

    @pytest.fixture(autouse=True)
    def _isolate(self) -> object:
        import saturnday.capability_registry as reg
        reg.clear()
        yield
        reg.clear()

    def test_conforming_handler_registered_and_retrievable(self) -> None:
        import saturnday.capability_registry as reg
        from saturnday.interfaces.release import ReleaseGovernanceExt, STAGE_NAME

        class ConformingHandler:
            def apply_policy(self, check_results: list, policy: dict) -> list:
                return check_results

            def approve(self, args: Any) -> int:
                return 0

            def record_exception(self, exception: dict, evidence_dir: Any) -> None:
                pass

        handler = ConformingHandler()
        reg.register(STAGE_NAME, handler)

        retrieved = reg.get(STAGE_NAME)
        assert retrieved is handler
        assert reg.is_available(STAGE_NAME) is True
        assert isinstance(retrieved, ReleaseGovernanceExt)

    def test_is_available_false_before_registration(self) -> None:
        import saturnday.capability_registry as reg
        from saturnday.interfaces.release import STAGE_NAME

        assert reg.is_available(STAGE_NAME) is False
