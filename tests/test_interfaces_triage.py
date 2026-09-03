"""Tests for saturnday.interfaces.triage -- TriageHook protocol.

Coverage:
- SPLIT-003-1: positive protocol conformance (isinstance check passes)
- SPLIT-003-2: negative protocol conformance (isinstance check fails)
- SPLIT-003-3: STAGE_NAME constant equals "security_triage"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.interfaces.triage import STAGE_NAME, TriageHook


# ---------------------------------------------------------------------------
# Conforming implementation
# ---------------------------------------------------------------------------


class _GoodTriage:
    """Minimal class that satisfies the TriageHook protocol."""

    def filter_findings(
        self,
        findings: list[dict],
        repo_path: Path,
        coder_config: Any,
    ) -> list[dict]:
        return findings


# ---------------------------------------------------------------------------
# Non-conforming implementations
# ---------------------------------------------------------------------------


class _WrongReturnType:
    """filter_findings returns wrong type -- should still be detected at runtime
    because runtime_checkable only checks method presence, not full signature."""

    def filter_findings(
        self,
        findings: list[dict],
        repo_path: Path,
        coder_config: Any,
    ) -> str:  # wrong return type annotation
        return ""  # pragma: no cover


class _MissingMethod:
    """Has no filter_findings method at all."""

    def unrelated(self) -> None:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTriageHookPositiveConformance:
    """A class with the correct method signature satisfies the protocol."""

    def test_isinstance_passes_for_conforming_class(self) -> None:
        """isinstance returns True for a class that implements filter_findings."""
        hook = _GoodTriage()
        assert isinstance(hook, TriageHook), (
            "Expected _GoodTriage to satisfy TriageHook protocol"
        )

    def test_conforming_instance_is_callable(self) -> None:
        """Conforming instance can be called and returns expected shape."""
        hook = _GoodTriage()
        findings = [{"rule_id": "SEC-001", "severity": "HIGH"}]
        result = hook.filter_findings(findings, Path("/tmp/repo"), object())
        assert result == findings


class TestTriageHookNegativeConformance:
    """A class without filter_findings does NOT satisfy the protocol."""

    def test_isinstance_fails_for_missing_method(self) -> None:
        """isinstance returns False for a class missing filter_findings."""
        not_a_hook = _MissingMethod()
        assert not isinstance(not_a_hook, TriageHook), (
            "Expected _MissingMethod to fail TriageHook protocol check"
        )

    def test_isinstance_fails_for_plain_object(self) -> None:
        """isinstance returns False for a bare object instance."""
        assert not isinstance(object(), TriageHook)

    def test_isinstance_fails_for_none(self) -> None:
        """isinstance returns False for None."""
        assert not isinstance(None, TriageHook)


class TestStageNameConstant:
    """STAGE_NAME must equal the canonical registry key for security triage."""

    def test_stage_name_value(self) -> None:
        assert STAGE_NAME == "security_triage"

    def test_stage_name_is_string(self) -> None:
        assert isinstance(STAGE_NAME, str)
