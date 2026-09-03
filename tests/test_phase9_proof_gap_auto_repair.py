"""Phase 9 — non-interactive proof-gap auto-repair.

``_auto_repair_proof_gap`` is the CI-friendly sibling of Phase 6's
coder-retry branch.  When ``auto_repair=True`` AND the acceptance gate
fires AND ``local_proof_cmd`` still carries a ``FIX73_PROOF_GAP`` marker,
the runner calls the post-execution resolver once with the failure as
context and runs the derived proof.  This lets CI recover from a gap
without an operator.

Boundary cases:
- Resolver produces a new proof → run it, return ("", "") on pass or
  the new (cmd, failure) on fail.
- Resolver cannot derive → return the original (cmd, failure) unchanged.
- Resolver raises → swallow and return original unchanged.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday._types import CoderConfig, ProjectPlan, TicketResult, TicketSpec
from saturnday.ticket_runner import _auto_repair_proof_gap


def _plan(mode: str = "library") -> ProjectPlan:
    return ProjectPlan(
        version=1,
        project_id="p",
        tickets=(TicketSpec(ticket_id="T001", goal="g"),),
        operating_mode=mode,
        dependency_profile="self_contained",
        proof_realism="production_intent",
        testing_strategy="unspecified",
        external_dependencies=(),
        local_proof_cmd=(
            "python -c \"import sys; sys.exit('FIX73_PROOF_GAP: library "
            "acceptance unknown')\""
        ),
    )


def _cfg() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="k", model="m")


def _pass_ticket() -> TicketResult:
    return TicketResult(
        ticket_id="T001", disposition="PASS",
        attempts=1, changed_files=("src/m.py",),
    )


# ---------------------------------------------------------------------------
# Success path — derived proof passes
# ---------------------------------------------------------------------------


def test_auto_repair_derives_and_passes(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f(x): return x + 1\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            '```bash\n'
            'python -c "from src.m import f; assert f(1) == 2"\n'
            '```'
        )

    with (
        patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
    ):
        cmd, fail = _auto_repair_proof_gap(
            current_cmd=_plan().local_proof_cmd,
            current_failure="exit 1: FIX73_PROOF_GAP marker",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
            ticket_results=[_pass_ticket()],
        )

    assert fail == ""
    assert "f(1) == 2" in cmd


# ---------------------------------------------------------------------------
# Derived proof still fails
# ---------------------------------------------------------------------------


def test_auto_repair_derives_but_still_fails(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f(x): raise ValueError('no')\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            '```bash\n'
            'python -c "from src.m import f; assert f(1) == 1"\n'
            '```'
        )

    with (
        patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder),
        patch("saturnday.ticket_runner._run_verify_cmd",
              return_value="assertion error"),
    ):
        cmd, fail = _auto_repair_proof_gap(
            current_cmd=_plan().local_proof_cmd,
            current_failure="original fail",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
            ticket_results=[_pass_ticket()],
        )

    # Cmd is updated, but failure is from the new run.
    assert "f(1) == 1" in cmd
    assert fail == "assertion error"


# ---------------------------------------------------------------------------
# Resolver can't derive — preserve original cmd + failure
# ---------------------------------------------------------------------------


def test_auto_repair_resolver_insufficient_context(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("# no useful code\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        return "INSUFFICIENT_CONTEXT"

    original_cmd = _plan().local_proof_cmd
    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        cmd, fail = _auto_repair_proof_gap(
            current_cmd=original_cmd,
            current_failure="original fail",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
            ticket_results=[_pass_ticket()],
        )

    assert cmd == original_cmd
    assert fail == "original fail"


# ---------------------------------------------------------------------------
# Resolver raises — swallow, preserve original
# ---------------------------------------------------------------------------


def test_auto_repair_swallows_exception(tmp_path: Path) -> None:
    def fake_coder(cfg, messages, repo_path, **kw):
        raise RuntimeError("LLM unavailable")

    original_cmd = _plan().local_proof_cmd
    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        cmd, fail = _auto_repair_proof_gap(
            current_cmd=original_cmd,
            current_failure="original fail",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
            ticket_results=[_pass_ticket()],
        )

    assert cmd == original_cmd
    assert fail == "original fail"


# ---------------------------------------------------------------------------
# Validator rejection still preserves gap
# ---------------------------------------------------------------------------


def test_auto_repair_trivial_proof_rejected(tmp_path: Path) -> None:
    """Coder emits an import-only proof — the validator rejects it, so
    the resolver reports unresolved_gap and our helper preserves the
    original cmd + failure."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("def f(x): return x\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        return '```bash\npython -c "import src.m"\n```'

    original_cmd = _plan().local_proof_cmd
    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        cmd, fail = _auto_repair_proof_gap(
            current_cmd=original_cmd,
            current_failure="original fail",
            plan=_plan(),
            repo_path=tmp_path,
            coder_config=_cfg(),
            ticket_results=[_pass_ticket()],
        )

    assert cmd == original_cmd
    assert fail == "original fail"


# ---------------------------------------------------------------------------
# Worker mode — gap eligible at Phase 5 too, so auto-repair kicks in
# ---------------------------------------------------------------------------


def test_auto_repair_worker_mode(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text(
        "def run_one():\n    open('/tmp/x', 'w').write('ok')\n"
    )

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            "```bash\n"
            "python <<'PY'\n"
            "from src.worker import run_one\n"
            "import os\n"
            "run_one()\n"
            "assert os.path.exists('/tmp/x')\n"
            "PY\n"
            "```"
        )

    with (
        patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder),
        patch("saturnday.ticket_runner._run_verify_cmd", return_value=""),
    ):
        cmd, fail = _auto_repair_proof_gap(
            current_cmd=_plan("worker").local_proof_cmd,
            current_failure="gap marker",
            plan=_plan("worker"),
            repo_path=tmp_path,
            coder_config=_cfg(),
            ticket_results=[
                TicketResult(
                    ticket_id="T001", disposition="PASS", attempts=1,
                    changed_files=("src/worker.py",),
                ),
            ],
        )

    assert fail == ""
    assert "run_one" in cmd
