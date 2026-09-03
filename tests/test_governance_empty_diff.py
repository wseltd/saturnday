"""Tests for the governance empty-diff informational message."""
import subprocess
import tempfile
from pathlib import Path


def test_governance_empty_diff_shows_message(tmp_path):
    """When no files changed, governance prints an informational message."""
    # Create a repo with one commit
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    # Make another commit with no changes (empty)
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "noop"], check=True, capture_output=True)

    # Run governance — last commit has no file changes
    result = subprocess.run(
        ["python", "-m", "saturnday", "governance", "--repo", str(repo)],
        capture_output=True, text=True,
    )

    # Should contain the informational message
    combined = result.stdout + result.stderr
    assert "No changed files" in combined or "no checks" in combined.lower()


def test_governance_with_changes_no_message(tmp_path):
    """When files changed, governance does NOT print the empty-diff message."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "f.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    # Make a commit with real changes
    (repo / "f.py").write_text("x = 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "change"], check=True, capture_output=True)

    result = subprocess.run(
        ["python", "-m", "saturnday", "governance", "--repo", str(repo)],
        capture_output=True, text=True,
    )

    combined = result.stdout + result.stderr
    assert "No changed files" not in combined
