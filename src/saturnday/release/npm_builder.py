"""npm artefact builder wrapper for the release subpackage.

Wraps ``npm pack`` to produce a ``.tgz`` tarball from a target repository.
Parses ``npm pack --json`` for the file inventory metadata reported by npm
itself.  Returns a structured :class:`NpmBuildResult` with the tarball path,
npm's file list, build output, exit code, and elapsed time.

Build failures are returned as non-zero ``exit_code`` values — no exception
is raised, allowing callers to handle failures uniformly and write evidence
before exiting.  The only exception to this contract is a hard
:exc:`ValueError` raised before subprocess execution when preconditions on
*repo_path* cannot be met.

Usage::

    from pathlib import Path
    from saturnday.release.npm_builder import build_npm_artefact

    result = build_npm_artefact(repo_path=Path("."))
    if result.exit_code != 0:
        print("Build failed:", result.stderr)
    else:
        print("Tarball:", result.tarball_path)
        print("Files in pack:", result.npm_file_list)
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# npm writes the packed tarball into the current working directory by default.
# We run npm pack with ``--pack-destination`` (npm >= 7) or by using a temp
# cwd and moving the result.  We prefer --pack-destination where possible.
_NPM_PACK_DESTINATION_MIN_MAJOR = 7


@dataclass
class NpmBuildResult:
    """Result from an ``npm pack`` invocation.

    Attributes:
        tarball_path:  Path to the produced ``.tgz`` file, or ``None`` if the
                       build failed or no tarball was located.
        npm_file_list: File paths reported by ``npm pack --json``.  Each entry
                       is a relative path string as npm declared it (e.g.
                       ``"package.json"``, ``"dist/index.js"``).  Empty list if
                       ``--json`` parsing failed or if the build failed.
        pack_json:     Raw parsed JSON from ``npm pack --json`` (list with one
                       object element containing ``filename`` and ``files``).
                       ``None`` if parsing failed or build failed.
        stdout:        Combined standard output from the npm process.
        stderr:        Combined standard error from the npm process.
        exit_code:     Return code from ``npm pack``.  0 = success.
        elapsed_s:     Wall-clock seconds the build took.
    """

    tarball_path: Optional[Path]
    npm_file_list: list[str] = field(default_factory=list)
    pack_json: Optional[list[dict[str, Any]]] = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    elapsed_s: float = 0.0


def build_npm_artefact(
    repo_path: Path,
    output_dir: Optional[Path] = None,
) -> NpmBuildResult:
    """Build an npm tarball from *repo_path* using ``npm pack``.

    Invokes ``npm pack --json`` in the target directory.  The ``--json`` flag
    causes npm to emit a JSON array describing the packed artefact (filename
    and file list) on stdout in addition to producing the ``.tgz`` file.

    If *output_dir* is provided, ``--pack-destination`` is passed to npm so
    the tarball lands directly in that directory.  If *output_dir* is ``None``,
    a temporary directory is used and its path is the parent of
    :attr:`NpmBuildResult.tarball_path`.

    Args:
        repo_path:   Absolute or relative path to the npm project root.
                     Must contain a ``package.json``.
        output_dir:  Directory to write the produced ``.tgz`` into.  If
                     ``None``, a new temporary directory is created.  The
                     caller is responsible for cleanup in both cases.

    Returns:
        A :class:`NpmBuildResult`.  Always returns (never raises) so that
        callers can write evidence for failed builds.

    Raises:
        ValueError: If *repo_path* does not exist or is not a directory.
    """
    repo_path = Path(repo_path).resolve()

    if not repo_path.exists():
        raise ValueError(f"repo_path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")

    # ------------------------------------------------------------------
    # Verify package.json presence.
    # ------------------------------------------------------------------
    package_json_path = repo_path / "package.json"
    if not package_json_path.exists():
        logger.error("No package.json found in %s", repo_path)
        return NpmBuildResult(
            tarball_path=None,
            npm_file_list=[],
            pack_json=None,
            stdout="",
            stderr=(
                f"No package.json found in {repo_path}. "
                "Cannot run npm pack without a package.json."
            ),
            exit_code=1,
            elapsed_s=0.0,
        )

    # ------------------------------------------------------------------
    # Verify npm is installed.
    # ------------------------------------------------------------------
    npm_bin = shutil.which("npm")
    if npm_bin is None:
        logger.error("npm binary not found on PATH")
        return NpmBuildResult(
            tarball_path=None,
            npm_file_list=[],
            pack_json=None,
            stdout="",
            stderr=(
                "npm is not installed or not found on PATH. "
                "Install Node.js / npm to use the npm artefact builder."
            ),
            exit_code=1,
            elapsed_s=0.0,
        )

    # ------------------------------------------------------------------
    # Prepare output directory.
    # ------------------------------------------------------------------
    if output_dir is None:
        pack_dir = Path(tempfile.mkdtemp(prefix="saturnday-npm-"))
    else:
        pack_dir = Path(output_dir).resolve()
        pack_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Build the npm pack command.
    # ------------------------------------------------------------------
    cmd = [npm_bin, "pack", "--json", "--pack-destination", str(pack_dir)]

    logger.info(
        "Running npm pack: %s (cwd=%s, pack_destination=%s)",
        " ".join(cmd),
        repo_path,
        pack_dir,
    )

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=120,  # 2-minute timeout
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        logger.error("npm pack timed out after %.1fs for %s", elapsed, repo_path)
        return NpmBuildResult(
            tarball_path=None,
            npm_file_list=[],
            pack_json=None,
            stdout=exc.stdout or "",
            stderr=f"npm pack timed out after 120 seconds.\n{exc.stderr or ''}",
            exit_code=1,
            elapsed_s=elapsed,
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - t0
        logger.error("npm binary vanished during execution: %s", npm_bin)
        return NpmBuildResult(
            tarball_path=None,
            npm_file_list=[],
            pack_json=None,
            stdout="",
            stderr=f"npm binary not found at: {npm_bin}",
            exit_code=1,
            elapsed_s=elapsed,
        )

    elapsed = time.monotonic() - t0
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    if proc.returncode != 0:
        logger.error(
            "npm pack failed (exit=%d, elapsed=%.1fs): %s",
            proc.returncode,
            elapsed,
            stderr[:500],
        )
        return NpmBuildResult(
            tarball_path=None,
            npm_file_list=[],
            pack_json=None,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            elapsed_s=elapsed,
        )

    # ------------------------------------------------------------------
    # Parse npm pack --json output.
    # ------------------------------------------------------------------
    pack_json: Optional[list[dict[str, Any]]] = None
    npm_file_list: list[str] = []

    try:
        pack_json = json.loads(stdout)
        if isinstance(pack_json, list) and pack_json:
            entry = pack_json[0]
            raw_files = entry.get("files", [])
            npm_file_list = [
                f["path"] for f in raw_files if isinstance(f, dict) and "path" in f
            ]
            logger.debug(
                "npm pack --json reported %d files in the tarball",
                len(npm_file_list),
            )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "Could not parse npm pack --json output: %s — continuing without file list",
            exc,
        )
        pack_json = None
        npm_file_list = []

    # ------------------------------------------------------------------
    # Locate the produced tarball.
    # ------------------------------------------------------------------
    tarball_path: Optional[Path] = None

    # Prefer the filename declared by npm in its JSON output.
    if pack_json and isinstance(pack_json, list) and pack_json:
        declared_filename = pack_json[0].get("filename")
        if declared_filename:
            candidate = pack_dir / declared_filename
            if candidate.exists():
                tarball_path = candidate
            else:
                logger.warning(
                    "npm declared tarball filename %r but it does not exist at %s",
                    declared_filename,
                    candidate,
                )

    # Fall back to globbing for any .tgz in the pack_dir.
    if tarball_path is None:
        tarball_path = _find_tarball(pack_dir)

    if tarball_path is None:
        logger.warning(
            "npm pack succeeded but no .tgz file found in %s", pack_dir
        )
    else:
        logger.info(
            "npm pack complete in %.1fs: tarball=%s (%d files)",
            elapsed,
            tarball_path,
            len(npm_file_list),
        )

    return NpmBuildResult(
        tarball_path=tarball_path,
        npm_file_list=npm_file_list,
        pack_json=pack_json,
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_tarball(directory: Path) -> Optional[Path]:
    """Return the first ``.tgz`` file in *directory*, or ``None``.

    If multiple tarballs are found, the lexicographically first is returned
    and a warning is logged.

    Args:
        directory: Directory to search (non-recursive).

    Returns:
        Matching :class:`~pathlib.Path`, or ``None`` if not found.
    """
    matches = sorted(directory.glob("*.tgz"))
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple .tgz files found in %s: %s — using first",
            directory,
            matches,
        )
    return matches[0]


__all__ = ["NpmBuildResult", "build_npm_artefact"]
