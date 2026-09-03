"""Integration test for the cloud-core -> saturnday governance seam.

Validates the REAL cross-package call: ``ticket_runner._run_governance``
invokes ``saturnday.governance.run_governance_check`` and correctly unpacks
the ``(EvidencePack, Path)`` return value into ``(disposition, findings, evidence_path)``.

Requires the ``saturnday`` package to be installed.  All tests in this module
are skipped automatically when it is not available.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

saturnday = pytest.importorskip("saturnday")


def _create_git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with a single empty initial commit.

    A real git repo is required because ``run_governance_check`` runs
    ``git diff`` internally.
    """
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )
    # Add standard project files so readme/license/project_runnable checks pass
    (repo / "README.md").write_text("# Test\n## Installation\n## Usage\n## Trade-Offs\n## Limitations\n## Non-Goals\n")
    (repo / "LICENSE").write_text("MIT License\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "test"\nversion = "0.1.0"\n')
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md", "LICENSE", "pyproject.toml"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add project files"],
        check=True, capture_output=True,
    )
    return repo


@pytest.mark.integration
class TestGovernanceIntegration:
    """Validate the real cross-package governance call at the cloud-core seam."""

    def test_governance_returns_tuple(self, tmp_path: Path) -> None:
        """_run_governance returns a 3-tuple (disposition, findings, evidence_path)."""
        from saturnday.ticket_runner import _run_governance

        repo = _create_git_repo(tmp_path)

        # Write a file that contains a known governance violation: hardcoded JWT secret.
        vuln_file = repo / "auth.py"
        vuln_file.write_text(
            'JWT_SECRET = "hardcoded-secret-key-do-not-use-in-production"\n'
            "import jwt\n"
            'token = jwt.encode({"user": "admin"}, JWT_SECRET, algorithm="HS256")\n',
            encoding="utf-8",
        )

        subprocess.run(
            ["git", "-C", str(repo), "add", "auth.py"],
            check=True,
            capture_output=True,
        )

        result = _run_governance(repo)

        assert isinstance(result, tuple), "Expected a tuple from _run_governance"
        assert len(result) == 4, (
            "Expected exactly 4 elements: "
            "(disposition, findings, evidence_path, disposition_reasons)"
        )

        disposition, findings, evidence_path, _reasons = result

        assert isinstance(disposition, str), "disposition must be a str"
        assert disposition in ("PASS", "FAIL", "WARN"), (
            f"disposition must be PASS, FAIL, or WARN; got {disposition!r}"
        )
        assert isinstance(findings, list), "findings must be a list"
        assert isinstance(evidence_path, str), "evidence_path must be a str"

    def test_governance_detects_violation(self, tmp_path: Path) -> None:
        """A staged file with a hardcoded JWT secret triggers governance FAIL."""
        from saturnday.ticket_runner import _run_governance

        repo = _create_git_repo(tmp_path)

        vuln_file = repo / "secrets.py"
        vuln_file.write_text(
            'SECRET_KEY = "super-secret-jwt-signing-key-12345"\n'
            "import jwt\n"
            "def make_token(user_id):\n"
            '    return jwt.encode({"sub": user_id}, SECRET_KEY)\n',
            encoding="utf-8",
        )

        subprocess.run(
            ["git", "-C", str(repo), "add", "secrets.py"],
            check=True,
            capture_output=True,
        )

        disposition, findings, evidence_path, _reasons = _run_governance(repo)

        assert disposition == "FAIL", (
            f"Expected FAIL for hardcoded JWT secret, got {disposition!r}"
        )
        assert len(findings) > 0, "Expected at least one finding for the hardcoded secret"

    def test_clean_file_passes(self, tmp_path: Path) -> None:
        """A staged file with no violations passes governance."""
        from saturnday.ticket_runner import _run_governance

        repo = _create_git_repo(tmp_path)

        # Add project-level files so those checks pass
        (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        (repo / "README.md").write_text(
            "# Test\n\n## Trade-offs\nNone.\n\n## Limitations\nNone.\n\n## Non-goals\nNone.\n",
            encoding="utf-8",
        )
        (repo / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n'
            '[project]\nname = "test"\nversion = "0.1.0"\n\n'
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8",
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "test_utils.py").write_text(
            "from utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )

        # Use __all__ to avoid dead_code detection
        clean_file = repo / "utils.py"
        clean_file.write_text(
            '__all__ = ["add"]\n\n\n'
            "def add(a: int, b: int) -> int:\n"
            '    """Add two numbers."""\n'
            "    return a + b\n",
            encoding="utf-8",
        )

        subprocess.run(
            ["git", "-C", str(repo), "add", "."],
            check=True,
            capture_output=True,
        )

        disposition, findings, evidence_path, _reasons = _run_governance(repo)

        assert disposition in ("PASS", "WARN"), (
            f"Expected PASS or WARN for clean file, got {disposition!r}"
        )
