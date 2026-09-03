"""Policy manifest parser for governance mode.

Loads YAML policy files that control which checks run, their severity,
and scope constraints on changed files.
"""

from dataclasses import dataclass, field
from pathlib import Path

from .scope_rules import matches_path_patterns

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass
class ScopeConfig:
    allowed_paths: list[str] = field(default_factory=list)
    denied_paths: list[str] = field(default_factory=list)
    max_files_changed: int | None = None
    max_lines_changed: int | None = None


@dataclass
class CheckSeverity:
    name: str
    enabled: bool = True
    severity: str = "error"  # "error" | "warning" | "info" | "off"


@dataclass
class DependencyGovernance:
    allow_new: bool = True
    require_pinned_versions: bool = False
    licence_allowlist: list[str] = field(default_factory=list)


@dataclass
class JwtPolicy:
    """JWT verification policy for SEC-008."""
    allowed_algorithms: list[str] = field(default_factory=lambda: ["HS256", "RS256"])
    require_issuer: str | None = None
    require_audience: str | None = None
    require_exp: bool = True
    require_nbf: bool = False
    require_typ: bool = False
    allowed_token_types: list[str] = field(default_factory=list)


@dataclass
class DocumentClaimPolicy:
    """Policy settings that control document claim verification.

    Attributes:
        required_claim_types: Claim types that must have supporting evidence
            (e.g. ``["quantitative", "regulatory"]``).
        unsupported_verdict_action: What to do when a claim is UNSUPPORTED —
            ``"fail"`` (default) or ``"warn"``.
    """

    required_claim_types: list[str] = field(default_factory=list)
    unsupported_verdict_action: str = "fail"  # "fail" | "warn"


@dataclass
class PolicyManifest:
    schema_version: str = "1.0.0"
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    checks: dict[str, CheckSeverity] = field(default_factory=dict)
    dependencies: DependencyGovernance = field(default_factory=DependencyGovernance)
    auto_install_deps: bool = False
    # Security governance extensions
    jwt_policy: JwtPolicy | None = None  # None = not configured
    jwt_scope: str | None = None  # "internal_single_issuer" to use minimal fallback in strict mode
    unsupported_framework_action: str = "fail"  # "fail" (CI default) | "warn" (dev opt-in)
    strict_mode: bool = True  # CI default; set False for dev mode
    # Document mode governance extensions (additive — ignored in code mode)
    document_claim_policy: DocumentClaimPolicy | None = None
    document_sign_off_roles: list[str] = field(default_factory=list)
    document_expected_findings: list[str] = field(default_factory=list)


# Default classification: hard checks at error, soft checks at warning
HARD_CHECKS = {
    "syntax", "secrets", "bandit", "import_check", "api_version_check",
    "shellcheck", "line_continuations",
    # Security governance checks (SEC-001 through SEC-010)
    "hardcoded_jwt", "auth_bypass", "websocket_auth", "oauth_flow_integrity",
    "cookie_security_hard", "weak_randomness", "token_revocation",
    "jwt_verification_policy", "csrf_state_change", "rate_limit_wiring",
    # Security Expansion Pack 1 (SEC-FE-001, SEC-FE-002)
    "frontend_secret_exposure", "payment_secret_frontend",
}
SOFT_CHECKS = {
    "ruff", "placeholders", "dependency_declaration", "code_quality",
    "stubs", "test_quality", "injection_scan", "version_pinning",
    "typosquat", "module_conflicts",
    # Security governance checks (SEC-011 through SEC-018, SEC-OPS-001)
    "cookie_security_soft", "token_expiry", "xss_check", "idor_check",
    "sql_injection", "client_trusted_logic", "user_enumeration",
    "rate_limit_backend_quality", "security_event_logging",
    # Fix 12.A: packaging and schema checks
    "packaging_coverage", "competing_ddl",
    # Fix 12.B: truthfulness guards
    "constant_risk_suppression", "schema_write_read_coherence", "lookup_table_fabrication",
    "same_name_shape_divergence", "eval_preseeding",
    "test_vs_real_shape_parity", "doc_code_response_shape",
    # Fix 12.C: live-path reachability
    "live_path_reachability",
    # Fix 13: generated-app operator truth surfaces
    "read_without_write_surface", "frozen_status_fields",
    "write_read_schema_asymmetry", "hollow_resumed_flow",
    # Fix 15: dead-code sequencing — advisory warning, not a hard block
    "dead_code",
}

# TS/JS checks — emitted with _ts suffix to avoid collision with Python checks
TS_HARD_CHECKS = {
    "secrets_ts", "fake_tests_ts", "prompt_injection_ts",
    # Security governance TS variants (HARD)
    "hardcoded_jwt_ts", "auth_bypass_ts", "websocket_auth_ts",
    "oauth_flow_ts", "cookie_security_hard_ts", "weak_randomness_ts",
    "token_revocation_ts", "jwt_verification_ts", "csrf_state_change_ts",
    "rate_limit_wiring_ts",
}
TS_SOFT_CHECKS = {
    "hallucinated_imports_ts", "typosquat_ts", "placeholders_ts", "syntax_ts",
    # Security governance TS variants (SOFT)
    "cookie_security_soft_ts", "token_expiry_ts", "xss_check_ts",
    "idor_check_ts", "sql_injection_ts", "client_trusted_logic_ts",
    "user_enumeration_ts", "rate_limit_backend_quality_ts",
    "security_event_logging_ts",
}
TS_CHECKS = TS_HARD_CHECKS | TS_SOFT_CHECKS

# DevOps checks — warning severity (non-blocking, experimental)
DEVOPS_WARNING_CHECKS = {
    # Dockerfile
    "unpinned_base_image", "dockerfile_run_as_root", "dockerfile_add_over_copy",
    "dockerfile_missing_dockerignore", "dockerfile_secret_in_build",
    # GitHub Actions
    "github_action_not_sha_pinned", "github_broad_permissions",
    "github_pull_request_target", "github_self_hosted_runner_risk",
    "cicd_secret_in_plaintext", "cicd_no_verify",
    # GitLab CI
    "gitlab_secret_in_yaml", "gitlab_dind_or_privileged_pattern",
    # Jenkins
    "jenkins_hardcoded_credential", "jenkins_plaintext_password",
    # Terraform
    "tf_hardcoded_credential", "tf_public_access_cidr",
    # Kubernetes
    "k8s_privileged_container", "k8s_unpinned_image", "k8s_secret_in_manifest",
    # Config
    "config_secret_in_config",
    # Usability completeness
    "project_no_entrypoint",
    # Fix 12.A: install-truth smoke (advisory, warning)
    "install_truth_smoke",
}
# DevOps checks — info severity (informational only)
DEVOPS_INFO_CHECKS = {
    "github_cloud_secret_reference",
    "tf_sensitive_in_state",
    "k8s_no_resource_limits", "k8s_no_readiness_probe",
    "config_permissive_cors",
    # Usability completeness
    "skill_md_no_usage", "skill_md_no_io",
}

# Document mode checks — hard (blocking) and soft (warning) classifications.
# Rule ID prefix: DOC- to avoid collision with code-mode check IDs.
DOC_HARD_CHECKS: frozenset[str] = frozenset({
    "doc_structure",
    "doc_citation_existence",
    "doc_numeric_integrity",
    "doc_evidence_coverage",
})
DOC_SOFT_CHECKS: frozenset[str] = frozenset({
    "doc_terminology",
    "doc_placeholder",
})

ALL_CHECKS = HARD_CHECKS | SOFT_CHECKS
ALL_CHECKS_WITH_TS = ALL_CHECKS | TS_CHECKS

# Stable rule IDs — never reused, even if a check is retired
RULE_IDS: dict[str, str] = {
    "hardcoded_jwt": "SEC-001",
    "auth_bypass": "SEC-002",
    "websocket_auth": "SEC-003",
    "oauth_flow_integrity": "SEC-004",
    "cookie_security_hard": "SEC-005",
    "weak_randomness": "SEC-006",
    "token_revocation": "SEC-007",
    "jwt_verification_policy": "SEC-008",
    "csrf_state_change": "SEC-009",
    "rate_limit_wiring": "SEC-010",
    "cookie_security_soft": "SEC-011",
    "token_expiry": "SEC-012",
    "xss_check": "SEC-013",
    "idor_check": "SEC-014",
    "sql_injection": "SEC-015",
    "client_trusted_logic": "SEC-016",
    "user_enumeration": "SEC-017",
    "rate_limit_backend_quality": "SEC-018",
    "security_event_logging": "SEC-OPS-001",
    # Security Expansion Pack 1
    "frontend_secret_exposure": "SEC-FE-001",
    "payment_secret_frontend": "SEC-FE-002",
}

# CWE and OWASP mappings for security checks
RULE_CWE: dict[str, str] = {
    "hardcoded_jwt": "CWE-798",
    "auth_bypass": "CWE-284",
    "websocket_auth": "CWE-287",
    "oauth_flow_integrity": "CWE-352",
    "cookie_security_hard": "CWE-614",
    "weak_randomness": "CWE-338",
    "token_revocation": "CWE-613",
    "jwt_verification_policy": "CWE-327",
    "csrf_state_change": "CWE-352",
    "rate_limit_wiring": "CWE-307",
    "cookie_security_soft": "CWE-614",
    "token_expiry": "CWE-613",
    "xss_check": "CWE-79",
    "idor_check": "CWE-639",
    "sql_injection": "CWE-89",
    "client_trusted_logic": "CWE-602",
    "user_enumeration": "CWE-204",
    "rate_limit_backend_quality": "CWE-307",
    "security_event_logging": "CWE-778",
    # Security Expansion Pack 1
    "frontend_secret_exposure": "CWE-312",
    "payment_secret_frontend": "CWE-798",
}

RULE_OWASP: dict[str, str] = {
    "hardcoded_jwt": "A02:2021",
    "auth_bypass": "A01:2021",
    "websocket_auth": "A07:2021",
    "oauth_flow_integrity": "A07:2021",
    "cookie_security_hard": "A02:2021",
    "weak_randomness": "A02:2021",
    "token_revocation": "A07:2021",
    "jwt_verification_policy": "A02:2021",
    "csrf_state_change": "A01:2021",
    "rate_limit_wiring": "A07:2021",
    "cookie_security_soft": "A02:2021",
    "token_expiry": "A07:2021",
    "xss_check": "A03:2021",
    "idor_check": "A01:2021",
    "sql_injection": "A03:2021",
    "client_trusted_logic": "A04:2021",
    "user_enumeration": "A07:2021",
    "rate_limit_backend_quality": "A07:2021",
    "security_event_logging": "A09:2021",
    # Security Expansion Pack 1
    "frontend_secret_exposure": "A02:2021",
    "payment_secret_frontend": "A02:2021",
}

# Maps emitted check names → policy family names.
# Policy YAML uses family names; get_check_severity resolves via this map.
FAMILY_MAP: dict[str, str] = {
    # Python checks → families
    "secrets": "secrets",
    "import_check": "hallucinated_imports",
    "test_quality": "fake_tests",
    "injection_scan": "prompt_injection",
    "typosquat": "typosquat",
    "placeholders": "placeholders",
    "syntax": "syntax",
    # TS checks → families (original)
    "secrets_ts": "secrets",
    "hallucinated_imports_ts": "hallucinated_imports",
    "fake_tests_ts": "fake_tests",
    "prompt_injection_ts": "prompt_injection",
    "typosquat_ts": "typosquat",
    "placeholders_ts": "placeholders",
    "syntax_ts": "syntax",
    # Security governance TS checks → Python families
    "hardcoded_jwt_ts": "hardcoded_jwt",
    "auth_bypass_ts": "auth_bypass",
    "websocket_auth_ts": "websocket_auth",
    "oauth_flow_ts": "oauth_flow_integrity",
    "cookie_security_hard_ts": "cookie_security_hard",
    "weak_randomness_ts": "weak_randomness",
    "token_revocation_ts": "token_revocation",
    "jwt_verification_ts": "jwt_verification_policy",
    "csrf_state_change_ts": "csrf_state_change",
    "rate_limit_wiring_ts": "rate_limit_wiring",
    "cookie_security_soft_ts": "cookie_security_soft",
    "token_expiry_ts": "token_expiry",
    "xss_check_ts": "xss_check",
    "idor_check_ts": "idor_check",
    "sql_injection_ts": "sql_injection",
    "client_trusted_logic_ts": "client_trusted_logic",
    "user_enumeration_ts": "user_enumeration",
    "rate_limit_backend_quality_ts": "rate_limit_backend_quality",
    "security_event_logging_ts": "security_event_logging",
}


def default_policy() -> PolicyManifest:
    """All checks enabled. Hard checks at error, soft checks at warning."""
    checks: dict[str, CheckSeverity] = {}
    for name in sorted(ALL_CHECKS):
        severity = "error" if name in HARD_CHECKS else "warning"
        checks[name] = CheckSeverity(name=name, enabled=True, severity=severity)
    # DevOps checks — warning and info severity (non-blocking)
    for name in sorted(DEVOPS_WARNING_CHECKS):
        checks[name] = CheckSeverity(name=name, enabled=True, severity="warning")
    for name in sorted(DEVOPS_INFO_CHECKS):
        checks[name] = CheckSeverity(name=name, enabled=True, severity="info")
    return PolicyManifest(checks=checks)


def validate_policy(manifest: PolicyManifest) -> list[str]:
    """Return validation errors (empty = valid)."""
    errors: list[str] = []

    valid_severities = {"error", "warning", "info", "off"}
    for name, check in manifest.checks.items():
        if check.severity not in valid_severities:
            errors.append(
                f"check '{name}' has invalid severity '{check.severity}'; "
                f"must be one of {sorted(valid_severities)}"
            )

    if manifest.scope.max_files_changed is not None:
        if not isinstance(manifest.scope.max_files_changed, int) or manifest.scope.max_files_changed < 0:
            errors.append("scope.max_files_changed must be a non-negative integer")

    if manifest.scope.max_lines_changed is not None:
        if not isinstance(manifest.scope.max_lines_changed, int) or manifest.scope.max_lines_changed < 0:
            errors.append("scope.max_lines_changed must be a non-negative integer")

    return errors


def get_check_severity(manifest: PolicyManifest, check_name: str) -> str:
    """Lookup severity for a check.

    Resolves via FAMILY_MAP first: if the emitted check name maps to a
    policy family, and that family is configured, use the family severity.
    Falls back to direct check name lookup, then defaults to 'error'.
    """
    # Direct match first
    check = manifest.checks.get(check_name)
    if check is not None:
        return check.severity
    # Try family name via FAMILY_MAP
    family = FAMILY_MAP.get(check_name)
    if family:
        family_check = manifest.checks.get(family)
        if family_check is not None:
            return family_check.severity
    # Default for TS checks based on hard/soft classification
    if check_name in TS_HARD_CHECKS:
        return "error"
    if check_name in TS_SOFT_CHECKS:
        return "warning"
    return "error"


def should_run_check(manifest: PolicyManifest, check_name: str) -> bool:
    """False if disabled or severity='off'.

    Resolves via FAMILY_MAP for TS checks configured at the family level.
    """
    check = manifest.checks.get(check_name)
    if check is not None:
        return check.enabled and check.severity != "off"
    # Try family name via FAMILY_MAP
    family = FAMILY_MAP.get(check_name)
    if family:
        family_check = manifest.checks.get(family)
        if family_check is not None:
            return family_check.enabled and family_check.severity != "off"
    return True  # unknown checks run by default


def evaluate_scope(
    manifest: PolicyManifest, changed_files: list[str]
) -> tuple[bool, list[str]]:
    """Check changed files against scope rules. Returns (ok, reasons)."""
    reasons: list[str] = []

    if manifest.scope.max_files_changed is not None:
        if len(changed_files) > manifest.scope.max_files_changed:
            reasons.append(
                f"too many files changed: {len(changed_files)} > {manifest.scope.max_files_changed}"
            )

    if manifest.scope.denied_paths:
        for f in changed_files:
            if matches_path_patterns(f, manifest.scope.denied_paths):
                reasons.append(f"file in denied path: {f}")

    if manifest.scope.allowed_paths:
        for f in changed_files:
            if not matches_path_patterns(f, manifest.scope.allowed_paths):
                reasons.append(f"file not in allowed paths: {f}")

    return (len(reasons) == 0, reasons)


def load_policy(path: Path) -> PolicyManifest:
    """Parse YAML, validate, return PolicyManifest."""
    if yaml is None:
        raise ImportError(
            "pyyaml is required for policy manifest parsing. "
            "Install with: pip install pyyaml"
        )

    text = path.read_text()
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"Policy file must be a YAML mapping, got {type(raw).__name__}")

    manifest = PolicyManifest()
    manifest.schema_version = str(raw.get("schema_version", "1.0.0"))

    # Parse scope
    scope_raw = raw.get("scope", {})
    if isinstance(scope_raw, dict):
        manifest.scope = ScopeConfig(
            allowed_paths=_as_str_list(scope_raw.get("allowed_paths")),
            denied_paths=_as_str_list(scope_raw.get("denied_paths")),
            max_files_changed=_as_optional_int(scope_raw.get("max_files_changed")),
            max_lines_changed=_as_optional_int(scope_raw.get("max_lines_changed")),
        )

    # Parse checks
    checks_raw = raw.get("checks", {})
    if isinstance(checks_raw, dict):
        for name, cfg in checks_raw.items():
            name = str(name)
            if isinstance(cfg, dict):
                manifest.checks[name] = CheckSeverity(
                    name=name,
                    enabled=bool(cfg.get("enabled", True)),
                    severity=str(cfg.get("severity", "error")),
                )
            elif isinstance(cfg, str):
                # shorthand: check_name: "warning"
                manifest.checks[name] = CheckSeverity(
                    name=name,
                    enabled=cfg != "off",
                    severity=cfg,
                )

    # Parse dependencies
    deps_raw = raw.get("dependencies", {})
    if isinstance(deps_raw, dict):
        manifest.dependencies = DependencyGovernance(
            allow_new=bool(deps_raw.get("allow_new", True)),
            require_pinned_versions=bool(deps_raw.get("require_pinned_versions", False)),
            licence_allowlist=_as_str_list(deps_raw.get("licence_allowlist")),
        )

    # Parse auto_install_deps
    manifest.auto_install_deps = bool(raw.get("auto_install_deps", False))

    # Parse security governance extensions
    jwt_raw = raw.get("jwt_policy")
    if isinstance(jwt_raw, dict):
        manifest.jwt_policy = JwtPolicy(
            allowed_algorithms=_as_str_list(jwt_raw.get("allowed_algorithms", ["HS256", "RS256"])),
            require_issuer=jwt_raw.get("require_issuer"),
            require_audience=jwt_raw.get("require_audience"),
            require_exp=bool(jwt_raw.get("require_exp", True)),
            require_nbf=bool(jwt_raw.get("require_nbf", False)),
            require_typ=bool(jwt_raw.get("require_typ", False)),
            allowed_token_types=_as_str_list(jwt_raw.get("allowed_token_types")),
        )

    manifest.jwt_scope = raw.get("jwt_scope")
    manifest.unsupported_framework_action = str(raw.get("unsupported_framework_action", "fail"))
    manifest.strict_mode = bool(raw.get("strict_mode", True))

    # Parse document governance extensions (additive — ignored in code mode)
    doc_claim_raw = raw.get("document_claim_policy")
    if isinstance(doc_claim_raw, dict):
        manifest.document_claim_policy = DocumentClaimPolicy(
            required_claim_types=_as_str_list(
                doc_claim_raw.get("required_claim_types")
            ),
            unsupported_verdict_action=str(
                doc_claim_raw.get("unsupported_verdict_action", "fail")
            ),
        )

    manifest.document_sign_off_roles = _as_str_list(
        raw.get("document_sign_off_roles")
    )
    manifest.document_expected_findings = _as_str_list(
        raw.get("document_expected_findings")
    )

    # Fill in defaults for checks not specified in YAML
    for name in ALL_CHECKS:
        if name not in manifest.checks:
            severity = "error" if name in HARD_CHECKS else "warning"
            manifest.checks[name] = CheckSeverity(name=name, enabled=True, severity=severity)

    errors = validate_policy(manifest)
    if errors:
        raise ValueError(f"Invalid policy: {'; '.join(errors)}")

    return manifest


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _as_optional_int(value) -> int | None:
    if value is None:
        return None
    return int(value)
