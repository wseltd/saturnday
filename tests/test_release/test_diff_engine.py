"""Tests for the release diff engine (RS-011).

Covers:
- Identical inventories → no changes in any category
- Added file (in candidate, not in baseline) → listed in added_files
- Removed file (in baseline, not in candidate) → listed in removed_files
- Changed file (same path, different hash/size) → listed in changed_files
- Unchanged file (same path, same hash/size) → counted but NOT in changed_files
- Suspicious addition (.env file) → flagged in suspicious_additions
- Suspicious addition (*.key file) → flagged
- Suspicious addition (*.pem file) → flagged
- Suspicious addition (*.map file) → flagged
- Suspicious addition (*.sh executable) → flagged
- Suspicious addition (*.exe executable) → flagged
- Suspicious addition (very large file >1MiB) → flagged
- Clean addition (normal .py file) → not flagged
- Size delta calculated correctly
- Cross-type comparison raises ValueError
- Empty inventories → no diff
"""
from __future__ import annotations

import hashlib

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.diff_engine import (
    ChangedFile,
    ReleaseDiff,
    compute_release_diff,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file(path: str, content: bytes) -> ArtefactFile:
    return ArtefactFile(path=path, size=len(content), sha256=_sha(content))


def _inventory(files: list[ArtefactFile], artefact_type: str = "wheel") -> ArtefactInventory:
    return ArtefactInventory(
        artefact_type=artefact_type,
        artefact_path=f"/tmp/fake.{artefact_type}",
        artefact_sha256=_sha(b"fake"),
        files=files,
    )


# ---------------------------------------------------------------------------
# Core diff logic
# ---------------------------------------------------------------------------


class TestIdenticalInventories:
    """Identical baseline and candidate → no changes of any kind."""

    def test_no_added_files(self) -> None:
        files = [_file("pkg/__init__.py", b"# init"), _file("pkg/mod.py", b"# mod")]
        inv = _inventory(files)
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.added_files == []

    def test_no_removed_files(self) -> None:
        files = [_file("pkg/__init__.py", b"# init")]
        inv = _inventory(files)
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.removed_files == []

    def test_no_changed_files(self) -> None:
        files = [_file("pkg/mod.py", b"x = 1")]
        inv = _inventory(files)
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.changed_files == []

    def test_no_suspicious_additions(self) -> None:
        files = [_file("pkg/mod.py", b"x = 1")]
        inv = _inventory(files)
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.suspicious_additions == []

    def test_size_delta_is_zero(self) -> None:
        files = [_file("pkg/a.py", b"hello"), _file("pkg/b.py", b"world")]
        inv = _inventory(files)
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.size_delta == 0

    def test_empty_inventories(self) -> None:
        inv = _inventory([])
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.added_files == []
        assert diff.removed_files == []
        assert diff.changed_files == []
        assert diff.size_delta == 0


class TestAddedFiles:
    """Files present in candidate but absent from baseline."""

    def test_single_addition_listed(self) -> None:
        baseline = _inventory([_file("pkg/old.py", b"old")])
        candidate = _inventory([_file("pkg/old.py", b"old"), _file("pkg/new.py", b"new")])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert "pkg/new.py" in diff.added_files

    def test_baseline_file_absent_from_added(self) -> None:
        baseline = _inventory([_file("pkg/old.py", b"old")])
        candidate = _inventory([_file("pkg/old.py", b"old"), _file("pkg/new.py", b"new")])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert "pkg/old.py" not in diff.added_files

    def test_multiple_additions_all_listed(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([
            _file("pkg/a.py", b"a"),
            _file("pkg/b.py", b"b"),
            _file("pkg/c.py", b"c"),
        ])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert set(diff.added_files) == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}

    def test_added_files_are_sorted(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([
            _file("pkg/z.py", b"z"),
            _file("pkg/a.py", b"a"),
            _file("pkg/m.py", b"m"),
        ])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.added_files == sorted(diff.added_files)


class TestRemovedFiles:
    """Files present in baseline but absent from candidate."""

    def test_single_removal_listed(self) -> None:
        baseline = _inventory([_file("pkg/old.py", b"old"), _file("pkg/gone.py", b"gone")])
        candidate = _inventory([_file("pkg/old.py", b"old")])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert "pkg/gone.py" in diff.removed_files

    def test_present_file_not_in_removed(self) -> None:
        baseline = _inventory([_file("pkg/old.py", b"old"), _file("pkg/gone.py", b"gone")])
        candidate = _inventory([_file("pkg/old.py", b"old")])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert "pkg/old.py" not in diff.removed_files

    def test_multiple_removals_all_listed(self) -> None:
        baseline = _inventory([
            _file("pkg/a.py", b"a"),
            _file("pkg/b.py", b"b"),
            _file("pkg/c.py", b"c"),
        ])
        candidate = _inventory([])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert set(diff.removed_files) == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}

    def test_removed_files_are_sorted(self) -> None:
        baseline = _inventory([
            _file("pkg/z.py", b"z"),
            _file("pkg/a.py", b"a"),
        ])
        candidate = _inventory([])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.removed_files == sorted(diff.removed_files)


class TestChangedFiles:
    """Same path but different hash or size."""

    def test_changed_hash_detected(self) -> None:
        baseline = _inventory([_file("pkg/mod.py", b"version 1")])
        candidate = _inventory([_file("pkg/mod.py", b"version 2")])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert len(diff.changed_files) == 1
        changed = diff.changed_files[0]
        assert changed.path == "pkg/mod.py"
        assert changed.old_sha256 == _sha(b"version 1")
        assert changed.new_sha256 == _sha(b"version 2")

    def test_changed_size_detected(self) -> None:
        # Construct two files with same hash but different size (edge case:
        # size mismatch without hash mismatch is theoretically impossible, but
        # the engine checks both independently for defence in depth).
        f_baseline = ArtefactFile(path="pkg/mod.py", size=10, sha256=_sha(b"x"))
        f_candidate = ArtefactFile(path="pkg/mod.py", size=20, sha256=_sha(b"x"))
        baseline = _inventory([f_baseline])
        candidate = _inventory([f_candidate])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert len(diff.changed_files) == 1
        assert diff.changed_files[0].old_size == 10
        assert diff.changed_files[0].new_size == 20

    def test_unchanged_file_not_in_changed(self) -> None:
        content = b"identical"
        baseline = _inventory([_file("pkg/mod.py", content)])
        candidate = _inventory([_file("pkg/mod.py", content)])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.changed_files == []

    def test_changed_file_old_new_hashes_correct(self) -> None:
        old_content = b"old version"
        new_content = b"new version"
        baseline = _inventory([_file("pkg/mod.py", old_content)])
        candidate = _inventory([_file("pkg/mod.py", new_content)])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.changed_files[0].old_sha256 == _sha(old_content)
        assert diff.changed_files[0].new_sha256 == _sha(new_content)

    def test_multiple_changed_files(self) -> None:
        baseline = _inventory([
            _file("pkg/a.py", b"a old"),
            _file("pkg/b.py", b"b old"),
        ])
        candidate = _inventory([
            _file("pkg/a.py", b"a new"),
            _file("pkg/b.py", b"b new"),
        ])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert len(diff.changed_files) == 2


# ---------------------------------------------------------------------------
# Size delta
# ---------------------------------------------------------------------------


class TestSizeDelta:
    """Total byte size change between candidate and baseline."""

    def test_positive_delta_when_files_added(self) -> None:
        baseline = _inventory([_file("pkg/a.py", b"x" * 100)])
        candidate = _inventory([
            _file("pkg/a.py", b"x" * 100),
            _file("pkg/b.py", b"y" * 200),
        ])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.size_delta == 200

    def test_negative_delta_when_files_removed(self) -> None:
        baseline = _inventory([
            _file("pkg/a.py", b"x" * 100),
            _file("pkg/b.py", b"y" * 50),
        ])
        candidate = _inventory([_file("pkg/a.py", b"x" * 100)])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.size_delta == -50

    def test_zero_delta_identical(self) -> None:
        content = b"z" * 42
        inv = _inventory([_file("pkg/mod.py", content)])
        diff = compute_release_diff(baseline=inv, candidate=inv)
        assert diff.size_delta == 0

    def test_delta_accounts_for_changed_file_size(self) -> None:
        baseline = _inventory([_file("pkg/mod.py", b"x" * 100)])
        # Same path, bigger content → size_delta = 200 - 100 = 100
        candidate = _inventory([_file("pkg/mod.py", b"x" * 200)])
        diff = compute_release_diff(baseline=baseline, candidate=candidate)
        assert diff.size_delta == 100


# ---------------------------------------------------------------------------
# Suspicious additions
# ---------------------------------------------------------------------------


class TestSuspiciousAdditions:
    """.env, *.key, *.pem, *.map, executables, large files → flagged."""

    def _diff_with_single_addition(self, path: str, content: bytes = b"secret") -> ReleaseDiff:
        baseline = _inventory([])
        candidate = _inventory([_file(path, content)])
        return compute_release_diff(baseline=baseline, candidate=candidate)

    def test_env_file_flagged(self) -> None:
        diff = self._diff_with_single_addition(".env", b"SECRET=abc123")
        assert len(diff.suspicious_additions) >= 1
        assert any(s["path"] == ".env" for s in diff.suspicious_additions)

    def test_env_nested_file_flagged(self) -> None:
        diff = self._diff_with_single_addition("config/.env", b"DB_PASS=secret")
        assert any(s["path"] == "config/.env" for s in diff.suspicious_additions)

    def test_key_extension_flagged(self) -> None:
        diff = self._diff_with_single_addition("secrets/my.key", b"-----BEGIN PRIVATE KEY-----")
        assert any(s["path"] == "secrets/my.key" for s in diff.suspicious_additions)

    def test_pem_extension_flagged(self) -> None:
        diff = self._diff_with_single_addition("certs/cert.pem", b"-----BEGIN CERTIFICATE-----")
        assert any(s["path"] == "certs/cert.pem" for s in diff.suspicious_additions)

    def test_map_extension_flagged(self) -> None:
        diff = self._diff_with_single_addition("dist/bundle.js.map", b'{"version":3}')
        assert any(s["path"] == "dist/bundle.js.map" for s in diff.suspicious_additions)

    def test_sh_executable_flagged(self) -> None:
        diff = self._diff_with_single_addition("scripts/deploy.sh", b"#!/bin/bash")
        assert any(s["path"] == "scripts/deploy.sh" for s in diff.suspicious_additions)

    def test_exe_extension_flagged(self) -> None:
        diff = self._diff_with_single_addition("bin/tool.exe", b"MZ\x90")
        assert any(s["path"] == "bin/tool.exe" for s in diff.suspicious_additions)

    def test_large_file_flagged(self) -> None:
        # 1.1 MiB — above the 1 MiB threshold.
        # Use a .dat extension (not in _SUSPICIOUS_EXTENSIONS) so the only
        # trigger is the large-file heuristic.
        large_content = b"x" * (1_100_000)
        diff = self._diff_with_single_addition("data/large.dat", large_content)
        assert any(s["path"] == "data/large.dat" for s in diff.suspicious_additions)
        reasons = [
            s["reason"] for s in diff.suspicious_additions if s["path"] == "data/large.dat"
        ]
        assert any("large" in r.lower() for r in reasons)

    def test_normal_py_file_not_flagged(self) -> None:
        diff = self._diff_with_single_addition("pkg/utils.py", b"def helper(): pass")
        assert all(s["path"] != "pkg/utils.py" for s in diff.suspicious_additions)

    def test_normal_txt_file_not_flagged(self) -> None:
        diff = self._diff_with_single_addition("README.txt", b"Hello world")
        assert diff.suspicious_additions == []

    def test_suspicious_entry_has_required_keys(self) -> None:
        diff = self._diff_with_single_addition(".env", b"KEY=val")
        entry = next(s for s in diff.suspicious_additions if s["path"] == ".env")
        assert "path" in entry
        assert "size" in entry
        assert "sha256" in entry
        assert "reason" in entry

    def test_suspicious_sha256_matches_file(self) -> None:
        content = b"SECRET=abc"
        diff = self._diff_with_single_addition(".env", content)
        entry = next(s for s in diff.suspicious_additions if s["path"] == ".env")
        assert entry["sha256"] == _sha(content)

    def test_large_file_threshold_boundary_not_flagged(self) -> None:
        # Exactly 1 MiB — NOT above threshold, should not trigger large-file rule.
        exactly_1mib = b"x" * (1024 * 1024)
        diff = self._diff_with_single_addition("data/boundary.bin", exactly_1mib)
        # Only the large-file heuristic is not triggered; other heuristics may
        # still fire for other reasons, but not for size.
        for entry in diff.suspicious_additions:
            if entry["path"] == "data/boundary.bin":
                assert "large" not in entry["reason"].lower()


# ---------------------------------------------------------------------------
# Cross-type comparison
# ---------------------------------------------------------------------------


class TestCrossTypeError:
    """Comparing different artefact types must raise ValueError."""

    def test_wheel_vs_sdist_raises(self) -> None:
        wheel_inv = _inventory([], artefact_type="wheel")
        sdist_inv = _inventory([], artefact_type="sdist")
        with pytest.raises(ValueError, match="different types"):
            compute_release_diff(baseline=sdist_inv, candidate=wheel_inv)

    def test_wheel_vs_npm_raises(self) -> None:
        wheel_inv = _inventory([], artefact_type="wheel")
        npm_inv = _inventory([], artefact_type="npm")
        with pytest.raises(ValueError, match="different types"):
            compute_release_diff(baseline=npm_inv, candidate=wheel_inv)

    def test_sdist_vs_npm_raises(self) -> None:
        sdist_inv = _inventory([], artefact_type="sdist")
        npm_inv = _inventory([], artefact_type="npm")
        with pytest.raises(ValueError, match="different types"):
            compute_release_diff(baseline=sdist_inv, candidate=npm_inv)

    def test_error_message_mentions_both_types(self) -> None:
        wheel_inv = _inventory([], artefact_type="wheel")
        npm_inv = _inventory([], artefact_type="npm")
        with pytest.raises(ValueError) as exc_info:
            compute_release_diff(baseline=npm_inv, candidate=wheel_inv)
        msg = str(exc_info.value)
        assert "wheel" in msg
        assert "npm" in msg


# ---------------------------------------------------------------------------
# Path fields
# ---------------------------------------------------------------------------


class TestDiffPaths:
    """baseline_path and candidate_path are populated from inventories."""

    def test_baseline_path_from_inventory(self) -> None:
        inv = ArtefactInventory(
            artefact_type="wheel",
            artefact_path="/tmp/baseline-1.0.whl",
            artefact_sha256=_sha(b"b"),
            files=[],
        )
        candidate = ArtefactInventory(
            artefact_type="wheel",
            artefact_path="/tmp/candidate-2.0.whl",
            artefact_sha256=_sha(b"c"),
            files=[],
        )
        diff = compute_release_diff(baseline=inv, candidate=candidate)
        assert diff.baseline_path == "/tmp/baseline-1.0.whl"

    def test_candidate_path_from_inventory(self) -> None:
        inv = ArtefactInventory(
            artefact_type="wheel",
            artefact_path="/tmp/baseline-1.0.whl",
            artefact_sha256=_sha(b"b"),
            files=[],
        )
        candidate = ArtefactInventory(
            artefact_type="wheel",
            artefact_path="/tmp/candidate-2.0.whl",
            artefact_sha256=_sha(b"c"),
            files=[],
        )
        diff = compute_release_diff(baseline=inv, candidate=candidate)
        assert diff.candidate_path == "/tmp/candidate-2.0.whl"
