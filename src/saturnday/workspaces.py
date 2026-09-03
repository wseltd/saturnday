"""Bounded monorepo / subroot inference.

Saturnday's planner, governance, and auto-install paths historically
treated the repo root as the only project root.  On real codebases with
a ``frontend/`` + ``backend/`` split (or any single-subdir layout) this
produced three adoption-blocking symptoms:

  * planner hallucinated missing top-level ``package.json`` / ``pyproject.toml``
  * governance ``project_runnable`` failed on valid nested projects
  * auto-install silently skipped nested dependency files

This module provides one canonical detector used by all three consumers.
It is deliberately **bounded**: no support for pnpm/yarn workspace
manifests, no recursive traversal beyond 2 levels, no build-graph
awareness.  It closes the specific adoption blocker — nothing more.

Contract
========

    from saturnday.workspaces import detect_workspaces, Workspace

    workspaces = detect_workspaces(repo_path)
    # [Workspace(path=<repo>/frontend, kind="node"),
    #  Workspace(path=<repo>/backend,  kind="python")]

When a repo has a single root marker and no subroots, the list contains
exactly one entry pointing at the repo root — so single-root callers see
identical behaviour to pre-F code.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Hard limits — keep bounded.  Two levels catches the common
# ``frontend/`` / ``backend/`` / ``services/<name>/`` patterns without
# indexing every vendored ``node_modules/**/package.json``.
_MAX_DEPTH = 2
_MAX_WORKSPACES = 20

# Directory names we never descend into — vendored trees, build outputs,
# Saturnday's own state, Python + Node venvs.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules",
    ".venv", "venv", ".env",
    "__pycache__",
    "dist", "build", "out", "target",
    ".saturnday", ".saturnday-evidence",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".next", ".nuxt", ".svelte-kit",
    ".idea", ".vscode",
    ".tox", ".nox",
})


# Marker files that declare a workspace, keyed by the kind they produce.
# Multiple markers for the same kind mean "any of these".
_PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")
_NODE_MARKERS = ("package.json",)


@dataclass(frozen=True)
class Workspace:
    """One detected project root inside the repository.

    Attributes:
        path: Absolute path to the workspace directory.
        kind: ``"python"`` or ``"node"``.
        marker: Filename that identified the workspace (``"pyproject.toml"``,
            ``"package.json"``, etc.).  Useful for debug prints and for
            downstream tools deciding how to install dependencies.
    """

    path: Path
    kind: str
    marker: str


def _has_any(dir_path: Path, names: tuple[str, ...]) -> str | None:
    """Return the first marker name that exists in ``dir_path``, else None."""
    for name in names:
        if (dir_path / name).is_file():
            return name
    return None


def detect_workspaces(repo_path: Path) -> list[Workspace]:
    """Detect bounded python/node workspaces inside ``repo_path``.

    Rules:
      * The repo root itself is a workspace if a root-level marker exists.
      * Any directory up to 2 levels below the root with a marker file is a
        workspace.
      * Directories in :data:`_SKIP_DIRS` are never descended.
      * Capped at :data:`_MAX_WORKSPACES` to avoid runaway detection on
        repos with hundreds of nested ``package.json`` files (e.g. a
        vendored ``node_modules`` tree that somehow escaped the skip list).

    The returned list is sorted by ``path`` for determinism.  When the
    repo has a single top-level root and no subroots, the list contains
    exactly one entry pointing at the root — single-root callers see
    identical behaviour to the pre-F code path.
    """
    repo_path = Path(repo_path).resolve()
    found: list[Workspace] = []

    def _scan(dir_path: Path, depth: int) -> None:
        if len(found) >= _MAX_WORKSPACES:
            return
        py_marker = _has_any(dir_path, _PYTHON_MARKERS)
        if py_marker:
            found.append(Workspace(path=dir_path, kind="python", marker=py_marker))
        node_marker = _has_any(dir_path, _NODE_MARKERS)
        if node_marker:
            found.append(Workspace(path=dir_path, kind="node", marker=node_marker))
        if depth >= _MAX_DEPTH:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        for entry in entries:
            if len(found) >= _MAX_WORKSPACES:
                return
            if not entry.is_dir():
                continue
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            _scan(entry, depth + 1)

    _scan(repo_path, depth=0)
    # Deterministic order: by path, then kind (python before node).
    found.sort(key=lambda w: (str(w.path), 0 if w.kind == "python" else 1))
    return found


def workspaces_by_kind(
    repo_path: Path, kind: str,
) -> list[Workspace]:
    """Convenience: return only workspaces of the given kind."""
    return [w for w in detect_workspaces(repo_path) if w.kind == kind]


def workspace_for_file(
    repo_path: Path, file_path: str,
) -> Workspace | None:
    """Given a repo-relative ``file_path``, return the workspace that
    contains it (walks up through detected workspaces by directory
    ancestry).  Returns ``None`` if no workspace contains the file.

    Used by the dependency auto-installer so a changed
    ``frontend/package.json`` resolves to the ``frontend`` workspace and
    ``npm install`` runs in that directory, not the repo root.
    """
    repo_path = Path(repo_path).resolve()
    rel = Path(file_path)
    if rel.is_absolute():
        try:
            rel = rel.relative_to(repo_path)
        except ValueError:
            return None
    abs_file = (repo_path / rel).resolve()
    workspaces = detect_workspaces(repo_path)
    # Pick the DEEPEST workspace that is an ancestor of the file.  This
    # handles the case where both the root and a subdir are workspaces
    # — a file under ``backend/`` should resolve to ``backend`` even if
    # the root is also a workspace.
    best: Workspace | None = None
    best_depth = -1
    for ws in workspaces:
        try:
            abs_file.relative_to(ws.path)
        except ValueError:
            continue
        depth = len(ws.path.parts)
        if depth > best_depth:
            best = ws
            best_depth = depth
    return best


__all__ = [
    "Workspace",
    "detect_workspaces",
    "workspaces_by_kind",
    "workspace_for_file",
]
