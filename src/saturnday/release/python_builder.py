"""Python artefact builder wrapper for the release subpackage.

Wraps ``python -m build --wheel --sdist`` to produce a wheel and an sdist
from a target repository.  Uses subprocess only (no importlib).  Returns a
structured :class:`BuildResult` with paths to the built artefacts and full
build output.

Build failures are returned as non-zero ``exit_code`` values — no exception
is raised, allowing callers to handle failures uniformly and write evidence
before exiting.

Usage::

    from pathlib import Path
    from saturnday.release.python_builder import build_python_artefacts

    result = build_python_artefacts(repo_path=Path("."))
    if result.exit_code != 0:
        print("Build failed:", result.stderr)
    else:
        print("Wheel:", result.wheel_path)
        print("Sdist:", result.sdist_path)
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    """Result from a ``python -m build`` invocation.

    Attributes:
        wheel_path: Path to the built ``.whl`` file, or ``None`` if the
                    build failed or the wheel was not produced.
        sdist_path: Path to the built ``.tar.gz`` sdist file, or ``None``
                    if the build failed or the sdist was not produced.
        build_dir:  Path to the temporary directory used as the build output
                    directory (``--outdir``).  The caller is responsible for
                    cleaning it up when no longer needed.
        stdout:     Combined standard output from the build process.
        stderr:     Combined standard error from the build process.
        exit_code:  Return code from ``python -m build``.  0 = success.
        elapsed_s:  Wall-clock seconds the build took.
    """

    wheel_path: Optional[Path]
    sdist_path: Optional[Path]
    build_dir: Path
    stdout: str
    stderr: str
    exit_code: int
    elapsed_s: float


def build_python_artefacts(
    repo_path: Path,
    output_dir: Optional[Path] = None,
) -> BuildResult:
    """Build a Python wheel and sdist from *repo_path*.

    Invokes ``python -m build --wheel --sdist --outdir <output_dir>`` in a
    subprocess.  If *output_dir* is ``None``, a temporary directory is
    created automatically and its path is returned in
    :attr:`BuildResult.build_dir`.

    Does NOT install build dependencies — assumes the ``build`` package is
    already available in the current environment.  If ``build`` is missing,
    the subprocess will fail and the error message will be present in
    ``BuildResult.stderr``.

    Args:
        repo_path:  Absolute or relative path to the target repository root.
                    Must contain a ``pyproject.toml`` or ``setup.py``.
        output_dir: Directory to write built artefacts into.  If ``None``, a
                    new temporary directory is created.  The caller is
                    responsible for cleanup.

    Returns:
        A :class:`BuildResult`.  Always returns (never raises) so that
        callers can write evidence for failed builds.

    Raises:
        ValueError: If *repo_path* does not exist or is not a directory.
    """
    repo_path = Path(repo_path).resolve()

    if not repo_path.exists():
        raise ValueError(f"repo_path does not exist: {repo_path}")
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")

    # Validate that there is a recognisable Python project file present.
    has_pyproject = (repo_path / "pyproject.toml").exists()
    has_setup_py = (repo_path / "setup.py").exists()
    has_setup_cfg = (repo_path / "setup.cfg").exists()
    if not (has_pyproject or has_setup_py or has_setup_cfg):
        # Return a failure result rather than raising — lets callers handle it.
        logger.error(
            "No pyproject.toml, setup.py, or setup.cfg found in %s", repo_path
        )
        build_dir = Path(tempfile.mkdtemp(prefix="saturnday-build-"))
        return BuildResult(
            wheel_path=None,
            sdist_path=None,
            build_dir=build_dir,
            stdout="",
            stderr=(
                f"No pyproject.toml, setup.py, or setup.cfg found in {repo_path}. "
                "Cannot build Python artefacts."
            ),
            exit_code=1,
            elapsed_s=0.0,
        )

    if output_dir is None:
        build_dir = Path(tempfile.mkdtemp(prefix="saturnday-build-"))
    else:
        build_dir = Path(output_dir).resolve()
        build_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--sdist",
        "--outdir",
        str(build_dir),
        str(repo_path),
    ]

    logger.info(
        "Running build: %s (cwd=%s, outdir=%s)",
        " ".join(cmd),
        repo_path,
        build_dir,
    )

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5-minute timeout for slow builds
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        logger.error("Build timed out after %.1fs for %s", elapsed, repo_path)
        return BuildResult(
            wheel_path=None,
            sdist_path=None,
            build_dir=build_dir,
            stdout=exc.stdout or "",
            stderr=f"Build timed out after 300 seconds.\n{exc.stderr or ''}",
            exit_code=1,
            elapsed_s=elapsed,
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - t0
        logger.error("Python interpreter not found: %s", sys.executable)
        return BuildResult(
            wheel_path=None,
            sdist_path=None,
            build_dir=build_dir,
            stdout="",
            stderr=f"Python interpreter not found: {sys.executable}",
            exit_code=1,
            elapsed_s=elapsed,
        )

    elapsed = time.monotonic() - t0
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    if proc.returncode != 0:
        logger.error(
            "Build failed (exit=%d, elapsed=%.1fs): %s",
            proc.returncode,
            elapsed,
            stderr[:500],
        )
        return BuildResult(
            wheel_path=None,
            sdist_path=None,
            build_dir=build_dir,
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode,
            elapsed_s=elapsed,
        )

    # Locate wheel and sdist from the output directory.
    wheel_path = _find_file(build_dir, "*.whl")
    sdist_path = _find_file(build_dir, "*.tar.gz")

    if wheel_path is None:
        logger.warning(
            "Build succeeded but no .whl file found in %s", build_dir
        )
    if sdist_path is None:
        logger.warning(
            "Build succeeded but no .tar.gz file found in %s", build_dir
        )

    logger.info(
        "Build complete in %.1fs: wheel=%s, sdist=%s",
        elapsed,
        wheel_path,
        sdist_path,
    )

    return BuildResult(
        wheel_path=wheel_path,
        sdist_path=sdist_path,
        build_dir=build_dir,
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode,
        elapsed_s=elapsed,
    )


def _find_file(directory: Path, pattern: str) -> Optional[Path]:
    """Return the first file matching *pattern* in *directory*, or ``None``.

    If multiple files match, the lexicographically first path is returned
    and a warning is logged.

    Args:
        directory: Directory to search (non-recursive).
        pattern:   Glob pattern relative to *directory*.

    Returns:
        Matching :class:`~pathlib.Path`, or ``None`` if not found.
    """
    matches = sorted(directory.glob(pattern))
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple files match %s in %s: %s — using first",
            pattern,
            directory,
            matches,
        )
    return matches[0]


__all__ = ["BuildResult", "build_python_artefacts"]
