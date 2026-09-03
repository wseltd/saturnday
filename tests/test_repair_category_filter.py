"""Tests for saturnday.repair.finding_category.

Covers:
  A. check_name_to_category — 8 tests
  B. parse_repair_categories — 12 tests
  C. Category filtering integration — 5 tests
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.repair.finding_category import (
    _DEVOPS_CHECKS,
    _PROJECT_CHECKS,
    _SECURITY_CHECKS,
    check_name_to_category,
    parse_repair_categories,
)
from saturnday.openclaw_scanner import Finding


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finding(
    *,
    check: str = "ruff",
    category: str = "quality",
    severity: str = "medium",
    file: str = "main.py",
    message: str = "Test finding",
) -> Finding:
    """Create a Finding with the given check and category."""
    f = Finding(
        check=check,
        severity=severity,
        file=file,
        message=message,
    )
    f.category = category
    return f


def _filter_by_categories(
    findings: list[Finding],
    categories: set[str] | None,
) -> list[Finding]:
    """Replicate the filtering logic that guided_repair applies.

    When ``categories`` is None, no filter is applied and all findings are
    returned unchanged.  Otherwise only findings whose ``category`` field is
    in ``categories`` are kept.
    """
    if categories is None:
        return list(findings)
    return [f for f in findings if f.category in categories]


# ---------------------------------------------------------------------------
# A. check_name_to_category
# ---------------------------------------------------------------------------

class TestCheckNameToCategory:
    def test_security_python_check(self) -> None:
        """hardcoded_jwt is a Python SEC check → security."""
        assert check_name_to_category("hardcoded_jwt") == "security"

    def test_security_ts_check(self) -> None:
        """sql_injection_ts is a TypeScript SEC check → security."""
        assert check_name_to_category("sql_injection_ts") == "security"

    def test_security_expansion_pack(self) -> None:
        """frontend_secret_exposure is an SEC Expansion Pack check → security."""
        assert check_name_to_category("frontend_secret_exposure") == "security"

    def test_devops_check(self) -> None:
        """dockerfile is a DevOps check → devops."""
        assert check_name_to_category("dockerfile") == "devops"

    def test_project_check(self) -> None:
        """license is a project-structure check → project."""
        assert check_name_to_category("license") == "project"

    def test_quality_check(self) -> None:
        """ruff is a code-quality check → quality."""
        assert check_name_to_category("ruff") == "quality"

    def test_unknown_defaults_to_quality(self) -> None:
        """Unrecognised check names default to quality."""
        assert check_name_to_category("some_new_check") == "quality"

    def test_project_checks_not_security(self) -> None:
        """missing_license must not be categorised as security.

        It is either 'project' (if present in _PROJECT_CHECKS) or 'quality'
        — never 'security'.
        """
        result = check_name_to_category("missing_license")
        assert result != "security", (
            "missing_license is a project-structure finding, not a security finding"
        )
        # Canonical expectation: present in _PROJECT_CHECKS → "project"
        if "missing_license" in _PROJECT_CHECKS:
            assert result == "project"
        else:
            assert result == "quality"


# ---------------------------------------------------------------------------
# B. parse_repair_categories
# ---------------------------------------------------------------------------

class TestParseRepairCategories:
    def test_security_keyword(self) -> None:
        assert parse_repair_categories("fix all the security issues") == {"security"}

    def test_vulnerability_keyword(self) -> None:
        assert parse_repair_categories("fix the vulnerabilities") == {"security"}

    def test_vuln_keyword(self) -> None:
        assert parse_repair_categories("fix vuln findings") == {"security"}

    def test_quality_keyword(self) -> None:
        assert parse_repair_categories("fix quality issues") == {"quality"}

    def test_lint_keyword(self) -> None:
        assert parse_repair_categories("fix lint warnings") == {"quality"}

    def test_devops_keyword(self) -> None:
        assert parse_repair_categories("fix devops issues") == {"devops"}

    def test_infra_keyword(self) -> None:
        assert parse_repair_categories("fix infrastructure problems") == {"devops"}

    def test_ci_keyword(self) -> None:
        assert parse_repair_categories("fix ci issues") == {"devops"}

    def test_no_qualifier_returns_none(self) -> None:
        """Generic repair request with no category keyword → None (no filter)."""
        assert parse_repair_categories("fix the findings") is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_repair_categories("") is None

    def test_multiple_categories(self) -> None:
        """Text mentioning two category keywords → both returned."""
        result = parse_repair_categories("fix security and quality")
        assert result == {"security", "quality"}

    def test_bare_sec_not_matched(self) -> None:
        """Bare 'sec' abbreviation must NOT match the security category.

        Only the full word 'security' (or explicit synonyms like
        'vulnerability', 'vuln') should trigger the security filter.
        """
        result = parse_repair_categories("fix sec issues")
        assert result is None, (
            "Bare 'sec' abbreviation must not be treated as a category keyword"
        )


# ---------------------------------------------------------------------------
# C. Category filtering integration
# ---------------------------------------------------------------------------

class TestCategoryFilteringIntegration:
    def test_security_filter_excludes_quality(self) -> None:
        """When categories={'security'}, quality findings are dropped."""
        findings = [
            _finding(check="hardcoded_jwt", category="security"),
            _finding(check="ruff", category="quality"),
        ]
        result = _filter_by_categories(findings, {"security"})
        assert len(result) == 1
        assert result[0].check == "hardcoded_jwt"

    def test_no_filter_keeps_all(self) -> None:
        """When categories is None, all findings are preserved."""
        findings = [
            _finding(check="hardcoded_jwt", category="security"),
            _finding(check="ruff", category="quality"),
            _finding(check="dockerfile", category="devops"),
        ]
        result = _filter_by_categories(findings, None)
        assert len(result) == 3

    def test_project_findings_excluded_from_security(self) -> None:
        """Project-category findings are excluded when the user asks for security only."""
        findings = [
            _finding(check="hardcoded_jwt", category="security"),
            _finding(check="license", category="project"),
        ]
        result = _filter_by_categories(findings, {"security"})
        assert len(result) == 1
        assert result[0].check == "hardcoded_jwt"

    def test_policy_exemptions_still_apply(self) -> None:
        """Exempted findings are removed before category filtering takes effect.

        This test simulates the exemption step by pre-removing the exempted
        finding from the list, then verifying the category filter handles the
        already-reduced set correctly.
        """
        all_findings = [
            _finding(check="hardcoded_jwt", category="security"),
            _finding(check="ruff", category="quality"),
            _finding(check="sql_injection", category="security"),
        ]
        # Simulate policy exemption: sql_injection is waived
        post_exemption = [f for f in all_findings if f.check != "sql_injection"]
        # Now apply category filter for security
        result = _filter_by_categories(post_exemption, {"security"})
        checks = {f.check for f in result}
        assert "sql_injection" not in checks
        assert "hardcoded_jwt" in checks
        assert "ruff" not in checks

    def test_per_ticket_scope_constraint_unchanged(self) -> None:
        """The SCOPE CONSTRAINT text must still exist in interactive.py.

        The category filter must not remove or alter the per-finding scope
        constraint injected into the coder prompt.  This test reads
        interactive.py directly and asserts the sentinel text is present.
        """
        interactive_path = (
            Path(__file__).parent.parent
            / "src" / "saturnday" / "interactive.py"
        )
        content = interactive_path.read_text(encoding="utf-8")
        assert "Fix ONLY this specific finding" in content, (
            "SCOPE CONSTRAINT sentinel missing from interactive.py — "
            "the per-finding scope constraint must not be removed"
        )
