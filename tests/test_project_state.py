"""Tests for saturnday.project_state."""

from pathlib import Path

from saturnday.project_state import (
    ProjectState,
    generate_context_summary,
    load_state,
    save_state,
    scan_changed_files,
    update_state,
    write_context_file,
)


class TestScanChangedFiles:
    def test_scans_python_file(self, tmp_path: Path) -> None:
        src = tmp_path / "foo.py"
        src.write_text("def hello(name: str) -> str:\n    return name\n")
        result = scan_changed_files(tmp_path, ["foo.py"], "T001")
        assert "foo.py" in result
        assert "hello" in result["foo.py"].functions

    def test_ignores_non_python(self, tmp_path: Path) -> None:
        (tmp_path / "readme.md").write_text("# Hello")
        result = scan_changed_files(tmp_path, ["readme.md"], "T001")
        assert result == {}

    def test_ignores_skip_dirs(self, tmp_path: Path) -> None:
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "lib.py").write_text("x = 1")
        result = scan_changed_files(tmp_path, [".venv/lib.py"], "T001")
        assert result == {}

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        result = scan_changed_files(tmp_path, ["nonexistent.py"], "T001")
        assert result == {}


def _make_state_with_n_modules(n: int) -> ProjectState:
    """Return a ProjectState with ``n`` synthetic modules.

    Each module has two functions so the full-detail lines are non-trivial
    in length, making truncation realistic.
    """
    from saturnday.project_state import FunctionState, ModuleState

    state = ProjectState()
    for i in range(n):
        ms = ModuleState(created_by=f"T{i:03d}")
        ms.functions[f"func_alpha_{i}"] = FunctionState(
            name=f"func_alpha_{i}",
            params=["self", "request", "context", "extra_param"],
            return_type="dict[str, Any]",
            line=10,
        )
        ms.functions[f"func_beta_{i}"] = FunctionState(
            name=f"func_beta_{i}",
            params=["self", "value"],
            return_type="bool",
            line=20,
        )
        state.modules[f"src/module_{i:03d}.py"] = ms
    return state


class TestGenerateContextSummary:
    def test_empty_state(self) -> None:
        state = ProjectState()
        summary = generate_context_summary(state)
        assert "DO NOT remove" in summary

    def test_includes_modules(self, tmp_path: Path) -> None:
        src = tmp_path / "foo.py"
        src.write_text("def bar() -> int:\n    return 1\n")
        state = ProjectState()
        state = update_state(state, "T001", ["foo.py"], tmp_path)
        summary = generate_context_summary(state)
        assert "foo.py" in summary
        assert "bar" in summary

    def test_max_chars_enforced(self) -> None:
        """100-module state must be capped at max_chars=8000."""
        state = _make_state_with_n_modules(100)
        result = generate_context_summary(state, max_chars=8000)
        assert len(result) <= 8000, (
            f"Expected <= 8000 chars, got {len(result)}"
        )

    def test_small_state_untruncated(self) -> None:
        """3-module state fits within 8000 chars; all names must appear."""
        state = _make_state_with_n_modules(3)
        result = generate_context_summary(state, max_chars=8000)
        for i in range(3):
            assert f"src/module_{i:03d}.py" in result, (
                f"module_{i:03d}.py missing from summary"
            )

    def test_truncation_message_present(self) -> None:
        """Truncated output must contain the 'and N more modules' message."""
        state = _make_state_with_n_modules(100)
        result = generate_context_summary(state, max_chars=8000)
        assert "more modules (truncated)" in result, (
            "Expected truncation message to be present"
        )


class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        state = ProjectState(project_id="test-proj")
        save_state(state, tmp_path)
        loaded = load_state(tmp_path)
        assert loaded is not None
        assert loaded.project_id == "test-proj"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert load_state(tmp_path) is None

    def test_load_corrupted_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "saturnday-state.json").write_text("not json")
        assert load_state(tmp_path) is None


class TestWriteContextFile:
    def test_write_context_file_creates_file(self, tmp_path: Path) -> None:
        """write_context_file creates the output file containing module names."""
        from saturnday.project_state import FunctionState, ModuleState

        state = ProjectState()
        ms = ModuleState(created_by="T001")
        ms.functions["do_thing"] = FunctionState(name="do_thing", params=["x"], return_type="None", line=1)
        state.modules["src/mymodule.py"] = ms

        out = tmp_path / "context.md"
        write_context_file(state, out)

        assert out.exists(), "context.md was not created"
        content = out.read_text(encoding="utf-8")
        assert "src/mymodule.py" in content
        assert "do_thing" in content

    def test_write_context_file_no_truncation(self, tmp_path: Path) -> None:
        """All 100 module names must appear in the file — no truncation applied."""
        state = _make_state_with_n_modules(100)

        out = tmp_path / "context.md"
        write_context_file(state, out)

        content = out.read_text(encoding="utf-8")
        for i in range(100):
            assert f"src/module_{i:03d}.py" in content, (
                f"src/module_{i:03d}.py missing from context file — truncation must not apply"
            )


class TestUpdateState:
    def test_tracks_functions(self, tmp_path: Path) -> None:
        (tmp_path / "mod.py").write_text("def greet(name: str) -> str:\n    return f'hi {name}'\n")
        state = ProjectState()
        state = update_state(state, "T001", ["mod.py"], tmp_path)
        assert "mod.py" in state.modules
        assert "greet" in state.modules["mod.py"].functions
        assert state.modules["mod.py"].created_by == "T001"

    def test_tracks_classes(self, tmp_path: Path) -> None:
        (tmp_path / "models.py").write_text(
            "class User:\n    def __init__(self):\n        pass\n    def save(self):\n        pass\n"
        )
        state = ProjectState()
        state = update_state(state, "T003", ["models.py"], tmp_path)
        assert "User" in state.modules["models.py"].classes
        assert "save" in state.modules["models.py"].classes["User"].methods

    def test_handles_deleted_file(self, tmp_path: Path) -> None:
        (tmp_path / "old.py").write_text("x = 1\n")
        state = ProjectState()
        state = update_state(state, "T001", ["old.py"], tmp_path)
        assert "old.py" in state.modules
        # Now delete it
        (tmp_path / "old.py").unlink()
        state = update_state(state, "T002", ["old.py"], tmp_path)
        assert "old.py" not in state.modules
