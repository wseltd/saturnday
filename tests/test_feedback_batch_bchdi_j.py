"""Tests for the feedback-batch fixes B, C, D, H, I.1, J.1.

Each test asserts the smallest observable behaviour required by the
corresponding fix description.  No coupling across fixes.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday import cli as cli_mod
from saturnday.policy_loader import validated_expected_findings


# ---------------------------------------------------------------------------
# Fix B — hook must not block Saturnday's own internal commits
# ---------------------------------------------------------------------------

def test_fix_b_hook_template_skips_on_internal_commit_env() -> None:
    """The installed hook must early-exit 0 when SATURNDAY_INTERNAL_COMMIT=1."""
    bin_path = "/absolute/path/to/saturnday"
    hook = cli_mod._render_pre_commit_hook(bin_path)
    assert "SATURNDAY_INTERNAL_COMMIT" in hook
    # The guard must appear before the actual governance command invocation.
    guard_idx = hook.find("SATURNDAY_INTERNAL_COMMIT")
    invocation_idx = hook.find(f"{bin_path} governance")
    assert guard_idx != -1 and invocation_idx != -1
    assert guard_idx < invocation_idx, "hook must check env var BEFORE running governance"


def test_fix_b_ticket_runner_sets_internal_commit_env() -> None:
    """``_git_commit`` must set SATURNDAY_INTERNAL_COMMIT=1 on the commit subprocess."""
    from saturnday import ticket_runner as tr

    captured: dict = {}

    def _fake_safe_run(*args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch.object(tr, "safe_subprocess_run", side_effect=_fake_safe_run):
        tr._git_commit(Path("/tmp"), "T001", ["src/foo.py"])

    assert captured["env"].get("SATURNDAY_INTERNAL_COMMIT") == "1"


def test_fix_b_installed_hook_bypasses_when_internal_env_set(tmp_path: Path) -> None:
    """End-to-end: an installed hook exits 0 immediately when
    SATURNDAY_INTERNAL_COMMIT=1 is set; without the env it runs the
    governance command."""
    hook = cli_mod._render_pre_commit_hook("/opt/venv/bin/saturnday")
    script = tmp_path / "pre-commit"
    script.write_text(hook, encoding="utf-8")
    script.chmod(0o755)

    # Run with the env set: must exit 0 BEFORE invoking the binary.
    res_internal = subprocess.run(
        ["sh", str(script)],
        env={**os.environ, "SATURNDAY_INTERNAL_COMMIT": "1"},
        capture_output=True, text=True, check=False,
    )
    assert res_internal.returncode == 0
    # Governance must not have been attempted — the fake binary path
    # would have caused a "not found" error if the script reached it.
    assert "governance" not in res_internal.stdout
    assert "governance" not in res_internal.stderr

    # Run without the env: the script must attempt to invoke the binary.
    # The fake binary path doesn't exist, so the shell reports failure,
    # which is sufficient evidence that the guard did NOT short-circuit.
    res_normal = subprocess.run(
        ["sh", str(script)],
        env={k: v for k, v in os.environ.items() if k != "SATURNDAY_INTERNAL_COMMIT"},
        capture_output=True, text=True, check=False,
    )
    # Either the binary is not found (rc != 0) or the real saturnday
    # was on PATH — in both cases the "checking staged changes" banner
    # confirms we got past the guard.
    assert "Saturnday: checking staged changes" in res_normal.stdout


def test_fix_b_repair_runner_sets_internal_commit_env() -> None:
    """``_commit_repair_fix`` must set SATURNDAY_INTERNAL_COMMIT=1 on both add and commit."""
    from saturnday.repair import repair_runner as rr

    seen_envs: list[dict] = []

    def _fake_run(*args, **kwargs):
        seen_envs.append(kwargs.get("env", {}))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch.object(rr.subprocess, "run", side_effect=_fake_run):
        rr._commit_repair_fix(
            Path("/tmp"), "R001", "missing_license",
            scoped_files=["README.md"],
        )

    # Two calls: add + commit.  Both must carry the env var.
    assert len(seen_envs) == 2
    for env in seen_envs:
        assert env.get("SATURNDAY_INTERNAL_COMMIT") == "1"


# ---------------------------------------------------------------------------
# Fix H — hook install must bake the absolute saturnday binary path
# ---------------------------------------------------------------------------

def test_fix_h_template_has_placeholder() -> None:
    """The raw template must use a placeholder, not a bare ``saturnday`` name."""
    assert "{saturnday_bin}" in cli_mod._PRE_COMMIT_HOOK_TEMPLATE
    # The bare-name invocation must not appear as a shell command in the
    # template — it should always be the placeholder.  (The comment
    # header may mention the tool's name; we check specifically for the
    # governance call pattern.)
    bad_patterns = [
        "\n    saturnday governance",
        "\nsaturnday governance",
    ]
    for p in bad_patterns:
        assert p not in cli_mod._PRE_COMMIT_HOOK_TEMPLATE, f"bare name still present: {p!r}"


def test_fix_h_render_uses_which_when_available() -> None:
    """``_render_pre_commit_hook`` must substitute the resolved executable."""
    with patch("shutil.which", return_value="/opt/venv/bin/saturnday"):
        hook = cli_mod._render_pre_commit_hook()
    assert "/opt/venv/bin/saturnday" in hook
    assert "{saturnday_bin}" not in hook


def test_fix_h_render_falls_back_to_module_invocation() -> None:
    """When ``shutil.which`` fails, hook must fall back to ``python -m saturnday``."""
    with patch("shutil.which", return_value=None):
        hook = cli_mod._render_pre_commit_hook()
    assert "-m saturnday" in hook
    assert sys.executable in hook


def test_fix_h_install_writes_absolute_path(tmp_path: Path) -> None:
    """End-to-end: ``saturnday hook install`` writes a hook containing the absolute path."""
    # Prepare a fake git repo.
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)

    args = types.SimpleNamespace(
        hook_action="install",
        repo=str(repo),
        verbose=False,
        quiet=False,
    )

    fake_bin = "/opt/fakevenv/bin/saturnday"
    with patch("shutil.which", return_value=fake_bin):
        rc = cli_mod._handle_hook(args)

    assert rc == 0
    hook_file = repo / ".git" / "hooks" / "pre-commit"
    assert hook_file.is_file()
    content = hook_file.read_text(encoding="utf-8")
    assert fake_bin in content
    # The installed hook must NOT contain the bare-name invocation.
    assert "\n    saturnday governance" not in content
    assert "\nsaturnday governance" not in content


# ---------------------------------------------------------------------------
# Fix I.1 — YAML boolean footgun must warn explicitly
# ---------------------------------------------------------------------------

def test_fix_i1_yaml_bool_in_expected_findings_warns(caplog) -> None:
    """A bool value in ``expected_findings`` must trigger a YAML-boolean warning."""
    caplog.set_level(logging.WARNING, logger="saturnday.policy_loader")
    result = validated_expected_findings({"expected_findings": [False, "missing_license"]})
    # Only the string survives.
    assert result == {"missing_license"}
    # Warning must name the YAML coercion concern and recommend quoting.
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "YAML" in joined
    assert "quote" in joined.lower()


def test_fix_i1_valid_strings_emit_no_warning(caplog) -> None:
    """Valid string entries must not trigger the YAML coercion warning."""
    caplog.set_level(logging.WARNING, logger="saturnday.policy_loader")
    result = validated_expected_findings({"expected_findings": ["missing_license", "readme"]})
    assert result == {"missing_license", "readme"}
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "YAML" not in joined


def test_fix_i1_nonstring_nonbool_still_generic_warning(caplog) -> None:
    """A non-string, non-bool entry must still get the generic warning path."""
    caplog.set_level(logging.WARNING, logger="saturnday.policy_loader")
    result = validated_expected_findings({"expected_findings": [{"kind": "oops"}, "ok"]})
    assert result == {"ok"}
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "non-string" in joined


# ---------------------------------------------------------------------------
# Fix J.1 — package metadata credibility
# ---------------------------------------------------------------------------

def test_fix_j1_pyproject_has_authors() -> None:
    """The shipped pyproject.toml must declare authors for PyPI metadata."""
    pp = Path(__file__).resolve().parent.parent / "pyproject.toml"
    text = pp.read_text(encoding="utf-8")
    assert "\nauthors = [" in text, "pyproject.toml must declare an authors list"


def test_fix_j1_metadata_parser_lowercases_keys(tmp_path: Path) -> None:
    """``_parse_metadata`` must lowercase keys so case-insensitive consumers work."""
    from saturnday.release.wheel_inspector import _parse_metadata

    # Build a fake wheel-dist-info tree.
    dist_info = tmp_path / "fake-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\n"
        "Name: fake\n"
        "Author-email: Test <test@example.com>\n"
        "License-Expression: MIT\n"
        "Description-Content-Type: text/markdown\n"
        "Classifier: Programming Language :: Python :: 3\n"
        "Requires-Python: >=3.10\n"
        "Project-URL: Homepage, https://example.com\n",
        encoding="utf-8",
    )
    parsed = _parse_metadata(tmp_path)
    # Lowercase aliases must be present so the premium check can find them.
    assert parsed.get("author-email") == "Test <test@example.com>"
    assert parsed.get("description-content-type") == "text/markdown"
    assert parsed.get("classifier") == "Programming Language :: Python :: 3"
    assert parsed.get("requires-python") == ">=3.10"
    assert parsed.get("project-url") == "Homepage, https://example.com"


def test_fix_j1_license_expression_aliased_to_license(tmp_path: Path) -> None:
    """PEP 639 ``License-Expression`` must alias to ``license`` for legacy checks."""
    from saturnday.release.wheel_inspector import _parse_metadata

    dist_info = tmp_path / "fake-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.4\n"
        "Name: fake\n"
        "License-Expression: LicenseRef-Proprietary\n",
        encoding="utf-8",
    )
    parsed = _parse_metadata(tmp_path)
    # Both keys must be present so PEP 639 wheels pass legacy license checks.
    assert parsed.get("license-expression") == "LicenseRef-Proprietary"
    assert parsed.get("license") == "LicenseRef-Proprietary"


# ---------------------------------------------------------------------------
# Fix C — repair commit must not use broad staging
# ---------------------------------------------------------------------------

def test_fix_c_commit_uses_explicit_file_list() -> None:
    """``_commit_repair_fix`` must call ``git add`` with an explicit file list."""
    from saturnday.repair import repair_runner as rr

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch.object(rr.subprocess, "run", side_effect=_fake_run):
        rr._commit_repair_fix(
            Path("/tmp"), "R002", "secrets",
            scoped_files=["src/main.py"],
        )

    # First call is the ``git add``.  It must carry an explicit ``--``
    # separator and the scoped file — no ``-A``, no ``.``, no wildcard.
    add_cmd = calls[0]
    assert add_cmd[:5] == ["git", "-C", "/tmp", "add", "--"]
    assert add_cmd[5:] == ["src/main.py"]
    assert "-A" not in add_cmd


def test_fix_c_commit_without_scoped_files_refuses_and_does_not_add() -> None:
    """Without scoped_files, the commit must refuse — not fall back to ``git add -A``."""
    from saturnday.repair import repair_runner as rr

    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch.object(rr.subprocess, "run", side_effect=_fake_run):
        rr._commit_repair_fix(Path("/tmp"), "R003", "whatever", scoped_files=None)

    assert calls == [], "no subprocess call should be made when scope is missing"


def test_fix_c_all_production_callers_use_group_by_file() -> None:
    """Ground-truth audit: every in-repo caller of generate_repair_tickets
    uses the default ``group_by="file"``, which guarantees single-file tickets.
    This test is the canary — if someone adds a ``group_by="check"`` caller,
    it will fail and force a broader scoping decision."""
    import re
    src_root = Path(__file__).resolve().parent.parent / "src" / "saturnday"
    # Match calls (not the ``def`` line) and skip the definition file.
    call_pattern = re.compile(r"generate_repair_tickets\s*\([^)]*\)")
    suspicious: list[tuple[str, str]] = []
    for py in src_root.rglob("*.py"):
        if py.name == "repair_tickets.py":
            # The function's definition + one-line test grouping live here;
            # not a caller site.
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        for match in call_pattern.finditer(text):
            call = match.group(0)
            if "group_by" not in call:
                continue  # using the default ``"file"`` — OK.
            if 'group_by="file"' in call or "group_by='file'" in call:
                continue
            suspicious.append((str(py.relative_to(src_root)), call[:120]))
    assert not suspicious, (
        "New caller of generate_repair_tickets uses non-file grouping; "
        f"commit scoping in run_repair_batch must be widened: {suspicious!r}"
    )


def test_fix_c_check_group_by_drops_cross_file_legitimate_files() -> None:
    """Prove the known gap: if someone calls with ``group_by="check"``,
    a ticket's ``file_path`` is only the first finding's file — findings
    on other files in the same chunk are recorded in evidence but their
    files are not in ``file_path``.  Current scope = [ticket.file_path]
    would drop those.  This test documents the limit of closure."""
    from saturnday.repair.repair_tickets import generate_repair_tickets
    from saturnday.guard.cloud_scanner import Finding

    # Use a registered file-local kind (hardcoded_secret) so the
    # repair-ticket target-normalisation (Option C, commit on policy
    # schema refusal batch) keeps file_path pass-through for the
    # happy path.  The test's subject is cross-file grouping under
    # group_by="check", not the normalisation itself.
    findings = [
        Finding(file="a.py", line=1, kind="hardcoded_secret", message="m1", severity="error"),
        Finding(file="b.py", line=2, kind="hardcoded_secret", message="m2", severity="error"),
        Finding(file="c.py", line=3, kind="hardcoded_secret", message="m3", severity="error"),
    ]
    tickets = generate_repair_tickets(findings, group_by="check")
    # Under group_by="check", all three findings collapse into ONE ticket
    # whose ``file_path`` is only the first file.
    assert len(tickets) == 1
    assert tickets[0].file_path == "a.py"
    # Evidence records ALL three files.  If a user ever calls with this
    # grouping, the current commit scope would stage only a.py and drop
    # b.py + c.py edits.  Not a regression introduced by Fix C — it is
    # the latent gap we are choosing to leave until a real caller exists.
    evidence_files = {line.split(":")[0] for line in tickets[0].evidence}
    assert evidence_files == {"a.py", "b.py", "c.py"}


def test_fix_c_commit_scope_excludes_untracked_tfplan(tmp_path: Path) -> None:
    """End-to-end: a repair commit must not include untracked sentinel files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    # Seed a tracked file and an untracked sensitive artefact.
    (repo / "main.py").write_text("def foo(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    # Modify main.py (the repair target) and drop in an untracked tfplan.
    (repo / "main.py").write_text("def foo(): return 1\n", encoding="utf-8")
    (repo / "tfplan.bin").write_text("SECRET=hunter2\n", encoding="utf-8")

    from saturnday.repair import repair_runner as rr

    rr._commit_repair_fix(repo, "R100", "test_fix", scoped_files=["main.py"])

    # Inspect the resulting commit's tree.
    ls = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    files_in_commit = {line for line in ls.stdout.splitlines() if line}
    assert "main.py" in files_in_commit
    assert "tfplan.bin" not in files_in_commit, "untracked sensitive file leaked into commit"

    # tfplan.bin must still be present in the working tree, untracked.
    assert (repo / "tfplan.bin").is_file()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True,
    )
    assert "?? tfplan.bin" in status.stdout


# ---------------------------------------------------------------------------
# Fix D — failed repair writes roll back
# ---------------------------------------------------------------------------

def test_fix_d_api_mode_failed_repair_restores_pre_bytes(tmp_path: Path) -> None:
    """API-mode: on failed status, target file reverts to pre-repair bytes."""
    from saturnday.repair import repair_executor as re_mod
    from saturnday.repair.repair_tickets import RepairTicket

    # Seed a target file.
    target_rel = "src/foo.py"
    target = tmp_path / target_rel
    target.parent.mkdir(parents=True)
    pre_content = "PRE_REPAIR\n"
    target.write_text(pre_content, encoding="utf-8")

    ticket = RepairTicket(
        ticket_id="R201",
        title="fix broken thing",
        severity="error",
        file_path=target_rel,
        line=1,
        finding_kind="broken_thing",
        evidence=[],
    )

    # Coder returns junk that does NOT reduce findings.
    def _coder(_prompt: str, _path: str, _repo: Path) -> str:
        return "BAD_REPLACEMENT\n"

    class _Finding:
        def __init__(self, kind: str, file: str) -> None:
            self.kind = kind
            self.file = file

    # scan_fn returns one matching finding before AND after → status="failed".
    def _scan(_p: Path):
        return [_Finding("broken_thing", target_rel)]

    result = re_mod.execute_repair(ticket, tmp_path, _coder, scan_fn=_scan, cli_mode=False)
    assert result.status == "failed"
    # Target file must match pre-repair bytes.
    assert target.read_text(encoding="utf-8") == pre_content


def test_fix_d_api_mode_failed_repair_removes_newly_created_file(tmp_path: Path) -> None:
    """API-mode: if repair created a file that didn't exist, rollback must delete it."""
    from saturnday.repair import repair_executor as re_mod
    from saturnday.repair.repair_tickets import RepairTicket

    target_rel = "pkg/created.py"
    target = tmp_path / target_rel
    # Target file does NOT exist yet.

    ticket = RepairTicket(
        ticket_id="R202",
        title="add missing module",
        severity="error",
        file_path=target_rel,
        line=None,
        finding_kind="missing_module",
        evidence=[],
    )

    def _coder(_prompt: str, _path: str, _repo: Path) -> str:
        return "def newly_created(): pass\n"

    class _Finding:
        def __init__(self, kind: str, file: str) -> None:
            self.kind = kind
            self.file = file

    def _scan(_p: Path):
        return [_Finding("missing_module", target_rel)]

    result = re_mod.execute_repair(ticket, tmp_path, _coder, scan_fn=_scan, cli_mode=False)
    assert result.status == "failed"
    # Rollback must have removed the newly-created file.
    assert not target.exists(), "failed repair left a newly-created file on disk"


def test_fix_d_cli_mode_failed_repair_reverts_tracked_file_via_git(tmp_path: Path) -> None:
    """CLI-mode: when the file is tracked in git, a failed repair must
    ``git checkout HEAD --`` it back to the committed state."""
    from saturnday.repair import repair_executor as re_mod
    from saturnday.repair.repair_tickets import RepairTicket

    # Build a tiny git repo with one tracked file at committed state.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    target_rel = "main.py"
    target = repo / target_rel
    target.write_text("HEAD_CONTENT\n", encoding="utf-8")
    subprocess.run(["git", "add", target_rel], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    ticket = RepairTicket(
        ticket_id="R210", title="cli fail", severity="error",
        file_path=target_rel, line=1, finding_kind="broken", evidence=[],
    )

    # CLI mode: the "coder" writes directly to disk (simulating what
    # claude-cli would do), AND its return value is conversational text.
    def _cli_coder(_prompt: str, path: str, _repo: Path) -> str:
        # Simulate the agent having edited the file directly.
        Path(path).write_text("AGENT_BAD\n", encoding="utf-8")
        return "done"

    class _F:
        def __init__(self, kind, file):
            self.kind = kind; self.file = file

    # Scan always finds the same finding (so status will be failed).
    def _scan(_p):
        return [_F("broken", target_rel)]

    result = re_mod.execute_repair(ticket, repo, _cli_coder, scan_fn=_scan, cli_mode=True)
    assert result.status == "failed"
    # Rollback must return the file to the committed content.
    assert target.read_text(encoding="utf-8") == "HEAD_CONTENT\n"


def test_fix_d_failed_then_successful_no_residue_in_commit(tmp_path: Path) -> None:
    """End-to-end: a failed repair followed by a scoped successful repair
    must not commit any byte from the failed attempt."""
    from saturnday.repair import repair_executor as re_mod
    from saturnday.repair import repair_runner as rr_mod
    from saturnday.repair.repair_tickets import RepairTicket

    # Build a real git repo with two tracked files.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "a.py").write_text("A_ORIGINAL\n", encoding="utf-8")
    (repo / "b.py").write_text("B_ORIGINAL\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    ticket_fail = RepairTicket(
        ticket_id="R220", title="", severity="error",
        file_path="a.py", line=1, finding_kind="k_fail", evidence=[],
    )
    ticket_pass = RepairTicket(
        ticket_id="R221", title="", severity="error",
        file_path="b.py", line=1, finding_kind="k_pass", evidence=[],
    )

    class _F:
        def __init__(self, kind, file):
            self.kind = kind; self.file = file

    # Stage 1: run R220 (failing).  Post-scan still shows k_fail finding.
    def _coder_fail(_p, _path, _repo):
        return "A_BAD_REPLACEMENT\n"
    def _scan_fail(_p):
        return [_F("k_fail", "a.py")]
    res1 = re_mod.execute_repair(ticket_fail, repo, _coder_fail, scan_fn=_scan_fail, cli_mode=False)
    assert res1.status == "failed"

    # After Fix D rollback, a.py must be its original bytes.
    assert (repo / "a.py").read_text() == "A_ORIGINAL\n", (
        "failed repair left residue on disk"
    )

    # Stage 2: run R221 (passing) and let the real commit path fire.
    def _coder_ok(_p, _path, _repo):
        return "B_REPAIRED\n"

    scan_state = {"phase": "before"}
    def _scan_ok(_p):
        if scan_state["phase"] == "before":
            scan_state["phase"] = "after"
            return [_F("k_pass", "b.py")]
        return []

    res2 = re_mod.execute_repair(ticket_pass, repo, _coder_ok, scan_fn=_scan_ok, cli_mode=False)
    assert res2.status == "fixed"

    # Stage 3: simulate the scoped commit.  Only b.py should land.
    rr_mod._commit_repair_fix(repo, "R221", "k_pass", scoped_files=["b.py"])
    show = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    files_in_head = {f for f in show.stdout.splitlines() if f}
    assert files_in_head == {"b.py"}, (
        f"failed-ticket residue leaked into commit: {files_in_head}"
    )
    # And a.py on disk is still the seed value — no leftover bad bytes.
    assert (repo / "a.py").read_text() == "A_ORIGINAL\n"


def test_fix_d_successful_repair_keeps_changes(tmp_path: Path) -> None:
    """Happy path: on success, target file keeps the repaired content."""
    from saturnday.repair import repair_executor as re_mod
    from saturnday.repair.repair_tickets import RepairTicket

    target_rel = "src/bar.py"
    target = tmp_path / target_rel
    target.parent.mkdir(parents=True)
    target.write_text("ORIGINAL\n", encoding="utf-8")

    ticket = RepairTicket(
        ticket_id="R203",
        title="fix fixable thing",
        severity="error",
        file_path=target_rel,
        line=1,
        finding_kind="fixable",
        evidence=[],
    )

    def _coder(_prompt: str, _path: str, _repo: Path) -> str:
        return "REPAIRED\n"

    class _Finding:
        def __init__(self, kind: str, file: str) -> None:
            self.kind = kind
            self.file = file

    # Before: one finding; after: zero findings → status="fixed".
    state = {"phase": "before"}

    def _scan(_p: Path):
        if state["phase"] == "before":
            state["phase"] = "after"
            return [_Finding("fixable", target_rel)]
        return []

    result = re_mod.execute_repair(ticket, tmp_path, _coder, scan_fn=_scan, cli_mode=False)
    assert result.status == "fixed"
    # Successful repair must keep the repaired content.
    assert target.read_text(encoding="utf-8") == "REPAIRED\n"
