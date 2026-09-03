"""Tests for repair-ticket target-path normalisation (Option C fix).

Memo defect: ``generate_repair_tickets`` previously used ``first.file``
verbatim as ``file_path``.  Scanners that emit directory paths or
repo-level findings therefore produced malformed repair tickets that
the executor then caught with its ``is_dir()`` backstop and failed
with ``target is a directory — no fix applied``.

Fix: hybrid locality gate + directory check in
``generate_repair_tickets``.  Repo-level kinds, unknown kinds, empty
paths, and directory paths all normalise to ``file_path=""`` so the
executor's global-scope path takes over instead of the failure
backstop.

Executor's ``is_dir()`` guard stays in place as a last-line backstop
(see the pin test in :class:`TestExecutorBackstopStillPinned`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.guard.cloud_scanner import Finding
from saturnday.repair.repair_tickets import generate_repair_tickets


def _mk_finding(kind: str, file: str, **extra) -> Finding:
    """Build a Finding with the minimum required fields."""
    return Finding(
        kind=kind,
        file=file,
        line=extra.get("line", 1),
        message=extra.get("message", "test"),
        severity=extra.get("severity", "error"),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Tiny fixture repo: has a concrete file at `real.py`, a
    directory at `tests/`, and nothing else."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "real.py").write_text("x = 1\n")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Happy path unchanged — file-local kind + concrete file target
# ---------------------------------------------------------------------------


class TestHappyPathUnchanged:
    def test_file_local_kind_with_real_file_keeps_path(self, repo: Path) -> None:
        findings = [_mk_finding("hardcoded_secret", "real.py")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert len(tickets) == 1
        assert tickets[0].file_path == "real.py"

    def test_file_local_ticket_id_and_title_still_populated(
        self, repo: Path,
    ) -> None:
        findings = [_mk_finding("ruff", "real.py", message="E501 line too long")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].ticket_id == "REPAIR-001"
        assert "ruff" in tickets[0].title
        assert "real.py" in tickets[0].title
        assert tickets[0].finding_kind == "ruff"

    def test_no_repo_path_passthrough_for_file_local_kind(self) -> None:
        """When caller omits repo_path, the normaliser falls through
        to case 4 for file-local kinds with non-empty paths (the
        directory check is simply skipped).  Executor's is_dir()
        backstop still catches truly malformed paths downstream."""
        findings = [_mk_finding("hardcoded_secret", "real.py")]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].file_path == "real.py"


# ---------------------------------------------------------------------------
# 2. Repo-level / unknown-kind findings become global scope
# ---------------------------------------------------------------------------


class TestRepoLevelBecomesGlobalScope:
    def test_tests_failing_normalises_to_empty_path(self, repo: Path) -> None:
        """`tests_failing` is explicitly registered in REPO_LEVEL_KINDS."""
        findings = [_mk_finding("tests_failing", "tests")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""

    def test_missing_readme_normalises_to_empty_path(self, repo: Path) -> None:
        """`missing_readme` — classic repo-level check; scanner emits
        ``file="."`` or similar.  Must produce global-scope ticket."""
        findings = [_mk_finding("missing_readme", ".")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""

    def test_circular_import_normalises_regardless_of_emitted_file(
        self, repo: Path,
    ) -> None:
        findings = [_mk_finding("circular_import", "real.py")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        # Even though real.py exists, circular_import is repo-level, so
        # the ticket is global-scope by design.
        assert tickets[0].file_path == ""

    def test_normalised_ticket_still_has_finding_metadata(
        self, repo: Path,
    ) -> None:
        """Normalising file_path must NOT drop other ticket fields.
        The ticket still carries finding_kind, evidence, severity."""
        findings = [_mk_finding("tests_failing", "tests", message="1 failing")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].finding_kind == "tests_failing"
        assert tickets[0].severity == "error"
        assert any("1 failing" in e for e in tickets[0].evidence)


# ---------------------------------------------------------------------------
# 3. Directory-shaped target becomes global scope
# ---------------------------------------------------------------------------


class TestDirectoryTargetBecomesGlobalScope:
    def test_file_local_kind_but_scanner_emitted_directory(
        self, repo: Path,
    ) -> None:
        """Belt-and-braces: even when the kind IS registered as
        file-local, if the scanner mistakenly emits a directory path
        we must normalise.  The `tests/` dir exists in the fixture."""
        findings = [_mk_finding("hardcoded_secret", "tests")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""


# ---------------------------------------------------------------------------
# 4. Empty target becomes global scope
# ---------------------------------------------------------------------------


class TestEmptyTargetBecomesGlobalScope:
    def test_empty_string_normalises(self, repo: Path) -> None:
        findings = [_mk_finding("hardcoded_secret", "")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""

    def test_whitespace_only_normalises(self, repo: Path) -> None:
        findings = [_mk_finding("hardcoded_secret", "   \t  ")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""


# ---------------------------------------------------------------------------
# 5. Unknown kind is safe
# ---------------------------------------------------------------------------


class TestUnknownKindSafe:
    def test_unknown_kind_normalises_to_empty(self, repo: Path) -> None:
        """Per the memo: unknown kinds default to repo-level.  The ticket
        must use empty ``file_path`` rather than a speculative concrete
        path that could trip the executor backstop."""
        findings = [_mk_finding("brand_new_unregistered_kind", "real.py")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""


# ---------------------------------------------------------------------------
# 6. End-to-end: executor's is_dir() backstop is NOT reached
# ---------------------------------------------------------------------------


class TestEndToEndClosureForRealDefect:
    def test_executor_does_not_hit_is_dir_backstop_for_repo_level_finding(
        self, repo: Path,
    ) -> None:
        """The real defect closure: a finding that previously produced a
        ticket with ``file_path='tests'`` (a directory) used to trigger
        ``repair_executor.execute_repair``'s ``is_dir()`` failure branch
        at line ~212.  After the fix the ticket's ``file_path`` is
        empty, the executor takes the global-scope path, and the
        ``target is a directory`` error never appears."""
        from saturnday.repair.repair_executor import execute_repair

        findings = [_mk_finding("tests_failing", "tests")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""

        # The executor pre-scans then post-scans via ``scan_fn``.  The
        # counter-driven stub returns one matching finding on the
        # first call (so findings_before=1) and zero on subsequent
        # calls (so the post-scan shows findings_after=0 → status
        # 'fixed').  If the ticket still had ``file_path='tests'``,
        # the executor would short-circuit to failed with a
        # "target is a directory" error BEFORE coder_fn is called.
        scan_calls = {"n": 0}
        def counter_scan(rp: Path):
            scan_calls["n"] += 1
            if scan_calls["n"] == 1:
                return [_mk_finding("tests_failing", "tests")]
            return []

        coder_calls: list[tuple[str, str, Path]] = []
        def stub_coder(prompt: str, target: str, rp: Path) -> str:
            coder_calls.append((prompt, target, rp))
            return "# repaired\n"

        result = execute_repair(
            ticket=tickets[0],
            skill_path=repo,
            coder_fn=stub_coder,
            scan_fn=counter_scan,
            cli_mode=True,  # no file-write side-effects on stub output
        )
        assert len(coder_calls) == 1, (
            "executor hit a pre-coder backstop — the ticket was "
            "malformed before the fix"
        )
        assert "directory" not in (result.error or "").lower(), (
            f"unexpected directory-related error: {result.error!r}"
        )
        assert result.status == "fixed"


# ---------------------------------------------------------------------------
# 7. Executor backstop still pinned
# ---------------------------------------------------------------------------


class TestExecutorBackstopStillPinned:
    def test_executor_still_refuses_directory_target_if_handed_one(
        self, repo: Path,
    ) -> None:
        """Option C fixes the generation path but does NOT remove the
        executor's ``is_dir()`` backstop.  If some future caller builds
        a RepairTicket with a directory path directly (bypassing
        generate_repair_tickets), the executor must still refuse."""
        from saturnday.repair.repair_executor import execute_repair
        from saturnday.repair.repair_tickets import RepairTicket

        malformed = RepairTicket(
            ticket_id="REPAIR-X",
            title="unit-test malformed",
            severity="error",
            file_path="tests",  # directory — simulates a caller that
                                # bypassed the generation normaliser
            line=None,
            finding_kind="hardcoded_secret",
            evidence=["tests: x"],
            remediation=None,
            group_key="tests:hardcoded_secret",
        )

        def stub_coder(prompt: str, target: str, rp: Path) -> str:
            raise AssertionError(
                "executor backstop failed — coder was invoked on a "
                "directory target (regression)"
            )

        # Pre-scan must find the finding so the executor doesn't take
        # the "already clean" fast path; counter makes post-scan empty.
        scan_calls = {"n": 0}
        def counter_scan(rp: Path):
            scan_calls["n"] += 1
            if scan_calls["n"] == 1:
                return [_mk_finding("hardcoded_secret", "tests")]
            return []

        result = execute_repair(
            ticket=malformed,
            skill_path=repo,
            coder_fn=stub_coder,
            scan_fn=counter_scan,
            cli_mode=True,
        )
        assert result.status == "failed"
        assert "directory" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# 8. tests_pass specifically — the named failing case from the memo
# ---------------------------------------------------------------------------


class TestTestsPassSpecifically:
    def test_tests_pass_finding_no_longer_produces_directory_ticket(
        self, repo: Path,
    ) -> None:
        """The memo named REPAIR-001 as the real defect: a ``tests_pass``
        finding with ``file="tests"`` used to become a ticket with
        ``file_path="tests"``.  After the fix the ticket carries
        empty ``file_path`` and the executor takes the global-scope
        path instead of the directory-target failure branch."""
        findings = [_mk_finding("tests_pass", "tests")]
        tickets = generate_repair_tickets(findings, repo_path=repo)
        assert tickets[0].file_path == ""
        assert tickets[0].finding_kind == "tests_pass"

    def test_tests_pass_finding_without_repo_path_still_normalises(
        self,
    ) -> None:
        """tests_pass is not in FILE_LOCAL_KINDS, so the locality gate
        catches it even when the caller omits repo_path."""
        findings = [_mk_finding("tests_pass", "tests")]
        tickets = generate_repair_tickets(findings)
        assert tickets[0].file_path == ""
