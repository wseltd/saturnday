"""Tests for saturnday.interfaces.doc_stages -- DocPostGlobalExt protocol.

Coverage:
- SPLIT-008-1: positive protocol conformance (isinstance check passes)
- SPLIT-008-2: negative protocol conformance (isinstance check fails)
- SPLIT-008-3: STAGE_NAME constant equals "doc_post_global"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.interfaces.doc_stages import STAGE_NAME, DocPostGlobalExt


# ---------------------------------------------------------------------------
# Conforming implementation
# ---------------------------------------------------------------------------


class _GoodDocPostGlobal:
    """Minimal class that satisfies all four DocPostGlobalExt methods."""

    def extract_claims(
        self,
        sections_content: dict[str, str],
        spec: Any,
        repo_path: Path,
        coder_config: Any,
    ) -> list[dict]:
        return []

    def verify_claims(
        self,
        claims: list[dict],
        source_contents: dict[str, str],
        coder_config: Any,
        repo_path: Path,
    ) -> list[dict]:
        return []

    def evaluate_publishability(
        self,
        section_results: list[dict],
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        return True, []

    def check_signoff(
        self,
        document_id: str,
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        return True, []


# ---------------------------------------------------------------------------
# Non-conforming implementations
# ---------------------------------------------------------------------------


class _MissingExtractClaims:
    """Has verify_claims, evaluate_publishability, check_signoff but not extract_claims."""

    def verify_claims(
        self,
        claims: list[dict],
        source_contents: dict[str, str],
        coder_config: Any,
        repo_path: Path,
    ) -> list[dict]:
        return []  # pragma: no cover

    def evaluate_publishability(
        self,
        section_results: list[dict],
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        return True, []  # pragma: no cover

    def check_signoff(
        self,
        document_id: str,
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        return True, []  # pragma: no cover


class _MissingAllMethods:
    """Has no protocol methods at all."""

    def unrelated(self) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDocPostGlobalExtPositiveConformance:
    """A class implementing all four methods satisfies the protocol."""

    def test_isinstance_passes_for_conforming_class(self) -> None:
        """isinstance returns True for a class that implements all four methods."""
        impl = _GoodDocPostGlobal()
        assert isinstance(impl, DocPostGlobalExt), (
            "Expected _GoodDocPostGlobal to satisfy DocPostGlobalExt protocol"
        )

    def test_extract_claims_callable_and_returns_list(self) -> None:
        """extract_claims can be called and returns a list."""
        impl = _GoodDocPostGlobal()
        result = impl.extract_claims(
            {"intro": "Some text."},
            object(),
            Path("/tmp/repo"),
            object(),
        )
        assert isinstance(result, list)

    def test_verify_claims_callable_and_returns_list(self) -> None:
        """verify_claims can be called and returns a list."""
        impl = _GoodDocPostGlobal()
        result = impl.verify_claims(
            [{"claim_id": "c1", "text": "The sky is blue.", "section": "intro"}],
            {"source.py": "# sky is blue"},
            object(),
            Path("/tmp/repo"),
        )
        assert isinstance(result, list)

    def test_evaluate_publishability_callable_and_returns_tuple(self) -> None:
        """evaluate_publishability returns (bool, list[str])."""
        impl = _GoodDocPostGlobal()
        publishable, reasons = impl.evaluate_publishability(
            [{"section": "intro", "passed": True}],
            object(),
            [],
        )
        assert isinstance(publishable, bool)
        assert isinstance(reasons, list)

    def test_check_signoff_callable_and_returns_tuple(self) -> None:
        """check_signoff returns (bool, list[str])."""
        impl = _GoodDocPostGlobal()
        satisfied, missing = impl.check_signoff("doc-001", object(), [])
        assert isinstance(satisfied, bool)
        assert isinstance(missing, list)


class TestDocPostGlobalExtNegativeConformance:
    """Classes missing any required method do NOT satisfy the protocol."""

    def test_isinstance_fails_for_missing_extract_claims(self) -> None:
        """isinstance returns False when extract_claims is absent."""
        not_impl = _MissingExtractClaims()
        assert not isinstance(not_impl, DocPostGlobalExt), (
            "Expected _MissingExtractClaims to fail DocPostGlobalExt protocol check"
        )

    def test_isinstance_fails_for_class_with_no_methods(self) -> None:
        """isinstance returns False for a class with no protocol methods."""
        assert not isinstance(_MissingAllMethods(), DocPostGlobalExt)

    def test_isinstance_fails_for_bare_object(self) -> None:
        """isinstance returns False for a bare object()."""
        assert not isinstance(object(), DocPostGlobalExt)

    def test_isinstance_fails_for_none(self) -> None:
        """isinstance returns False for None."""
        assert not isinstance(None, DocPostGlobalExt)


class TestStageNameConstant:
    """STAGE_NAME must equal the canonical registry key for doc_post_global."""

    def test_stage_name_value(self) -> None:
        assert STAGE_NAME == "doc_post_global"

    def test_stage_name_is_string(self) -> None:
        assert isinstance(STAGE_NAME, str)
