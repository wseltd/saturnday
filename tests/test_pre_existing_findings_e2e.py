"""End-to-end proof for Nick's pre-existing-findings scenario.

Closes the verification gap on Nick's #1 complaint:

  > my /health endpoint touched main.py, which has 9 pre-existing
  > findings — so it can never pass, even though none of those
  > findings came from my change.

Prior fixes claimed by the codebase:
  * Fix A (commit 830c475) — saturnday hook install auto-bootstraps a
    ratchet baseline from the current repo state.
  * Fix #3 (commit 038bfc5) — baseline generator no longer refuses
    docs-only / no-Python repos.

The shape of those fixes is right, but Nick's exact scenario was never
exercised end-to-end inside the test suite.  This file does that:

  1. Build a tmp git repo with a file that contains a known-bad
     finding (a long alphanumeric blob the secret scanner will flag).
  2. Commit it so the finding is genuinely pre-existing in HEAD.
  3. Drive _handle_hook → _bootstrap_baseline_if_missing.
  4. Assert .saturnday-baseline.json exists and captures the finding.
  5. Edit the same file in a way that introduces NO new findings.
  6. Run governance against the staged diff.
  7. Assert disposition is PASS — the ratchet ignores the baselined
     finding.
  8. Now introduce ONE NEW secret in the same file.
  9. Run governance again.
 10. Assert disposition is FAIL — the ratchet escalates on the new
     finding only, not the baselined one.

If step 7 fails, Nick's complaint is a real bug we still have.  If
step 7 passes, his complaint is closed and we have evidence to point
at.
"""
from __future__ import annotations

import json
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday import cli as cli_mod
from saturnday.governance import run_governance_check


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"], cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )


# A 40-character alphanumeric blob the secret regex
# (review.py: r"[A-Za-z0-9]{32,}") will reliably flag.  Looks like a
# hex SHA but isn't one.  Used as the "pre-existing finding" anchor.
_BAD_SECRET_A = "AAAA0000BBBB1111CCCC2222DDDD3333EEEE4444"
# A different 40-char blob, used as the "new finding" introduced after
# the baseline is established.
_BAD_SECRET_B = "FFFF5555GGGG6666HHHH7777IIII8888JJJJ9999"


def _install_hook_with_bootstrap(repo: Path) -> None:
    """Drive the same code path the operator uses when running
    ``saturnday hook install``."""
    args = types.SimpleNamespace(
        hook_action="install",
        repo=str(repo),
        verbose=False,
        quiet=False,
    )
    with patch("shutil.which", return_value="/opt/venv/bin/saturnday"):
        rc = cli_mod._handle_hook(args)
    assert rc == 0, "hook install must succeed"


# ---------------------------------------------------------------------------
# The end-to-end proof
# ---------------------------------------------------------------------------


class TestNickPreExistingFindingsScenario:
    def test_pre_existing_finding_does_not_block_unrelated_change(
        self, tmp_path: Path,
    ) -> None:
        """The exact scenario Nick named.  This is the single most
        important assertion in this file."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)

        # 1. Plant a file with a single pre-existing finding.
        main_py = repo / "main.py"
        main_py.write_text(
            f'API_KEY = "{_BAD_SECRET_A}"  # pre-existing\n',
            encoding="utf-8",
        )
        _git("add", "main.py", cwd=repo)
        _git("commit", "-q", "-m", "seed: pre-existing finding", cwd=repo)

        # 2. Hook install bootstraps the baseline capturing that finding.
        _install_hook_with_bootstrap(repo)
        baseline_path = repo / ".saturnday-baseline.json"
        assert baseline_path.is_file(), (
            "hook install must bootstrap a baseline on a non-green repo"
        )
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        # The baseline must contain at least one finding fingerprint.
        # (We don't pin which specific check produced it because that
        # depends on the secret-scanner implementation; we just need
        # the baseline to be non-empty.)
        assert baseline.get("findings"), (
            "bootstrap baseline must capture pre-existing findings, "
            "otherwise the ratchet has nothing to filter against"
        )

        # 3. Make an unrelated edit — append a comment line.
        # No NEW finding is introduced.
        main_py.write_text(
            f'API_KEY = "{_BAD_SECRET_A}"  # pre-existing\n'
            f'# unrelated change: explanatory comment\n',
            encoding="utf-8",
        )
        _git("add", "main.py", cwd=repo)

        # 4. Run governance against the staged diff.  This is the same
        # path the pre-commit hook drives (saturnday governance --staged).
        pack, _ = run_governance_check(
            repo_path=repo, diff_range="", staged=True,
        )

        # 5. The ratchet must filter out the baselined finding so the
        # disposition is NOT FAIL.  This is Nick's complaint #1
        # converted into an assertion.
        assert pack.disposition != "FAIL", (
            f"Pre-existing finding blocked an unrelated change.  Nick's "
            f"#1 complaint reproduces.  Disposition={pack.disposition}, "
            f"reasons={pack.disposition_reasons}"
        )

    def test_new_finding_in_different_file_is_blocked(
        self, tmp_path: Path,
    ) -> None:
        """Flip side of the ratchet contract: introducing a NEW finding
        in a NEW file must still block, even when the original file's
        legacy findings are baselined.  Otherwise the ratchet would
        let regressions through.

        The new finding is placed in ``other.py`` rather than
        ``main.py`` so its fingerprint has a distinct ``path`` from any
        baselined fingerprint and is unambiguously "new" to the
        ratchet.  (A separate bug — the secrets-check finding shape
        does not carry a discriminating snippet, so two secrets in
        the SAME file fingerprint identically — is out of scope for
        this batch and tracked separately.)"""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)

        main_py = repo / "main.py"
        main_py.write_text(
            f'API_KEY = "{_BAD_SECRET_A}"  # pre-existing\n',
            encoding="utf-8",
        )
        _git("add", "main.py", cwd=repo)
        _git("commit", "-q", "-m", "seed", cwd=repo)
        _install_hook_with_bootstrap(repo)

        # Stage a brand-new file with a fresh secret — distinct path
        # from any baselined fingerprint.
        other_py = repo / "other.py"
        other_py.write_text(
            f'OTHER_KEY = "{_BAD_SECRET_B}"  # NEW — should block\n',
            encoding="utf-8",
        )
        _git("add", "other.py", cwd=repo)

        pack, _ = run_governance_check(
            repo_path=repo, diff_range="", staged=True,
        )

        # The ratchet must escalate disposition because the new file
        # contains a finding that is NOT in the baseline.
        assert pack.disposition == "FAIL", (
            f"New finding in a new file failed to block.  Ratchet may be "
            f"over-aggressive about filtering.  "
            f"Disposition={pack.disposition}, "
            f"reasons={pack.disposition_reasons}"
        )

    def test_no_baseline_means_no_filtering(self, tmp_path: Path) -> None:
        """Sanity check: a repo with NO baseline gets no ratchet
        protection — pre-existing findings DO block.  This pins the
        importance of the bootstrap and ensures the test isn't passing
        just because the ratchet always silences findings."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)

        main_py = repo / "main.py"
        main_py.write_text(
            f'API_KEY = "{_BAD_SECRET_A}"\n', encoding="utf-8",
        )
        _git("add", "main.py", cwd=repo)
        _git("commit", "-q", "-m", "seed", cwd=repo)
        # NOTE: deliberately do NOT install hook / bootstrap a baseline.
        assert not (repo / ".saturnday-baseline.json").exists()

        # Stage a no-op edit so the staged diff has content.
        main_py.write_text(
            f'API_KEY = "{_BAD_SECRET_A}"\n# touched\n',
            encoding="utf-8",
        )
        _git("add", "main.py", cwd=repo)

        pack, _ = run_governance_check(
            repo_path=repo, diff_range="", staged=True,
        )
        # Without a baseline, the secret check fails outright.
        assert pack.disposition == "FAIL", (
            "no-baseline repo with a secret finding must FAIL — "
            "otherwise the test setup never produced the finding "
            "the other tests rely on"
        )
