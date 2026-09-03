"""Tests for saturnday.interfaces.impact (SPLIT-006).

Covers:
- STAGE_NAME constant value.
- Positive protocol conformance: a class with matching method signatures
  satisfies the runtime_checkable isinstance check.
- Negative protocol conformance: a class missing a required method does NOT
  satisfy the check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.interfaces.impact import ImpactAnalysisExt, STAGE_NAME


# ---------------------------------------------------------------------------
# Helpers: conforming and non-conforming implementations
# ---------------------------------------------------------------------------


class _ConformingImpl:
    """Minimal class that satisfies the ImpactAnalysisExt protocol."""

    def compute_impact(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> dict:
        return {"total_blast_radius": 0, "touched_files": changed_files}

    def select_verification_layers(
        self,
        impact: dict,
        ticket: Any,
    ) -> dict[str, bool]:
        return {"contract": True, "security": True}


class _MissingSelectImpl:
    """Class that only implements compute_impact -- missing select_verification_layers."""

    def compute_impact(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> dict:
        return {}


class _EmptyImpl:
    """Class with no protocol methods."""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stage_name_constant() -> None:
    """STAGE_NAME must equal 'impact_analysis' (matches capability registry key)."""
    assert STAGE_NAME == "impact_analysis"


def test_positive_conformance() -> None:
    """A class implementing both methods satisfies the runtime_checkable protocol."""
    impl = _ConformingImpl()
    assert isinstance(impl, ImpactAnalysisExt), (
        "_ConformingImpl should satisfy ImpactAnalysisExt"
    )


def test_negative_conformance_missing_method() -> None:
    """A class missing select_verification_layers does NOT satisfy the protocol."""
    impl = _MissingSelectImpl()
    assert not isinstance(impl, ImpactAnalysisExt), (
        "_MissingSelectImpl should NOT satisfy ImpactAnalysisExt"
    )


def test_negative_conformance_empty_class() -> None:
    """A class with no protocol methods does NOT satisfy the protocol."""
    impl = _EmptyImpl()
    assert not isinstance(impl, ImpactAnalysisExt), (
        "_EmptyImpl should NOT satisfy ImpactAnalysisExt"
    )


def test_conforming_impl_returns_correct_shapes() -> None:
    """Smoke-test that the conforming implementation returns the expected types."""
    impl = _ConformingImpl()

    impact = impl.compute_impact(["src/foo.py"], Path("/repo"), None)
    assert isinstance(impact, dict)
    assert "total_blast_radius" in impact

    layers = impl.select_verification_layers(impact, object())
    assert isinstance(layers, dict)
    assert all(isinstance(v, bool) for v in layers.values())
