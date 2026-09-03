"""Finding-kind locality registry for targeted repair scans.

Classifies governance finding kinds as either file-local (the finding is
fully determined by inspecting a single source file) or repo-level (the
finding requires examining the full repository — project structure, dependency
manifests, cross-file relationships, etc.).

Usage::

    from saturnday.repair.finding_locality import is_file_local

    if is_file_local(ticket.finding_kind) and ticket.file_path:
        # scope the governance scan to the single target file
        ...

Design notes
------------
Classification uses an **explicit allowlist** (``FILE_LOCAL_KINDS``).  Only
kinds that have been explicitly verified as file-local appear in that set.
Unknown or new kinds default to ``False`` (full-repo scan) so that a
forgotten or mistyped kind never silently takes the targeted-scan path when
it actually requires the full repository.

A kind is **repo-level** when any of the following are true:

- The check reads files outside the changed file (e.g. ``circular_import``,
  ``duplicate_module``).
- The check inspects project-root artefacts regardless of which source file
  changed (e.g. ``missing_readme``, ``missing_license``).
- The check runs an external tool over the whole project (e.g. ``pip_audit``,
  ``tests_failing``).

Everything else is **file-local**: the check reads only the content of the
target file and does not need the rest of the repo to produce a verdict.

Adding a new kind
-----------------
If a new check is **file-local**, add its kind string to ``FILE_LOCAL_KINDS``
after verifying that the check does not read outside the target file.

If a new check is **repo-level**, add its kind string to ``REPO_LEVEL_KINDS``
(documentation/test reference only — the classification function uses
``FILE_LOCAL_KINDS``).

Do NOT add a kind to both sets.
"""

from __future__ import annotations

__all__ = ["FILE_LOCAL_KINDS", "REPO_LEVEL_KINDS", "is_file_local"]

# ---------------------------------------------------------------------------
# Registry — allowlist (classification is based on this set)
# ---------------------------------------------------------------------------

FILE_LOCAL_KINDS: frozenset[str] = frozenset({
    # --- Python code quality (review.py) ------------------------------------
    "missing_type_hint",
    "missing_repr",
    "deprecated_utcnow",
    "docstring_only_stub",
    "hardcoded_return",
    "print_only_function",
    "test_empty_body",
    "test_no_assert",
    "tautological_assert",
    "assert_caught",
    "syntax_error",
    # NOTE: dead_code is NOT file-local — it builds a cross-file reference index
    # over the entire repo to detect unreferenced symbols.  Moved to REPO_LEVEL_KINDS.

    # --- Python security (review.py) ----------------------------------------
    "regex_candidate",
    "role_marker",
    "override_pattern",
    "possible_typosquat",
    "hardcoded_secret",
    "env_fallback_secret",
    "jwt_literal_secret",
    "weak_random_in_auth",
    "weak_uuid1",
    "timestamp_seed",
    "unpinned_algorithm",
    "alg_none_allowed",
    "missing_jwt_policy",
    "samesite_none_no_secure",
    "session_cookie_no_httponly",
    "missing_samesite",
    "broad_domain",
    "missing_token_invalidation",
    "missing_token_expiry",
    "csrf_missing",
    "missing_oauth_state",
    "missing_pkce",
    "unsafe_email_link",
    "redirect_from_input",
    "route_no_auth",
    "ws_no_auth",
    "ws_no_origin_check",
    "rate_limit_not_applied",
    "dangerous_xss_sink",
    "sql_string_building",
    "user_enumeration",
    "client_trusted_field",
    "idor_no_ownership",
    "missing_security_logging",
    "unstructured_security_logging",

    # --- External tools — file-local ----------------------------------------
    "ruff",
    "bandit",
    "shellcheck",

    # --- Fallback check names used as kind ----------------------------------
    # (no explicit kind field in finding; check name is used instead)
    "placeholders",
    "dependency_declaration",
    "import_check",
    "line_continuations",
    "code_quality",
    "stubs",
    "test_quality",
    "injection_scan",

    # --- Python DevOps — file-local -----------------------------------------
    "unpinned_base_image",
    "dockerfile_add_over_copy",
    "dockerfile_secret_in_build",
    "dockerfile_run_as_root",
    "github_action_not_sha_pinned",
    "github_broad_permissions",
    "github_pull_request_target",
    "github_self_hosted_runner_risk",
    "cicd_secret_in_plaintext",
    "cicd_no_verify",
    "github_cloud_secret_reference",
    "gitlab_secret_in_yaml",
    "gitlab_dind_or_privileged_pattern",
    "jenkins_hardcoded_credential",
    "jenkins_plaintext_password",
    "tf_hardcoded_credential",
    "tf_public_access_cidr",
    "tf_sensitive_in_state",
    "k8s_privileged_container",
    "k8s_unpinned_image",
    "k8s_secret_in_manifest",
    "k8s_no_resource_limits",
    "k8s_no_readiness_probe",
    "config_permissive_cors",
    "config_secret_in_config",

    # --- TypeScript / JS — file-local ----------------------------------------
    "api_key",
    "bearer_token",
    "aws_key",
    "private_key",
    "github_token",
    "generic_secret",
    "openai_key",
    "anthropic_key",
    "slack_token",
    "npm_token",
    "empty_test",
    "tautological_assertion",
    "skipped_test",
    "empty_describe",
    "template_injection",
    "concat_injection",
    "api_template_injection",
    "todo_comment",
    "not_implemented",
    "console_todo",
    "typescript_error",
    "typescript_type_error",
    "memory_store_rate_limit",
    "payment_secret_literal",
    "frontend_secret_assignment",
    "frontend_secret_object_key",
    "frontend_env_fallback_secret",

    # --- api_version_check file-level findings ------------------------------
    "declared_not_installed",
    "package_not_importable",
    "api_not_found",
    "api_attr_not_found",
    "python_version_compat",
    "mypy_error",
    "hallucinated_import",
    "typosquat",
})


# ---------------------------------------------------------------------------
# Registry — repo-level reference (documentation and tests only)
# ---------------------------------------------------------------------------
# NOTE: The classification function ``is_file_local`` uses ``FILE_LOCAL_KINDS``,
# not this set.  ``REPO_LEVEL_KINDS`` is kept for documentation clarity and so
# that tests can assert that known repo-level kinds are correctly rejected.

REPO_LEVEL_KINDS: frozenset[str] = frozenset({
    # --- Project structure checks -------------------------------------------
    # These inspect project-root artefacts, not individual source files.
    "missing_package_json",
    "missing_tsconfig",
    "missing_project_config",
    "missing_license",
    "excessive_blast_radius",
    "missing_readme",
    "readme_missing_section",
    "readme_language_mismatch",

    # --- Dependency / environment checks ------------------------------------
    # Require reading lock files or running external audit tools over the
    # whole dependency graph.
    "unpinned_dependency",
    "pip_audit",

    # --- Cross-file analysis ------------------------------------------------
    # Verdicts depend on relationships between multiple source files.
    "duplicate_module",
    "circular_import",
    "dead_code",        # scans ALL repo .py files to build a reference index

    # --- Whole-project execution checks -------------------------------------
    # Must execute the full test suite or a project-wide command.
    "tests_failing",
    "tests_timeout",

    # --- Repo-root file checks ----------------------------------------------
    # Inspect companion files that live at the repo root, not inside any
    # individual source file.
    "dockerfile_missing_dockerignore",
    "skill_md_no_usage",
    "skill_md_no_io",
    "project_no_entrypoint",
})


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def is_file_local(finding_kind: str) -> bool:
    """Return ``True`` only if *finding_kind* is explicitly classified as file-local.

    Classification uses ``FILE_LOCAL_KINDS`` (explicit allowlist).  Unknown
    kinds default to ``False`` so that new, forgotten, or mistyped kind strings
    never silently take the targeted-scan path when a full-repo scan is needed.

    A file-local finding is one where re-scanning only the target file is
    sufficient to determine whether the fix worked.  Repo-level findings
    require a full repository scan and must not be targeted.

    Args:
        finding_kind: The ``kind`` string from a governance ``Finding`` or
            ``RepairTicket``.

    Returns:
        ``True``  — the finding is explicitly verified as file-local; a
                    targeted single-file scan is safe and correct.
        ``False`` — the finding is repo-level, or its locality has not been
                    verified; a full-repo scan is required.

    Examples::

        >>> is_file_local("hardcoded_secret")
        True
        >>> is_file_local("missing_readme")
        False
        >>> is_file_local("circular_import")
        False
        >>> is_file_local("some_brand_new_unknown_kind")
        False
    """
    return finding_kind in FILE_LOCAL_KINDS
