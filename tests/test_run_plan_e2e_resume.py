"""End-to-end test for run_plan auto-resume, latest.json ordering, and evidence isolation.

Exercises the real run_plan() path with a minimal plan and mocked coder to prove:

1. A prior run exists with a prior ledger.
2. latest.json initially points to that prior run.
3. A new run_plan() invocation discovers the prior run for auto-resume
   BEFORE overwriting latest.json.
4. The project-id safety check still applies.
5. After the new run completes, latest.json points to the new run.
6. The returned RunResult.evidence_dir is the new run's actual directory.
7. The previous run's evidence is not overwritten.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from saturnday._types import CoderConfig
from saturnday.ticket_runner import _read_latest_pointer


def _create_test_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one Python file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (repo / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    # Create standards dir
    standards = repo / "standards"
    standards.mkdir()
    (standards / "engineering_standards.md").write_text("# Standards\nWrite good code.\n")
    return repo


def _create_plan(repo: Path, project_id: str = "test-proj") -> Path:
    """Write a minimal one-ticket plan under .saturnday/."""
    sat_dir = repo / ".saturnday"
    sat_dir.mkdir(parents=True, exist_ok=True)
    plan_path = sat_dir / "plan.json"
    plan = {
        "project_id": project_id,
        "notes": "test plan",
        "definition_of_done": "T001 passes",
        "stop_conditions": [],
        "max_project_tickets": 10,
        "phases": [],
        "tickets": [
            {
                "ticket_id": "T001",
                "title": "Test ticket",
                "goal": "Do a thing",
                "file_hints": ["main.py"],
                "acceptance_criteria": ["It works"],
                "dependencies": [],
            }
        ],
    }
    plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return plan_path


def _mock_llm_only():
    """Return patches for the minimum set of calls that require an LLM backend.

    Only these three functions need mocking — they call external LLM services
    that are not available in the test environment:

    - _execute_ticket: sends the ticket to the coder backend (LLM call)
    - _generate_progress_message: generates an LLM-authored progress blurb
    - analyze_and_split: may call LLM to decide whether to split a ticket;
      locally imported inside run_plan so the mock targets the source module

    Everything else runs for real against the test repo:
    - _ensure_project_venv, _ensure_git_exclude (real filesystem)
    - _snapshot_project_checks, _run_governance, run_post_checks (real checks)
    - _git_add, _git_commit (real git — operates on actual staged changes)
    - _auto_install_deps (real pip — no deps to install in test plan)
    - Evidence writing, ledger, latest.json, auto-resume (real filesystem)
    """
    patches = [
        patch("saturnday.ticket_runner._execute_ticket", return_value=("", [])),
        patch("saturnday.ticket_runner._generate_progress_message", return_value=""),
        patch(
            "saturnday.ticket_splitter.analyze_and_split",
            side_effect=lambda ticket, *a, **k: [ticket],
        ),
    ]
    return patches


class TestRunPlanE2EResumeOrdering:
    """End-to-end test through real run_plan() for resume and latest.json ordering."""

    def test_two_runs_full_lifecycle(self, tmp_path: Path) -> None:
        """Run 1 creates evidence + latest.json. Run 2 discovers Run 1 via
        latest.json for auto-resume, then updates latest.json to itself.
        Proves ordering, safety, and evidence isolation."""

        repo = _create_test_repo(tmp_path)
        plan_path = _create_plan(repo, project_id="test-proj")
        config = CoderConfig(backend="claude-cli")
        standards_dir = str(repo / "standards")

        from saturnday.ticket_runner import run_plan

        # === RUN 1 ===
        # Execute with T001 processed normally.
        # Mock heavy calls to avoid real coder/governance.
        patches = _mock_llm_only()
        for p in patches:
            p.start()
        try:
            result1 = run_plan(
                plan_path=str(plan_path),
                repo_path=str(repo),
                coder_config=config,
                standards_dir=standards_dir,
                output_dir=None,  # let run_plan generate unique dir
                role_passes=False,
            )
        finally:
            for p in patches:
                p.stop()

        # Verify Run 1 produced evidence
        run1_dir = Path(result1.evidence_dir)
        assert run1_dir.is_dir(), f"Run 1 evidence dir does not exist: {run1_dir}"
        assert (run1_dir / "run-metadata.json").is_file(), "Run 1 missing metadata"
        assert (run1_dir / "plan.json").is_file(), "Run 1 missing plan copy"
        assert (run1_dir / "run-summary.json").is_file(), "Run 1 missing summary"

        # Verify latest.json points to Run 1
        run_parent = repo / ".saturnday" / "run"
        latest1 = _read_latest_pointer(run_parent)
        assert latest1 is not None
        assert latest1["path"] == str(run1_dir)
        run1_id = latest1["run_id"]

        # Record Run 1 metadata content for later comparison
        run1_metadata = json.loads((run1_dir / "run-metadata.json").read_text())
        run1_summary = json.loads((run1_dir / "run-summary.json").read_text())

        # === RUN 2 ===
        # Run again with skip_tickets=None (auto-resume should discover Run 1).
        # Instrument _read_latest_pointer to capture what it returns DURING
        # auto-resume (before _write_latest_pointer overwrites it).
        captured_latest_during_resume: list[dict | None] = []
        original_read = _read_latest_pointer

        def instrumented_read(parent_dir: Path) -> dict | None:
            result = original_read(parent_dir)
            captured_latest_during_resume.append(result)
            return result

        patches2 = _mock_llm_only()
        for p in patches2:
            p.start()
        try:
            with patch(
                "saturnday.ticket_runner._read_latest_pointer",
                side_effect=instrumented_read,
            ):
                result2 = run_plan(
                    plan_path=str(plan_path),
                    repo_path=str(repo),
                    coder_config=config,
                    standards_dir=standards_dir,
                    output_dir=None,
                    role_passes=False,
                )
        finally:
            for p in patches2:
                p.stop()

        # === ASSERTIONS ===

        # 1. Run 2 got a different evidence dir
        run2_dir = Path(result2.evidence_dir)
        assert run2_dir.is_dir(), f"Run 2 evidence dir does not exist: {run2_dir}"
        assert run2_dir != run1_dir, "Run 2 must have a different dir than Run 1"

        # 2. During auto-resume, _read_latest_pointer saw Run 1 (not Run 2)
        assert len(captured_latest_during_resume) >= 1, \
            "_read_latest_pointer was never called during auto-resume"
        resume_read = captured_latest_during_resume[0]
        assert resume_read is not None, \
            "Auto-resume found no latest.json — should have found Run 1"
        assert resume_read["run_id"] == run1_id, \
            f"During auto-resume, latest.json pointed to {resume_read['run_id']}, " \
            f"expected {run1_id} (Run 1)"

        # 3. After Run 2, latest.json now points to Run 2
        latest2 = _read_latest_pointer(run_parent)
        assert latest2 is not None
        assert latest2["path"] == str(run2_dir)
        assert latest2["run_id"] != run1_id

        # 4. RunResult.evidence_dir matches the actual directory
        assert result2.evidence_dir == str(run2_dir)

        # 5. Run 1 evidence is NOT overwritten
        assert (run1_dir / "run-metadata.json").is_file()
        assert (run1_dir / "run-summary.json").is_file()
        assert (run1_dir / "plan.json").is_file()
        run1_metadata_after = json.loads((run1_dir / "run-metadata.json").read_text())
        run1_summary_after = json.loads((run1_dir / "run-summary.json").read_text())
        assert run1_metadata_after == run1_metadata, "Run 1 metadata was mutated"
        assert run1_summary_after == run1_summary, "Run 1 summary was mutated"

        # 6. Run 2 has its own independent evidence
        assert (run2_dir / "run-metadata.json").is_file()
        assert (run2_dir / "run-summary.json").is_file()
        assert (run2_dir / "plan.json").is_file()

    def test_mismatched_project_id_does_not_resume(self, tmp_path: Path) -> None:
        """When prior run has a different project_id, auto-resume must not skip tickets."""

        repo = _create_test_repo(tmp_path)
        config = CoderConfig(backend="claude-cli")
        standards_dir = str(repo / "standards")

        from saturnday.ticket_runner import run_plan

        # Run 1 with project_id "proj-alpha"
        plan1 = _create_plan(repo, project_id="proj-alpha")
        patches = _mock_llm_only()
        for p in patches:
            p.start()
        try:
            result1 = run_plan(
                plan_path=str(plan1),
                repo_path=str(repo),
                coder_config=config,
                standards_dir=standards_dir,
                output_dir=None,
                role_passes=False,
            )
        finally:
            for p in patches:
                p.stop()

        run1_dir = Path(result1.evidence_dir)
        assert run1_dir.is_dir()

        # Run 2 with different project_id "proj-beta"
        plan2 = _create_plan(repo, project_id="proj-beta")
        patches2 = _mock_llm_only()
        for p in patches2:
            p.start()
        try:
            result2 = run_plan(
                plan_path=str(plan2),
                repo_path=str(repo),
                coder_config=config,
                standards_dir=standards_dir,
                output_dir=None,
                role_passes=False,
            )
        finally:
            for p in patches2:
                p.stop()

        run2_dir = Path(result2.evidence_dir)

        # Run 2 must NOT have skipped T001 (different project_id)
        # Check that T001 was executed (not skipped) in run 2
        t001_results = [tr for tr in result2.ticket_results if tr.ticket_id == "T001"]
        assert len(t001_results) == 1
        # If it were skipped via auto-resume, disposition would be absent from
        # ticket_results (skip_tickets causes a `continue` before result recording).
        # Since it's present, it was executed.
        assert t001_results[0].disposition != "SKIP", \
            "T001 was skipped despite project_id mismatch — safety check failed"

        # Both runs have independent evidence
        assert run1_dir != run2_dir
        assert (run1_dir / "run-metadata.json").is_file()
        assert (run2_dir / "run-metadata.json").is_file()
