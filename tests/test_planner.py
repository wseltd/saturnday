"""Tests for saturnday.run.planner."""

from pathlib import Path

from saturnday.run.planner import _extract_json, _scan_repo_layout


class TestExtractJson:
    def test_plain_json(self) -> None:
        result = _extract_json('{"version": 1, "project_id": "test", "tickets": []}')
        assert result is not None
        assert result["version"] == 1

    def test_markdown_fenced_json(self) -> None:
        text = '```json\n{"version": 1, "project_id": "test", "tickets": []}\n```'
        result = _extract_json(text)
        assert result is not None
        assert result["version"] == 1

    def test_json_with_preamble(self) -> None:
        text = 'Here is the plan:\n{"version": 1, "project_id": "test", "tickets": []}\nDone.'
        result = _extract_json(text)
        assert result is not None

    def test_invalid_json(self) -> None:
        result = _extract_json("not json at all")
        assert result is None

    def test_empty_string(self) -> None:
        result = _extract_json("")
        assert result is None


class TestScanRepoLayout:
    def test_empty_dir(self, tmp_path: Path) -> None:
        result = _scan_repo_layout(tmp_path)
        assert "Empty" in result or "minimal" in result

    def test_python_project(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n")
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("print('hello')\n")
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_main.py").write_text("def test_main(): pass\n")
        result = _scan_repo_layout(tmp_path)
        assert "Python" in result
        assert "tests" in result

    def test_node_project(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"name": "test"}')
        (tmp_path / "index.js").write_text("module.exports = {}\n")
        result = _scan_repo_layout(tmp_path)
        assert "Node" in result
