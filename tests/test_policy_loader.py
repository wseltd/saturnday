"""Tests for Fix 53.e — shared policy_loader module.

Proves:
1. load_policy returns dict from valid YAML, empty dict on missing/invalid
2. validated_expected_findings filters non-strings
3. validated_exemptions filters bad entries, warns on missing check
4. is_finding_exempt matches check+pattern, path alias, global scope
5. filter_findings_by_exemptions removes matched findings
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.policy_loader import (
    filter_findings_by_exemptions,
    is_finding_exempt,
    load_policy,
    validated_expected_findings,
    validated_exemptions,
)


class TestLoadPolicy:
    def test_valid_yaml(self, tmp_path: Path) -> None:
        (tmp_path / ".saturnday-policy.yaml").write_text(
            "expected_findings:\n  - blast_radius\n"
        )
        result = load_policy(tmp_path)
        assert result["expected_findings"] == ["blast_radius"]

    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_policy(tmp_path) == {}

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        (tmp_path / ".saturnday-policy.yaml").write_text(":\n  - :\n  bad: [")
        result = load_policy(tmp_path)
        assert result == {}

    def test_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / ".saturnday-policy.yaml").write_text("")
        assert load_policy(tmp_path) == {}


class TestValidatedExpectedFindings:
    def test_all_strings(self) -> None:
        assert validated_expected_findings({"expected_findings": ["a", "b"]}) == {"a", "b"}

    def test_mixed_strings_and_dicts(self) -> None:
        policy = {"expected_findings": [
            "blast_radius",
            {"check": "dep", "reason": "bad"},
            "ruff",
        ]}
        result = validated_expected_findings(policy)
        assert result == {"blast_radius", "ruff"}

    def test_all_dicts(self) -> None:
        policy = {"expected_findings": [{"check": "a"}, {"check": "b"}]}
        assert validated_expected_findings(policy) == set()

    def test_empty(self) -> None:
        assert validated_expected_findings({}) == set()

    def test_not_a_list(self) -> None:
        assert validated_expected_findings({"expected_findings": "oops"}) == set()


class TestValidatedExemptions:
    def test_valid_entries(self) -> None:
        policy = {"exemptions": [
            {"check": "stubs", "pattern": "src/foo.py", "reason": "ok"},
            {"check": "ruff", "path": "src/bar.py", "reason": "ok"},
        ]}
        result = validated_exemptions(policy)
        assert len(result) == 2

    def test_missing_check_now_refuses(self) -> None:
        """Option-B contract: missing 'check' no longer warn-and-skip.
        validated_exemptions raises PolicySchemaError so the caller can
        exit non-zero instead of running governance with a partially
        filtered exemption list."""
        from saturnday._exceptions import PolicySchemaError
        policy = {"exemptions": [
            {"pattern": "src/foo.py", "reason": "no check field"},
        ]}
        with pytest.raises(PolicySchemaError) as exc_info:
            validated_exemptions(policy)
        assert "missing required field 'check'" in str(exc_info.value)

    def test_non_dict_entry_now_refuses(self) -> None:
        """Option-B contract: non-dict entries also refuse."""
        from saturnday._exceptions import PolicySchemaError
        policy = {"exemptions": ["just a string"]}
        with pytest.raises(PolicySchemaError) as exc_info:
            validated_exemptions(policy)
        assert "not a dict" in str(exc_info.value)

    def test_empty(self) -> None:
        assert validated_exemptions({}) == []


class TestIsFindingExempt:
    def test_pattern_match(self) -> None:
        assert is_finding_exempt("stubs", "src/foo.py", [
            {"check": "stubs", "pattern": "src/foo.py"},
        ])

    def test_path_alias(self) -> None:
        assert is_finding_exempt("stubs", "src/foo.py", [
            {"check": "stubs", "path": "src/foo.py"},
        ])

    def test_global_star(self) -> None:
        assert is_finding_exempt("blast", "any/file.py", [
            {"check": "blast", "pattern": "*"},
        ])

    def test_missing_pattern_is_global(self) -> None:
        assert is_finding_exempt("secrets", "some/path.py", [
            {"check": "secrets", "reason": "false positive"},
        ])

    def test_wrong_check(self) -> None:
        assert not is_finding_exempt("ruff", "src/foo.py", [
            {"check": "stubs", "pattern": "src/foo.py"},
        ])

    def test_glob_pattern(self) -> None:
        assert is_finding_exempt("stubs", "src/providers/aws.py", [
            {"check": "stubs", "pattern": "src/providers/*"},
        ])
        assert not is_finding_exempt("stubs", "src/utils.py", [
            {"check": "stubs", "pattern": "src/providers/*"},
        ])


class TestFilterFindingsByExemptions:
    def test_filters_matching(self) -> None:
        findings = [
            {"file": "src/foo.py", "message": "stub"},
            {"file": "src/bar.py", "message": "stub"},
        ]
        exemptions = [{"check": "stubs", "pattern": "src/foo.py"}]
        result = filter_findings_by_exemptions("stubs", findings, exemptions)
        assert len(result) == 1
        assert result[0]["file"] == "src/bar.py"

    def test_empty_exemptions_noop(self) -> None:
        findings = [{"file": "a.py", "message": "x"}]
        assert filter_findings_by_exemptions("stubs", findings, []) == findings

    def test_all_filtered(self) -> None:
        findings = [{"file": "a.py", "message": "x"}]
        exemptions = [{"check": "stubs", "pattern": "*"}]
        assert filter_findings_by_exemptions("stubs", findings, exemptions) == []
