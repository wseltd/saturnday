"""Tests for saturnday.repair.finding_locality.

Validates the registry contents and the is_file_local predicate.
"""

from __future__ import annotations

import pytest

from saturnday.repair.finding_locality import FILE_LOCAL_KINDS, REPO_LEVEL_KINDS, is_file_local


# ---------------------------------------------------------------------------
# Registry structural invariants
# ---------------------------------------------------------------------------

def test_repo_level_kinds_is_frozenset() -> None:
    assert isinstance(REPO_LEVEL_KINDS, frozenset)


def test_repo_level_kinds_non_empty() -> None:
    assert len(REPO_LEVEL_KINDS) > 0


def test_all_repo_level_kinds_are_strings() -> None:
    for kind in REPO_LEVEL_KINDS:
        assert isinstance(kind, str), f"Non-string entry in REPO_LEVEL_KINDS: {kind!r}"


def test_no_empty_strings_in_registry() -> None:
    for kind in REPO_LEVEL_KINDS:
        assert kind.strip(), f"Empty or whitespace kind in REPO_LEVEL_KINDS: {kind!r}"


# ---------------------------------------------------------------------------
# is_file_local — known repo-level kinds must return False
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [
    "missing_package_json",
    "missing_tsconfig",
    "missing_project_config",
    "missing_license",
    "excessive_blast_radius",
    "missing_readme",
    "readme_missing_section",
    "readme_language_mismatch",
    "unpinned_dependency",
    "pip_audit",
    "duplicate_module",
    "circular_import",
    "dead_code",
    "tests_failing",
    "tests_timeout",
    "dockerfile_missing_dockerignore",
    "skill_md_no_usage",
    "skill_md_no_io",
    "project_no_entrypoint",
])
def test_is_file_local_false_for_repo_level_kinds(kind: str) -> None:
    assert is_file_local(kind) is False, f"Expected {kind!r} to be repo-level"


# ---------------------------------------------------------------------------
# is_file_local — known file-local kinds must return True
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", [
    # Python code quality
    "missing_type_hint",
    "missing_repr",
    "deprecated_utcnow",
    "docstring_only_stub",
    "hardcoded_return",
    "print_only_function",
    "test_empty_body",
    "test_no_assert",
    "syntax_error",
    # Python security
    "hardcoded_secret",
    "env_fallback_secret",
    "jwt_literal_secret",
    "weak_random_in_auth",
    "sql_string_building",
    "dangerous_xss_sink",
    "csrf_missing",
    "missing_security_logging",
    # External tools
    "ruff",
    "bandit",
    "shellcheck",
    # TypeScript / JS security
    "api_key",
    "bearer_token",
    "aws_key",
    "generic_secret",
    "template_injection",
    "typescript_error",
    # Python DevOps
    "unpinned_base_image",
    "dockerfile_run_as_root",
    "github_action_not_sha_pinned",
    "tf_hardcoded_credential",
    "k8s_privileged_container",
    "config_permissive_cors",
    # api_version_check
    "declared_not_installed",
    "mypy_error",
    "hallucinated_import",
])
def test_is_file_local_true_for_file_local_kinds(kind: str) -> None:
    assert is_file_local(kind) is True, f"Expected {kind!r} to be file-local"


# ---------------------------------------------------------------------------
# is_file_local — unknown kinds trigger a full-repo scan (allowlist design)
# ---------------------------------------------------------------------------

def test_unknown_kind_defaults_to_full_repo_scan() -> None:
    """Unrecognised kinds default to False (full-repo scan) — allowlist design.

    The allowlist (FILE_LOCAL_KINDS) is the safe default: a kind not on the
    list may need cross-file context, so we conservatively require a full scan.
    """
    assert is_file_local("totally_unknown_xyz_987") is False


def test_empty_string_is_not_file_local() -> None:
    """Empty string is not in FILE_LOCAL_KINDS, so it triggers a full-repo scan."""
    assert is_file_local("") is False


# ---------------------------------------------------------------------------
# FILE_LOCAL_KINDS — explicit allowlist structural invariants
# ---------------------------------------------------------------------------

def test_file_local_kinds_is_explicit_allowlist() -> None:
    """FILE_LOCAL_KINDS must exist and contain the full set of known file-local kinds.

    The allowlist-based design requires this set to have comprehensive coverage
    of real file-local check kinds (>90 entries expected).
    """
    assert isinstance(FILE_LOCAL_KINDS, frozenset), "FILE_LOCAL_KINDS must be a frozenset"
    assert len(FILE_LOCAL_KINDS) > 90, (
        f"FILE_LOCAL_KINDS has only {len(FILE_LOCAL_KINDS)} entries — "
        "expected >90 for full check coverage"
    )


# ---------------------------------------------------------------------------
# Consistency: every REPO_LEVEL_KINDS entry is non-file-local
# ---------------------------------------------------------------------------

def test_repo_level_kinds_all_return_false() -> None:
    """Every entry in REPO_LEVEL_KINDS must be classified as non-file-local."""
    for kind in REPO_LEVEL_KINDS:
        assert is_file_local(kind) is False, (
            f"REPO_LEVEL_KINDS contains {kind!r} but is_file_local returned True"
        )
