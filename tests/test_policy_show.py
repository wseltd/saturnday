"""Tests for saturnday policy show command.

Covers:
1. No policy file — says so plainly
2. Expected findings shown correctly
3. Scope restrictions shown correctly
4. Severity overrides shown correctly
5. Optional sections handled cleanly
6. Does not mutate repo state
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from saturnday.policy_show import format_policy, gather_policy


def _write_policy(repo: Path, data: dict) -> None:
    path = repo / ".saturnday-policy.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. No policy file
# ---------------------------------------------------------------------------

class TestNoPolicyFile:
    def test_no_policy_returns_not_exists(self, tmp_path: Path) -> None:
        result = gather_policy(tmp_path)
        assert result["exists"] is False
        assert result["policy_path"] is None

    def test_formatted_says_no_policy(self, tmp_path: Path) -> None:
        output = format_policy(gather_policy(tmp_path))
        assert "No policy file found" in output
        assert "default settings" in output


# ---------------------------------------------------------------------------
# 2. Expected findings
# ---------------------------------------------------------------------------

class TestExpectedFindings:
    def test_shows_expected_findings(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "schema_version": "1.0.0",
            "expected_findings": ["missing_license", "project_runnable"],
        })
        result = gather_policy(tmp_path)
        assert result["expected_findings"] == ["missing_license", "project_runnable"]

    def test_formatted_lists_expected_findings(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "expected_findings": ["blast_radius", "dead_code"],
        })
        output = format_policy(gather_policy(tmp_path))
        assert "blast_radius" in output
        assert "dead_code" in output

    def test_empty_expected_findings(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {"schema_version": "1.0.0"})
        result = gather_policy(tmp_path)
        assert result["expected_findings"] == []
        output = format_policy(result)
        assert "Expected findings: none" in output


# ---------------------------------------------------------------------------
# 3. Scope restrictions
# ---------------------------------------------------------------------------

class TestScopeRestrictions:
    def test_shows_denied_paths(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "scope": {"denied_paths": ["tests/fixtures/**", "vendor/**"]},
        })
        result = gather_policy(tmp_path)
        assert result["scope"]["denied_paths"] == ["tests/fixtures/**", "vendor/**"]

    def test_formatted_lists_scope(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "scope": {"denied_paths": ["tests/fixtures/**"]},
        })
        output = format_policy(gather_policy(tmp_path))
        assert "tests/fixtures/**" in output
        assert "Denied paths" in output

    def test_no_scope_says_not_configured(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {"schema_version": "1.0.0"})
        output = format_policy(gather_policy(tmp_path))
        assert "not configured" in output


# ---------------------------------------------------------------------------
# 4. Severity overrides
# ---------------------------------------------------------------------------

class TestSeverityOverrides:
    def test_shows_overrides(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "checks": {
                "blast_radius": {"severity": "warning"},
                "dead_code": "off",
            },
        })
        result = gather_policy(tmp_path)
        overrides = result["severity_overrides"]
        names = {o["name"] for o in overrides}
        assert "blast_radius" in names
        assert "dead_code" in names

    def test_formatted_shows_overrides(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "checks": {"ruff": {"severity": "info", "enabled": False}},
        })
        output = format_policy(gather_policy(tmp_path))
        assert "ruff" in output
        assert "info" in output
        assert "disabled" in output

    def test_no_overrides_says_defaults(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {"schema_version": "1.0.0"})
        output = format_policy(gather_policy(tmp_path))
        assert "defaults apply" in output


# ---------------------------------------------------------------------------
# 5. Optional sections
# ---------------------------------------------------------------------------

class TestOptionalSections:
    def test_dependency_governance(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "dependencies": {"require_pinned_versions": True, "licence_allowlist": ["MIT", "Apache-2.0"]},
        })
        output = format_policy(gather_policy(tmp_path))
        assert "Require pinned versions: True" in output
        assert "MIT" in output

    def test_jwt_policy(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "jwt_policy": {"allowed_algorithms": ["RS256"]},
        })
        output = format_policy(gather_policy(tmp_path))
        assert "JWT policy: configured" in output
        assert "RS256" in output

    def test_document_sign_off_roles(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {
            "document_sign_off_roles": ["cfo", "legal"],
        })
        output = format_policy(gather_policy(tmp_path))
        assert "cfo" in output
        assert "legal" in output

    def test_all_optional_absent(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {"schema_version": "1.0.0"})
        result = gather_policy(tmp_path)
        assert result["dependency_governance"] is None
        assert result["jwt_policy"] is None
        assert result["document_claim_policy"] is None
        output = format_policy(result)
        assert "not configured" in output


# ---------------------------------------------------------------------------
# 6. No mutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_no_files_created(self, tmp_path: Path) -> None:
        _write_policy(tmp_path, {"expected_findings": ["test"]})
        before = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                before.add(os.path.join(root, f))

        gather_policy(tmp_path)

        after = set()
        for root, dirs, files in os.walk(tmp_path):
            for f in files:
                after.add(os.path.join(root, f))

        assert before == after
