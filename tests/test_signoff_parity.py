"""Test that public and premium signoff completeness logic agree.

The public package has a read-only mirror of premium signoff
completeness semantics. This test proves they produce identical
results on the same data.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from saturnday.release_signoff_list import check_signoff_completeness


def _write_signoff(evidence_dir: Path, approver: str) -> None:
    sig_dir = evidence_dir / "release" / "signoffs"
    sig_dir.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    record = {
        "signoff_id": sid,
        "artefact_sha256": "ab" * 32,
        "approver": approver,
        "approved_at": datetime.now(tz=timezone.utc).isoformat(),
        "notes": "",
    }
    (sig_dir / f"{sid}.json").write_text(json.dumps(record), encoding="utf-8")


class TestPublicPremiumParity:
    """Both paths must agree on the same evidence directory."""

    def _check_premium(self, evidence_dir: Path, minimum: int) -> tuple[bool, str]:
        try:
            from saturnday_premium._release_signoff import check_signoff_requirement
        except ImportError:
            pytest.skip("saturnday-premium not installed")
        return check_signoff_requirement(evidence_dir, minimum)

    def test_both_agree_met(self, tmp_path: Path) -> None:
        _write_signoff(tmp_path, "Alice")
        _write_signoff(tmp_path, "Bob")

        pub_ok, _ = check_signoff_completeness(tmp_path, 2)
        pre_ok, _ = self._check_premium(tmp_path, 2)
        assert pub_ok is True
        assert pre_ok is True

    def test_both_agree_not_met(self, tmp_path: Path) -> None:
        _write_signoff(tmp_path, "Alice")

        pub_ok, _ = check_signoff_completeness(tmp_path, 2)
        pre_ok, _ = self._check_premium(tmp_path, 2)
        assert pub_ok is False
        assert pre_ok is False

    def test_both_agree_duplicate_approver(self, tmp_path: Path) -> None:
        _write_signoff(tmp_path, "Alice")
        _write_signoff(tmp_path, "Alice")

        pub_ok, _ = check_signoff_completeness(tmp_path, 2)
        pre_ok, _ = self._check_premium(tmp_path, 2)
        assert pub_ok is False
        assert pre_ok is False

    def test_both_agree_empty(self, tmp_path: Path) -> None:
        pub_ok, _ = check_signoff_completeness(tmp_path, 2)
        pre_ok, _ = self._check_premium(tmp_path, 2)
        assert pub_ok is False
        assert pre_ok is False

    def test_both_agree_threshold_one(self, tmp_path: Path) -> None:
        _write_signoff(tmp_path, "Alice")

        pub_ok, _ = check_signoff_completeness(tmp_path, 1)
        pre_ok, _ = self._check_premium(tmp_path, 1)
        assert pub_ok is True
        assert pre_ok is True
