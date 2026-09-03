"""Tests for the MemoryProvider protocol (SPLIT-004).

Covers:
- Positive conformance: a class implementing all three methods satisfies
  isinstance() via runtime_checkable.
- Negative conformance: a class missing a method does NOT satisfy isinstance().
- STAGE_NAME constant value.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.interfaces.memory import STAGE_NAME, MemoryProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConformingProvider:
    """A minimal class that satisfies the MemoryProvider protocol."""

    def retrieve(
        self,
        conn: Any,
        ticket: Any,
        repo_path: Path,
        top_k: int = 5,
    ) -> list[dict]:
        return []

    def enforce(
        self,
        conn: Any,
        changed_files: list[str],
        repo_path: Path,
    ) -> list[dict]:
        return []

    def cleanup(
        self,
        conn: Any,
        repo_path: Path,
    ) -> int:
        return 0


class _MissingEnforce:
    """A class missing the ``enforce`` method -- does NOT conform."""

    def retrieve(
        self,
        conn: Any,
        ticket: Any,
        repo_path: Path,
        top_k: int = 5,
    ) -> list[dict]:
        return []

    def cleanup(
        self,
        conn: Any,
        repo_path: Path,
    ) -> int:
        return 0


class _MissingCleanup:
    """A class missing the ``cleanup`` method -- does NOT conform."""

    def retrieve(
        self,
        conn: Any,
        ticket: Any,
        repo_path: Path,
        top_k: int = 5,
    ) -> list[dict]:
        return []

    def enforce(
        self,
        conn: Any,
        changed_files: list[str],
        repo_path: Path,
    ) -> list[dict]:
        return []


class _EmptyClass:
    """A class with no relevant methods -- does NOT conform."""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_positive_conformance() -> None:
    """A class implementing all three methods satisfies isinstance."""
    provider = _ConformingProvider()
    assert isinstance(provider, MemoryProvider)


def test_negative_conformance_missing_enforce() -> None:
    """A class missing ``enforce`` does not satisfy isinstance."""
    obj = _MissingEnforce()
    assert not isinstance(obj, MemoryProvider)


def test_negative_conformance_missing_cleanup() -> None:
    """A class missing ``cleanup`` does not satisfy isinstance."""
    obj = _MissingCleanup()
    assert not isinstance(obj, MemoryProvider)


def test_negative_conformance_empty_class() -> None:
    """A class with no protocol methods does not satisfy isinstance."""
    obj = _EmptyClass()
    assert not isinstance(obj, MemoryProvider)


def test_stage_name_constant() -> None:
    """STAGE_NAME must equal the canonical registry key for memory."""
    assert STAGE_NAME == "memory_provider"


def test_module_importable_without_premium() -> None:
    """The protocol module must be importable with no premium packages present.

    This test acts as a smoke-check: if any premium module is accidentally
    imported at module load time, this test will fail with an ImportError in
    environments where premium is not installed.
    """
    import importlib

    module = importlib.import_module("saturnday.interfaces.memory")
    assert hasattr(module, "MemoryProvider")
    assert hasattr(module, "STAGE_NAME")


def test_stage_name_in_all() -> None:
    """Both public names must be listed in __all__."""
    import saturnday.interfaces.memory as m

    assert "MemoryProvider" in m.__all__
    assert "STAGE_NAME" in m.__all__
