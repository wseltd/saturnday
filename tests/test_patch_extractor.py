"""Tests for saturnday.patch_extractor."""

import pytest
from pathlib import Path

from saturnday._exceptions import PatchApplicationError, PatchExtractionError
from saturnday.patch_extractor import (
    apply_file_blocks,
    extract_changes,
    extract_unified_diff,
    parse_file_blocks,
)


class TestParseFileBlocks:
    def test_single_block(self) -> None:
        raw = (
            "Here is the file:\n\n"
            "FILE: src/foo.py\n"
            "def hello():\n"
            "    return 42\n"
            "END FILE\n"
        )
        blocks = parse_file_blocks(raw)
        assert "src/foo.py" in blocks
        assert "def hello():" in blocks["src/foo.py"]

    def test_multiple_blocks(self) -> None:
        raw = (
            "FILE: a.py\ncode_a\nEND FILE\n\n"
            "FILE: b.py\ncode_b\nEND FILE\n"
        )
        blocks = parse_file_blocks(raw)
        assert len(blocks) == 2
        assert "code_a" in blocks["a.py"]
        assert "code_b" in blocks["b.py"]

    def test_no_blocks_returns_empty(self) -> None:
        raw = "Just some text with no file blocks."
        blocks = parse_file_blocks(raw)
        assert blocks == {}


class TestExtractUnifiedDiff:
    def test_fenced_diff(self) -> None:
        raw = "```diff\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n```"
        diff = extract_unified_diff(raw)
        assert "--- a/foo.py" in diff
        assert "+++ b/foo.py" in diff

    def test_no_diff_returns_empty(self) -> None:
        raw = "No diff content here."
        diff = extract_unified_diff(raw)
        assert diff == ""


class TestExtractChanges:
    def test_prefers_file_blocks(self) -> None:
        raw = "FILE: x.py\ncode\nEND FILE\n"
        changes = extract_changes(raw)
        assert "x.py" in changes

    def test_falls_back_to_diff(self) -> None:
        raw = "```diff\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n```"
        changes = extract_changes(raw)
        assert "__unified_diff__" in changes

    def test_no_changes_raises(self) -> None:
        with pytest.raises(PatchExtractionError):
            extract_changes("Just a conversation, no code.")


class TestApplyFileBlocks:
    def test_writes_files(self, tmp_path: Path) -> None:
        blocks = {"src/hello.py": "print('hello')\n"}
        written = apply_file_blocks(tmp_path, blocks, ("**",), ())
        assert written == ["src/hello.py"]
        assert (tmp_path / "src" / "hello.py").read_text() == "print('hello')\n"

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        blocks = {"/etc/passwd": "bad"}
        with pytest.raises(PatchApplicationError, match="Absolute path"):
            apply_file_blocks(tmp_path, blocks, ("**",), ())

    def test_rejects_traversal(self, tmp_path: Path) -> None:
        blocks = {"../escape.py": "bad"}
        with pytest.raises(PatchApplicationError, match="traversal"):
            apply_file_blocks(tmp_path, blocks, ("**",), ())

    def test_rejects_forbidden_glob(self, tmp_path: Path) -> None:
        blocks = {"secrets.env": "API_KEY=xxx"}
        with pytest.raises(PatchApplicationError, match="forbidden"):
            apply_file_blocks(tmp_path, blocks, ("**",), ("*.env",))

    def test_rejects_outside_allowed_globs(self, tmp_path: Path) -> None:
        blocks = {"other/file.py": "code"}
        with pytest.raises(PatchApplicationError, match="allowed"):
            apply_file_blocks(tmp_path, blocks, ("src/**",), ())
