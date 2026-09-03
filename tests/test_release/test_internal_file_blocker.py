"""Tests for saturnday.release.checks.internal_file_blocker (RS-009 / REL-003).

Covers:
- .env in artefact                      → FAIL (severity=error)
- id_rsa in artefact                    → FAIL
- __pycache__/ directory entry          → FAIL
- .npmrc in artefact                    → FAIL
- generate_licence.py in artefact       → FAIL
- core dump (bare "core") in artefact   → FAIL
- .map file in artefact                 → FAIL
- Clean artefact (no forbidden files)   → PASS
- .env exempted in manifest             → exempted finding, overall PASS
- Multiple forbidden files              → FAIL with multiple findings
- Nested forbidden file (under subdir)  → FAIL (e.g. config/.env)
- __pycache__ as a path component       → FAIL (mylib/__pycache__/foo.pyc)
- Manifest allowed_internal not a list  → warning, no crash (FAIL still)
- Empty inventory                       → PASS, files_checked=0
- Result name and rule_id               → always "internal_file_blocker"/"REL-003"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.checks.internal_file_blocker import (
    FORBIDDEN_FILE_PATTERNS,
    run_check,
)
from saturnday.release.evidence import ReleaseCheckResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inventory(paths: list[str], artefact_type: str = "wheel") -> ArtefactInventory:
    """Build a minimal ArtefactInventory containing the given relative paths."""
    files = [ArtefactFile(path=p, size=10, sha256="ab" * 16) for p in paths]
    return ArtefactInventory(
        artefact_type=artefact_type,
        artefact_path="/tmp/dummy-1.0-py3-none-any.whl",
        artefact_sha256="cd" * 16,
        files=files,
    )


# ---------------------------------------------------------------------------
# Constant sanity checks
# ---------------------------------------------------------------------------


def test_forbidden_patterns_is_list() -> None:
    assert isinstance(FORBIDDEN_FILE_PATTERNS, list)
    assert len(FORBIDDEN_FILE_PATTERNS) > 0, "deny list must not be empty"


def test_forbidden_patterns_no_duplicates() -> None:
    seen: set[str] = set()
    for p in FORBIDDEN_FILE_PATTERNS:
        assert p not in seen, f"duplicate pattern in FORBIDDEN_FILE_PATTERNS: {p!r}"
        seen.add(p)


# ---------------------------------------------------------------------------
# Result shape invariants
# ---------------------------------------------------------------------------


def test_result_name_and_rule_id_pass() -> None:
    inv = _make_inventory(["mylib/__init__.py"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.name == "internal_file_blocker"
    assert result.rule_id == "REL-003"


def test_result_name_and_rule_id_fail() -> None:
    inv = _make_inventory([".env"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.name == "internal_file_blocker"
    assert result.rule_id == "REL-003"


def test_result_is_check_result_instance() -> None:
    inv = _make_inventory(["mylib/__init__.py"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert isinstance(result, ReleaseCheckResult)


# ---------------------------------------------------------------------------
# FAIL cases — each forbidden category
# ---------------------------------------------------------------------------


def test_env_file_blocked() -> None:
    """.env at artefact root → FAIL."""
    inv = _make_inventory([".env", "mylib/__init__.py"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == ".env" for f in blocked)
    assert any(f["kind"] == "env_file" for f in blocked)
    assert any(f["pattern"] == ".env" for f in blocked)


def test_id_rsa_blocked() -> None:
    """id_rsa → FAIL."""
    inv = _make_inventory(["keys/id_rsa", "mylib/__init__.py"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == "keys/id_rsa" for f in blocked)
    assert any(f["kind"] == "ssh_key" for f in blocked)


def test_pycache_directory_entry_blocked() -> None:
    """__pycache__/ directory entry → FAIL."""
    inv = _make_inventory(["mylib/__pycache__/module.cpython-312.pyc"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert len(blocked) >= 1


def test_npmrc_blocked() -> None:
    """.npmrc → FAIL."""
    inv = _make_inventory([".npmrc"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == ".npmrc" for f in blocked)
    assert any(f["kind"] == "npm_internal" for f in blocked)


def test_generate_licence_blocked() -> None:
    """generate_licence.py → FAIL (signing script pattern)."""
    inv = _make_inventory(["scripts/generate_licence.py"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == "scripts/generate_licence.py" for f in blocked)
    assert any(f["kind"] == "signing_script" for f in blocked)


def test_core_dump_blocked() -> None:
    """Bare 'core' file → FAIL (debug artefact)."""
    inv = _make_inventory(["core"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == "core" for f in blocked)
    assert any(f["kind"] == "debug_artefact" for f in blocked)


def test_map_file_blocked() -> None:
    """*.map source map → FAIL."""
    inv = _make_inventory(["dist/bundle.js.map"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == "dist/bundle.js.map" for f in blocked)
    assert any(f["kind"] == "source_map" for f in blocked)


# ---------------------------------------------------------------------------
# PASS case
# ---------------------------------------------------------------------------


def test_clean_artefact_passes() -> None:
    """Clean artefact with no forbidden files → PASS."""
    inv = _make_inventory([
        "mylib/__init__.py",
        "mylib/utils.py",
        "mylib-1.0.dist-info/METADATA",
        "mylib-1.0.dist-info/RECORD",
        "mylib-1.0.dist-info/WHEEL",
    ])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "PASS"
    assert result.severity == "info"
    assert result.files_checked == 5
    # No non-exempted findings
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert blocked == []


# ---------------------------------------------------------------------------
# Manifest exemption
# ---------------------------------------------------------------------------


def test_env_exempted_by_manifest() -> None:
    """.env present but listed in manifest allowed_internal → exempted, PASS."""
    inv = _make_inventory([".env", "mylib/__init__.py"])
    manifest = {"allowed_internal": [".env"]}
    result = run_check(inv, Path("/tmp/unpacked"), manifest=manifest)
    assert result.status == "PASS"
    assert result.severity == "info"
    # The .env finding must still be present, tagged as exempted
    exempted = [f for f in result.findings if f.get("status") == "exempted"]
    assert any(f["file"] == ".env" for f in exempted)


def test_manifest_glob_exemption() -> None:
    """allowed_internal glob pattern exempts matching files → PASS."""
    inv = _make_inventory(["config/.env.test", "mylib/__init__.py"])
    manifest = {"allowed_internal": [".env.*"]}
    result = run_check(inv, Path("/tmp/unpacked"), manifest=manifest)
    assert result.status == "PASS"
    exempted = [f for f in result.findings if f.get("status") == "exempted"]
    assert any(f["file"] == "config/.env.test" for f in exempted)


def test_manifest_exemption_does_not_cover_other_files() -> None:
    """Exemption for .env does not exempt other forbidden files."""
    inv = _make_inventory([".env", "id_rsa"])
    manifest = {"allowed_internal": [".env"]}
    result = run_check(inv, Path("/tmp/unpacked"), manifest=manifest)
    # id_rsa is not exempted → FAIL
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == "id_rsa" for f in blocked)
    exempted = [f for f in result.findings if f.get("status") == "exempted"]
    assert any(f["file"] == ".env" for f in exempted)


# ---------------------------------------------------------------------------
# Multiple forbidden files
# ---------------------------------------------------------------------------


def test_multiple_forbidden_files_all_reported() -> None:
    """All forbidden files appear in findings, check FAIL."""
    inv = _make_inventory([
        ".env",
        "id_rsa",
        ".npmrc",
        "mylib/__init__.py",
    ])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    blocked_files = {f["file"] for f in blocked}
    assert ".env" in blocked_files
    assert "id_rsa" in blocked_files
    assert ".npmrc" in blocked_files


# ---------------------------------------------------------------------------
# Path nesting
# ---------------------------------------------------------------------------


def test_nested_env_file_blocked() -> None:
    """config/.env is blocked even when nested under a subdirectory."""
    inv = _make_inventory(["config/.env"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"


def test_nested_pycache_as_path_component() -> None:
    """mylib/__pycache__/foo.cpython-312.pyc blocked via directory component scan."""
    inv = _make_inventory(["mylib/__pycache__/foo.cpython-312.pyc"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"


def test_deeply_nested_key_file_blocked() -> None:
    """secrets/certs/server.pem blocked regardless of nesting depth."""
    inv = _make_inventory(["secrets/certs/server.pem"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["kind"] == "signing_key_or_cert" for f in blocked)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_inventory_passes() -> None:
    """Empty file list → PASS with files_checked=0."""
    inv = _make_inventory([])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "PASS"
    assert result.files_checked == 0
    assert result.findings == []


def test_manifest_allowed_internal_not_a_list_does_not_crash() -> None:
    """allowed_internal with unexpected type logs warning, check still runs."""
    inv = _make_inventory([".env"])
    manifest: dict[str, Any] = {"allowed_internal": ".env"}  # string, not list
    # Must not raise; .env should still be blocked (exemption list is empty)
    result = run_check(inv, Path("/tmp/unpacked"), manifest=manifest)
    assert result.status == "FAIL"


def test_no_manifest_uses_deny_by_default() -> None:
    """When manifest=None all forbidden files are blocked (no implicit exemptions)."""
    inv = _make_inventory([".env"])
    result = run_check(inv, Path("/tmp/unpacked"), manifest=None)
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert blocked


def test_files_checked_reflects_inventory_size() -> None:
    """files_checked equals the total number of files in the inventory."""
    paths = ["mylib/__init__.py", "mylib/utils.py", ".env"]
    inv = _make_inventory(paths)
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.files_checked == len(paths)


def test_elapsed_s_is_non_negative_float() -> None:
    inv = _make_inventory(["mylib/__init__.py"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert isinstance(result.elapsed_s, float)
    assert result.elapsed_s >= 0.0


def test_finding_has_required_keys_on_fail() -> None:
    """Each blocking finding must contain file, kind, and pattern keys."""
    inv = _make_inventory([".env"])
    result = run_check(inv, Path("/tmp/unpacked"))
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    for finding in blocked:
        assert "file" in finding, "finding missing 'file' key"
        assert "kind" in finding, "finding missing 'kind' key"
        assert "pattern" in finding, "finding missing 'pattern' key"


def test_npm_artefact_type_works() -> None:
    """.npmrc in an npm artefact is blocked correctly."""
    inv = _make_inventory([".npmrc", "package/index.js"], artefact_type="npm")
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["file"] == ".npmrc" for f in blocked)


def test_pyc_file_blocked() -> None:
    """Bare *.pyc file at root is blocked."""
    inv = _make_inventory(["module.pyc"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["kind"] == "build_artefact" for f in blocked)


def test_vault_token_blocked() -> None:
    """*.vault-token matches unintended_config category."""
    inv = _make_inventory(["prod.vault-token"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["kind"] == "unintended_config" for f in blocked)


def test_runbook_blocked() -> None:
    """runbook.md matches internal_runbook category."""
    inv = _make_inventory(["docs/runbook.md"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["kind"] == "internal_runbook" for f in blocked)


def test_sign_script_blocked() -> None:
    """sign_release.sh matches signing_script category."""
    inv = _make_inventory(["scripts/sign_release.sh"])
    result = run_check(inv, Path("/tmp/unpacked"))
    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f.get("status") != "exempted"]
    assert any(f["kind"] == "signing_script" for f in blocked)
