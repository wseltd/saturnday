"""Closure-proof tests for F — bounded monorepo / subroot inference.

Scope bounded to the frontend/backend split case the evaluator reported.
Release preflight auto-detection across multiple workspaces remains out
of scope (operators target a subroot with --repo today).

Proven surfaces:
  * saturnday.workspaces.detect_workspaces  (the detector contract)
  * review._check_project_runnable          (governance no longer falsely fails)
  * run/planner._scan_repo_layout           (planner context reflects real roots)
  * ticket_runner._auto_install_deps        (install runs in the right workspace)
  * single-root safety                      (identical behaviour for root-only repos)
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# detect_workspaces contract
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)


def _make_monorepo(tmp_path: Path) -> Path:
    """frontend/ (package.json + tsconfig.json) + backend/ (pyproject.toml)."""
    repo = tmp_path / "monorepo"
    _init_git_repo(repo)
    (repo / "frontend").mkdir()
    (repo / "frontend" / "package.json").write_text(
        '{"name":"frontend","version":"0.0.1"}', encoding="utf-8"
    )
    (repo / "frontend" / "tsconfig.json").write_text("{}", encoding="utf-8")
    (repo / "frontend" / "src").mkdir()
    (repo / "frontend" / "src" / "app.ts").write_text(
        "export const x: number = 1;\n", encoding="utf-8"
    )
    (repo / "backend").mkdir()
    (repo / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "backend"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n',
        encoding="utf-8",
    )
    (repo / "backend" / "app").mkdir()
    (repo / "backend" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "backend" / "app" / "main.py").write_text(
        "def ok(): return 1\n", encoding="utf-8"
    )
    return repo


def _make_single_root_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "single-root"
    _init_git_repo(repo)
    (repo / "pyproject.toml").write_text(
        '[project]\nname="p"\nversion="0.1.0"\nrequires-python=">=3.10"\n',
        encoding="utf-8",
    )
    (repo / "app").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "main.py").write_text("def ok(): return 1\n", encoding="utf-8")
    return repo


class TestDetectWorkspaces:
    def test_detects_frontend_and_backend_subroots(self, tmp_path: Path) -> None:
        from saturnday.workspaces import detect_workspaces
        repo = _make_monorepo(tmp_path)
        workspaces = detect_workspaces(repo)
        kinds = {(w.path.name, w.kind) for w in workspaces}
        assert ("frontend", "node") in kinds
        assert ("backend", "python") in kinds

    def test_single_root_repo_returns_root_workspace(self, tmp_path: Path) -> None:
        from saturnday.workspaces import detect_workspaces
        repo = _make_single_root_repo(tmp_path)
        workspaces = detect_workspaces(repo)
        assert len(workspaces) == 1
        assert workspaces[0].path == repo
        assert workspaces[0].kind == "python"

    def test_skips_vendored_trees(self, tmp_path: Path) -> None:
        """package.json inside node_modules MUST NOT be returned."""
        from saturnday.workspaces import detect_workspaces
        repo = tmp_path / "r"
        _init_git_repo(repo)
        (repo / "package.json").write_text('{"name":"root"}', encoding="utf-8")
        nm = repo / "node_modules" / "some-pkg"
        nm.mkdir(parents=True)
        (nm / "package.json").write_text('{"name":"vendored"}', encoding="utf-8")
        workspaces = detect_workspaces(repo)
        paths = {str(w.path) for w in workspaces}
        assert str(repo) in paths
        assert not any("node_modules" in p for p in paths)

    def test_respects_two_level_depth_cap(self, tmp_path: Path) -> None:
        """A package.json 3+ levels deep must NOT be reported as a workspace."""
        from saturnday.workspaces import detect_workspaces
        repo = tmp_path / "r"
        _init_git_repo(repo)
        deep = repo / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "package.json").write_text('{"name":"too-deep"}', encoding="utf-8")
        workspaces = detect_workspaces(repo)
        assert not any("too-deep" in str(w.path) or w.path == deep for w in workspaces)

    def test_workspace_for_file_picks_deepest_match(self, tmp_path: Path) -> None:
        """A file under backend/ resolves to the backend workspace, not root."""
        from saturnday.workspaces import workspace_for_file
        repo = _make_monorepo(tmp_path)
        ws = workspace_for_file(repo, "backend/pyproject.toml")
        assert ws is not None
        assert ws.path == repo / "backend"
        assert ws.kind == "python"

    def test_workspace_for_file_returns_none_when_outside(self, tmp_path: Path) -> None:
        from saturnday.workspaces import workspace_for_file
        repo = _make_monorepo(tmp_path)
        ws = workspace_for_file(repo, "docs/README.md")
        assert ws is None  # docs/ has no workspace markers


# ---------------------------------------------------------------------------
# review._check_project_runnable — governance behaviour
# ---------------------------------------------------------------------------


class TestProjectRunnableMonorepo:
    def test_monorepo_with_ts_and_py_passes_when_subroots_have_configs(
        self, tmp_path: Path
    ) -> None:
        from saturnday.review import _check_project_runnable
        repo = _make_monorepo(tmp_path)
        # Simulate a run where changed files span both subroots.
        changed = ["frontend/src/app.ts", "backend/app/main.py"]
        result = _check_project_runnable(repo, changed)
        assert result["status"] == "PASS", result["findings"]

    def test_monorepo_without_backend_config_fails(self, tmp_path: Path) -> None:
        """Monorepo where backend/ has py files but no pyproject.toml must
        still fail — the fix doesn't become toothless."""
        from saturnday.review import _check_project_runnable
        repo = tmp_path / "half-broken"
        _init_git_repo(repo)
        (repo / "frontend").mkdir()
        (repo / "frontend" / "package.json").write_text(
            '{"name":"frontend"}', encoding="utf-8"
        )
        (repo / "backend").mkdir()
        (repo / "backend" / "main.py").write_text(
            "def ok(): return 1\n", encoding="utf-8"
        )
        result = _check_project_runnable(
            repo, ["frontend/src/x.ts", "backend/main.py"]
        )
        assert result["status"] == "FAIL"
        kinds = {f["kind"] for f in result["findings"]}
        assert "missing_project_config" in kinds

    def test_single_root_python_without_config_still_fails(self, tmp_path: Path) -> None:
        """Pre-F single-root behaviour preserved."""
        from saturnday.review import _check_project_runnable
        repo = tmp_path / "no-config"
        _init_git_repo(repo)
        (repo / "main.py").write_text("def ok(): return 1\n", encoding="utf-8")
        result = _check_project_runnable(repo, ["main.py"])
        assert result["status"] == "FAIL"
        kinds = {f["kind"] for f in result["findings"]}
        assert "missing_project_config" in kinds

    def test_single_root_python_with_pyproject_passes(self, tmp_path: Path) -> None:
        """Pre-F single-root PASS behaviour preserved."""
        from saturnday.review import _check_project_runnable
        repo = _make_single_root_repo(tmp_path)
        result = _check_project_runnable(repo, ["app/main.py"])
        assert result["status"] == "PASS"

    def test_ts_with_subroot_tsconfig_passes(self, tmp_path: Path) -> None:
        from saturnday.review import _check_project_runnable
        repo = _make_monorepo(tmp_path)
        result = _check_project_runnable(repo, ["frontend/src/app.ts"])
        assert result["status"] == "PASS"

    def test_ts_without_tsconfig_at_root_or_subroot_still_fails(
        self, tmp_path: Path
    ) -> None:
        from saturnday.review import _check_project_runnable
        repo = tmp_path / "ts-no-tsconfig"
        _init_git_repo(repo)
        (repo / "frontend").mkdir()
        (repo / "frontend" / "package.json").write_text(
            '{"name":"frontend"}', encoding="utf-8"
        )
        result = _check_project_runnable(repo, ["frontend/src/x.ts"])
        kinds = {f["kind"] for f in result["findings"]}
        assert "missing_tsconfig" in kinds


# ---------------------------------------------------------------------------
# run/planner._scan_repo_layout — planner context
# ---------------------------------------------------------------------------


class TestPlannerLayoutContext:
    def test_monorepo_layout_surfaces_both_subroots(self, tmp_path: Path) -> None:
        from saturnday.run.planner import _scan_repo_layout
        repo = _make_monorepo(tmp_path)
        context = _scan_repo_layout(repo)
        assert "Workspaces:" in context
        assert "frontend" in context and "Node.js" in context
        assert "backend" in context and "Python" in context

    def test_monorepo_layout_does_not_imply_top_level_missing_configs(
        self, tmp_path: Path
    ) -> None:
        """Regression proof: the planner prompt historically said
        "Framework: Node.js" only when root had package.json — an LLM
        could infer "no framework → create one at root."  Under F the
        prompt explicitly shows the subroot locations so that inference
        doesn't happen."""
        from saturnday.run.planner import _scan_repo_layout
        repo = _make_monorepo(tmp_path)
        context = _scan_repo_layout(repo)
        # The workspace labels must include the subroot relative paths.
        assert "frontend (Node.js" in context
        assert "backend (Python" in context

    def test_single_root_layout_unchanged(self, tmp_path: Path) -> None:
        from saturnday.run.planner import _scan_repo_layout
        repo = _make_single_root_repo(tmp_path)
        context = _scan_repo_layout(repo)
        # Single-root repo still appears as a "." workspace.
        assert "Workspaces:" in context
        assert ". (Python" in context

    def test_empty_repo_is_handled_gracefully(self, tmp_path: Path) -> None:
        from saturnday.run.planner import _scan_repo_layout
        repo = tmp_path / "empty"
        _init_git_repo(repo)
        context = _scan_repo_layout(repo)
        # No workspaces and no languages — but no crash either.
        assert context  # Some text returned.


# ---------------------------------------------------------------------------
# ticket_runner._auto_install_deps — install in the right workspace
# ---------------------------------------------------------------------------


class TestAutoInstallDepsMonorepo:
    def test_frontend_package_json_triggers_npm_install_in_frontend(
        self, tmp_path: Path
    ) -> None:
        from saturnday.ticket_runner import _auto_install_deps

        repo = _make_monorepo(tmp_path)
        subprocess_calls: list[dict] = []

        def _fake_run(*args, **kwargs):
            cmd = args[0]
            subprocess_calls.append({
                "cmd": cmd,
                "cwd": kwargs.get("cwd"),
            })
            class _R:
                returncode = 0
                stderr = ""
                stdout = ""
            return _R()

        with patch("saturnday.ticket_runner.subprocess.run", side_effect=_fake_run):
            _auto_install_deps(repo, ["frontend/package.json"])

        # At least one call must be npm install with cwd=frontend subdir.
        npm_calls = [c for c in subprocess_calls if c["cmd"][0] == "npm"]
        assert npm_calls, f"npm install never invoked: {subprocess_calls}"
        assert npm_calls[0]["cwd"] == str(repo / "frontend"), (
            f"npm install ran in wrong dir: {npm_calls[0]['cwd']}"
        )

    def test_backend_pyproject_triggers_pip_install_in_backend(
        self, tmp_path: Path
    ) -> None:
        from saturnday.ticket_runner import _auto_install_deps

        repo = _make_monorepo(tmp_path)
        subprocess_calls: list[dict] = []

        def _fake_run(*args, **kwargs):
            cmd = args[0]
            subprocess_calls.append({
                "cmd": cmd,
                "cwd": kwargs.get("cwd"),
            })
            class _R:
                returncode = 0
                stderr = ""
                stdout = ""
            return _R()

        with patch("saturnday.ticket_runner.subprocess.run", side_effect=_fake_run):
            _auto_install_deps(repo, ["backend/pyproject.toml"])

        pip_calls = [
            c for c in subprocess_calls
            if len(c["cmd"]) >= 3 and c["cmd"][1] == "install"
        ]
        assert pip_calls, f"pip install never invoked: {subprocess_calls}"
        # First call must be pip install -e . in backend/
        assert pip_calls[0]["cwd"] == str(repo / "backend")

    def test_both_dep_files_changed_installs_both_workspaces(
        self, tmp_path: Path
    ) -> None:
        from saturnday.ticket_runner import _auto_install_deps

        repo = _make_monorepo(tmp_path)
        cwds: list[str] = []

        def _fake_run(*args, **kwargs):
            cwds.append(kwargs.get("cwd"))
            class _R:
                returncode = 0
                stderr = ""
                stdout = ""
            return _R()

        with patch("saturnday.ticket_runner.subprocess.run", side_effect=_fake_run):
            _auto_install_deps(
                repo,
                ["frontend/package.json", "backend/pyproject.toml"],
            )

        # Both subroot paths must show up.
        assert str(repo / "frontend") in cwds
        assert str(repo / "backend") in cwds

    def test_single_root_repo_install_still_runs_at_root(
        self, tmp_path: Path
    ) -> None:
        """Back-compat: a single-root repo's auto-install behaves as before."""
        from saturnday.ticket_runner import _auto_install_deps

        repo = _make_single_root_repo(tmp_path)
        cwds: list[str] = []

        def _fake_run(*args, **kwargs):
            cwds.append(kwargs.get("cwd"))
            class _R:
                returncode = 0
                stderr = ""
                stdout = ""
            return _R()

        with patch("saturnday.ticket_runner.subprocess.run", side_effect=_fake_run):
            _auto_install_deps(repo, ["pyproject.toml"])

        # Install ran in the repo root, not a subdir.
        assert str(repo) in cwds

    def test_unchanged_dep_files_do_not_trigger_install(self, tmp_path: Path) -> None:
        """If no dependency files changed, no install call fires."""
        from saturnday.ticket_runner import _auto_install_deps

        repo = _make_monorepo(tmp_path)
        calls: list[list[str]] = []

        def _fake_run(*args, **kwargs):
            calls.append(args[0])
            class _R:
                returncode = 0
                stderr = ""
                stdout = ""
            return _R()

        with patch("saturnday.ticket_runner.subprocess.run", side_effect=_fake_run):
            _auto_install_deps(repo, ["backend/app/main.py", "frontend/src/app.ts"])

        assert calls == []
