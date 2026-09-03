"""Central finding category mapping for repair intent filtering.

Maps check names (as they appear in CheckResult.name) to categories.
Used by guided_repair to filter findings when the user specifies a
category qualifier like "fix all the security issues".

Categories:
- "security": SEC-* checks (Python + TS), security expansion pack
- "quality": code quality, linting, type checking, stubs, tests
- "devops": CI/CD, Dockerfile, Terraform, Kubernetes, config
- "project": project-level structural checks (license, readme, etc.)
"""

from __future__ import annotations

import re

# Security checks — Python (have SEC-* rule_ids in policy_manifest.RULE_IDS)
_SECURITY_CHECKS: frozenset[str] = frozenset({
    "hardcoded_jwt",
    "auth_bypass",
    "websocket_auth",
    "oauth_flow_integrity",
    "cookie_security_hard",
    "weak_randomness",
    "token_revocation",
    "jwt_verification_policy",
    "csrf_state_change",
    "rate_limit_wiring",
    "cookie_security_soft",
    "token_expiry",
    "xss_check",
    "idor_check",
    "sql_injection",
    "client_trusted_logic",
    "user_enumeration",
    "rate_limit_backend_quality",
    "security_event_logging",
    # Security expansion pack
    "frontend_secret_exposure",
    "payment_secret_frontend",
    # TS/JS equivalents
    "hardcoded_jwt_ts",
    "auth_bypass_ts",
    "websocket_auth_ts",
    "oauth_flow_ts",
    "cookie_security_hard_ts",
    "weak_randomness_ts",
    "token_revocation_ts",
    "jwt_verification_ts",
    "csrf_state_change_ts",
    "rate_limit_wiring_ts",
    "cookie_security_soft_ts",
    "token_expiry_ts",
    "xss_check_ts",
    "idor_check_ts",
    "sql_injection_ts",
    "client_trusted_logic_ts",
    "user_enumeration_ts",
    "rate_limit_backend_quality_ts",
    "security_event_logging_ts",
})

_DEVOPS_CHECKS: frozenset[str] = frozenset({
    "dockerfile",
    "github_actions",
    "gitlab_ci",
    "jenkinsfile",
    "terraform",
    "kubernetes",
    "config_security",
    "trivy_config",
    "skill_md_operational",
    "project_entrypoint",
})

_PROJECT_CHECKS: frozenset[str] = frozenset({
    "project_runnable",
    "tests_pass",
    "license",
    "readme",
    "readme_consistency",
    "blast_radius",
})


def check_name_to_category(check_name: str) -> str:
    """Map a check name to its category.

    Args:
        check_name: The CheckResult.name / Finding.check value.

    Returns:
        One of "security", "quality", "devops", "project".
    """
    if check_name in _SECURITY_CHECKS:
        return "security"
    if check_name in _DEVOPS_CHECKS:
        return "devops"
    if check_name in _PROJECT_CHECKS:
        return "project"
    return "quality"


# --- Intent parsing ---

_SECURITY_TERMS = re.compile(
    r"\b(security|vulnerability|vulnerabilities|vuln)\b", re.IGNORECASE,
)
_QUALITY_TERMS = re.compile(
    r"\b(quality|lint|style)\b", re.IGNORECASE,
)
_DEVOPS_TERMS = re.compile(
    r"\b(devops|ci|infra|infrastructure)\b", re.IGNORECASE,
)


def parse_repair_categories(user_text: str) -> set[str] | None:
    """Extract category filter from user text.

    Returns a set of category strings if the user specified a recognisable
    qualifier, or None if no qualifier was found (meaning: repair all).

    Args:
        user_text: The user's original input, e.g. "fix all the security issues".

    Returns:
        A set like {"security"}, {"quality"}, {"devops"}, or None for all.
        Multiple categories can be returned if the user mentions several.
    """
    if not user_text:
        return None

    categories: set[str] = set()
    if _SECURITY_TERMS.search(user_text):
        categories.add("security")
    if _QUALITY_TERMS.search(user_text):
        categories.add("quality")
    if _DEVOPS_TERMS.search(user_text):
        categories.add("devops")

    return categories if categories else None
