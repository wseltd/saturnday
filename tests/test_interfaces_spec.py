"""Tests for saturnday.interfaces.spec -- SpecVerifierExt protocol."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.interfaces.spec import STAGE_NAME, SpecVerifierExt


# ---------------------------------------------------------------------------
# Minimal conforming implementation
# ---------------------------------------------------------------------------


class _ConformingVerifier:
    """Minimal class that satisfies the SpecVerifierExt protocol."""

    def generate_assertions(self, ticket: Any, repo_path: Path) -> list[dict]:
        return []

    def run_assertions(
        self, assertions: list[dict], repo_path: Path
    ) -> list[dict]:
        return []

    def run_property_tests(
        self, changed_files: list[str], repo_path: Path
    ) -> list[dict]:
        return []

    def check_dataflow(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> list[dict]:
        return []


# ---------------------------------------------------------------------------
# Non-conforming implementations
# ---------------------------------------------------------------------------


class _MissingOneMethod:
    """Missing check_dataflow -- must NOT satisfy the protocol."""

    def generate_assertions(self, ticket: Any, repo_path: Path) -> list[dict]:
        return []

    def run_assertions(
        self, assertions: list[dict], repo_path: Path
    ) -> list[dict]:
        return []

    def run_property_tests(
        self, changed_files: list[str], repo_path: Path
    ) -> list[dict]:
        return []


class _EmptyClass:
    """No methods at all -- must NOT satisfy the protocol."""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpecVerifierExtConformance:
    """Positive and negative runtime_checkable conformance tests."""

    def test_conforming_class_satisfies_protocol(self) -> None:
        """A class with all four correct methods passes isinstance."""
        verifier = _ConformingVerifier()
        assert isinstance(verifier, SpecVerifierExt)

    def test_missing_method_does_not_satisfy_protocol(self) -> None:
        """A class missing check_dataflow fails isinstance."""
        obj = _MissingOneMethod()
        assert not isinstance(obj, SpecVerifierExt)

    def test_empty_class_does_not_satisfy_protocol(self) -> None:
        """A class with no methods fails isinstance."""
        obj = _EmptyClass()
        assert not isinstance(obj, SpecVerifierExt)

    def test_non_object_does_not_satisfy_protocol(self) -> None:
        """A plain dict does not satisfy the protocol."""
        assert not isinstance({}, SpecVerifierExt)


class TestStageNameConstant:
    """STAGE_NAME must equal the canonical registry key."""

    def test_stage_name_value(self) -> None:
        assert STAGE_NAME == "spec_verifier"

    def test_stage_name_is_str(self) -> None:
        assert isinstance(STAGE_NAME, str)


class TestMethodSignaturesAcceptable:
    """Smoke-test that conforming implementation methods are callable."""

    def setup_method(self) -> None:
        self.verifier = _ConformingVerifier()
        self.repo = Path("/tmp")

    def test_generate_assertions_callable(self) -> None:
        result = self.verifier.generate_assertions(object(), self.repo)
        assert isinstance(result, list)

    def test_run_assertions_callable(self) -> None:
        result = self.verifier.run_assertions([], self.repo)
        assert isinstance(result, list)

    def test_run_property_tests_callable(self) -> None:
        result = self.verifier.run_property_tests([], self.repo)
        assert isinstance(result, list)

    def test_check_dataflow_callable(self) -> None:
        result = self.verifier.check_dataflow([], self.repo, None)
        assert isinstance(result, list)
