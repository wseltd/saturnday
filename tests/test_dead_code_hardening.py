"""Dead-code locality and path hardening tests (post-Fix-15).

Covers:
1. dead_code findings include the 'path' field for _filter_findings_to_files.
2. dead_code findings still carry the 'file' field (backward compatibility).
3. dead_code is no longer in FILE_LOCAL_KINDS.
4. dead_code is in REPO_LEVEL_KINDS.
5. is_file_local("dead_code") returns False.
6. Existing dead-code detection behaviour is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.repair.finding_locality import FILE_LOCAL_KINDS, REPO_LEVEL_KINDS, is_file_local
from saturnday.review import _check_dead_code


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: dead_code findings include 'path' field
# ---------------------------------------------------------------------------

def test_dead_code_finding_has_path(tmp_path: Path) -> None:
    """Each dead_code finding must include a 'path' field for downstream filtering."""
    _write(tmp_path, "lib.py", "def orphan():\n    return 42\n")
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "FAIL"
    assert len(result["findings"]) >= 1
    for finding in result["findings"]:
        assert "path" in finding, (
            f"dead_code finding missing 'path': {finding}"
        )
        assert finding["path"] == "lib.py"


def test_dead_code_path_matches_changed_file(tmp_path: Path) -> None:
    """'path' value must equal the repo-relative file path in changed_files."""
    _write(tmp_path, "src/utils.py", "def unused_fn():\n    pass\n")
    result = _check_dead_code(tmp_path, ["src/utils.py"])
    findings = result["findings"]
    assert any(f.get("path") == "src/utils.py" for f in findings), (
        "Expected 'path' == 'src/utils.py' in at least one finding"
    )


# ---------------------------------------------------------------------------
# Test 2: backward compatibility — 'file' field still present
# ---------------------------------------------------------------------------

def test_dead_code_finding_still_has_file_field(tmp_path: Path) -> None:
    """'file' field must remain for backward compatibility with existing consumers."""
    _write(tmp_path, "mod.py", "def ghost():\n    return 0\n")
    result = _check_dead_code(tmp_path, ["mod.py"])
    assert result["status"] == "FAIL"
    for finding in result["findings"]:
        assert "file" in finding, (
            f"dead_code finding lost 'file' field: {finding}"
        )
        assert finding["file"] == finding["path"], (
            "'file' and 'path' must contain the same value"
        )


# ---------------------------------------------------------------------------
# Test 3: dead_code is NOT in FILE_LOCAL_KINDS
# ---------------------------------------------------------------------------

def test_dead_code_not_in_file_local_kinds() -> None:
    """dead_code must be removed from FILE_LOCAL_KINDS (it is cross-file)."""
    assert "dead_code" not in FILE_LOCAL_KINDS, (
        "dead_code is in FILE_LOCAL_KINDS but the check scans the whole repo"
    )


# ---------------------------------------------------------------------------
# Test 4: dead_code IS in REPO_LEVEL_KINDS
# ---------------------------------------------------------------------------

def test_dead_code_in_repo_level_kinds() -> None:
    """dead_code must be in REPO_LEVEL_KINDS to document its cross-file nature."""
    assert "dead_code" in REPO_LEVEL_KINDS, (
        "dead_code must be listed in REPO_LEVEL_KINDS (cross-file reference scan)"
    )


# ---------------------------------------------------------------------------
# Test 5: is_file_local("dead_code") returns False
# ---------------------------------------------------------------------------

def test_is_file_local_dead_code_returns_false() -> None:
    """is_file_local must return False for dead_code after the classification fix."""
    assert is_file_local("dead_code") is False, (
        "dead_code must not be classified as file-local"
    )


# ---------------------------------------------------------------------------
# Test 6: existing detection behaviour is unchanged
# ---------------------------------------------------------------------------

def test_unreferenced_function_still_flagged(tmp_path: Path) -> None:
    """The detection logic itself must be unchanged — orphaned functions are flagged."""
    _write(tmp_path, "lib.py", "def orphaned():\n    return 1\n")
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "FAIL"
    assert result["findings"][0]["kind"] == "dead_code"
    assert "orphaned" in result["findings"][0]["detail"]


def test_used_function_not_flagged(tmp_path: Path) -> None:
    """Functions used from another file must not be flagged."""
    _write(tmp_path, "lib.py", "def helper():\n    return 1\n")
    _write(tmp_path, "main.py", "from lib import helper\nhelper()\n")
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "PASS", result["findings"]


def test_path_and_file_both_present_on_all_findings(tmp_path: Path) -> None:
    """When multiple findings are produced, every one must have both 'path' and 'file'."""
    _write(
        tmp_path,
        "multi.py",
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
    )
    result = _check_dead_code(tmp_path, ["multi.py"])
    assert result["status"] == "FAIL"
    assert len(result["findings"]) == 2
    for f in result["findings"]:
        assert "path" in f
        assert "file" in f
        assert f["path"] == f["file"] == "multi.py"
