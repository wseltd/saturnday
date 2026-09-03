"""SPLIT-034 — Integration tests proving the pipeline works with ALL premium
capabilities registered.

Five test classes:

1. TestFullRegistration       — snapshot has all 8+1 stages; each handler satisfies
                                its Protocol isinstance check.
2. TestEvidenceFieldsPremium  — build_capability_state() and build_skipped_stages()
                                are correct under full registration.
3. TestSelectiveRegistration  — partial registration produces accurate evidence.
4. TestPremiumModulesImportable — premium modules remain directly importable after
                                  registration (they haven't moved).
5. TestProtocolConformanceAfterRegistration — programmatic isinstance check for all 8
                                  protocol-bound stages.

Every class uses an autouse fixture that clears the registry before AND after each
test, guaranteeing total isolation.
"""
from __future__ import annotations

import base64
import os
import time

import pytest
pytest.importorskip("saturnday_premium", reason="saturnday-premium not installed")

import nacl.signing as _nacl_signing  # noqa: E402 — after importorskip gate

from saturnday import capability_registry
from saturnday.interfaces import (
    DocPostGlobalExt,
    EvidenceAppender,
    ImpactAnalysisExt,
    MemoryProvider,
    ReleaseGovernanceExt,
    ReviewerExt,
    SpecVerifierExt,
    TriageHook,
)
from saturnday.interfaces.doc_stages import STAGE_NAME as _DPG_NAME
from saturnday.interfaces.evidence import STAGE_NAME as _EA_NAME
from saturnday.interfaces.impact import STAGE_NAME as _IA_NAME
from saturnday.interfaces.memory import STAGE_NAME as _MEM_NAME
from saturnday.interfaces.release import STAGE_NAME as _REL_NAME
from saturnday.interfaces.reviewer import STAGE_NAME as _CR_NAME
from saturnday.interfaces.spec import STAGE_NAME as _SV_NAME
from saturnday.interfaces.triage import STAGE_NAME as _ST_NAME
from saturnday.shared.evidence_schema import (
    PREMIUM_STAGE_NAMES,
    build_capability_state,
    build_skipped_stages,
)


# ---------------------------------------------------------------------------
# JWT test helper
# ---------------------------------------------------------------------------

_TEST_SIGNING_KEY = _nacl_signing.SigningKey.generate()
_TEST_VERIFY_KEY_B64 = (
    base64.urlsafe_b64encode(_TEST_SIGNING_KEY.verify_key.encode()).rstrip(b"=").decode()
)
_TEST_PRIVATE_KEY_B64 = (
    base64.urlsafe_b64encode(bytes(_TEST_SIGNING_KEY)).rstrip(b"=").decode()
)


def _make_test_jwt(**overrides: object) -> str:
    """Generate a short-lived valid test JWT using a test Ed25519 key.

    Args:
        **overrides: Optional claim overrides (e.g. ``exp``, ``edition``).

    Returns:
        Signed JWT string suitable for ``SATURNDAY_LICENSE_KEY``.
    """
    import uuid

    from saturnday_premium._jwt import encode_jwt

    payload: dict[str, object] = {
        "lid": str(uuid.uuid4()),
        "org": "test-org",
        "edition": "premium",
        "exp": int(time.time()) + 3600,
    }
    payload.update(overrides)
    return encode_jwt(payload, private_key_b64=_TEST_PRIVATE_KEY_B64)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All stage names that register_premium_stages() should populate.
_ALL_STAGES: frozenset[str] = frozenset(
    {
        "security_triage",
        "memory_provider",
        "spec_verifier",
        "impact_analysis",
        "code_reviewer",
        "doc_post_global",
        "run_metrics",         # direct callable, not a protocol stage
        "evidence_appender",
        "release_governance",  # RS-015: premium release governance
    }
)

#: The 8 protocol-bound stage names (excludes run_metrics which is a plain callable).
_PROTOCOL_STAGES: tuple[tuple[str, type], ...] = (
    (_ST_NAME,  TriageHook),
    (_MEM_NAME, MemoryProvider),
    (_SV_NAME,  SpecVerifierExt),
    (_IA_NAME,  ImpactAnalysisExt),
    (_CR_NAME,  ReviewerExt),
    (_DPG_NAME, DocPostGlobalExt),
    (_EA_NAME,  EvidenceAppender),
    (_REL_NAME, ReleaseGovernanceExt),
)

# ---------------------------------------------------------------------------
# Autouse fixture — guarantees registry isolation for every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_registry(monkeypatch) -> object:
    """Provide a valid test JWT and clear registry before and after every test.

    Sets SATURNDAY_LICENSE_KEY so that any call to try_register_premium() or
    is_entitled() within a test runs the full JWT validation path and succeeds.
    Resets the cached entitlement state to None so the first entitlement query
    in each test reads the fresh env var.

    Does NOT pre-register any stages — tests that need registration call
    _full_register() themselves.
    """
    import saturnday_premium
    import saturnday_premium._jwt as jwt_mod

    monkeypatch.setattr(jwt_mod, "_VERIFY_KEY_B64", _TEST_VERIFY_KEY_B64)
    capability_registry.clear()
    jwt = _make_test_jwt()
    os.environ["SATURNDAY_LICENSE_KEY"] = jwt
    saturnday_premium._entitlement_state = None
    yield
    os.environ.pop("SATURNDAY_LICENSE_KEY", None)
    saturnday_premium._entitlement_state = None
    capability_registry.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_register() -> None:
    """Register all premium stages via the canonical entry point.

    The SATURNDAY_LICENSE_KEY env var must already be set by the autouse
    fixture before this helper is called.
    """
    from saturnday.premium_registration import try_register_premium

    try_register_premium()


# ---------------------------------------------------------------------------
# 1. TestFullRegistration
# ---------------------------------------------------------------------------


class TestFullRegistration:
    """try_register_premium() registers all 8+1 stages with correct adapter types."""

    def test_snapshot_has_all_stages(self) -> None:
        """capability_registry.snapshot() must have exactly the 9 expected keys."""
        _full_register()
        snap = capability_registry.snapshot()
        registered = frozenset(snap.keys())
        assert registered == _ALL_STAGES, (
            f"Missing stages: {_ALL_STAGES - registered}; "
            f"Unexpected stages: {registered - _ALL_STAGES}"
        )

    def test_snapshot_values_are_non_empty_strings(self) -> None:
        """snapshot() values are handler qualnames — must be non-empty strings."""
        _full_register()
        snap = capability_registry.snapshot()
        for stage, qualname in snap.items():
            assert isinstance(qualname, str), (
                f"snapshot[{stage!r}] value is not a str: {qualname!r}"
            )
            assert qualname, f"snapshot[{stage!r}] qualname is empty"

    def test_adapter_stages_have_adapter_in_qualname(self) -> None:
        """All protocol-bound adapters should report an 'Adapter' qualname."""
        _full_register()
        snap = capability_registry.snapshot()
        adapter_stage_names = {name for name, _ in _PROTOCOL_STAGES}
        for stage in adapter_stage_names:
            assert "Adapter" in snap[stage], (
                f"Expected 'Adapter' in qualname for {stage!r}, got {snap[stage]!r}"
            )

    def test_each_stage_is_available(self) -> None:
        """capability_registry.is_available() returns True for every registered stage."""
        _full_register()
        for stage in _ALL_STAGES:
            assert capability_registry.is_available(stage), (
                f"Stage {stage!r} should be available after full registration"
            )

    def test_protocol_stages_isinstance_each_protocol(self) -> None:
        """Every protocol-bound handler must satisfy isinstance() for its protocol."""
        _full_register()
        for stage_name, protocol in _PROTOCOL_STAGES:
            handler = capability_registry.get(stage_name)
            assert handler is not None, f"Handler for {stage_name!r} is None"
            assert isinstance(handler, protocol), (  # type: ignore[arg-type]
                f"Handler for {stage_name!r} ({type(handler).__qualname__}) does not "
                f"satisfy {protocol.__name__}"
            )


# ---------------------------------------------------------------------------
# 2. TestEvidenceFieldsPremiumMode
# ---------------------------------------------------------------------------


class TestEvidenceFieldsPremiumMode:
    """Evidence helpers return correct fields under full premium registration."""

    def test_capability_state_premium_enabled_is_true(self) -> None:
        """build_capability_state() must report premium_capabilities_enabled: True."""
        _full_register()
        state = build_capability_state()
        assert state["premium_capabilities_enabled"] is True

    def test_capability_state_package_installed(self) -> None:
        """premium_package_installed must be True when saturnday_premium is installed."""
        _full_register()
        state = build_capability_state()
        assert state["premium_package_installed"] is True

    def test_capability_state_entitlement_valid(self) -> None:
        """entitlement_valid must be True and reason must be 'valid' (JWT path)."""
        _full_register()
        state = build_capability_state()
        assert state["entitlement_valid"] is True
        assert state["entitlement_reason"] == "valid"

    def test_capability_state_entitlement_metadata_populated(self) -> None:
        """entitlement_org, entitlement_edition, entitlement_expires must be populated after valid JWT."""
        _full_register()
        state = build_capability_state()
        assert isinstance(state["entitlement_org"], str) and state["entitlement_org"], (
            f"entitlement_org must be a non-empty string, got {state['entitlement_org']!r}"
        )
        assert state["entitlement_edition"] == "premium", (
            f"entitlement_edition must be 'premium', got {state['entitlement_edition']!r}"
        )
        assert isinstance(state["entitlement_expires"], str) and state["entitlement_expires"], (
            f"entitlement_expires must be a non-empty string, got {state['entitlement_expires']!r}"
        )

    def test_capability_state_available_hooks_lists_all_stages(self) -> None:
        """available_premium_hooks must include all 9 registered stage names."""
        _full_register()
        state = build_capability_state()
        hooks = frozenset(state["available_premium_hooks"])
        assert hooks == _ALL_STAGES, (
            f"Missing from hooks: {_ALL_STAGES - hooks}; "
            f"Extra in hooks: {hooks - _ALL_STAGES}"
        )

    def test_build_skipped_stages_empty_when_ran_stages_none(self) -> None:
        """When ran_stages=None (default), registered stages are treated as having run.

        With full registration, only stages that are in PREMIUM_STAGE_NAMES but
        NOT yet registered by the premium package appear in the skipped list.
        ``release_governance`` is in PREMIUM_STAGE_NAMES but its premium handler
        is implemented by RS-017 (deferred premium ticket) — so it may appear as
        ``premium_not_available``.  All other registered stages must NOT appear.
        """
        _full_register()
        skipped = build_skipped_stages()
        # Only release_governance may appear (premium handler not yet implemented).
        # All other PREMIUM_STAGE_NAMES are registered and treated as having run.
        skipped_names = {e["stage"] for e in skipped}
        unexpected = skipped_names - {"release_governance"}
        assert not unexpected, (
            f"Unexpected stages in skipped list after full registration: {unexpected}. "
            f"Full skipped list: {skipped}"
        )

    def test_build_skipped_stages_reports_registered_but_not_executed(self) -> None:
        """Passing an empty ran_stages set → registered stages appear as
        registered_but_not_executed; unregistered stages appear as premium_not_available.

        ``release_governance`` is in PREMIUM_STAGE_NAMES but its premium handler
        is implemented by RS-017 (deferred premium ticket).  It will therefore
        appear as ``premium_not_available``, not ``registered_but_not_executed``.
        All other PREMIUM_STAGE_NAMES that are registered must appear as
        ``registered_but_not_executed``.
        """
        _full_register()
        skipped = build_skipped_stages(ran_stages=set())
        by_stage = {e["stage"]: e["reason"] for e in skipped}

        # All PREMIUM_STAGE_NAMES must appear in the skipped output.
        assert set(PREMIUM_STAGE_NAMES).issubset(set(by_stage)), (
            f"Some PREMIUM_STAGE_NAMES missing from skipped output. "
            f"Missing: {set(PREMIUM_STAGE_NAMES) - set(by_stage)}"
        )

        # For stages that were registered, reason must be registered_but_not_executed.
        registered = set(capability_registry.registered_stages())
        for stage_name in PREMIUM_STAGE_NAMES:
            if stage_name in registered:
                assert by_stage[stage_name] == "registered_but_not_executed", (
                    f"Registered stage {stage_name!r} should be "
                    f"'registered_but_not_executed', got {by_stage[stage_name]!r}"
                )
            else:
                # Unregistered stages (release_governance until RS-017) must be
                # premium_not_available.
                assert by_stage[stage_name] == "premium_not_available", (
                    f"Unregistered stage {stage_name!r} should be "
                    f"'premium_not_available', got {by_stage[stage_name]!r}"
                )

    def test_security_triage_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("security_triage") is True

    def test_memory_provider_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("memory_provider") is True

    def test_spec_verifier_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("spec_verifier") is True

    def test_impact_analysis_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("impact_analysis") is True

    def test_code_reviewer_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("code_reviewer") is True

    def test_doc_post_global_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("doc_post_global") is True

    def test_evidence_appender_is_available(self) -> None:
        _full_register()
        assert capability_registry.is_available("evidence_appender") is True


# ---------------------------------------------------------------------------
# 3. TestSelectiveRegistration
# ---------------------------------------------------------------------------


class TestSelectiveRegistration:
    """Registering only a subset of stages produces accurate evidence output."""

    def _register_two(self) -> None:
        """Manually register only security_triage and code_reviewer."""
        from saturnday_premium._adapters import _ReviewerAdapter, _TriageAdapter

        capability_registry.register(_ST_NAME, _TriageAdapter())
        capability_registry.register(_CR_NAME, _ReviewerAdapter())

    def test_capability_state_enabled_with_partial_registration(self) -> None:
        """premium_capabilities_enabled is True even with only 2 stages."""
        self._register_two()
        state = build_capability_state()
        assert state["premium_capabilities_enabled"] is True

    def test_available_hooks_has_only_two_stages(self) -> None:
        """available_premium_hooks lists only the 2 registered stages."""
        self._register_two()
        state = build_capability_state()
        hooks = set(state["available_premium_hooks"])
        assert hooks == {"security_triage", "code_reviewer"}, (
            f"Unexpected hooks: {hooks}"
        )

    def test_build_skipped_stages_lists_six_not_available(self) -> None:
        """With 2 of 8 PREMIUM_STAGE_NAMES registered, 6 appear as premium_not_available."""
        self._register_two()
        skipped = build_skipped_stages()
        not_available = [e for e in skipped if e["reason"] == "premium_not_available"]
        assert len(not_available) == 6, (
            f"Expected 6 premium_not_available entries, got {len(not_available)}: {not_available}"
        )

    def test_build_skipped_stages_omits_registered_stages(self) -> None:
        """security_triage and code_reviewer must NOT appear in the skipped list
        when ran_stages is None (registered → treated as having run).
        """
        self._register_two()
        skipped = build_skipped_stages()
        skipped_names = {e["stage"] for e in skipped}
        assert "security_triage" not in skipped_names, (
            "security_triage is registered; it must not appear in skipped"
        )
        assert "code_reviewer" not in skipped_names, (
            "code_reviewer is registered; it must not appear in skipped"
        )

    def test_registered_stages_are_available_via_is_available(self) -> None:
        self._register_two()
        assert capability_registry.is_available("security_triage") is True
        assert capability_registry.is_available("code_reviewer") is True

    def test_unregistered_stages_not_available(self) -> None:
        self._register_two()
        for stage in ("memory_provider", "spec_verifier", "impact_analysis",
                      "doc_post_global", "evidence_appender", "run_metrics"):
            assert capability_registry.is_available(stage) is False, (
                f"Stage {stage!r} should not be available after selective registration"
            )

    def test_protocol_conformance_for_registered_subset(self) -> None:
        """The 2 manually registered handlers satisfy their respective protocols."""
        self._register_two()
        triage = capability_registry.get("security_triage")
        assert isinstance(triage, TriageHook), type(triage)
        reviewer = capability_registry.get("code_reviewer")
        assert isinstance(reviewer, ReviewerExt), type(reviewer)


# ---------------------------------------------------------------------------
# 4. TestPremiumModulesImportable
# ---------------------------------------------------------------------------


class TestPremiumModulesImportable:
    """Premium modules remain directly importable after registration (they haven't moved)."""

    def test_security_triage_importable(self) -> None:
        _full_register()
        import saturnday.security_triage  # noqa: PLC0415

        assert hasattr(saturnday.security_triage, "triage_security_findings")

    def test_run_lessons_importable(self) -> None:
        _full_register()
        import saturnday.run.lessons  # noqa: PLC0415

        assert saturnday.run.lessons is not None

    def test_run_impact_analysis_importable(self) -> None:
        _full_register()
        import saturnday.run.impact_analysis  # noqa: PLC0415

        assert hasattr(saturnday.run.impact_analysis, "compute_impact")

    def test_run_memory_retrieval_importable(self) -> None:
        _full_register()
        import saturnday.run.memory_retrieval  # noqa: PLC0415

        assert hasattr(saturnday.run.memory_retrieval, "filter_relevant_items")

    def test_run_memory_enforcement_importable(self) -> None:
        _full_register()
        import saturnday.run.memory_enforcement  # noqa: PLC0415

        assert hasattr(saturnday.run.memory_enforcement, "check_enforced_rules")

    def test_run_spec_verifier_importable(self) -> None:
        _full_register()
        import saturnday.run.spec_verifier  # noqa: PLC0415

        assert hasattr(saturnday.run.spec_verifier, "run_spec_assertions")

    def test_run_property_tests_importable(self) -> None:
        _full_register()
        import saturnday.run.property_tests  # noqa: PLC0415

        assert hasattr(saturnday.run.property_tests, "run_property_tests")

    def test_run_dataflow_checker_importable(self) -> None:
        _full_register()
        import saturnday.run.dataflow_checker  # noqa: PLC0415

        assert hasattr(saturnday.run.dataflow_checker, "check_cross_function_flow")

    def test_run_metrics_importable(self) -> None:
        _full_register()
        import saturnday.run.metrics  # noqa: PLC0415

        assert hasattr(saturnday.run.metrics, "compute_run_metrics")

    def test_direct_import_handler_identity(self) -> None:
        """The adapter wraps the real module — the module is not replaced by the adapter."""
        _full_register()
        # The registry holds an adapter, not the raw function.
        handler = capability_registry.get("security_triage")
        import saturnday.security_triage as st_mod  # noqa: PLC0415

        # handler is an adapter object; the raw function still lives in the module
        assert callable(st_mod.triage_security_findings)
        assert handler is not st_mod.triage_security_findings, (
            "Registry should hold a _TriageAdapter wrapper, not the raw function"
        )


# ---------------------------------------------------------------------------
# 5. TestProtocolConformanceAfterRegistration
# ---------------------------------------------------------------------------


class TestProtocolConformanceAfterRegistration:
    """After try_register_premium(), every protocol-bound handler satisfies isinstance().

    Exercises the full _PROTOCOL_STAGES matrix programmatically so that adding a
    new protocol in future automatically exercises it here.
    """

    @pytest.mark.parametrize("stage_name,protocol", _PROTOCOL_STAGES)
    def test_handler_satisfies_protocol(
        self,
        stage_name: str,
        protocol: type,
    ) -> None:
        """get(stage_name) must pass isinstance(handler, protocol)."""
        _full_register()
        handler = capability_registry.get(stage_name)
        assert handler is not None, (
            f"No handler registered for {stage_name!r} — "
            "did try_register_premium() run before the check?"
        )
        assert isinstance(handler, protocol), (  # type: ignore[arg-type]
            f"{stage_name!r}: handler {type(handler).__qualname__!r} does not "
            f"satisfy {protocol.__name__}"
        )

    def test_run_metrics_is_callable(self) -> None:
        """run_metrics is registered as a plain callable, not a protocol object."""
        _full_register()
        handler = capability_registry.get("run_metrics")
        assert callable(handler), (
            f"run_metrics handler should be callable, got {type(handler)!r}"
        )

    def test_total_registered_stage_count(self) -> None:
        """Sanity: exactly 8 stages are in the registry after full registration."""
        _full_register()
        stages = capability_registry.registered_stages()
        assert len(stages) == len(_ALL_STAGES), (
            f"Expected {len(_ALL_STAGES)} registered stages, got {len(stages)}: {stages}"
        )
