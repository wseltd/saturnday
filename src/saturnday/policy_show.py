"""Read-only policy display for ``saturnday policy show``.

Reads and formats the repo's ``.saturnday-policy.yaml`` for human-readable
terminal output without mutating state or creating files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def gather_policy(repo_path: Path) -> dict[str, Any]:
    """Read the repo policy file and return structured data for display.

    Returns a dict with all relevant policy sections.  Missing sections
    are represented as ``None`` or empty lists, never guessed.
    """
    repo_path = repo_path.resolve()
    policy_path = repo_path / ".saturnday-policy.yaml"

    result: dict[str, Any] = {
        "repo_path": str(repo_path),
        "policy_path": None,
        "exists": False,
        "schema_version": None,
        "expected_findings": [],
        "scope": None,
        "severity_overrides": [],
        "dependency_governance": None,
        "jwt_policy": None,
        "document_claim_policy": None,
        "document_sign_off_roles": [],
        "document_expected_findings": [],
        "strict_mode": None,
        "auto_install_deps": None,
    }

    if not policy_path.is_file():
        return result

    result["policy_path"] = str(policy_path)
    result["exists"] = True

    try:
        import yaml
    except ImportError:
        logger.warning("pyyaml not installed — cannot parse policy file")
        return result

    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return result
    except Exception:
        return result

    result["schema_version"] = raw.get("schema_version")
    result["expected_findings"] = raw.get("expected_findings", []) or []

    # Scope
    scope_raw = raw.get("scope", {})
    if isinstance(scope_raw, dict) and scope_raw:
        result["scope"] = {
            "allowed_paths": scope_raw.get("allowed_paths", []) or [],
            "denied_paths": scope_raw.get("denied_paths", []) or [],
            "max_files_changed": scope_raw.get("max_files_changed"),
            "max_lines_changed": scope_raw.get("max_lines_changed"),
        }

    # Severity overrides
    checks_raw = raw.get("checks", {})
    if isinstance(checks_raw, dict):
        for name, cfg in checks_raw.items():
            if isinstance(cfg, dict):
                result["severity_overrides"].append({
                    "name": str(name),
                    "enabled": cfg.get("enabled", True),
                    "severity": cfg.get("severity", "error"),
                })
            elif isinstance(cfg, str):
                result["severity_overrides"].append({
                    "name": str(name),
                    "enabled": cfg != "off",
                    "severity": str(cfg),
                })

    # Dependency governance
    deps_raw = raw.get("dependencies", {})
    if isinstance(deps_raw, dict) and deps_raw:
        result["dependency_governance"] = {
            "allow_new": deps_raw.get("allow_new", True),
            "require_pinned_versions": deps_raw.get("require_pinned_versions", False),
            "licence_allowlist": deps_raw.get("licence_allowlist", []) or [],
        }

    # JWT policy
    jwt_raw = raw.get("jwt_policy")
    if isinstance(jwt_raw, dict):
        result["jwt_policy"] = jwt_raw

    # Document extensions
    result["document_sign_off_roles"] = raw.get("document_sign_off_roles", []) or []
    result["document_expected_findings"] = raw.get("document_expected_findings", []) or []
    doc_claim_raw = raw.get("document_claim_policy")
    if isinstance(doc_claim_raw, dict):
        result["document_claim_policy"] = doc_claim_raw

    result["strict_mode"] = raw.get("strict_mode")
    result["auto_install_deps"] = raw.get("auto_install_deps")

    return result


def format_policy(data: dict[str, Any]) -> str:
    """Format policy data as a compact human-readable string."""
    lines: list[str] = []
    lines.append(f"  Repo: {data['repo_path']}")
    lines.append("")

    if not data["exists"]:
        lines.append("  No policy file found (.saturnday-policy.yaml)")
        lines.append("  All governance checks run with default settings.")
        return "\n".join(lines)

    lines.append(f"  Policy: {data['policy_path']}")
    if data["schema_version"]:
        lines.append(f"  Schema: {data['schema_version']}")
    lines.append("")

    # Expected findings
    ef = data.get("expected_findings", [])
    if ef:
        lines.append("  Expected findings (exempted from FAIL):")
        for f in ef:
            lines.append(f"    - {f}")
    else:
        lines.append("  Expected findings: none")
    lines.append("")

    # Scope
    scope = data.get("scope")
    if scope:
        lines.append("  Scope:")
        if scope.get("denied_paths"):
            lines.append("    Denied paths:")
            for p in scope["denied_paths"]:
                lines.append(f"      - {p}")
        if scope.get("allowed_paths"):
            lines.append("    Allowed paths:")
            for p in scope["allowed_paths"]:
                lines.append(f"      - {p}")
        if scope.get("max_files_changed") is not None:
            lines.append(f"    Max files changed: {scope['max_files_changed']}")
        if scope.get("max_lines_changed") is not None:
            lines.append(f"    Max lines changed: {scope['max_lines_changed']}")
    else:
        lines.append("  Scope: not configured (all files checked)")
    lines.append("")

    # Severity overrides
    overrides = data.get("severity_overrides", [])
    if overrides:
        lines.append("  Severity overrides:")
        for o in overrides:
            enabled = "enabled" if o.get("enabled", True) else "disabled"
            lines.append(f"    {o['name']}: {o['severity']} ({enabled})")
    else:
        lines.append("  Severity overrides: none (defaults apply)")
    lines.append("")

    # Dependency governance
    deps = data.get("dependency_governance")
    if deps:
        lines.append("  Dependency governance:")
        lines.append(f"    Allow new dependencies: {deps.get('allow_new', True)}")
        lines.append(f"    Require pinned versions: {deps.get('require_pinned_versions', False)}")
        if deps.get("licence_allowlist"):
            lines.append(f"    Licence allowlist: {', '.join(deps['licence_allowlist'])}")
    else:
        lines.append("  Dependency governance: not configured")
    lines.append("")

    # JWT policy
    jwt = data.get("jwt_policy")
    if jwt:
        lines.append("  JWT policy: configured")
        if jwt.get("allowed_algorithms"):
            lines.append(f"    Allowed algorithms: {', '.join(jwt['allowed_algorithms'])}")
    else:
        lines.append("  JWT policy: not configured")

    # Document extensions
    roles = data.get("document_sign_off_roles", [])
    if roles:
        lines.append(f"  Document sign-off roles: {', '.join(roles)}")
    doc_ef = data.get("document_expected_findings", [])
    if doc_ef:
        lines.append(f"  Document expected findings: {', '.join(doc_ef)}")
    doc_claim = data.get("document_claim_policy")
    if doc_claim:
        lines.append("  Document claim policy: configured")

    # Mode
    if data.get("strict_mode") is not None:
        lines.append(f"  Strict mode: {data['strict_mode']}")
    if data.get("auto_install_deps") is not None:
        lines.append(f"  Auto-install deps: {data['auto_install_deps']}")

    return "\n".join(lines)
