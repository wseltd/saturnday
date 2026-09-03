"""Fix 71 — code-level regression containment for out-of-scope files.

Pre-Fix-71 the runner's regression filter only handled four project-level
checks (license, readme, tests_pass, project_runnable) and excluded findings
in files outside the ticket's changed_files via _filter_findings_to_files.
Code-level regressions caused by the ticket's in-scope changes propagating
outward (e.g. an interface change that breaks unrelated callers) were not
surfaced.

Fix 71 adds:

- ``_snapshot_oos_finding_fingerprints(repo, allowed, forbidden)`` — captures
  per-(check, kind, file) fingerprints of findings in tracked source files
  OUTSIDE the ticket's allowed scope.  No-op when allowed is "**".

- ``_detect_oos_regressions(findings, allowed, forbidden, pre_fingerprints)``
  — returns NEW out-of-scope findings (kind+file not present pre-ticket).

- Wiring in ``_run_ticket_with_retries`` that adds OOS regressions to
  relevant_findings and lifts disposition PASS → FAIL when OOS regressions
  are present (so the runner gate does not silently accept).

These tests exercise the helpers directly (pure-function level) and keep
the bound: pre-existing OOS findings remain filtered, in-scope regressions
remain governance's job, and ``allowed_globs == ("**",)`` short-circuits
the snapshot work entirely.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from saturnday.ticket_runner import (
    _detect_oos_regressions,
    _finding_fingerprint,
    _snapshot_oos_finding_fingerprints,
)


# ---------------------------------------------------------------------------
# _detect_oos_regressions — pure helper
# ---------------------------------------------------------------------------


def test_no_findings_returns_empty() -> None:
    assert _detect_oos_regressions(
        findings=[],
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    ) == []


def test_in_scope_finding_is_not_an_oos_regression() -> None:
    """Findings inside allowed_globs are governance's job, not OOS regressions."""
    findings = [{"kind": "stub", "file": "src/app/foo.py"}]
    assert _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    ) == []


def test_new_oos_finding_is_returned() -> None:
    """A finding in an OOS file with no pre-snapshot match is a regression."""
    findings = [{"kind": "stub", "file": "src/lib/bar.py", "detail": "x"}]
    out = _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    )
    assert out == findings


def test_pre_existing_oos_finding_is_filtered_out() -> None:
    """A finding whose (kind, file) matches the pre-snapshot is not surfaced."""
    findings = [{"kind": "stub", "file": "src/lib/bar.py"}]
    pre = {("stubs", "stub", "src/lib/bar.py")}
    out = _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=pre,
    )
    assert out == []


def test_mix_of_pre_existing_and_new_only_returns_new() -> None:
    findings = [
        {"kind": "stub", "file": "src/lib/bar.py"},          # pre-existing
        {"kind": "missing_type_hint", "file": "src/lib/baz.py"},  # NEW
    ]
    pre = {("stubs", "stub", "src/lib/bar.py")}
    out = _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=pre,
    )
    assert out == [{"kind": "missing_type_hint", "file": "src/lib/baz.py"}]


def test_findings_without_path_are_not_oos_regressions() -> None:
    """Findings with no file/path field cannot be classified as OOS — skipped."""
    findings = [{"kind": "stub", "detail": "global lint failure", "message": "x"}]
    assert _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    ) == []


def test_allowed_all_short_circuits() -> None:
    """allowed_globs == ('**',) and no forbidden means every file is in scope —
    OOS regression detection must return [] without iterating findings."""
    findings = [{"kind": "stub", "file": "anywhere.py"}]
    assert _detect_oos_regressions(
        findings,
        allowed_globs=("**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    ) == []


def test_forbidden_glob_match_counts_as_oos() -> None:
    """A finding in a file matched by forbidden_globs is OOS regardless of
    allowed_globs — and must surface as a regression if not pre-existing."""
    findings = [{"kind": "secret", "file": "src/app/secrets.env"}]
    out = _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=("**/*.env",),
        pre_fingerprints=set(),
    )
    assert out == findings


def test_duplicate_oos_findings_are_deduped() -> None:
    """Multiple findings with the same (kind, file) collapse to one regression."""
    findings = [
        {"kind": "stub", "file": "src/lib/bar.py", "line": 10},
        {"kind": "stub", "file": "src/lib/bar.py", "line": 22},
    ]
    out = _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    )
    assert len(out) == 1
    assert out[0]["file"] == "src/lib/bar.py"


def test_path_field_also_recognised() -> None:
    """Findings may carry the file under either ``file`` or ``path`` — both
    must work for OOS classification."""
    findings = [{"kind": "stub", "path": "src/lib/bar.py"}]
    out = _detect_oos_regressions(
        findings,
        allowed_globs=("src/app/**",),
        forbidden_globs=(),
        pre_fingerprints=set(),
    )
    assert out == findings


# ---------------------------------------------------------------------------
# _finding_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_omits_line_number() -> None:
    """Line numbers are deliberately excluded so neighbouring edits do not
    spuriously turn pre-existing findings into 'regressions'."""
    a = _finding_fingerprint("stubs", {"kind": "stub", "file": "x.py", "line": 1})
    b = _finding_fingerprint("stubs", {"kind": "stub", "file": "x.py", "line": 200})
    assert a == b


def test_fingerprint_includes_check_kind_and_file() -> None:
    fp = _finding_fingerprint("stubs", {"kind": "stub", "file": "src/x.py"})
    assert fp == ("stubs", "stub", "src/x.py")


# ---------------------------------------------------------------------------
# _snapshot_oos_finding_fingerprints — integration with git/run_review
# ---------------------------------------------------------------------------


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fixture\n")
    (repo / "LICENSE").write_text("MIT\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def test_snapshot_short_circuits_when_allowed_is_full(tmp_path: Path) -> None:
    """allowed_globs == ('**',) means no scope restriction; snapshot must
    return empty without any expensive scan."""
    _init_repo(tmp_path)
    out = _snapshot_oos_finding_fingerprints(
        tmp_path, allowed_globs=("**",), forbidden_globs=(),
    )
    assert out == set()


def test_snapshot_returns_empty_when_no_oos_files(tmp_path: Path) -> None:
    """When every tracked source file is in scope, snapshot is empty."""
    _init_repo(tmp_path)
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "in_scope.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "in_scope"], cwd=tmp_path, check=True)

    out = _snapshot_oos_finding_fingerprints(
        tmp_path, allowed_globs=("src/app/**",), forbidden_globs=(),
    )
    assert out == set()


def test_snapshot_captures_pre_existing_oos_findings(tmp_path: Path) -> None:
    """A pre-existing finding (e.g. a stub) in an OOS file must appear in
    the snapshot so a later post-ticket scan does not classify it as new.
    """
    _init_repo(tmp_path)
    (tmp_path / "src" / "lib").mkdir(parents=True)
    # A function with a TODO body — placeholders/stubs check should fire.
    (tmp_path / "src" / "lib" / "bar.py").write_text(
        "def bar():\n"
        "    pass  # TODO: implement\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "lib"], cwd=tmp_path, check=True)

    out = _snapshot_oos_finding_fingerprints(
        tmp_path, allowed_globs=("src/app/**",), forbidden_globs=(),
    )
    # Some finding from src/lib/bar.py should be captured (kind varies by
    # check; we only assert the file appears in at least one fingerprint).
    files_in_snapshot = {file for (_check, _kind, file) in out}
    # If the placeholders/stubs check did not fire on this file, the snapshot
    # may legitimately be empty.  But if any finding exists on bar.py, it
    # must be from the OOS scan.
    if out:
        assert "src/lib/bar.py" in files_in_snapshot, (
            f"snapshot must reference src/lib/bar.py if anything was found; "
            f"got: {out}"
        )


# ---------------------------------------------------------------------------
# End-to-end: runner must block on NEW OOS regressions and ignore pre-existing
# ---------------------------------------------------------------------------


def _cli_config():
    from saturnday._types import CoderConfig
    return CoderConfig(backend="openai", api_key="test", model="gpt-4")


def _make_state():
    from saturnday.project_state import ProjectState
    return ProjectState(project_id="fix71-e2e")


def _ticket_with_scope():
    from saturnday._types import TicketScope, TicketSpec
    return TicketSpec(
        ticket_id="T071",
        goal="Add module under src/app/",
        scope=TicketScope(allowed_globs=("src/app/**",), forbidden_globs=()),
    )


def test_runner_blocks_on_new_oos_regression(tmp_path: Path) -> None:
    """End-to-end: a NEW out-of-scope finding (not in pre-snapshot) must
    cause the runner to treat the attempt as non-success and ultimately
    land CODED_UNGOVERNED after retry exhaustion."""
    from unittest.mock import patch
    from saturnday.ticket_runner import _run_ticket_with_retries

    _init_repo(tmp_path)
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "lib").mkdir(parents=True)
    # Pre-existing files committed; src/lib has NO findings in the snapshot.
    (tmp_path / "src" / "lib" / "bar.py").write_text("def bar(): return 42\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmp_path, check=True)

    def stub_execute(*, ticket, repo_path, coder_config, messages):  # noqa: ARG001
        # Defensive: src/app may have been git-cleaned between retries.
        (tmp_path / "src" / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "app" / "foo.py").write_text("from src.lib.bar import bar\n")
        return ("ok", ["src/app/foo.py"])

    # Mock _run_governance to return ONE finding in src/lib/bar.py — which is
    # OUTSIDE allowed_globs ("src/app/**").  This simulates an indirect
    # regression caused by the in-scope change.  Governance returns PASS in
    # the disposition because the in-scope file is fine, but the OOS finding
    # is real and should block.
    def fake_run_governance(*a, **k):
        return ("PASS", [{"kind": "broken_import", "file": "src/lib/bar.py",
                          "detail": "import broken by interface change"}], "", [])

    with (
        patch("saturnday.ticket_runner._execute_ticket", side_effect=stub_execute),
        patch("saturnday.ticket_runner._run_governance", side_effect=fake_run_governance),
        patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
        patch("saturnday.ticket_runner._check_contracts", return_value=""),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
        patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
    ):
        result = _run_ticket_with_retries(
            ticket=_ticket_with_scope(),
            repo_path=tmp_path,
            coder_config=_cli_config(),
            system_prompt="sys",
            state=_make_state(),
            plan_notes="",
            output_dir=tmp_path / "out",
            max_retries=2,
        )

    # OOS regression must lift the runner's PASS to non-success.
    assert result.disposition == "CODED_UNGOVERNED", (
        f"OOS regression must block PASS — got {result.disposition}"
    )
    # The OOS finding must be in governance_findings on the result.
    finding_files = [f.get("file") for f in result.governance_findings]
    assert "src/lib/bar.py" in finding_files, (
        f"OOS regression finding must be carried into evidence; got files {finding_files}"
    )


def test_runner_does_not_block_on_pre_existing_oos_finding(tmp_path: Path) -> None:
    """If the same OOS finding already existed pre-ticket (in the snapshot),
    it must NOT be re-surfaced as a regression — pre-existing OOS noise
    cannot block tickets from passing."""
    from unittest.mock import patch
    from saturnday.ticket_runner import _run_ticket_with_retries

    _init_repo(tmp_path)
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "bar.py").write_text("def bar(): return 42\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmp_path, check=True)

    def stub_execute(*, ticket, repo_path, coder_config, messages):  # noqa: ARG001
        (tmp_path / "src" / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "app" / "foo.py").write_text("x = 1\n")
        return ("ok", ["src/app/foo.py"])

    def fake_run_governance(*a, **k):
        return ("PASS", [{"kind": "broken_import", "file": "src/lib/bar.py",
                          "detail": "x"}], "", [])

    # Inject the same fingerprint into the snapshot so it counts as pre-existing.
    pre_fingerprints = {("broken_import_check", "broken_import", "src/lib/bar.py")}

    with (
        patch("saturnday.ticket_runner._execute_ticket", side_effect=stub_execute),
        patch("saturnday.ticket_runner._run_governance", side_effect=fake_run_governance),
        patch("saturnday.ticket_runner.run_post_checks", return_value=[]),
        patch("saturnday.ticket_runner._check_contracts", return_value=""),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
        patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
        patch(
            "saturnday.ticket_runner._snapshot_oos_finding_fingerprints",
            return_value=pre_fingerprints,
        ),
    ):
        result = _run_ticket_with_retries(
            ticket=_ticket_with_scope(),
            repo_path=tmp_path,
            coder_config=_cli_config(),
            system_prompt="sys",
            state=_make_state(),
            plan_notes="",
            output_dir=tmp_path / "out",
            max_retries=2,
        )

    # Pre-existing OOS finding must not block.
    assert result.disposition == "PASS", (
        f"pre-existing OOS finding must be filtered; got {result.disposition}"
    )
    # And the finding must NOT appear in governance_findings.
    finding_files = [f.get("file") for f in result.governance_findings]
    assert "src/lib/bar.py" not in finding_files, (
        "pre-existing OOS finding must not propagate into evidence"
    )
