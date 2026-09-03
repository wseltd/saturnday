"""Tests for SPLIT-007: ReviewerExt protocol and STAGE_NAME constant."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.interfaces.reviewer import ReviewerExt, STAGE_NAME


class _ConformingReviewer:
    """Minimal class that satisfies the ReviewerExt protocol."""

    def review(
        self,
        task: str,
        coder_config: Any,
        repo_path: Path,
    ) -> dict:
        return {"success": True, "output": "LGTM", "has_concerns": False}


class _NonConformingReviewer:
    """Class that does NOT implement the required ``review`` method."""

    def scan(self, task: str) -> dict:
        return {}


class _WrongSignatureReviewer:
    """Class whose ``review`` method has a wrong (missing) required parameter."""

    def review(self, task: str) -> dict:  # missing coder_config and repo_path
        return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_positive_conformance() -> None:
    """A class that implements review() with the correct signature is a ReviewerExt."""
    reviewer = _ConformingReviewer()
    assert isinstance(reviewer, ReviewerExt), (
        "A class with the correct review() signature must satisfy the "
        "ReviewerExt runtime_checkable protocol."
    )


def test_negative_conformance_missing_method() -> None:
    """A class without a review() method is NOT a ReviewerExt."""
    not_reviewer = _NonConformingReviewer()
    assert not isinstance(not_reviewer, ReviewerExt), (
        "A class that lacks review() must not satisfy the ReviewerExt protocol."
    )


def test_stage_name_constant() -> None:
    """STAGE_NAME must equal 'code_reviewer' -- this is the registry key."""
    assert STAGE_NAME == "code_reviewer"
