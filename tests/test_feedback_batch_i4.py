"""Closure-proof tests for I.4 — opt-in work-branch feature.

Scope bounded exactly to what the design approved:

  * single flag --work-branch[=NAME] on 6 commands
  * refusal on dirty tree, detached HEAD, existing branch, failed creation
  * no default-on behaviour, no auto-stash, no auto-push
  * evidence fields populated only when the flag is active
  * resume/rerun picks up recorded work branch

Tests exercise the pre-flight helper directly AND the RunResult /
summary evidence end-to-end via a real minimal run_plan invocation.
"""
from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def _current_branch(repo: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# pre_flight_create — refusal rules
# ---------------------------------------------------------------------------


class TestPreFlightCreateRefusals:
    def test_refuses_on_dirty_working_tree(self, tmp_path: Path) -> None:
        from saturnday.run.work_branch import (
            WorkBranchRefused, pre_flight_create,
        )
        repo = tmp_path / "r"
        _init_git_repo(repo)
        (repo / "dirty.txt").write_text("dirt\n", encoding="utf-8")

        with pytest.raises(WorkBranchRefused) as exc:
            pre_flight_create(repo, "saturnday/run-test")

        assert "working tree is dirty" in str(exc.value)
        assert "dirty.txt" in str(exc.value)
        # Did NOT create the branch.
        assert _current_branch(repo) == "main"

    def test_refuses_on_detached_head(self, tmp_path: Path) -> None:
        from saturnday.run.work_branch import (
            WorkBranchRefused, pre_flight_create,
        )
        repo = tmp_path / "r"
        _init_git_repo(repo)
        # Detach HEAD.
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", sha], check=True,
                       capture_output=True)

        with pytest.raises(WorkBranchRefused) as exc:
            pre_flight_create(repo, "saturnday/run-test")

        assert "detached" in str(exc.value).lower()

    def test_refuses_on_existing_branch(self, tmp_path: Path) -> None:
        from saturnday.run.work_branch import (
            WorkBranchRefused, pre_flight_create,
        )
        repo = tmp_path / "r"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "saturnday/run-existing"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "-C", str(repo), "checkout", "main"],
                       check=True, capture_output=True)

        with pytest.raises(WorkBranchRefused) as exc:
            pre_flight_create(repo, "saturnday/run-existing")

        msg = str(exc.value)
        assert "already exists" in msg
        # Operator must still be on main — not switched to the existing branch.
        assert _current_branch(repo) == "main"

    def test_ignores_saturnday_state_dir_when_checking_cleanliness(
        self, tmp_path: Path
    ) -> None:
        """Saturnday's own .saturnday/ state must not count as dirty."""
        from saturnday.run.work_branch import pre_flight_create
        repo = tmp_path / "r"
        _init_git_repo(repo)
        (repo / ".saturnday").mkdir()
        (repo / ".saturnday" / "evidence.json").write_text("{}", encoding="utf-8")

        ctx = pre_flight_create(repo, "saturnday/run-ignore-saturnday-state")
        assert ctx.work_branch == "saturnday/run-ignore-saturnday-state"
        assert _current_branch(repo) == "saturnday/run-ignore-saturnday-state"


class TestPreFlightCreateSuccess:
    def test_creates_branch_and_switches_to_it(self, tmp_path: Path) -> None:
        from saturnday.run.work_branch import pre_flight_create
        repo = tmp_path / "r"
        _init_git_repo(repo)
        parent_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        ctx = pre_flight_create(repo, "saturnday/run-alpha")

        assert ctx.work_branch == "saturnday/run-alpha"
        assert ctx.parent_branch == "main"
        assert ctx.parent_head_sha == parent_sha
        assert ctx.auto_created is True
        assert _current_branch(repo) == "saturnday/run-alpha"

    def test_creates_branch_from_non_main_parent(self, tmp_path: Path) -> None:
        """Parent branch can be any branch — recorded verbatim."""
        from saturnday.run.work_branch import pre_flight_create
        repo = tmp_path / "r"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "feature/foo"],
            check=True, capture_output=True,
        )

        ctx = pre_flight_create(repo, "saturnday/run-on-feature")

        assert ctx.parent_branch == "feature/foo"
        assert ctx.work_branch == "saturnday/run-on-feature"
        assert _current_branch(repo) == "saturnday/run-on-feature"


class TestResolveWorkBranchArg:
    def test_none_returns_none(self) -> None:
        from saturnday.run.work_branch import resolve_work_branch_arg
        assert resolve_work_branch_arg(None, "run") is None

    def test_sentinel_auto_generates_name_with_command(self) -> None:
        from saturnday.run.work_branch import (
            AUTO_NAME_SENTINEL, resolve_work_branch_arg,
        )
        name = resolve_work_branch_arg(AUTO_NAME_SENTINEL, "repair")
        assert name.startswith("saturnday/repair-")
        # Timestamp suffix is ISO-ish.
        suffix = name.split("saturnday/repair-", 1)[1]
        assert "T" in suffix and suffix.endswith("Z")

    def test_explicit_name_passes_through(self) -> None:
        from saturnday.run.work_branch import resolve_work_branch_arg
        assert resolve_work_branch_arg("my-branch", "run") == "my-branch"


# ---------------------------------------------------------------------------
# CLI flag surface
# ---------------------------------------------------------------------------


class TestCliWorkBranchFlag:
    def test_run_accepts_flag_without_value(self) -> None:
        from saturnday.cli import build_parser
        p = build_parser()
        ns = p.parse_args([
            "run", "--plan", "/tmp/p.json", "--repo", "/tmp/r",
            "--backend", "openai", "--work-branch",
        ])
        # Flag present without value → sentinel.
        assert ns.work_branch == "__saturnday_auto__"

    def test_run_accepts_flag_with_explicit_name(self) -> None:
        from saturnday.cli import build_parser
        p = build_parser()
        ns = p.parse_args([
            "run", "--plan", "/tmp/p.json", "--repo", "/tmp/r",
            "--backend", "openai", "--work-branch", "saturnday/run-custom",
        ])
        assert ns.work_branch == "saturnday/run-custom"

    def test_run_default_is_none(self) -> None:
        from saturnday.cli import build_parser
        p = build_parser()
        ns = p.parse_args([
            "run", "--plan", "/tmp/p.json", "--repo", "/tmp/r",
            "--backend", "openai",
        ])
        assert ns.work_branch is None

    def test_all_governed_commands_accept_the_flag(self) -> None:
        from saturnday.cli import build_parser
        p = build_parser()
        for cmd, extra in (
            ("run", []),
            ("resume", ["--output-dir", "/tmp/o"]),
            ("rerun-failed", ["--output-dir", "/tmp/o"]),
            ("rerun-remaining", ["--output-dir", "/tmp/o"]),
            ("repair", []),
            ("start", []),
        ):
            argv = [cmd]
            if cmd == "repair":
                argv += ["--repo", "/tmp/r"]
            elif cmd == "start":
                pass
            else:
                argv += ["--plan", "/tmp/p.json", "--repo", "/tmp/r",
                         "--backend", "openai"]
            argv += extra
            argv += ["--work-branch"]
            ns = p.parse_args(argv)
            assert ns.work_branch == "__saturnday_auto__", (
                f"{cmd} did not accept --work-branch"
            )

    def test_plan_does_NOT_accept_the_flag(self) -> None:
        """Design decision: plan doesn't commit, so the flag would be noise."""
        from saturnday.cli import build_parser
        p = build_parser()
        with pytest.raises(SystemExit):
            # plan rejects unknown args.
            p.parse_args([
                "plan", "--brief", "x", "--repo", "/tmp/r",
                "--backend", "openai", "--work-branch",
            ])


# ---------------------------------------------------------------------------
# RunResult + evidence round-trip
# ---------------------------------------------------------------------------


class TestRunResultEvidenceFields:
    def test_run_result_has_work_branch_fields(self) -> None:
        from saturnday._types import RunResult
        r = RunResult(project_id="p")
        assert hasattr(r, "git_parent_branch")
        assert hasattr(r, "git_parent_head_sha")
        assert hasattr(r, "git_work_branch")
        assert hasattr(r, "git_work_branch_auto_created")
        assert hasattr(r, "git_final_head_sha")
        # Defaults preserve back-compat — empty / False when feature inactive.
        assert r.git_parent_branch == ""
        assert r.git_work_branch == ""
        assert r.git_work_branch_auto_created is False

    def test_evidence_serialises_work_branch_fields(self, tmp_path: Path) -> None:
        from saturnday.run.evidence import write_run_summary
        from saturnday._types import RunResult

        result = RunResult(
            project_id="p",
            git_parent_branch="main",
            git_parent_head_sha="deadbeef" * 5,
            git_work_branch="saturnday/run-test",
            git_work_branch_auto_created=True,
            git_final_head_sha="cafef00d" * 5,
        )
        write_run_summary(result, tmp_path, plan_data={})
        data = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        assert data["git_parent_branch"] == "main"
        assert data["git_work_branch"] == "saturnday/run-test"
        assert data["git_work_branch_auto_created"] is True
        assert data["git_parent_head_sha"].startswith("deadbeef")
        assert data["git_final_head_sha"].startswith("cafef00d")

    def test_evidence_empty_fields_when_feature_inactive(self, tmp_path: Path) -> None:
        """Back-compat: a RunResult with no I.4 fields populated
        produces empty-string outputs — no new required JSON shape."""
        from saturnday.run.evidence import write_run_summary
        from saturnday._types import RunResult

        result = RunResult(project_id="p")
        write_run_summary(result, tmp_path, plan_data={})
        data = json.loads((tmp_path / "run-summary.json").read_text(encoding="utf-8"))
        assert data["git_work_branch"] == ""
        assert data["git_work_branch_auto_created"] is False


# ---------------------------------------------------------------------------
# Real-lifecycle proof — run_plan end-to-end with --work-branch
# ---------------------------------------------------------------------------


def _write_minimal_plan(tmp_path: Path) -> tuple[Path, Path]:
    """Legacy plan with one pre-skipped ticket — reaches run_plan without a coder."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    plan = {
        "version": 1,
        "project_id": "i4-lifecycle",
        "notes": "I.4 real-lifecycle proof",
        "operating_mode": "legacy_unclassified",
        "is_runnable_product": False,
        "definition_of_done": ["all_tickets_passed"],
        "tickets": [{
            "ticket_id": "T001", "goal": "noop",
            "acceptance_criteria": ["noop"],
        }],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path, repo


class TestRunPlanWorkBranchLifecycle:
    def test_run_plan_creates_and_stays_on_work_branch(self, tmp_path: Path) -> None:
        """run_plan with --work-branch creates the branch, runs the
        (pre-skipped) ticket loop, leaves the operator on the new branch,
        and records the metadata on RunResult."""
        from saturnday import ticket_runner as tr
        from saturnday._types import CoderConfig

        plan_path, repo = _write_minimal_plan(tmp_path)
        output_dir = tmp_path / "evidence"

        config = CoderConfig(backend="openai", base_url="", api_key="k", model="m")
        result = tr.run_plan(
            plan_path=plan_path, repo_path=repo, coder_config=config,
            standards_dir=repo, output_dir=output_dir,
            skip_tickets=frozenset({"T001"}),
            auto_repair=False, role_passes=False,
            work_branch="saturnday/run-lifecycle-test",
        )

        assert result.git_work_branch == "saturnday/run-lifecycle-test"
        assert result.git_parent_branch == "main"
        assert result.git_work_branch_auto_created is True
        assert len(result.git_parent_head_sha) == 40
        assert len(result.git_final_head_sha) == 40
        assert _current_branch(repo) == "saturnday/run-lifecycle-test"

    def test_no_flag_means_no_branch_creation(self, tmp_path: Path) -> None:
        """Without --work-branch, branch state is untouched."""
        from saturnday import ticket_runner as tr
        from saturnday._types import CoderConfig

        plan_path, repo = _write_minimal_plan(tmp_path)
        output_dir = tmp_path / "evidence"

        config = CoderConfig(backend="openai", base_url="", api_key="k", model="m")
        result = tr.run_plan(
            plan_path=plan_path, repo_path=repo, coder_config=config,
            standards_dir=repo, output_dir=output_dir,
            skip_tickets=frozenset({"T001"}),
            auto_repair=False, role_passes=False,
            # work_branch omitted
        )

        assert result.git_work_branch == ""
        assert result.git_parent_branch == ""
        assert _current_branch(repo) == "main"

    def test_dirty_working_tree_refuses(self, tmp_path: Path) -> None:
        """Dirty tree + --work-branch → RuntimeError surfaces refusal message."""
        from saturnday import ticket_runner as tr
        from saturnday._types import CoderConfig

        plan_path, repo = _write_minimal_plan(tmp_path)
        (repo / "dirty.txt").write_text("dirt\n", encoding="utf-8")
        output_dir = tmp_path / "evidence"

        config = CoderConfig(backend="openai", base_url="", api_key="k", model="m")
        with pytest.raises(RuntimeError) as exc:
            tr.run_plan(
                plan_path=plan_path, repo_path=repo, coder_config=config,
                standards_dir=repo, output_dir=output_dir,
                skip_tickets=frozenset({"T001"}),
                auto_repair=False, role_passes=False,
                work_branch="saturnday/run-dirty",
            )
        assert "working tree is dirty" in str(exc.value)
        # No branch was created.
        r = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", "saturnday/run-dirty"],
            capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Resume / rerun pick up recorded work branch
# ---------------------------------------------------------------------------


class TestResumePicksUpRecordedBranch:
    def test_read_recorded_work_branch_finds_it_in_run_summary(
        self, tmp_path: Path
    ) -> None:
        from saturnday.run.resume import _read_recorded_work_branch

        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        sub = evidence_dir / "evidence" / "run"
        sub.mkdir(parents=True)
        (sub / "run-summary.json").write_text(
            json.dumps({"git_work_branch": "saturnday/run-recorded"}),
            encoding="utf-8",
        )
        assert _read_recorded_work_branch(evidence_dir) == "saturnday/run-recorded"

    def test_read_recorded_work_branch_empty_when_absent(self, tmp_path: Path) -> None:
        from saturnday.run.resume import _read_recorded_work_branch

        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        assert _read_recorded_work_branch(evidence_dir) == ""

    def test_resume_plan_signature_exposes_work_branch(self) -> None:
        import inspect
        from saturnday.run import resume as resume_mod
        for name in ("resume_plan", "rerun_failed", "rerun_remaining"):
            fn = getattr(resume_mod, name)
            sig = inspect.signature(fn)
            assert "work_branch" in sig.parameters, (
                f"{name} does not accept work_branch"
            )

    def test_pre_flight_resume_same_branch_continues_in_place(
        self, tmp_path: Path
    ) -> None:
        from saturnday.run.work_branch import pre_flight_resume
        repo = tmp_path / "r"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "saturnday/run-resumed"],
            check=True, capture_output=True,
        )
        ctx = pre_flight_resume(repo, "saturnday/run-resumed")
        assert ctx.work_branch == "saturnday/run-resumed"
        assert ctx.auto_created is False
        assert _current_branch(repo) == "saturnday/run-resumed"

    def test_pre_flight_resume_checks_out_when_on_different_clean_branch(
        self, tmp_path: Path
    ) -> None:
        from saturnday.run.work_branch import pre_flight_resume
        repo = tmp_path / "r"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "saturnday/run-persisted"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "main"],
            check=True, capture_output=True,
        )
        # Working tree clean → should silently switch back.
        ctx = pre_flight_resume(repo, "saturnday/run-persisted")
        assert ctx.work_branch == "saturnday/run-persisted"
        assert _current_branch(repo) == "saturnday/run-persisted"

    def test_pre_flight_resume_refuses_on_dirty_when_switching(
        self, tmp_path: Path
    ) -> None:
        from saturnday.run.work_branch import (
            WorkBranchRefused, pre_flight_resume,
        )
        repo = tmp_path / "r"
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "saturnday/run-other"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "main"],
            check=True, capture_output=True,
        )
        (repo / "dirt.txt").write_text("x\n", encoding="utf-8")
        with pytest.raises(WorkBranchRefused) as exc:
            pre_flight_resume(repo, "saturnday/run-other")
        assert "dirty" in str(exc.value).lower()

    def test_pre_flight_resume_refuses_when_recorded_branch_vanished(
        self, tmp_path: Path
    ) -> None:
        from saturnday.run.work_branch import (
            WorkBranchRefused, pre_flight_resume,
        )
        repo = tmp_path / "r"
        _init_git_repo(repo)
        with pytest.raises(WorkBranchRefused) as exc:
            pre_flight_resume(repo, "saturnday/run-ghost")
        assert "no longer exists" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Canary: repair path also wires the flag
# ---------------------------------------------------------------------------


class TestRepairWiresWorkBranch:
    def test_repair_command_branch_creation_via_source_canary(self) -> None:
        """Source-level canary: _cmd_repair invokes pre_flight_create
        when args.work_branch is set.  End-to-end repair requires a
        coder backend; the canary keeps the test suite fast."""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "saturnday" / "cli.py"
        ).read_text(encoding="utf-8")
        # The code must call pre_flight_create inside _cmd_repair.
        repair_idx = src.index("def _cmd_repair")
        # Take a generous slice.
        slab = src[repair_idx:repair_idx + 6000]
        assert "pre_flight_create" in slab, (
            "_cmd_repair does not call pre_flight_create"
        )
        assert "work_branch" in slab
