"""Tests for release check REL-001 — source_map_blocker (RS-007).

Covers:
- .map extension file → FAIL
- sourceMappingURL= directive in .js file → FAIL
- Inline data: URI source map → FAIL
- "sourcesContent" key in a .map file → FAIL (embedded original source)
- .ts pre-compiled source file → FAIL
- Clean artefact (no source maps) → PASS
- .map file exempted in manifest → PASS with exempted finding
- Multiple files: some blocked, some clean → FAIL
- Multiple files: all exempted → PASS
- Manifest with non-list allowed_source_maps → treated as no exemptions
- Binary file skipped during content scan
- File absent from unpack_dir → no content findings (extension still caught)
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from saturnday.release._types import ArtefactFile, ArtefactInventory
from saturnday.release.checks.source_map_blocker import run_check, CHECK_NAME, RULE_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inventory(files: list[tuple[str, bytes]], tmp_path: Path) -> tuple[ArtefactInventory, Path]:
    """Create an ArtefactInventory and populate unpack_dir with file contents.

    Args:
        files:    List of (relative_path, content_bytes) tuples.
        tmp_path: Base temporary directory.

    Returns:
        Tuple of (inventory, unpack_dir).
    """
    unpack_dir = tmp_path / "unpacked"
    unpack_dir.mkdir()

    artefact_files: list[ArtefactFile] = []
    for rel_path, content in files:
        abs_path = unpack_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(content)
        sha = hashlib.sha256(content).hexdigest()
        artefact_files.append(
            ArtefactFile(path=rel_path, size=len(content), sha256=sha)
        )

    inventory = ArtefactInventory(
        artefact_type="wheel",
        artefact_path=str(tmp_path / "dummy-1.0-py3-none-any.whl"),
        artefact_sha256="a" * 64,
        files=artefact_files,
    )
    return inventory, unpack_dir


# ---------------------------------------------------------------------------
# Test: .map extension → FAIL
# ---------------------------------------------------------------------------


def test_map_extension_fails(tmp_path: Path) -> None:
    """A file ending in .map triggers a FAIL."""
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.js.map", b'{"version":3,"sources":["app.js"]}')],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.name == CHECK_NAME
    assert result.rule_id == RULE_ID
    assert result.status == "FAIL"
    assert result.severity == "error"
    assert result.files_checked == 1
    assert len(result.findings) >= 1
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) >= 1
    assert blocked[0]["path"] == "dist/bundle.js.map"
    assert ".map" in blocked[0]["reason"]


# ---------------------------------------------------------------------------
# Test: sourceMappingURL= directive → FAIL
# ---------------------------------------------------------------------------


def test_source_mapping_url_directive_fails(tmp_path: Path) -> None:
    """A .js file containing a sourceMappingURL= comment triggers a FAIL."""
    content = b"(function(){\n  console.log('hello');\n})();\n//# sourceMappingURL=bundle.js.map\n"
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.js", content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) == 1
    assert "sourceMappingURL=" in blocked[0]["reason"]


# ---------------------------------------------------------------------------
# Test: CSS sourceMappingURL= directive → FAIL
# ---------------------------------------------------------------------------


def test_css_source_mapping_url_fails(tmp_path: Path) -> None:
    """A .css file with a sourceMappingURL comment triggers a FAIL."""
    content = b"body { color: red; }\n/*# sourceMappingURL=styles.css.map */\n"
    inventory, unpack_dir = _make_inventory(
        [("dist/styles.css", content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) == 1


# ---------------------------------------------------------------------------
# Test: Inline data: URI source map → FAIL
# ---------------------------------------------------------------------------


def test_inline_data_uri_source_map_fails(tmp_path: Path) -> None:
    """A file containing a sourceMappingURL=data:application/json inline map fails."""
    inline_map = (
        b"(function(){}());\n"
        b"//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozfQ==\n"
    )
    inventory, unpack_dir = _make_inventory(
        [("dist/inline.js", inline_map)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) == 1
    assert "inline data" in blocked[0]["reason"].lower() or "data:" in blocked[0]["reason"]


# ---------------------------------------------------------------------------
# Test: sourcesContent key in file → FAIL
# ---------------------------------------------------------------------------


def test_sources_content_key_fails(tmp_path: Path) -> None:
    """A file containing the "sourcesContent" JSON key (embedded source) fails."""
    content = b'{"version":3,"sources":["app.ts"],"sourcesContent":["const x = 1;"]}'
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.js.map", content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    reasons = [f["reason"] for f in result.findings if f["status"] == "blocked"]
    assert any("sourcesContent" in r for r in reasons)


# ---------------------------------------------------------------------------
# Test: .ts file present → FAIL
# ---------------------------------------------------------------------------


def test_ts_file_fails(tmp_path: Path) -> None:
    """A .ts file present in the artefact triggers a FAIL."""
    inventory, unpack_dir = _make_inventory(
        [("src/app.ts", b"const x: number = 1;\n")],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    assert result.severity == "error"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) == 1
    assert ".ts" in blocked[0]["reason"]


# ---------------------------------------------------------------------------
# Test: .tsx file present → FAIL
# ---------------------------------------------------------------------------


def test_tsx_file_fails(tmp_path: Path) -> None:
    """A .tsx file present in the artefact triggers a FAIL."""
    inventory, unpack_dir = _make_inventory(
        [("src/App.tsx", b"export default function App() { return <div/>; }\n")],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert blocked[0]["path"] == "src/App.tsx"


# ---------------------------------------------------------------------------
# Test: clean artefact → PASS
# ---------------------------------------------------------------------------


def test_no_source_maps_passes(tmp_path: Path) -> None:
    """A clean artefact with no source maps or pre-compiled sources passes."""
    inventory, unpack_dir = _make_inventory(
        [
            ("saturnday/__init__.py", b"# package\n"),
            ("saturnday/core.py", b"def main(): pass\n"),
            ("dist/bundle.js", b"(function(){}());\n"),
        ],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "PASS"
    assert result.severity == "error"
    assert result.findings == []
    assert result.files_checked == 3


# ---------------------------------------------------------------------------
# Test: .map file allowed in manifest → PASS (exempted finding recorded)
# ---------------------------------------------------------------------------


def test_map_file_exempted_by_manifest_passes(tmp_path: Path) -> None:
    """A .map file that matches an allowed_source_maps pattern is exempted — PASS."""
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.js.map", b'{"version":3,"sources":["app.js"]}')],
        tmp_path,
    )
    manifest = {"allowed_source_maps": ["dist/*.map"]}

    result = run_check(inventory, unpack_dir, manifest=manifest)

    assert result.status == "PASS"
    assert result.severity == "error"
    # Finding is still recorded but marked exempted.
    assert len(result.findings) >= 1
    assert all(f["status"] == "exempted" for f in result.findings)
    assert result.findings[0]["path"] == "dist/bundle.js.map"


# ---------------------------------------------------------------------------
# Test: multiple files, one blocked → FAIL
# ---------------------------------------------------------------------------


def test_mixed_files_one_blocked_fails(tmp_path: Path) -> None:
    """When one file is blocked and others are clean, the result is FAIL."""
    inventory, unpack_dir = _make_inventory(
        [
            ("saturnday/core.py", b"def main(): pass\n"),
            ("dist/bundle.js.map", b'{"version":3}'),
        ],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) >= 1


# ---------------------------------------------------------------------------
# Test: all findings exempted → PASS
# ---------------------------------------------------------------------------


def test_all_exempted_passes(tmp_path: Path) -> None:
    """When all found issues are exempted in the manifest, overall status is PASS."""
    inventory, unpack_dir = _make_inventory(
        [
            ("dist/bundle.js.map", b'{"version":3}'),
            ("dist/styles.css.map", b'{"version":3}'),
        ],
        tmp_path,
    )
    manifest = {"allowed_source_maps": ["dist/*.map"]}

    result = run_check(inventory, unpack_dir, manifest=manifest)

    assert result.status == "PASS"
    assert all(f["status"] == "exempted" for f in result.findings)
    assert len(result.findings) >= 2


# ---------------------------------------------------------------------------
# Test: manifest allowed_source_maps not a list → no exemptions applied
# ---------------------------------------------------------------------------


def test_invalid_manifest_allowed_source_maps_no_exemptions(tmp_path: Path) -> None:
    """A non-list allowed_source_maps value is ignored — file is still blocked."""
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.js.map", b'{"version":3}')],
        tmp_path,
    )
    manifest: dict[str, Any] = {"allowed_source_maps": "dist/*.map"}  # string, not list

    result = run_check(inventory, unpack_dir, manifest=manifest)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) >= 1


# ---------------------------------------------------------------------------
# Test: binary file skipped for content scan (extension still caught)
# ---------------------------------------------------------------------------


def test_binary_map_file_still_caught_by_extension(tmp_path: Path) -> None:
    """A binary .map file is caught by extension even though content scan is skipped."""
    # Insert a null byte to trigger the binary-skip heuristic.
    binary_content = b"\x00\x01\x02\x03" + b'{"version":3}' + b"\x00"
    inventory, unpack_dir = _make_inventory(
        [("dist/binary.js.map", binary_content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) >= 1
    assert blocked[0]["path"] == "dist/binary.js.map"
    # Extension reason should be present; content reasons skipped.
    assert any(".map" in f["reason"] for f in blocked)


# ---------------------------------------------------------------------------
# Test: file absent from unpack_dir — extension still caught, no content error
# ---------------------------------------------------------------------------


def test_map_file_absent_from_unpack_dir(tmp_path: Path) -> None:
    """Inventory entry with no corresponding file on disk: extension still caught."""
    # Build inventory manually without writing the file to disk.
    inventory = ArtefactInventory(
        artefact_type="wheel",
        artefact_path=str(tmp_path / "dummy.whl"),
        artefact_sha256="b" * 64,
        files=[
            ArtefactFile(path="dist/missing.js.map", size=0, sha256="c" * 64),
        ],
    )
    unpack_dir = tmp_path / "unpacked"
    unpack_dir.mkdir()

    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) >= 1
    assert blocked[0]["path"] == "dist/missing.js.map"


# ---------------------------------------------------------------------------
# Test: no manifest (None) → no exemptions
# ---------------------------------------------------------------------------


def test_no_manifest_no_exemptions(tmp_path: Path) -> None:
    """When manifest is None, no exemptions are applied."""
    inventory, unpack_dir = _make_inventory(
        [("dist/app.js.map", b'{"version":3}')],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir, manifest=None)

    assert result.status == "FAIL"
    assert all(f["status"] == "blocked" for f in result.findings)


# ---------------------------------------------------------------------------
# Test: elapsed_s is populated
# ---------------------------------------------------------------------------


def test_elapsed_s_populated(tmp_path: Path) -> None:
    """elapsed_s is a non-negative float."""
    inventory, unpack_dir = _make_inventory([], tmp_path)
    result = run_check(inventory, unpack_dir)
    assert isinstance(result.elapsed_s, float)
    assert result.elapsed_s >= 0.0


# ---------------------------------------------------------------------------
# Test: files_checked count
# ---------------------------------------------------------------------------


def test_files_checked_count(tmp_path: Path) -> None:
    """files_checked reflects the number of inventory entries examined."""
    inventory, unpack_dir = _make_inventory(
        [
            ("a.py", b"pass\n"),
            ("b.py", b"pass\n"),
            ("c.py", b"pass\n"),
        ],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)
    assert result.files_checked == 3


# ---------------------------------------------------------------------------
# Test: .py file containing pattern literals is NOT content-scanned (RS-020)
# ---------------------------------------------------------------------------


def test_py_file_with_pattern_literals_not_flagged(tmp_path: Path) -> None:
    """A .py file containing sourceMappingURL= as a regex literal must not trigger FAIL.

    This is the exact false-positive scenario from RS-020: the check's own
    source (source_map_blocker.py) contains detection patterns as string
    constants and was flagging itself during self-scan.
    """
    py_content = (
        b'_RE = re.compile(rb"sourceMappingURL\\s*=\\s*\\S+")\n'
        b"_MARKER = b'\"sourcesContent\"'\n"
    )
    inventory, unpack_dir = _make_inventory(
        [("saturnday/release/checks/source_map_blocker.py", py_content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "PASS"
    assert result.findings == []


def test_non_scannable_extension_skips_content(tmp_path: Path) -> None:
    """Files with extensions outside _CONTENT_SCAN_EXTENSIONS skip content scan.

    A .rb file containing a sourceMappingURL= string must not trigger.
    Extension-based checks (.map, precompiled) still work regardless.
    """
    rb_content = b'PATTERN = "sourceMappingURL=something"\n'
    inventory, unpack_dir = _make_inventory(
        [("lib/scanner.rb", rb_content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "PASS"
    assert result.findings == []


def test_js_file_still_content_scanned(tmp_path: Path) -> None:
    """A .js file is still content-scanned — the allowlist must include .js."""
    js_content = b"//# sourceMappingURL=bundle.js.map\n"
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.js", js_content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert len(blocked) == 1
    assert "sourceMappingURL=" in blocked[0]["reason"]


def test_json_file_still_content_scanned(tmp_path: Path) -> None:
    """A .json file with sourcesContent is still caught by content scanning."""
    json_content = b'{"version":3,"sourcesContent":["const x = 1;"]}'
    inventory, unpack_dir = _make_inventory(
        [("dist/bundle.json", json_content)],
        tmp_path,
    )
    result = run_check(inventory, unpack_dir)

    assert result.status == "FAIL"
    blocked = [f for f in result.findings if f["status"] == "blocked"]
    assert any("sourcesContent" in f["reason"] for f in blocked)
