"""Tests for shared evidence schema constants (U8, SPLIT-011)."""

from __future__ import annotations

import pytest


def test_schema_version_is_string() -> None:
    """SCHEMA_VERSION must be a non-empty string."""
    from saturnday.shared.evidence_schema import SCHEMA_VERSION

    assert isinstance(SCHEMA_VERSION, str)
    assert SCHEMA_VERSION


def test_schema_version_matches_guard_evidence() -> None:
    """Guard evidence module must expose the same SCHEMA_VERSION as the shared schema."""
    from saturnday.shared.evidence_schema import SCHEMA_VERSION as shared_version
    from saturnday.evidence import SCHEMA_VERSION as guard_version

    assert guard_version == shared_version


def test_schema_version_matches_run_evidence() -> None:
    """Run evidence module must import SCHEMA_VERSION from the shared schema."""
    from saturnday.shared.evidence_schema import SCHEMA_VERSION as shared_version
    from saturnday.run.evidence import SCHEMA_VERSION as run_version

    assert run_version == shared_version


def test_evidence_dir_constants_are_strings() -> None:
    """Evidence directory constants must be non-empty strings."""
    from saturnday.shared.evidence_schema import (
        EVIDENCE_DIR_GUARD,
        EVIDENCE_DIR_REPAIR,
        EVIDENCE_DIR_RUN,
    )

    for constant in (EVIDENCE_DIR_GUARD, EVIDENCE_DIR_RUN, EVIDENCE_DIR_REPAIR):
        assert isinstance(constant, str)
        assert constant


def test_evidence_dir_constants_are_distinct() -> None:
    """Each evidence directory constant must be a unique path."""
    from saturnday.shared.evidence_schema import (
        EVIDENCE_DIR_GUARD,
        EVIDENCE_DIR_REPAIR,
        EVIDENCE_DIR_RUN,
    )

    dirs = {EVIDENCE_DIR_GUARD, EVIDENCE_DIR_RUN, EVIDENCE_DIR_REPAIR}
    assert len(dirs) == 3, "Evidence directory constants must be distinct"


def test_guard_evidence_schema_version_in_evidence_pack(tmp_path: object) -> None:
    """EvidencePack.schema_version written by Guard must match shared SCHEMA_VERSION."""
    from saturnday.shared.evidence_schema import SCHEMA_VERSION
    from saturnday.evidence import EvidencePack

    pack = EvidencePack(
        schema_version=SCHEMA_VERSION,
        run_id="run-001",
        mode="check",
        repo_path="/tmp/repo",
        diff_range="HEAD~1..HEAD",
        saturnday_version="0.1.0",
        created_utc="2026-03-19T00:00:00Z",
    )
    assert pack.schema_version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# SPLIT-011: PREMIUM_STAGE_NAMES and build_capability_state
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=False)
def _isolate_registry():
    """Clear the capability registry before and after each premium test."""
    import saturnday.capability_registry as reg

    reg.clear()
    yield
    reg.clear()


def test_premium_stage_names_lists_all_eight() -> None:
    """PREMIUM_STAGE_NAMES must contain exactly the 8 canonical stage names."""
    from saturnday.shared.evidence_schema import PREMIUM_STAGE_NAMES

    expected = {
        "security_triage",
        "memory_provider",
        "spec_verifier",
        "impact_analysis",
        "code_reviewer",
        "doc_post_global",
        "evidence_appender",
        "release_governance",
    }
    assert isinstance(PREMIUM_STAGE_NAMES, list)
    assert set(PREMIUM_STAGE_NAMES) == expected
    assert len(PREMIUM_STAGE_NAMES) == 8, "Exactly 8 canonical stage names required"


@pytest.mark.usefixtures("_isolate_registry")
def test_build_capability_state_empty_registry() -> None:
    """With no registered stages, build_capability_state returns disabled state."""
    from saturnday.shared.evidence_schema import build_capability_state

    state = build_capability_state()

    assert state["premium_capabilities_enabled"] is False
    assert state["available_premium_hooks"] == []
    assert state["skipped_premium_stages"] == []


@pytest.mark.usefixtures("_isolate_registry")
def test_build_capability_state_after_registration() -> None:
    """After registering one stage, build_capability_state reflects it as enabled."""
    import saturnday.capability_registry as reg
    from saturnday.shared.evidence_schema import build_capability_state

    sentinel = object()
    reg.register("security_triage", sentinel)

    state = build_capability_state()

    assert state["premium_capabilities_enabled"] is True
    assert "security_triage" in state["available_premium_hooks"]
    assert state["skipped_premium_stages"] == []


@pytest.mark.usefixtures("_isolate_registry")
def test_build_capability_state_hooks_are_sorted() -> None:
    """available_premium_hooks must be returned in sorted order."""
    import saturnday.capability_registry as reg
    from saturnday.shared.evidence_schema import build_capability_state

    reg.register("spec_verifier", object())
    reg.register("code_reviewer", object())
    reg.register("memory_provider", object())

    state = build_capability_state()

    assert state["available_premium_hooks"] == sorted(state["available_premium_hooks"])
    assert len(state["available_premium_hooks"]) == 3


# ---------------------------------------------------------------------------
# SPLIT-012: build_skipped_stages
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_isolate_registry")
def test_build_skipped_stages_empty_registry_ran_stages_none() -> None:
    """With no registered stages and ran_stages=None, all 8 stages are premium_not_available."""
    from saturnday.shared.evidence_schema import PREMIUM_STAGE_NAMES, build_skipped_stages

    skipped = build_skipped_stages(ran_stages=None)

    assert len(skipped) == 8, "All 8 premium stages must appear when none are registered"
    stage_names = [entry["stage"] for entry in skipped]
    assert set(stage_names) == set(PREMIUM_STAGE_NAMES)
    for entry in skipped:
        assert entry["reason"] == "premium_not_available", (
            f"Stage {entry['stage']!r} should be 'premium_not_available', got {entry['reason']!r}"
        )


@pytest.mark.usefixtures("_isolate_registry")
def test_build_skipped_stages_registered_but_not_executed() -> None:
    """A stage that is registered but not in ran_stages gets reason 'registered_but_not_executed'."""
    import saturnday.capability_registry as reg
    from saturnday.shared.evidence_schema import build_skipped_stages

    reg.register("spec_verifier", object())

    # ran_stages does not include "spec_verifier"
    skipped = build_skipped_stages(ran_stages={"memory_provider"})

    by_stage = {entry["stage"]: entry["reason"] for entry in skipped}
    assert by_stage["spec_verifier"] == "registered_but_not_executed", (
        "Registered but unexecuted stage must have reason 'registered_but_not_executed'"
    )
    # All other stages (not registered) must be premium_not_available
    for stage, reason in by_stage.items():
        if stage != "spec_verifier":
            assert reason == "premium_not_available", (
                f"Unregistered stage {stage!r} should be 'premium_not_available', got {reason!r}"
            )


@pytest.mark.usefixtures("_isolate_registry")
def test_build_skipped_stages_registered_and_ran_not_in_output() -> None:
    """A stage that is registered and present in ran_stages must NOT appear in skipped list."""
    import saturnday.capability_registry as reg
    from saturnday.shared.evidence_schema import build_skipped_stages

    reg.register("code_reviewer", object())

    skipped = build_skipped_stages(ran_stages={"code_reviewer"})

    stage_names = [entry["stage"] for entry in skipped]
    assert "code_reviewer" not in stage_names, (
        "A stage that was registered and ran must not appear in the skipped list"
    )
    # The remaining 7 unregistered stages should still appear
    assert len(skipped) == 7
