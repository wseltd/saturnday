"""Tests for policy_manifest.py — policy parsing, validation, and lookup."""

import textwrap
from pathlib import Path

import pytest

from saturnday.policy_manifest import (
    ALL_CHECKS,
    HARD_CHECKS,
    SOFT_CHECKS,
    CheckSeverity,
    PolicyManifest,
    ScopeConfig,
    default_policy,
    evaluate_scope,
    get_check_severity,
    load_policy,
    should_run_check,
    validate_policy,
)


class TestDefaultPolicy:
    def test_all_checks_present(self):
        from saturnday.policy_manifest import DEVOPS_WARNING_CHECKS, DEVOPS_INFO_CHECKS
        policy = default_policy()
        expected = ALL_CHECKS | DEVOPS_WARNING_CHECKS | DEVOPS_INFO_CHECKS
        assert set(policy.checks.keys()) == expected

    def test_hard_checks_are_error(self):
        policy = default_policy()
        for name in HARD_CHECKS:
            assert policy.checks[name].severity == "error"
            assert policy.checks[name].enabled is True

    def test_soft_checks_are_warning(self):
        policy = default_policy()
        for name in SOFT_CHECKS:
            assert policy.checks[name].severity == "warning"
            assert policy.checks[name].enabled is True


class TestValidatePolicy:
    def test_valid_policy(self):
        policy = default_policy()
        errors = validate_policy(policy)
        assert errors == []

    def test_invalid_severity(self):
        policy = PolicyManifest(
            checks={"syntax": CheckSeverity(name="syntax", severity="critical")}
        )
        errors = validate_policy(policy)
        assert len(errors) == 1
        assert "invalid severity" in errors[0]

    def test_negative_max_files(self):
        policy = PolicyManifest(
            scope=ScopeConfig(max_files_changed=-1)
        )
        errors = validate_policy(policy)
        assert len(errors) == 1
        assert "max_files_changed" in errors[0]

    def test_negative_max_lines(self):
        policy = PolicyManifest(
            scope=ScopeConfig(max_lines_changed=-5)
        )
        errors = validate_policy(policy)
        assert len(errors) == 1
        assert "max_lines_changed" in errors[0]


class TestGetCheckSeverity:
    def test_known_check(self):
        policy = default_policy()
        assert get_check_severity(policy, "syntax") == "error"
        assert get_check_severity(policy, "ruff") == "warning"

    def test_unknown_check_defaults_to_error(self):
        policy = default_policy()
        assert get_check_severity(policy, "nonexistent") == "error"


class TestShouldRunCheck:
    def test_enabled_check(self):
        policy = default_policy()
        assert should_run_check(policy, "syntax") is True

    def test_disabled_check(self):
        policy = PolicyManifest(
            checks={"syntax": CheckSeverity(name="syntax", enabled=False)}
        )
        assert should_run_check(policy, "syntax") is False

    def test_off_severity(self):
        policy = PolicyManifest(
            checks={"syntax": CheckSeverity(name="syntax", severity="off")}
        )
        assert should_run_check(policy, "syntax") is False

    def test_unknown_check_runs_by_default(self):
        policy = PolicyManifest()
        assert should_run_check(policy, "unknown_check") is True


class TestEvaluateScope:
    def test_no_constraints(self):
        policy = default_policy()
        ok, reasons = evaluate_scope(policy, ["src/app.py"])
        assert ok is True
        assert reasons == []

    def test_max_files_exceeded(self):
        policy = PolicyManifest(
            scope=ScopeConfig(max_files_changed=2)
        )
        ok, reasons = evaluate_scope(policy, ["a.py", "b.py", "c.py"])
        assert ok is False
        assert any("too many files" in r for r in reasons)

    def test_max_files_within_limit(self):
        policy = PolicyManifest(
            scope=ScopeConfig(max_files_changed=5)
        )
        ok, reasons = evaluate_scope(policy, ["a.py", "b.py"])
        assert ok is True

    def test_denied_paths(self):
        policy = PolicyManifest(
            scope=ScopeConfig(denied_paths=["*.secret", "config/*"])
        )
        ok, reasons = evaluate_scope(policy, ["src/app.py", "config/db.yaml"])
        assert ok is False
        assert any("denied" in r for r in reasons)

    def test_allowed_paths(self):
        policy = PolicyManifest(
            scope=ScopeConfig(allowed_paths=["src/*"])
        )
        ok, reasons = evaluate_scope(policy, ["src/app.py"])
        assert ok is True

    def test_not_in_allowed_paths(self):
        policy = PolicyManifest(
            scope=ScopeConfig(allowed_paths=["src/*"])
        )
        ok, reasons = evaluate_scope(policy, ["src/app.py", "docs/readme.md"])
        assert ok is False
        assert any("not in allowed" in r for r in reasons)


class TestLoadPolicy:
    def test_load_minimal_yaml(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            schema_version: "1.0.0"
            checks:
              syntax:
                severity: error
              ruff:
                severity: warning
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        policy = load_policy(policy_file)
        assert policy.schema_version == "1.0.0"
        assert policy.checks["syntax"].severity == "error"
        assert policy.checks["ruff"].severity == "warning"

    def test_load_with_scope(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            scope:
              allowed_paths:
                - "src/*"
              denied_paths:
                - "*.secret"
              max_files_changed: 50
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        policy = load_policy(policy_file)
        assert policy.scope.allowed_paths == ["src/*"]
        assert policy.scope.denied_paths == ["*.secret"]
        assert policy.scope.max_files_changed == 50

    def test_load_with_dependencies(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            dependencies:
              allow_new: false
              require_pinned_versions: true
              licence_allowlist:
                - MIT
                - Apache-2.0
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        policy = load_policy(policy_file)
        assert policy.dependencies.allow_new is False
        assert policy.dependencies.require_pinned_versions is True
        assert "MIT" in policy.dependencies.licence_allowlist

    def test_shorthand_severity(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            checks:
              ruff: "info"
              stubs: "off"
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        policy = load_policy(policy_file)
        assert policy.checks["ruff"].severity == "info"
        assert policy.checks["stubs"].severity == "off"
        assert policy.checks["stubs"].enabled is False

    def test_fills_in_missing_checks(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            checks:
              syntax:
                severity: error
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        policy = load_policy(policy_file)
        assert set(policy.checks.keys()) == ALL_CHECKS

    def test_invalid_yaml_not_mapping(self, tmp_path):
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_policy(policy_file)

    def test_invalid_severity_in_yaml(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            checks:
              syntax:
                severity: critical
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        with pytest.raises(ValueError, match="Invalid policy"):
            load_policy(policy_file)

    def test_empty_yaml(self, tmp_path):
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_policy(policy_file)

    def test_full_policy(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            schema_version: "1.0.0"
            scope:
              allowed_paths: ["src/**/*.py", "tests/**/*.py"]
              denied_paths: ["*.env", "secrets/*"]
              max_files_changed: 100
              max_lines_changed: 5000
            checks:
              syntax:
                enabled: true
                severity: error
              ruff:
                enabled: true
                severity: warning
              bandit:
                enabled: false
                severity: error
            dependencies:
              allow_new: true
              require_pinned_versions: false
        """)
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(yaml_content)
        policy = load_policy(policy_file)
        assert policy.scope.max_lines_changed == 5000
        assert policy.checks["bandit"].enabled is False
        assert should_run_check(policy, "bandit") is False
