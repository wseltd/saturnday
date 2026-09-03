"""Tests for SPLIT-009: EvidenceAppender protocol and STAGE_NAME constant."""
from __future__ import annotations

import pytest

from saturnday.interfaces.evidence import EvidenceAppender, STAGE_NAME


class _ConformingAppender:
    """Minimal class that satisfies the EvidenceAppender protocol."""

    def append(self, base_pack: dict, artifacts: dict) -> dict:
        merged = dict(base_pack)
        for key, value in artifacts.items():
            if key not in base_pack:
                merged[key] = value
        return merged


class _NonConformingAppender:
    """Class that does NOT implement the required ``append`` method."""

    def merge(self, base: dict, extra: dict) -> dict:
        return {**base, **extra}


class _WrongSignatureAppender:
    """Class whose ``append`` method is missing required parameters."""

    def append(self, base_pack: dict) -> dict:  # missing artifacts param
        return base_pack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_positive_conformance() -> None:
    """A class that implements append() with the correct signature is an EvidenceAppender."""
    appender = _ConformingAppender()
    assert isinstance(appender, EvidenceAppender), (
        "A class with the correct append() signature must satisfy the "
        "EvidenceAppender runtime_checkable protocol."
    )


def test_negative_conformance_missing_method() -> None:
    """A class without an append() method is NOT an EvidenceAppender."""
    not_appender = _NonConformingAppender()
    assert not isinstance(not_appender, EvidenceAppender), (
        "A class that lacks append() must not satisfy the EvidenceAppender protocol."
    )


def test_stage_name_constant() -> None:
    """STAGE_NAME must equal 'evidence_appender' -- this is the registry key."""
    assert STAGE_NAME == "evidence_appender"


def test_conforming_appender_additive_merge() -> None:
    """Conforming appender adds premium keys without overwriting base keys."""
    appender = _ConformingAppender()
    base = {"status": "PASSED", "governance_version": "1.0"}
    artifacts = {"triage_rationale": "no high sev", "status": "PREMIUM_OVERRIDE"}
    result = appender.append(base, artifacts)

    # Base keys must be present and unchanged.
    assert result["status"] == "PASSED", "Base key 'status' must not be overwritten."
    assert result["governance_version"] == "1.0"

    # Premium-only key is added.
    assert result["triage_rationale"] == "no high sev"
