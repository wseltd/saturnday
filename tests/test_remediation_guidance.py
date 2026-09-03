"""Tests for saturnday.remediation_guidance.

Verifies coverage, field population, and structural invariants
across all scanner and security check finding kinds.
"""

from __future__ import annotations

import pytest

from saturnday.remediation_guidance import (
    GUIDANCE_REGISTRY,
    RemediationGuidance,
    get_guidance,
)

# ---------------------------------------------------------------------------
# Known finding kind sets
# ---------------------------------------------------------------------------

SCANNER_FINDING_KINDS: tuple[str, ...] = (
    "missing_skill_md",
    "missing_heading",
    "thin_skill_md",
    "shell_danger",
    "remote_download",
    "credential_leak",
    "command_interpolation",
    "broad_filesystem",
    "missing_approval_gate",
    "no_tests",
    "no_license",
)

SECURITY_CHECK_KINDS: tuple[str, ...] = (
    "hardcoded_jwt",
    "frontend_secret_exposure",
    "payment_secret_frontend",
    "auth_bypass",
    "websocket_auth",
    "oauth_flow_integrity",
    "cookie_security_hard",
    "csrf_state_change",
    "rate_limit_wiring",
    "token_expiry",
    "token_revocation",
    "idor_check",
    "client_trusted_logic",
)

# Finding kinds that must have a patch_template (high-severity / actionable code change)
HIGH_SEVERITY_PATCH_REQUIRED: tuple[str, ...] = (
    "shell_danger",
    "credential_leak",
    "command_interpolation",
    "hardcoded_jwt",
    "frontend_secret_exposure",
    "payment_secret_frontend",
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestScannerFindingCoverage:
    def test_all_scanner_finding_kinds_have_guidance(self) -> None:
        """Every scanner finding kind must resolve to a RemediationGuidance entry."""
        missing = [kind for kind in SCANNER_FINDING_KINDS if get_guidance(kind) is None]
        assert missing == [], f"Missing guidance for scanner kinds: {missing}"


class TestSecurityCheckCoverage:
    def test_all_security_check_kinds_have_guidance(self) -> None:
        """Every security check finding kind must resolve to a RemediationGuidance entry."""
        missing = [kind for kind in SECURITY_CHECK_KINDS if get_guidance(kind) is None]
        assert missing == [], f"Missing guidance for security check kinds: {missing}"


class TestFieldPopulation:
    def test_guidance_fields_populated(self) -> None:
        """Every registered entry must have non-empty why_it_matters and how_to_fix."""
        problems: list[str] = []
        for kind, guidance in GUIDANCE_REGISTRY.items():
            if not guidance.why_it_matters or not guidance.why_it_matters.strip():
                problems.append(f"{kind}: why_it_matters is empty")
            if not guidance.how_to_fix or not guidance.how_to_fix.strip():
                problems.append(f"{kind}: how_to_fix is empty")
        assert problems == [], "\n".join(problems)


class TestPatchTemplates:
    def test_high_severity_checks_have_patch_template(self) -> None:
        """High-severity / actionable findings must have a concrete patch_template."""
        missing = [
            kind
            for kind in HIGH_SEVERITY_PATCH_REQUIRED
            if (g := get_guidance(kind)) is None or g.patch_template is None
        ]
        assert missing == [], f"Missing patch_template for: {missing}"


class TestUnknownKind:
    def test_unknown_kind_returns_none(self) -> None:
        """A finding kind with no registered guidance must return None."""
        assert get_guidance("nonexistent") is None
        assert get_guidance("") is None
        assert get_guidance("SHELL_DANGER") is None  # case-sensitive


class TestReferences:
    def test_references_are_tuples(self) -> None:
        """Every registered entry must have references as a tuple (may be empty)."""
        non_tuples = [
            kind
            for kind, guidance in GUIDANCE_REGISTRY.items()
            if not isinstance(guidance.references, tuple)
        ]
        assert non_tuples == [], f"references is not a tuple for: {non_tuples}"


class TestRegistryIntegrity:
    def test_all_registry_values_are_remediation_guidance_instances(self) -> None:
        """All values in GUIDANCE_REGISTRY are RemediationGuidance dataclass instances."""
        bad = [
            kind
            for kind, val in GUIDANCE_REGISTRY.items()
            if not isinstance(val, RemediationGuidance)
        ]
        assert bad == []

    def test_registry_keys_match_rule_family(self) -> None:
        """Each registry key must equal the rule_family field of its entry."""
        mismatches = [
            (key, g.rule_family)
            for key, g in GUIDANCE_REGISTRY.items()
            if key != g.rule_family
        ]
        assert mismatches == [], f"Key/rule_family mismatches: {mismatches}"

    def test_full_coverage_count(self) -> None:
        """Registry must contain entries for all known scanner + security kinds."""
        all_known = set(SCANNER_FINDING_KINDS) | set(SECURITY_CHECK_KINDS)
        registered = set(GUIDANCE_REGISTRY.keys())
        uncovered = all_known - registered
        assert uncovered == set(), f"Uncovered finding kinds: {uncovered}"
