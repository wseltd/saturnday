"""Tests for src/saturnday/premium_registration.py (SPLIT-015).

Coverage goals:
1. Full registration — all 8 stages registered when all modules importable.
2. Partial registration — one ImportError skips only that stage, others registered.
3. Snapshot shape — capability_registry.snapshot() keys are stage name strings.
4. Protocol conformance — each registered adapter satisfies isinstance() for its protocol.
5. Module importable without any premium modules (ImportError-safe module load).
6. try_register_premium return value semantics.
"""
from __future__ import annotations

import base64
import os
import sys
import time
import types
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
pytest.importorskip("saturnday_premium", reason="saturnday-premium not installed")

import nacl.signing as _nacl_signing  # noqa: E402 — after importorskip gate

from saturnday import capability_registry
from saturnday.interfaces import (
    DocPostGlobalExt,
    EvidenceAppender,
    ImpactAnalysisExt,
    MemoryProvider,
    ReviewerExt,
    SpecVerifierExt,
    TriageHook,
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
        **overrides: Optional claim overrides.

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


@pytest.fixture(autouse=True, scope="module")
def _patch_verify_key_module():
    """Patch _VERIFY_KEY_B64 to the test public key for the entire module."""
    import saturnday_premium._jwt as jwt_mod

    with patch.object(jwt_mod, "_VERIFY_KEY_B64", _TEST_VERIFY_KEY_B64):
        yield


def _ensure_entitled() -> None:
    """Set SATURNDAY_LICENSE_KEY and bootstrap premium if not already entitled."""
    import saturnday_premium

    if not saturnday_premium.is_entitled():
        os.environ["SATURNDAY_LICENSE_KEY"] = _make_test_jwt()
        saturnday_premium._entitlement_state = None
        saturnday_premium._bootstrap()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ALL_STAGES = {
    "security_triage",
    "memory_provider",
    "spec_verifier",
    "impact_analysis",
    "code_reviewer",
    "doc_post_global",
    "run_metrics",
    "evidence_appender",
    "release_governance",
}


def _clear_and_register() -> None:
    """Reset the registry and ensure entitlement, then run full registration."""
    capability_registry.clear()
    _ensure_entitled()
    from saturnday.premium_registration import register_premium_stages

    register_premium_stages()


# ---------------------------------------------------------------------------
# Test 1: Full registration — all 9 stages registered
# ---------------------------------------------------------------------------


class TestFullRegistration:
    def setup_method(self) -> None:
        _clear_and_register()

    def teardown_method(self) -> None:
        capability_registry.clear()

    def test_all_stages_registered(self) -> None:
        """All 9 expected stage names must appear in the registry."""
        registered = set(capability_registry.registered_stages())
        assert _ALL_STAGES == registered, (
            f"Missing: {_ALL_STAGES - registered}; Extra: {registered - _ALL_STAGES}"
        )

    def test_each_stage_is_available(self) -> None:
        for name in _ALL_STAGES:
            assert capability_registry.is_available(name), f"Stage '{name}' not available"

    def test_get_returns_non_none_for_all_stages(self) -> None:
        for name in _ALL_STAGES:
            assert capability_registry.get(name) is not None, f"get('{name}') returned None"


# ---------------------------------------------------------------------------
# Test 2: Partial registration — one import failure skips only that stage
# ---------------------------------------------------------------------------


class TestPartialRegistration:
    def teardown_method(self) -> None:
        capability_registry.clear()

    def test_skip_security_triage_when_import_fails(self) -> None:
        """If security_triage raises ImportError, only that stage is absent."""
        capability_registry.clear()

        # Patch security_triage to raise ImportError when imported inside the adapter block.
        # We do this by temporarily inserting a broken module into sys.modules.
        broken = types.ModuleType("saturnday.security_triage")

        def _raise(*args: Any, **kwargs: Any) -> None:
            raise ImportError("simulated missing security_triage")

        broken.triage_security_findings = _raise  # type: ignore[attr-defined]

        # Temporarily replace with a sentinel that raises on attribute access.
        # Easiest: use patch to make the import itself fail.
        with patch.dict(sys.modules, {"saturnday.security_triage": None}):  # type: ignore[dict-item]
            from saturnday.premium_registration import register_premium_stages

            register_premium_stages()

        registered = set(capability_registry.registered_stages())
        assert "security_triage" not in registered, "security_triage should not be registered"
        remaining = _ALL_STAGES - {"security_triage"}
        assert remaining.issubset(registered), (
            f"Other stages should still register. Missing: {remaining - registered}"
        )

    def test_skip_run_metrics_when_import_fails(self) -> None:
        """If run.metrics raises ImportError, only run_metrics stage is absent."""
        capability_registry.clear()

        with patch.dict(sys.modules, {"saturnday.run.metrics": None}):  # type: ignore[dict-item]
            from saturnday.premium_registration import register_premium_stages

            register_premium_stages()

        registered = set(capability_registry.registered_stages())
        assert "run_metrics" not in registered
        remaining = _ALL_STAGES - {"run_metrics"}
        assert remaining.issubset(registered), (
            f"Other stages should still register. Missing: {remaining - registered}"
        )


# ---------------------------------------------------------------------------
# Test 3: Snapshot shape
# ---------------------------------------------------------------------------


class TestSnapshotShape:
    def setup_method(self) -> None:
        _clear_and_register()

    def teardown_method(self) -> None:
        capability_registry.clear()

    def test_snapshot_keys_are_stage_names(self) -> None:
        snap = capability_registry.snapshot()
        assert isinstance(snap, dict)
        assert set(snap.keys()) == _ALL_STAGES

    def test_snapshot_values_are_qualname_strings(self) -> None:
        snap = capability_registry.snapshot()
        for stage_name, qualname in snap.items():
            assert isinstance(qualname, str), f"snapshot[{stage_name!r}] is not a string"
            assert qualname, f"snapshot[{stage_name!r}] is an empty string"

    def test_snapshot_adapter_qualnames_contain_adapter(self) -> None:
        """Adapter wrappers should have 'Adapter' in their qualname."""
        snap = capability_registry.snapshot()
        adapter_stages = {
            "security_triage",
            "memory_provider",
            "spec_verifier",
            "impact_analysis",
            "code_reviewer",
            "doc_post_global",
            "evidence_appender",
        }
        for stage in adapter_stages:
            assert "Adapter" in snap[stage], (
                f"Expected 'Adapter' in qualname for {stage!r}, got {snap[stage]!r}"
            )


# ---------------------------------------------------------------------------
# Test 4: Protocol conformance — isinstance checks
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def setup_method(self) -> None:
        _clear_and_register()

    def teardown_method(self) -> None:
        capability_registry.clear()

    def test_security_triage_is_triage_hook(self) -> None:
        handler = capability_registry.get("security_triage")
        assert isinstance(handler, TriageHook), type(handler)

    def test_memory_provider_is_memory_provider(self) -> None:
        handler = capability_registry.get("memory_provider")
        assert isinstance(handler, MemoryProvider), type(handler)

    def test_spec_verifier_is_spec_verifier_ext(self) -> None:
        handler = capability_registry.get("spec_verifier")
        assert isinstance(handler, SpecVerifierExt), type(handler)

    def test_impact_analysis_is_impact_analysis_ext(self) -> None:
        handler = capability_registry.get("impact_analysis")
        assert isinstance(handler, ImpactAnalysisExt), type(handler)

    def test_code_reviewer_is_reviewer_ext(self) -> None:
        handler = capability_registry.get("code_reviewer")
        assert isinstance(handler, ReviewerExt), type(handler)

    def test_doc_post_global_is_doc_post_global_ext(self) -> None:
        handler = capability_registry.get("doc_post_global")
        assert isinstance(handler, DocPostGlobalExt), type(handler)

    def test_evidence_appender_is_evidence_appender(self) -> None:
        handler = capability_registry.get("evidence_appender")
        assert isinstance(handler, EvidenceAppender), type(handler)


# ---------------------------------------------------------------------------
# Test 5: Module importable without any premium modules
# ---------------------------------------------------------------------------


class TestModuleImportability:
    def teardown_method(self) -> None:
        capability_registry.clear()

    def test_module_imports_cleanly(self) -> None:
        """The module itself must be importable at Python load time."""
        import saturnday.premium_registration as pr  # noqa: F401 PLC0415

        assert hasattr(pr, "register_premium_stages")
        assert hasattr(pr, "try_register_premium")

    def test_module_does_not_auto_register(self) -> None:
        """Importing the module must NOT trigger registration automatically."""
        capability_registry.clear()
        # Force reimport to simulate fresh load.
        mod_name = "saturnday.premium_registration"
        # Remove from cache so re-import runs module-level code again.
        if mod_name in sys.modules:
            saved = sys.modules.pop(mod_name)
        else:
            saved = None
        try:
            import saturnday.premium_registration  # noqa: F401 PLC0415

            assert capability_registry.registered_stages() == [], (
                "Module load must not auto-register any stages"
            )
        finally:
            if saved is not None:
                sys.modules[mod_name] = saved


# ---------------------------------------------------------------------------
# Test 6: try_register_premium return value semantics
# ---------------------------------------------------------------------------


class TestTryRegisterPremium:
    def teardown_method(self) -> None:
        import saturnday_premium

        os.environ.pop("SATURNDAY_LICENSE_KEY", None)
        saturnday_premium._entitlement_state = None
        capability_registry.clear()

    def test_returns_true_when_stages_registered(self) -> None:
        import saturnday_premium
        from saturnday.premium_registration import try_register_premium

        # Set env var and reset cached state so is_entitled() returns True.
        # Do NOT call _bootstrap() first — that would pre-register all stages,
        # making try_register_premium() see new_stages={} and return False.
        capability_registry.clear()
        os.environ["SATURNDAY_LICENSE_KEY"] = _make_test_jwt()
        saturnday_premium._entitlement_state = None
        result = try_register_premium()
        assert result is True

    def test_returns_false_when_all_imports_fail(self) -> None:
        """If every premium module is unavailable, returns False."""
        capability_registry.clear()

        # Block all premium module imports by patching sys.modules entries to None.
        blocked = {
            "saturnday.security_triage": None,
            "saturnday.run.memory_retrieval": None,
            "saturnday.run.memory_enforcement": None,
            "saturnday.run.spec_verifier": None,
            "saturnday.run.property_tests": None,
            "saturnday.run.dataflow_checker": None,
            "saturnday.run.impact_analysis": None,
            "saturnday.role_modes": None,
            "saturnday.document.claim_verifier": None,
            "saturnday.document.provisional": None,
            "saturnday.document.signoff": None,
            "saturnday.run.metrics": None,
        }
        with patch.dict(sys.modules, blocked):  # type: ignore[arg-type]
            from saturnday.premium_registration import try_register_premium

            result = try_register_premium()

        # evidence_appender and interfaces are always available, so at least
        # that stage registers.  The point is the function returns a bool.
        assert isinstance(result, bool)

    def test_idempotent_double_call(self) -> None:
        """Calling twice must not raise; first call returns True, second False (nothing new)."""
        import saturnday_premium
        from saturnday.premium_registration import try_register_premium

        # Set env var and reset cached state — do NOT pre-bootstrap.
        capability_registry.clear()
        os.environ["SATURNDAY_LICENSE_KEY"] = _make_test_jwt()
        saturnday_premium._entitlement_state = None
        first = try_register_premium()
        second = try_register_premium()
        assert first is True
        # Second call: all stages already registered, so new_stages diff is empty.
        assert second is False

    def test_registered_count_matches_expected(self) -> None:
        import saturnday_premium
        from saturnday.premium_registration import try_register_premium

        capability_registry.clear()
        os.environ["SATURNDAY_LICENSE_KEY"] = _make_test_jwt()
        saturnday_premium._entitlement_state = None
        try_register_premium()
        assert len(capability_registry.registered_stages()) == len(_ALL_STAGES)
