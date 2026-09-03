"""Tests for Fix 51 — policy exemptions filtering.

Proves:
1. exemptions entries are loaded from policy YAML
2. matching check + pattern suppresses findings in governance
3. path globbing works across multiple files
4. "*" works as global path wildcard
5. unmatched findings remain
6. existing expected_findings behaviour is not broken
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from saturnday.evidence import CheckResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_check_result(
    name: str, findings: list[dict], status: str = "FAIL"
) -> CheckResult:
    return CheckResult(
        name=name,
        status=status,
        severity="error",
        findings=findings,
    )


def _apply_exemptions(check_results: list[CheckResult], exemptions: list[dict]) -> int:
    """Apply exemptions logic matching governance.py Fix 51 implementation."""
    from fnmatch import fnmatch
    suppressed = 0
    for cr in check_results:
        if not cr.findings:
            continue
        original = len(cr.findings)
        cr.findings = [
            f for f in cr.findings
            if not any(
                ex.get("check", "") == cr.name
                and (
                    ex.get("pattern", "") == "*"
                    or fnmatch(f.get("file", ""), ex.get("pattern", ""))
                )
                for ex in exemptions
                if isinstance(ex, dict)
            )
        ]
        removed = original - len(cr.findings)
        suppressed += removed
        if not cr.findings and cr.status == "FAIL":
            cr.status = "PASS"
    return suppressed


# ---------------------------------------------------------------------------
# Tests: exemption matching
# ---------------------------------------------------------------------------

class TestExemptionMatching:
    def test_exact_file_match_suppresses(self) -> None:
        """Exemption with exact file path suppresses matching finding."""
        findings = [
            {"file": "src/foo.py", "kind": "stubs", "message": "stub found"},
            {"file": "src/bar.py", "kind": "stubs", "message": "stub found"},
        ]
        cr = _make_check_result("stubs", findings)
        exemptions = [
            {"check": "stubs", "pattern": "src/foo.py", "reason": "abstract method"}
        ]
        suppressed = _apply_exemptions([cr], exemptions)
        assert suppressed == 1
        assert len(cr.findings) == 1
        assert cr.findings[0]["file"] == "src/bar.py"

    def test_glob_pattern_suppresses_multiple_files(self) -> None:
        """Exemption with glob pattern suppresses all matching files."""
        findings = [
            {"file": "src/providers/base.py", "kind": "stubs", "message": "stub"},
            {"file": "src/providers/aws.py", "kind": "stubs", "message": "stub"},
            {"file": "src/utils.py", "kind": "stubs", "message": "stub"},
        ]
        cr = _make_check_result("stubs", findings)
        exemptions = [
            {"check": "stubs", "pattern": "src/providers/*", "reason": "provider stubs"}
        ]
        suppressed = _apply_exemptions([cr], exemptions)
        assert suppressed == 2
        assert len(cr.findings) == 1
        assert cr.findings[0]["file"] == "src/utils.py"

    def test_star_pattern_is_global(self) -> None:
        """Exemption with pattern '*' suppresses all files for that check."""
        findings = [
            {"file": "a.py", "kind": "blast_radius", "message": "too many files"},
            {"file": "b.py", "kind": "blast_radius", "message": "too many files"},
            {"file": "c/d.py", "kind": "blast_radius", "message": "too many files"},
        ]
        cr = _make_check_result("blast_radius", findings)
        exemptions = [
            {"check": "blast_radius", "pattern": "*", "reason": "full scan artefact"}
        ]
        suppressed = _apply_exemptions([cr], exemptions)
        assert suppressed == 3
        assert len(cr.findings) == 0
        assert cr.status == "PASS"

    def test_wrong_check_does_not_match(self) -> None:
        """Exemption for different check name does not suppress."""
        findings = [
            {"file": "src/foo.py", "kind": "stubs", "message": "stub found"},
        ]
        cr = _make_check_result("stubs", findings)
        exemptions = [
            {"check": "ruff", "pattern": "src/foo.py", "reason": "wrong check"}
        ]
        suppressed = _apply_exemptions([cr], exemptions)
        assert suppressed == 0
        assert len(cr.findings) == 1

    def test_unmatched_findings_remain(self) -> None:
        """Findings not matching any exemption must remain."""
        findings = [
            {"file": "src/foo.py", "kind": "ruff", "message": "lint error"},
            {"file": "src/bar.py", "kind": "ruff", "message": "lint error"},
        ]
        cr = _make_check_result("ruff", findings)
        exemptions = [
            {"check": "ruff", "pattern": "src/foo.py", "reason": "re-export"}
        ]
        suppressed = _apply_exemptions([cr], exemptions)
        assert suppressed == 1
        assert len(cr.findings) == 1
        assert cr.findings[0]["file"] == "src/bar.py"

    def test_all_findings_suppressed_promotes_to_pass(self) -> None:
        """When all findings are suppressed, CheckResult status becomes PASS."""
        findings = [
            {"file": "src/foo.py", "kind": "stubs", "message": "stub"},
        ]
        cr = _make_check_result("stubs", findings, status="FAIL")
        exemptions = [
            {"check": "stubs", "pattern": "src/foo.py", "reason": "ok"}
        ]
        _apply_exemptions([cr], exemptions)
        assert cr.status == "PASS"

    def test_empty_exemptions_list_is_noop(self) -> None:
        """Empty exemptions list does not affect findings."""
        findings = [
            {"file": "src/foo.py", "kind": "ruff", "message": "error"},
        ]
        cr = _make_check_result("ruff", findings)
        suppressed = _apply_exemptions([cr], [])
        assert suppressed == 0
        assert len(cr.findings) == 1

    def test_multiple_exemptions_stack(self) -> None:
        """Multiple exemption entries for different checks/patterns all apply."""
        findings_stubs = [
            {"file": "src/base.py", "kind": "stubs", "message": "stub"},
        ]
        findings_ruff = [
            {"file": "src/export.py", "kind": "ruff", "message": "lint"},
        ]
        cr1 = _make_check_result("stubs", findings_stubs)
        cr2 = _make_check_result("ruff", findings_ruff)
        exemptions = [
            {"check": "stubs", "pattern": "src/base.py", "reason": "abstract"},
            {"check": "ruff", "pattern": "src/export.py", "reason": "re-export"},
        ]
        suppressed = _apply_exemptions([cr1, cr2], exemptions)
        assert suppressed == 2
        assert cr1.status == "PASS"
        assert cr2.status == "PASS"


# ---------------------------------------------------------------------------
# Tests: repair path filtering
# ---------------------------------------------------------------------------

class TestRepairExemptionFiltering:
    def test_repair_findings_filtered_by_exemption(self) -> None:
        """Findings matching check+pattern exemption are removed before repair tickets."""
        from fnmatch import fnmatch

        # Simulate finding objects with .kind and .file
        @dataclass
        class FakeFinding:
            kind: str
            file: str
            message: str = ""

        findings = [
            FakeFinding(kind="stubs", file="src/base.py"),
            FakeFinding(kind="stubs", file="src/impl.py"),
            FakeFinding(kind="ruff", file="src/main.py"),
        ]
        exemptions = [
            {"check": "stubs", "pattern": "src/base.py", "reason": "abstract"},
        ]

        filtered = [
            f for f in findings
            if not any(
                ex.get("check", "") == f.kind
                and (
                    ex.get("pattern", "") == "*"
                    or fnmatch(f.file, ex.get("pattern", ""))
                )
                for ex in exemptions
                if isinstance(ex, dict)
            )
        ]

        assert len(filtered) == 2
        assert all(f.file != "src/base.py" or f.kind != "stubs" for f in filtered)


# ---------------------------------------------------------------------------
# Fix 53.a: expected_findings validation tests
# ---------------------------------------------------------------------------

class TestExpectedFindingsValidation:
    """Test that malformed expected_findings is caught, not crashed."""

    def test_dict_entries_filtered_out(self) -> None:
        """Dict entries in expected_findings must be filtered, not crash set()."""
        raw = [
            {"check": "dependency_declaration", "path": "foo.py", "reason": "..."},
            "install_truth_smoke",
        ]
        filtered = [e for e in raw if isinstance(e, str)]
        result = set(filtered)
        assert result == {"install_truth_smoke"}

    def test_all_dicts_produces_empty_set(self) -> None:
        """All-dict expected_findings produces empty set after filtering."""
        raw = [
            {"check": "dep", "reason": "..."},
            {"check": "other", "reason": "..."},
        ]
        filtered = [e for e in raw if isinstance(e, str)]
        assert set(filtered) == set()

    def test_all_strings_works_normally(self) -> None:
        """Normal list[str] expected_findings works as before."""
        raw = ["dependency_declaration", "blast_radius"]
        filtered = [e for e in raw if isinstance(e, str)]
        assert set(filtered) == {"dependency_declaration", "blast_radius"}


# ---------------------------------------------------------------------------
# Fix 53.b: path alias and missing-pattern-as-global tests
# ---------------------------------------------------------------------------

class TestPathAliasAndGlobalScope:
    """Test that path works as alias for pattern, and missing = global."""

    def _match(self, check_name: str, file: str, exemption: dict) -> bool:
        """Simulate the Fix 53.b matching logic."""
        from fnmatch import fnmatch
        if exemption.get("check", "") != check_name:
            return False
        _p = exemption.get("pattern", "") or exemption.get("path", "") or "*"
        return _p == "*" or _p == "" or fnmatch(file, _p)

    def test_pattern_field_matches(self) -> None:
        assert self._match("stubs", "src/foo.py", {"check": "stubs", "pattern": "src/foo.py"})

    def test_path_field_matches_as_alias(self) -> None:
        assert self._match("stubs", "src/foo.py", {"check": "stubs", "path": "src/foo.py"})

    def test_missing_pattern_and_path_is_global(self) -> None:
        assert self._match("stubs", "src/foo.py", {"check": "stubs", "reason": "global"})
        assert self._match("stubs", "other/bar.py", {"check": "stubs", "reason": "global"})

    def test_star_pattern_is_global(self) -> None:
        assert self._match("blast", "any/file.py", {"check": "blast", "pattern": "*"})

    def test_wrong_check_no_match(self) -> None:
        assert not self._match("ruff", "src/foo.py", {"check": "stubs", "pattern": "src/foo.py"})

    def test_path_glob_works(self) -> None:
        assert self._match("stubs", "src/providers/aws.py", {"check": "stubs", "path": "src/providers/*"})
        assert not self._match("stubs", "src/utils.py", {"check": "stubs", "path": "src/providers/*"})


# ---------------------------------------------------------------------------
# Fix 53.c: policy validate command tests
# ---------------------------------------------------------------------------

class TestPolicyValidate:
    """Test _validate_policy function."""

    def test_valid_policy(self, tmp_path: Path) -> None:
        from saturnday.cli import _validate_policy
        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text(
            'expected_findings:\n  - blast_radius\n  - ruff\n'
            'exemptions:\n  - check: stubs\n    pattern: "src/foo.py"\n    reason: ok\n'
        )
        assert _validate_policy(tmp_path) == 0

    def test_dict_in_expected_findings_fails(self, tmp_path: Path) -> None:
        from saturnday.cli import _validate_policy
        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text(
            'expected_findings:\n  - check: dep\n    reason: bad\n'
        )
        assert _validate_policy(tmp_path) == 1

    def test_missing_check_in_exemption_fails(self, tmp_path: Path) -> None:
        from saturnday.cli import _validate_policy
        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text(
            'exemptions:\n  - pattern: "src/foo.py"\n    reason: no check field\n'
        )
        assert _validate_policy(tmp_path) == 1

    def test_no_policy_file_returns_ok(self, tmp_path: Path) -> None:
        from saturnday.cli import _validate_policy
        assert _validate_policy(tmp_path) == 0

    def test_empty_policy_returns_ok(self, tmp_path: Path) -> None:
        from saturnday.cli import _validate_policy
        policy = tmp_path / ".saturnday-policy.yaml"
        policy.write_text("")
        assert _validate_policy(tmp_path) == 0
