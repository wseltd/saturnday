"""SPLIT-042 / 043 / 044 — Three-state package split tests.

Validates the public/premium split behaves correctly in three distinct states:

State A (SPLIT-042) — premium package not importable.
    ``sys.modules["saturnday_premium"]`` is set to None to simulate the package
    not being installed.  All premium evidence fields must reflect absence.

State B (SPLIT-043) — premium installed and entitled (valid JWT).
    The real ``saturnday_premium`` package is imported and bootstrapped with a
    valid test JWT.  All 8 stages must be registered; entitlement fields must
    be True with reason "valid".

State C (SPLIT-044) — premium installed but not entitled.
    ``saturnday_premium._entitlement.check_entitlement`` is patched to return
    ``valid=False``, then ``_bootstrap()`` is called manually.  The registry
    must remain empty; evidence must record the failure reason.
"""
from __future__ import annotations

import base64
import os
import sys
import time
from typing import Any
from unittest.mock import patch

import pytest
pytest.importorskip("saturnday_premium", reason="saturnday-premium not installed")

import nacl.signing as _nacl_signing  # noqa: E402 — after importorskip gate

from saturnday import capability_registry
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
# Helpers shared across all test classes
# ---------------------------------------------------------------------------


def _assert_all_stages_skipped_not_available(skipped: list[dict[str, str]]) -> None:
    """Assert every PREMIUM_STAGE_NAME appears with reason ``premium_not_available``."""
    by_name = {entry["stage"]: entry["reason"] for entry in skipped}
    for name in PREMIUM_STAGE_NAMES:
        assert name in by_name, (
            f"Stage '{name}' missing from skipped list; got {sorted(by_name)}"
        )
        assert by_name[name] == "premium_not_available", (
            f"Stage '{name}' expected 'premium_not_available', got '{by_name[name]}'"
        )


# ===========================================================================
# State A: public-only (premium package not importable)
# ===========================================================================


class TestStateA_PublicOnly:
    """Premium package blocked via ``sys.modules`` sentinel; all premium absent."""

    @pytest.fixture(autouse=True)
    def _setup_state_a(self):
        """Clear registry and block premium import before each test; restore after."""
        capability_registry.clear()
        # Setting sys.modules["saturnday_premium"] to None causes any
        # ``import saturnday_premium`` inside the test to raise ImportError.
        with patch.dict(sys.modules, {"saturnday_premium": None}):
            yield
        capability_registry.clear()

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_try_register_premium_returns_false(self) -> None:
        """try_register_premium() must return False when the package is absent."""
        from saturnday.premium_registration import try_register_premium

        result = try_register_premium()
        assert result is False

    def test_registry_empty_after_attempt(self) -> None:
        """Registry must remain empty after a failed premium registration attempt."""
        from saturnday.premium_registration import try_register_premium

        try_register_premium()
        assert capability_registry.registered_stages() == []

    def test_evidence_premium_not_installed(self) -> None:
        """build_capability_state() must report the package as absent."""
        state: dict[str, Any] = build_capability_state()
        assert state["premium_package_installed"] is False
        assert state["entitlement_valid"] is None
        assert state["entitlement_reason"] is None

    def test_evidence_entitlement_metadata_all_none(self) -> None:
        """All three licence metadata fields must be None when premium is absent."""
        state: dict[str, Any] = build_capability_state()
        assert state["entitlement_org"] is None, (
            f"entitlement_org must be None in State A, got {state['entitlement_org']!r}"
        )
        assert state["entitlement_edition"] is None, (
            f"entitlement_edition must be None in State A, got {state['entitlement_edition']!r}"
        )
        assert state["entitlement_expires"] is None, (
            f"entitlement_expires must be None in State A, got {state['entitlement_expires']!r}"
        )

    def test_evidence_premium_capabilities_disabled(self) -> None:
        """build_capability_state() must report premium_capabilities_enabled=False."""
        state: dict[str, Any] = build_capability_state()
        assert state["premium_capabilities_enabled"] is False

    def test_all_stages_skipped_not_available(self) -> None:
        """build_skipped_stages() must list all 8 premium stages as not available."""
        skipped = build_skipped_stages()
        _assert_all_stages_skipped_not_available(skipped)

    def test_public_modules_import_cleanly(self) -> None:
        """Key public modules must be importable without premium present."""
        import saturnday.ticket_runner  # noqa: F401
        import saturnday.document.document_runner  # noqa: F401
        import saturnday.interactive  # noqa: F401
        import saturnday.cli  # noqa: F401
        import saturnday.run.planner  # noqa: F401


# ===========================================================================
# State B: premium installed and entitled
# ===========================================================================


class TestStateB_PremiumEntitled:
    """Premium package installed with a valid test JWT; all 9 stages up."""

    #: All 8+1 stage names that register_premium_stages() must populate.
    _ALL_STAGES: frozenset[str] = frozenset(
        {
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
    )

    @pytest.fixture(autouse=True)
    def _setup_state_b(self) -> object:
        """Provide a valid test JWT, bootstrap premium, register stages; restore after."""
        import saturnday_premium
        import saturnday_premium._jwt as jwt_mod

        capability_registry.clear()
        jwt = _make_test_jwt()
        os.environ["SATURNDAY_LICENSE_KEY"] = jwt
        saturnday_premium._entitlement_state = None
        with patch.object(jwt_mod, "_VERIFY_KEY_B64", _TEST_VERIFY_KEY_B64):
            saturnday_premium._bootstrap()
            yield
        os.environ.pop("SATURNDAY_LICENSE_KEY", None)
        saturnday_premium._entitlement_state = None
        capability_registry.clear()

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_try_register_premium_returns_true(self) -> None:
        """A fresh try_register_premium() call must return True when entitled."""
        from saturnday.premium_registration import try_register_premium

        # Clear the registry so try_register_premium() has new stages to add.
        # The SATURNDAY_LICENSE_KEY env var is still set by the fixture, so
        # is_entitled() returns True and register_premium_stages() runs.
        capability_registry.clear()
        result = try_register_premium()
        assert result is True

    def test_all_9_stages_registered(self) -> None:
        """registered_stages() must contain exactly 9 entries."""
        stages = frozenset(capability_registry.registered_stages())
        assert stages == self._ALL_STAGES, (
            f"Missing: {self._ALL_STAGES - stages}; "
            f"Unexpected: {stages - self._ALL_STAGES}"
        )

    def test_evidence_premium_installed_and_entitled(self) -> None:
        """build_capability_state() must reflect installed + entitled state."""
        state: dict[str, Any] = build_capability_state()
        assert state["premium_package_installed"] is True
        assert state["entitlement_valid"] is True
        assert state["entitlement_reason"] == "valid"

    def test_evidence_entitlement_metadata_populated(self) -> None:
        """entitlement_org, entitlement_edition, entitlement_expires must be populated in State B."""
        state: dict[str, Any] = build_capability_state()
        assert state["entitlement_org"] == "test-org", (
            f"entitlement_org must be 'test-org' in State B, got {state['entitlement_org']!r}"
        )
        assert state["entitlement_edition"] == "premium", (
            f"entitlement_edition must be 'premium' in State B, got {state['entitlement_edition']!r}"
        )
        assert isinstance(state["entitlement_expires"], str) and state["entitlement_expires"], (
            f"entitlement_expires must be a non-empty string in State B, got {state['entitlement_expires']!r}"
        )

    def test_evidence_premium_capabilities_enabled(self) -> None:
        """build_capability_state() must report premium_capabilities_enabled=True."""
        state: dict[str, Any] = build_capability_state()
        assert state["premium_capabilities_enabled"] is True

    def test_entitlement_state_accessible(self) -> None:
        """saturnday_premium.entitlement_state() must return a valid EntitlementState."""
        import saturnday_premium

        estate = saturnday_premium.entitlement_state()
        assert estate.valid is True
        assert isinstance(estate.reason, str)
        assert estate.reason  # non-empty

    def test_is_entitled_returns_true(self) -> None:
        """saturnday_premium.is_entitled() must return True when entitled."""
        import saturnday_premium

        assert saturnday_premium.is_entitled() is True

    def test_protocol_conformance(self) -> None:
        """All 7 protocol-bound handlers must satisfy isinstance for their Protocol."""
        from saturnday.interfaces import (
            DocPostGlobalExt,
            EvidenceAppender,
            ImpactAnalysisExt,
            MemoryProvider,
            ReviewerExt,
            SpecVerifierExt,
            TriageHook,
        )
        from saturnday.interfaces.doc_stages import STAGE_NAME as _DPG
        from saturnday.interfaces.evidence import STAGE_NAME as _EA
        from saturnday.interfaces.impact import STAGE_NAME as _IA
        from saturnday.interfaces.memory import STAGE_NAME as _MEM
        from saturnday.interfaces.reviewer import STAGE_NAME as _CR
        from saturnday.interfaces.spec import STAGE_NAME as _SV
        from saturnday.interfaces.triage import STAGE_NAME as _ST

        protocol_stages: list[tuple[str, type]] = [
            (_ST, TriageHook),
            (_MEM, MemoryProvider),
            (_SV, SpecVerifierExt),
            (_IA, ImpactAnalysisExt),
            (_CR, ReviewerExt),
            (_DPG, DocPostGlobalExt),
            (_EA, EvidenceAppender),
        ]
        for stage_name, protocol in protocol_stages:
            handler = capability_registry.get(stage_name)
            assert handler is not None, f"No handler registered for {stage_name!r}"
            assert isinstance(handler, protocol), (  # type: ignore[arg-type]
                f"{stage_name!r}: {type(handler).__qualname__} does not satisfy "
                f"{protocol.__name__}"
            )


# ===========================================================================
# State C: premium installed but not entitled
# ===========================================================================


class TestStateC_PremiumNotEntitled:
    """Premium installed but entitlement patched to invalid; registry stays empty."""

    @pytest.fixture(autouse=True)
    def _setup_state_c(self):
        """Patch entitlement to invalid, clear registry, run bootstrap, restore after."""
        import saturnday_premium
        from saturnday_premium._entitlement import EntitlementState

        capability_registry.clear()
        # Capture original state so we can restore it precisely.
        original_state = saturnday_premium._entitlement_state

        # Patch the ``check_entitlement`` name in the saturnday_premium.__init__
        # module globals — that is the binding _bootstrap() calls directly.
        # Include org/edition/expires so we can assert that expired licences
        # still surface available metadata fields in evidence (ENT-010).
        with patch.object(
            saturnday_premium,
            "check_entitlement",
            return_value=EntitlementState(
                valid=False,
                reason="license_expired",
                org="expired-org",
                edition="premium",
                expires="2020-01-01T00:00:00+00:00",
            ),
        ):
            # Reset the cached state so _bootstrap() re-runs the check.
            saturnday_premium._entitlement_state = None
            saturnday_premium._bootstrap()
            yield

        # Teardown: restore entitlement state and clear registry.
        saturnday_premium._entitlement_state = original_state
        capability_registry.clear()

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_registry_empty_when_not_entitled(self) -> None:
        """Registry must be empty after bootstrap with invalid entitlement."""
        assert capability_registry.registered_stages() == []

    def test_evidence_premium_installed_not_entitled(self) -> None:
        """build_capability_state() must show installed + not entitled."""
        state: dict[str, Any] = build_capability_state()
        assert state["premium_package_installed"] is True
        assert state["entitlement_valid"] is False
        assert state["entitlement_reason"] == "license_expired"

    def test_evidence_entitlement_metadata_from_expired_state(self) -> None:
        """Expired-licence metadata (org, edition, expires) must still be surfaced in evidence."""
        state: dict[str, Any] = build_capability_state()
        assert state["entitlement_org"] == "expired-org", (
            f"entitlement_org must be 'expired-org' in State C, got {state['entitlement_org']!r}"
        )
        assert state["entitlement_edition"] == "premium", (
            f"entitlement_edition must be 'premium' in State C, got {state['entitlement_edition']!r}"
        )
        assert isinstance(state["entitlement_expires"], str) and state["entitlement_expires"], (
            f"entitlement_expires must be a non-empty string in State C, got {state['entitlement_expires']!r}"
        )

    def test_evidence_premium_capabilities_disabled(self) -> None:
        """build_capability_state() must report premium_capabilities_enabled=False."""
        state: dict[str, Any] = build_capability_state()
        assert state["premium_capabilities_enabled"] is False

    def test_all_stages_skipped(self) -> None:
        """build_skipped_stages() must list all 8 stages as premium_not_available."""
        skipped = build_skipped_stages()
        _assert_all_stages_skipped_not_available(skipped)

    def test_public_still_works(self) -> None:
        """Public modules must remain importable and functional when not entitled."""
        from saturnday import capability_registry as cr
        from saturnday.shared.evidence_schema import build_capability_state as bcs

        # Verify the public registry API works normally.
        assert cr.registered_stages() == []
        assert cr.is_available("security_triage") is False

        # Evidence schema must return a valid dict.
        state = bcs()
        assert isinstance(state, dict)
        assert "premium_capabilities_enabled" in state
        assert state["premium_capabilities_enabled"] is False
