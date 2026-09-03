"""Phase 4 — plan-time coder-assisted proof resolution.

When the planner emits ``FIX73_PROOF_GAP`` for library / cli_tool /
web_service, ``run.proof_resolver.resolve_proof_gap`` invokes the coder
backend to derive a concrete proof, validates it, and either replaces
the gap or preserves it honestly.

Scope:
- Only library / cli_tool / web_service attempted at plan time.
- worker / frontend stay as gaps (their operator paths are too varied).
- On coder error, unparseable response, or validator rejection, the gap
  is preserved unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from saturnday._types import CoderConfig
from saturnday.run.planner import _generate_local_proof_cmd
from saturnday.run.proof_resolver import (
    _build_derivation_prompt,
    _extract_proof_command,
    proof_is_gap,
    resolve_proof_gap,
)


def _gap_plan(mode: str) -> dict:
    return {
        "project_id": "p",
        "operating_mode": mode,
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "testing_strategy": "unspecified",
        "external_dependencies": [],
        "local_proof_cmd": _generate_local_proof_cmd(mode, "p"),
        "notes": "",
    }


def _config() -> CoderConfig:
    return CoderConfig(backend="openai", api_key="k", model="m")


def _ticket(tid: str = "T001", goal: str = "Build foo",
            criteria: list | None = None) -> dict:
    return {
        "ticket_id": tid,
        "goal": goal,
        "acceptance_criteria": criteria or ["function foo exists in src/x.py"],
    }


# ---------------------------------------------------------------------------
# proof_is_gap
# ---------------------------------------------------------------------------


def test_proof_is_gap_recognises_marker() -> None:
    plan = _gap_plan("library")
    assert proof_is_gap(plan["local_proof_cmd"]) is True


def test_proof_is_gap_false_for_concrete() -> None:
    assert proof_is_gap("python -c 'from x import f; assert f(1) == 1'") is False


def test_proof_is_gap_false_for_empty() -> None:
    assert proof_is_gap("") is False


# ---------------------------------------------------------------------------
# Early-exit paths
# ---------------------------------------------------------------------------


def test_no_proof_returns_not_attempted() -> None:
    plan = {"operating_mode": "library", "local_proof_cmd": "",
            "dependency_profile": "self_contained",
            "proof_realism": "production_intent",
            "testing_strategy": "unspecified"}
    status, source = resolve_proof_gap(plan, _config(), "/tmp", [])
    assert status == "not_attempted"
    assert source == "none"


def test_concrete_proof_returns_resolved_from_planning() -> None:
    plan = {
        "operating_mode": "pipeline",
        "local_proof_cmd": _generate_local_proof_cmd("pipeline", "p"),
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "testing_strategy": "unspecified",
    }
    status, source = resolve_proof_gap(plan, _config(), "/tmp", [])
    assert status == "resolved_from_planning"
    assert source == "planner_heuristic"


def test_worker_gap_stays_unresolved_at_plan_time() -> None:
    plan = _gap_plan("worker")
    status, source = resolve_proof_gap(plan, _config(), "/tmp", [])
    assert status == "unresolved_gap"
    assert source == "planner_gap"
    # Gap preserved unchanged
    assert proof_is_gap(plan["local_proof_cmd"])


def test_frontend_gap_stays_unresolved_at_plan_time() -> None:
    plan = _gap_plan("frontend")
    status, source = resolve_proof_gap(plan, _config(), "/tmp", [])
    assert status == "unresolved_gap"
    assert source == "planner_gap"


# ---------------------------------------------------------------------------
# Successful coder-assisted derivation
# ---------------------------------------------------------------------------


def test_library_gap_resolved_by_coder_with_valid_proof() -> None:
    plan = _gap_plan("library")
    tickets = [_ticket(criteria=["function compute exists in src/calc.py"])]

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return (
            "```bash\n"
            "python -c \"from src.calc import compute; "
            "result = compute(5); assert result == 10\"\n"
            "```"
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", tickets)

    assert status == "resolved_coder_plan_time"
    assert source == "coder_plan_time"
    assert not proof_is_gap(plan["local_proof_cmd"])
    assert "compute" in plan["local_proof_cmd"]
    assert "assert" in plan["local_proof_cmd"]


def test_cli_tool_gap_resolved_by_coder() -> None:
    plan = _gap_plan("cli_tool")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return (
            "```bash\n"
            "python <<'PY'\n"
            "import subprocess, sys, re\n"
            "r = subprocess.run([sys.executable, '-m', 'p', '--input', 'data.json'], "
            "capture_output=True, text=True, timeout=30)\n"
            "assert r.returncode == 0\n"
            "assert len(r.stdout) >= 20\n"
            "PY\n"
            "```"
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp",
                                            [_ticket(goal="CLI with --input")])

    assert status == "resolved_coder_plan_time"
    assert "subprocess" in plan["local_proof_cmd"]


def test_web_service_gap_resolved_by_coder() -> None:
    plan = _gap_plan("web_service")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return (
            "```bash\n"
            "python <<'PY'\n"
            "import subprocess, sys, time, urllib.request\n"
            "proc = subprocess.Popen([sys.executable, '-m', 'p'])\n"
            "deadline = time.monotonic() + 30\n"
            "while time.monotonic() < deadline:\n"
            "    try:\n"
            "        urllib.request.urlopen('http://127.0.0.1:8000/api/users', timeout=2)\n"
            "        break\n"
            "    except Exception:\n"
            "        time.sleep(0.5)\n"
            "with urllib.request.urlopen('http://127.0.0.1:8000/api/users') as resp:\n"
            "    assert resp.status == 200\n"
            "    body = resp.read().decode()\n"
            "    assert len(body) >= 50\n"
            "proc.terminate()\n"
            "PY\n"
            "```"
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp",
                                            [_ticket(goal="POST /api/users")])

    assert status == "resolved_coder_plan_time"
    assert "urlopen" in plan["local_proof_cmd"]


# ---------------------------------------------------------------------------
# Validator rejection — gap preserved
# ---------------------------------------------------------------------------


def test_coder_returns_trivial_proof_is_rejected() -> None:
    """If the coder emits a trivial proof (import-only for library),
    the validator rejects it and the gap is preserved."""
    plan = _gap_plan("library")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return '```bash\npython -c "import src.calc"\n```'

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", [_ticket()])

    assert status == "unresolved_gap"
    assert source == "planner_gap"
    assert proof_is_gap(plan["local_proof_cmd"])  # gap preserved


def test_coder_returns_version_only_cli_is_rejected() -> None:
    plan = _gap_plan("cli_tool")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return "```bash\npython -m p --version\n```"

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", [_ticket()])

    assert status == "unresolved_gap"
    assert proof_is_gap(plan["local_proof_cmd"])


def test_coder_returns_sleep_then_curl_web_service_is_rejected() -> None:
    plan = _gap_plan("web_service")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return (
            "```bash\n"
            "(python -m p &) && sleep 2 && curl http://127.0.0.1:8000/\n"
            "```"
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", [_ticket()])

    assert status == "unresolved_gap"
    assert proof_is_gap(plan["local_proof_cmd"])


# ---------------------------------------------------------------------------
# Coder errors — gap preserved
# ---------------------------------------------------------------------------


def test_coder_exception_leaves_gap_unchanged() -> None:
    plan = _gap_plan("library")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        raise RuntimeError("LLM unavailable")

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", [_ticket()])

    assert status == "unresolved_gap"
    assert proof_is_gap(plan["local_proof_cmd"])


def test_coder_returns_insufficient_context_leaves_gap() -> None:
    plan = _gap_plan("library")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return "INSUFFICIENT_CONTEXT"

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", [_ticket()])

    assert status == "unresolved_gap"


def test_coder_returns_prose_no_fence_leaves_gap() -> None:
    """If the coder babbles instead of emitting a fenced command, the gap
    must be preserved — no fake extraction."""
    plan = _gap_plan("library")

    def fake_coder(cfg, messages, repo_path, **kwargs):
        return (
            "Sure, here's what I'd suggest for your library. "
            "You might want to try calling the compute function with a "
            "value and checking the result..."
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap(plan, _config(), "/tmp", [_ticket()])

    assert status == "unresolved_gap"


# ---------------------------------------------------------------------------
# _extract_proof_command — parser
# ---------------------------------------------------------------------------


def test_extract_from_bash_fence() -> None:
    r = "```bash\npython -c 'assert 1==1'\n```"
    assert _extract_proof_command(r) == "python -c 'assert 1==1'"


def test_extract_from_sh_fence() -> None:
    r = "```sh\ncurl http://example.com\n```"
    assert _extract_proof_command(r) == "curl http://example.com"


def test_extract_from_plain_fence() -> None:
    r = "```\npython -m foo\n```"
    assert _extract_proof_command(r) == "python -m foo"


def test_extract_from_single_line_command() -> None:
    r = "python -c 'print(1)'"
    assert _extract_proof_command(r) == "python -c 'print(1)'"


def test_extract_returns_empty_on_insufficient_context() -> None:
    assert _extract_proof_command("INSUFFICIENT_CONTEXT") == ""


def test_extract_returns_empty_on_prose() -> None:
    assert _extract_proof_command(
        "I think you should use pytest but I'm not sure"
    ) == ""


def test_extract_returns_empty_on_multiline_unfenced() -> None:
    """Multi-line text without a fence is unsafe to interpret as a proof."""
    r = "python -c 'x=1'\ndo something else\nalso this"
    assert _extract_proof_command(r) == ""


# ---------------------------------------------------------------------------
# Prompt construction — sanity checks
# ---------------------------------------------------------------------------


def test_prompt_includes_mode_rules_per_mode() -> None:
    p = _build_derivation_prompt(
        operating_mode="web_service",
        dependency_profile="external_dependencies",
        proof_realism="production_intent",
        testing_strategy="local_emulator",
        external_dependencies=["stripe"],
        tickets=[_ticket()],
        project_id="p",
        notes="build a payment service",
    )
    assert "WEB_SERVICE rules" in p
    assert "readiness" in p
    assert "loopback" in p.lower()
    assert "stripe" in p
    assert "local_emulator" in p


def test_prompt_caps_ticket_count() -> None:
    many = [_ticket(tid=f"T{i:03d}") for i in range(30)]
    p = _build_derivation_prompt(
        operating_mode="library",
        dependency_profile="self_contained",
        proof_realism="production_intent",
        testing_strategy="unspecified",
        external_dependencies=[],
        tickets=many,
        project_id="p",
        notes="",
    )
    assert "(+ 20 more tickets)" in p


def test_prompt_surfaces_insufficient_context_escape_hatch() -> None:
    p = _build_derivation_prompt(
        operating_mode="library",
        dependency_profile="self_contained",
        proof_realism="production_intent",
        testing_strategy="unspecified",
        external_dependencies=[],
        tickets=[],
        project_id="p",
        notes="",
    )
    assert "INSUFFICIENT_CONTEXT" in p
