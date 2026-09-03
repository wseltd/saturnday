"""Tests for release check REL-005 — release_diff (RS-012).

Covers:
- No baseline provided → SKIPPED with informational message
- Identical inventories (clean diff) → PASS
- Suspicious addition (no deny_always) → WARN
- Added file matching deny_always manifest pattern → FAIL
- Multiple suspicious additions → WARN (unless deny_always)
- deny_always file and suspicious file together → FAIL (deny_always wins)
- Cross-type baseline triggers graceful WARN (not crash)
- Summary finding always present in findings
- Files_checked reflects candidate inventory size
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.checks.release_diff import run_check, CHECK_NAME, RULE_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file(path: str, content: bytes) -> ArtefactFile:
    return ArtefactFile(path=path, size=len(content), sha256=_sha(content))


def _inventory(
    files: list[ArtefactFile],
    artefact_type: str = "wheel",
    artefact_path: str = "/tmp/fake.whl",
) -> ArtefactInventory:
    return ArtefactInventory(
        artefact_type=artefact_type,
        artefact_path=artefact_path,
        artefact_sha256=_sha(b"fake"),
        files=files,
    )


_UNPACK_DIR = Path("/tmp/fake_unpack")  # Not accessed by the diff check


# ---------------------------------------------------------------------------
# SKIPPED when no baseline
# ---------------------------------------------------------------------------


class TestNoBaseline:
    """When baseline is not provided the check returns SKIPPED."""

    def test_status_is_skipped(self) -> None:
        inv = _inventory([_file("pkg/mod.py", b"x = 1")])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR)
        assert result.status == "SKIPPED"

    def test_rule_id_correct(self) -> None:
        inv = _inventory([])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR)
        assert result.rule_id == RULE_ID

    def test_check_name_correct(self) -> None:
        inv = _inventory([])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR)
        assert result.name == CHECK_NAME

    def test_has_informational_message(self) -> None:
        inv = _inventory([])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR)
        # At least one finding with a descriptive message
        assert len(result.findings) >= 1
        messages = [str(f.get("message", "")) for f in result.findings]
        assert any("baseline" in m.lower() or "skipped" in m.lower() for m in messages)

    def test_explicit_none_baseline(self) -> None:
        inv = _inventory([_file("pkg/a.py", b"a")])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR, baseline=None)
        assert result.status == "SKIPPED"

    def test_severity_is_info(self) -> None:
        inv = _inventory([])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR)
        assert result.severity == "info"


# ---------------------------------------------------------------------------
# PASS for clean diff
# ---------------------------------------------------------------------------


class TestCleanDiff:
    """Identical inventories or non-suspicious additions → PASS."""

    def test_identical_inventories_pass(self) -> None:
        files = [_file("pkg/mod.py", b"x = 1"), _file("pkg/__init__.py", b"")]
        inv = _inventory(files)
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR, baseline=inv)
        assert result.status == "PASS"

    def test_clean_addition_passes(self) -> None:
        baseline = _inventory([_file("pkg/old.py", b"old")])
        candidate = _inventory([
            _file("pkg/old.py", b"old"),
            _file("pkg/new.py", b"new"),
        ])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "PASS"

    def test_summary_finding_always_present(self) -> None:
        inv = _inventory([_file("pkg/mod.py", b"x")])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR, baseline=inv)
        summary_findings = [f for f in result.findings if f.get("kind") == "summary"]
        assert len(summary_findings) == 1

    def test_summary_contains_counts(self) -> None:
        inv = _inventory([_file("pkg/mod.py", b"x")])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR, baseline=inv)
        summary = next(f for f in result.findings if f.get("kind") == "summary")
        assert "added_count" in summary
        assert "removed_count" in summary
        assert "changed_count" in summary

    def test_files_checked_is_candidate_count(self) -> None:
        files = [_file(f"pkg/mod_{i}.py", f"x={i}".encode()) for i in range(5)]
        inv = _inventory(files)
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR, baseline=inv)
        assert result.files_checked == 5

    def test_empty_inventories_pass(self) -> None:
        inv = _inventory([])
        result = run_check(inventory=inv, unpack_dir=_UNPACK_DIR, baseline=inv)
        assert result.status == "PASS"


# ---------------------------------------------------------------------------
# WARN for suspicious additions
# ---------------------------------------------------------------------------


class TestSuspiciousAddition:
    """Suspicious additions without deny_always → WARN."""

    def test_env_file_addition_warns(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file(".env", b"SECRET=abc")])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "WARN"

    def test_key_file_addition_warns(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("config/my.key", b"-----BEGIN PRIVATE KEY-----")])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "WARN"

    def test_pem_file_addition_warns(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("certs/cert.pem", b"-----BEGIN CERTIFICATE-----")])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "WARN"

    def test_map_file_addition_warns(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("dist/bundle.js.map", b'{"version":3}')])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "WARN"

    def test_sh_file_addition_warns(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("scripts/run.sh", b"#!/bin/bash\necho hi")])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "WARN"

    def test_severity_is_warning(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file(".env", b"KEY=val")])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.severity == "warning"

    def test_suspicious_finding_has_path(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file(".env", b"KEY=val")])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        suspicious = [f for f in result.findings if f.get("kind") in ("suspicious", "denied")]
        assert len(suspicious) >= 1
        assert all("path" in f for f in suspicious)

    def test_non_suspicious_addition_with_suspicious_addition_still_warns(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([
            _file("pkg/normal.py", b"x = 1"),
            _file(".env", b"SECRET=val"),
        ])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        assert result.status == "WARN"

    def test_multiple_suspicious_additions_all_in_findings(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([
            _file(".env", b"KEY=val"),
            _file("cert.pem", b"-----BEGIN CERTIFICATE-----"),
        ])
        result = run_check(inventory=candidate, unpack_dir=_UNPACK_DIR, baseline=baseline)
        suspicious_paths = {
            f["path"] for f in result.findings if f.get("kind") in ("suspicious", "denied")
        }
        assert ".env" in suspicious_paths
        assert "cert.pem" in suspicious_paths


# ---------------------------------------------------------------------------
# FAIL for deny_always additions
# ---------------------------------------------------------------------------


class TestDenyAlwaysAddition:
    """Files matching deny_always in manifest → FAIL."""

    def test_deny_always_match_fails(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("config/internal.txt", b"internal")])
        manifest = {"deny_always": ["config/internal.txt"]}
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.status == "FAIL"

    def test_deny_always_severity_is_error(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("secrets.json", b"{}")])
        manifest = {"deny_always": ["secrets.json"]}
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.severity == "error"

    def test_deny_always_glob_pattern_matches(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("config/prod.env", b"DB_PASS=secret")])
        manifest = {"deny_always": ["*.env", "**/*.env"]}
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.status == "FAIL"

    def test_deny_always_finding_kind_is_denied(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("runbook.md", b"# Internal Runbook")])
        manifest = {"deny_always": ["runbook.md"]}
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        denied = [f for f in result.findings if f.get("kind") == "denied"]
        assert len(denied) >= 1
        assert denied[0]["path"] == "runbook.md"

    def test_deny_always_file_not_added_does_not_fail(self) -> None:
        # The deny_always file is not in the candidate → clean diff
        baseline = _inventory([_file("pkg/mod.py", b"x")])
        candidate = _inventory([_file("pkg/mod.py", b"x")])
        manifest = {"deny_always": ["secrets.json"]}
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.status == "PASS"

    def test_deny_always_suspicious_also_flagged_as_denied(self) -> None:
        # A .env file that matches deny_always should be FAIL, not just WARN.
        baseline = _inventory([])
        candidate = _inventory([_file(".env", b"KEY=val")])
        manifest = {"deny_always": [".env"]}
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.status == "FAIL"

    def test_no_deny_always_in_manifest_does_not_fail(self) -> None:
        baseline = _inventory([])
        # Even a suspicious file should only WARN without deny_always.
        candidate = _inventory([_file(".env", b"KEY=val")])
        manifest = {"allowed_source_maps": []}  # manifest present but no deny_always
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.status == "WARN"

    def test_non_list_deny_always_ignored_gracefully(self) -> None:
        baseline = _inventory([])
        candidate = _inventory([_file("pkg/mod.py", b"clean")])
        manifest = {"deny_always": "not-a-list"}
        # Should not crash; should not FAIL either (deny_always ignored)
        result = run_check(
            inventory=candidate,
            unpack_dir=_UNPACK_DIR,
            manifest=manifest,
            baseline=baseline,
        )
        assert result.status == "PASS"


# ---------------------------------------------------------------------------
# Cross-type baseline
# ---------------------------------------------------------------------------


class TestCrossTypeBaseline:
    """Cross-type comparison → graceful WARN, no crash."""

    def test_wheel_candidate_npm_baseline_warns(self) -> None:
        wheel_inv = _inventory([], artefact_type="wheel")
        npm_inv = _inventory([], artefact_type="npm")
        result = run_check(inventory=wheel_inv, unpack_dir=_UNPACK_DIR, baseline=npm_inv)
        assert result.status == "WARN"
        assert len(result.findings) >= 1

    def test_cross_type_error_message_in_findings(self) -> None:
        wheel_inv = _inventory([], artefact_type="wheel")
        sdist_inv = _inventory([], artefact_type="sdist")
        result = run_check(inventory=wheel_inv, unpack_dir=_UNPACK_DIR, baseline=sdist_inv)
        messages = [str(f.get("message", "")) for f in result.findings]
        assert any(msg for msg in messages)  # at least some message
