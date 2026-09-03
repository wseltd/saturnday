"""Unit tests for _check_dead_code in saturnday.review.

Covers:
1. __all__ exported symbols are NOT flagged as dead code.
2. Truly unreferenced functions ARE still flagged.
3. Usage from an untouched repo file prevents a dead-code finding.
4. Decorated functions are skipped (existing behaviour preserved).
5. Private/test functions are skipped (existing behaviour preserved).
"""
from __future__ import annotations

from pathlib import Path

from saturnday.review import _check_dead_code


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── 1. __all__ export suppresses dead-code finding ──────────────────────


def test_all_exported_symbol_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        '__all__ = ["compute"]\n\ndef compute():\n    return 42\n',
    )
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "PASS", result["findings"]
    assert len(result["findings"]) == 0


def test_all_exported_tuple_form(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        '__all__ = ("compute",)\n\ndef compute():\n    return 42\n',
    )
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "PASS", result["findings"]


# ── 2. Real dead code is still flagged ──────────────────────────────────


def test_unreferenced_function_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        "def orphaned_helper():\n    return 99\n",
    )
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "FAIL"
    assert len(result["findings"]) == 1
    assert result["findings"][0]["kind"] == "dead_code"
    assert "orphaned_helper" in result["findings"][0]["detail"]


def test_unreferenced_not_in_all_still_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        '__all__ = ["compute"]\n\ndef compute():\n    return 1\n\ndef orphan():\n    return 2\n',
    )
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "FAIL"
    assert len(result["findings"]) == 1
    assert "orphan" in result["findings"][0]["detail"]


# ── 3. Usage from an untouched repo file prevents flagging ──────────────


def test_usage_from_untouched_file_prevents_finding(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        "def helper():\n    return 1\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from lib import helper\nhelper()\n",
    )
    # Only lib.py is in the changed set; main.py is untouched.
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "PASS", result["findings"]


def test_usage_from_untouched_file_string_reference(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "utils.py",
        "def process():\n    return True\n",
    )
    _write(
        tmp_path,
        "app.py",
        "from utils import process\nresult = process()\n",
    )
    # utils.py changed, app.py not in changed set.
    result = _check_dead_code(tmp_path, ["utils.py"])
    assert result["status"] == "PASS", result["findings"]


# ── 4. Decorated functions are skipped ──────────────────────────────────


def test_decorated_function_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "routes.py",
        "def decorator(f):\n    return f\n\n@decorator\ndef handle():\n    pass\n",
    )
    result = _check_dead_code(tmp_path, ["routes.py"])
    dead = [f for f in result["findings"] if "handle" in f["detail"]]
    assert len(dead) == 0


# ── 5. Private and test functions are skipped ───────────────────────────


def test_private_function_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        "def _internal():\n    return 1\n",
    )
    result = _check_dead_code(tmp_path, ["lib.py"])
    assert result["status"] == "PASS"


def test_test_function_not_flagged(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "test_lib.py",
        "def test_something():\n    assert True\n",
    )
    result = _check_dead_code(tmp_path, ["test_lib.py"])
    assert result["status"] == "PASS"


# ── 6. Same-file usage still works ─────────────────────────────────────


def test_same_file_caller_prevents_finding(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "lib.py",
        "def helper():\n    return 1\n\ndef main():\n    return helper()\n",
    )
    result = _check_dead_code(tmp_path, ["lib.py"])
    helper_findings = [f for f in result["findings"] if "helper" in f["detail"]]
    assert len(helper_findings) == 0
