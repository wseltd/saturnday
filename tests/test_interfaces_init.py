"""Tests for saturnday.interfaces -- package-level re-export surface.

Coverage:
- SPLIT-010-1: all 8 protocols importable directly from ``saturnday.interfaces``
- SPLIT-010-2: all 8 names listed in ``__all__``
- SPLIT-010-3: each imported name is a runtime-checkable Protocol (has
  ``__protocol_attrs__`` or passes the ``typing.Protocol`` identity check)
"""
from __future__ import annotations

from typing import Protocol

import pytest

import saturnday.interfaces as interfaces_pkg
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXPECTED_NAMES: list[str] = [
    "TriageHook",
    "MemoryProvider",
    "SpecVerifierExt",
    "ImpactAnalysisExt",
    "ReviewerExt",
    "DocPostGlobalExt",
    "EvidenceAppender",
    "ReleaseGovernanceExt",
]

_PROTOCOL_OBJECTS: list[tuple[str, type]] = [
    ("TriageHook", TriageHook),
    ("MemoryProvider", MemoryProvider),
    ("SpecVerifierExt", SpecVerifierExt),
    ("ImpactAnalysisExt", ImpactAnalysisExt),
    ("ReviewerExt", ReviewerExt),
    ("DocPostGlobalExt", DocPostGlobalExt),
    ("EvidenceAppender", EvidenceAppender),
    ("ReleaseGovernanceExt", ReleaseGovernanceExt),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInterfacesImport:
    """All 8 protocols must be importable directly from saturnday.interfaces."""

    def test_all_eight_names_importable(self) -> None:
        """Each protocol class is accessible as a top-level attribute of the package."""
        for name, obj in _PROTOCOL_OBJECTS:
            assert obj is not None, f"{name} resolved to None"
            assert callable(obj), f"{name} is not callable (expected a class)"

    def test_all_names_in_dunder_all(self) -> None:
        """__all__ must contain exactly the 8 canonical protocol names."""
        for name in _EXPECTED_NAMES:
            assert name in interfaces_pkg.__all__, (
                f"'{name}' missing from saturnday.interfaces.__all__"
            )

    def test_dunder_all_has_no_extra_names(self) -> None:
        """__all__ must not contain names beyond the 8 defined protocols."""
        extra = set(interfaces_pkg.__all__) - set(_EXPECTED_NAMES)
        assert not extra, f"Unexpected names in __all__: {extra}"

    @pytest.mark.parametrize("name,proto_cls", _PROTOCOL_OBJECTS)
    def test_each_is_a_protocol_subclass(self, name: str, proto_cls: type) -> None:
        """Each exported class must be a typing.Protocol (has __protocol_attrs__)."""
        assert hasattr(proto_cls, "__protocol_attrs__"), (
            f"{name} lacks __protocol_attrs__ -- is it a @runtime_checkable Protocol?"
        )

    @pytest.mark.parametrize("name,proto_cls", _PROTOCOL_OBJECTS)
    def test_each_is_runtime_checkable(self, name: str, proto_cls: type) -> None:
        """Each protocol must be decorated with @runtime_checkable so callers
        can use isinstance() checks at the capability-registry boundary."""
        # runtime_checkable protocols set _is_runtime_protocol = True on the class
        assert getattr(proto_cls, "_is_runtime_protocol", False), (
            f"{name} is not @runtime_checkable"
        )
