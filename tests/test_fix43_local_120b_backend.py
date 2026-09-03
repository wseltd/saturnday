"""Fix 43 — local GPT-OSS 120B backend integration tests.

Proves that:
1. "local-120b" appears in _SUPPORTED_BACKENDS and has the required fields.
2. _check_backend_ready("local-120b") returns False when vLLM is not running.
3. _check_backend_ready("local-120b") returns True when vLLM responds.
4. select_backend menu prompt reflects the actual count of _SUPPORTED_BACKENDS.
5. _build_coder_config("local-120b") returns openai backend with correct params.
6. _build_coder_config for any other backend returns backend unchanged.
7. _wait_for_backend("local-120b") shows Terminal 1 instructions and probes.
8. _wait_for_backend("local-120b") returns name when probe succeeds on retry.
9. guided_run passes local-120b CoderConfig to generate_plan and run_plan.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Test 1: "local-120b" in _SUPPORTED_BACKENDS with required fields
# ---------------------------------------------------------------------------

def test_local_120b_in_supported_backends() -> None:
    from saturnday.interactive import _SUPPORTED_BACKENDS
    names = [b["name"] for b in _SUPPORTED_BACKENDS]
    assert "local-120b" in names


def test_local_120b_backend_has_required_fields() -> None:
    from saturnday.interactive import _SUPPORTED_BACKENDS
    entry = next(b for b in _SUPPORTED_BACKENDS if b["name"] == "local-120b")
    assert "label" in entry
    assert "setup" in entry
    assert "localhost:8000" in entry["check"] or "localhost:8000" in entry["setup"]


# ---------------------------------------------------------------------------
# Test 2: _check_backend_ready returns False when vLLM not running
# ---------------------------------------------------------------------------

def test_check_backend_ready_local_120b_not_running() -> None:
    from saturnday.interactive import _check_backend_ready
    with patch("saturnday.interactive._probe_local_vllm", return_value=False):
        ready, reason = _check_backend_ready("local-120b")
    assert ready is False
    assert "not running" in reason.lower() or "vllm" in reason.lower()


# ---------------------------------------------------------------------------
# Test 3: _check_backend_ready returns True when vLLM responds
# ---------------------------------------------------------------------------

def test_check_backend_ready_local_120b_running() -> None:
    from saturnday.interactive import _check_backend_ready
    with patch("saturnday.interactive._probe_local_vllm", return_value=True):
        ready, reason = _check_backend_ready("local-120b")
    assert ready is True
    assert "8000" in reason or "responding" in reason.lower() or "vllm" in reason.lower()


# ---------------------------------------------------------------------------
# Test 4: select_backend prompt reflects actual backend count
# ---------------------------------------------------------------------------

def test_select_backend_prompt_is_dynamic() -> None:
    """The prompt must say Select [1-N] where N == len(_SUPPORTED_BACKENDS)."""
    import saturnday.interactive as _mod
    n = len(_mod._SUPPORTED_BACKENDS)
    expected_fragment = f"[1-{n}]"

    captured: list[str] = []

    def fake_input(prompt: str = "") -> str:
        captured.append(prompt)
        raise KeyboardInterrupt

    with patch("saturnday.interactive.input", fake_input), \
         patch("saturnday.interactive._check_backend_ready", return_value=(False, "x")):
        try:
            _mod.select_backend(session=None)
        except KeyboardInterrupt:
            pass

    assert any(expected_fragment in p for p in captured), (
        f"Expected '{expected_fragment}' in prompt, got: {captured}"
    )


# ---------------------------------------------------------------------------
# Test 5: _build_coder_config("local-120b") returns correct openai config
# ---------------------------------------------------------------------------

def test_build_coder_config_local_120b() -> None:
    from saturnday.interactive import _build_coder_config
    cfg = _build_coder_config("local-120b")
    assert cfg.backend == "openai"
    assert cfg.base_url == "http://localhost:8000/v1"
    assert cfg.model == "openai/gpt-oss-120b"
    assert cfg.api_key == "EMPTY"


# ---------------------------------------------------------------------------
# Test 6: _build_coder_config for other backends returns unchanged
# ---------------------------------------------------------------------------

def test_build_coder_config_standard_backend() -> None:
    from saturnday.interactive import _build_coder_config
    cfg = _build_coder_config("claude-cli")
    assert cfg.backend == "claude-cli"
    assert cfg.base_url == ""
    assert cfg.model == ""


def test_build_coder_config_openai_backend() -> None:
    from saturnday.interactive import _build_coder_config
    cfg = _build_coder_config("openai")
    assert cfg.backend == "openai"
    assert cfg.base_url == ""  # not the 120b override


# ---------------------------------------------------------------------------
# Test 7: _wait_for_backend("local-120b") shows Terminal 1 instructions
# ---------------------------------------------------------------------------

def test_wait_for_backend_local_120b_shows_instructions() -> None:
    from saturnday.interactive import _wait_for_backend

    outputs: list[str] = []
    call_count = 0

    def fake_print(*args: Any, **kwargs: Any) -> None:
        outputs.append(" ".join(str(a) for a in args))

    def fake_input(prompt: str = "") -> str:
        nonlocal call_count
        call_count += 1
        raise KeyboardInterrupt  # user aborts immediately

    with patch("saturnday.interactive._probe_local_vllm", return_value=False), \
         patch("saturnday.interactive._check_backend_ready", return_value=(False, "not running")), \
         patch("builtins.print", fake_print), \
         patch("saturnday.interactive.input", fake_input):
        result = _wait_for_backend("local-120b")

    assert result is None  # user aborted
    full_output = "\n".join(outputs)
    assert "terminal 1" in full_output.lower() or "Terminal 1" in full_output
    assert "vllm serve" in full_output.lower() or "vllm" in full_output.lower()
    assert "8000" in full_output


# ---------------------------------------------------------------------------
# Test 8: _wait_for_backend returns name when probe succeeds on retry
# ---------------------------------------------------------------------------

def test_wait_for_backend_local_120b_succeeds_on_retry() -> None:
    from saturnday.interactive import _wait_for_backend

    probe_calls = [False, True]  # first press: not ready; second press: ready
    probe_iter = iter(probe_calls)

    input_calls = 0

    def fake_input(prompt: str = "") -> str:
        nonlocal input_calls
        input_calls += 1
        return ""

    with patch("saturnday.interactive._probe_local_vllm", side_effect=probe_iter), \
         patch("saturnday.interactive._check_backend_ready", return_value=(False, "not running")), \
         patch("builtins.print"), \
         patch("saturnday.interactive.input", fake_input):
        result = _wait_for_backend("local-120b")

    assert result == "local-120b"
    assert input_calls == 2  # waited twice


# ---------------------------------------------------------------------------
# Test 9: guided_run uses local-120b CoderConfig (not plain openai)
# ---------------------------------------------------------------------------

def test_guided_run_local_120b_passes_correct_config(tmp_path: Path) -> None:
    """guided_run must pass CoderConfig(backend='openai', base_url=...) when backend=local-120b."""
    from saturnday.interactive import guided_run
    from saturnday._types import RunResult, TicketResult

    plan_data = {
        "version": 1,
        "project_id": "fix43-local120b",
        "tickets": [{"ticket_id": "T001", "goal": "do something", "acceptance_criteria": ["done"]}],
        "phases": [],
        "definition_of_done": [],
        "governing_goal": "test",
    }

    captured_configs: list[Any] = []

    def fake_generate_plan(brief, repo_path, coder_config, output_path, **kw):
        captured_configs.append(("generate_plan", coder_config))
        plan_path = Path(output_path)
        plan_path.write_text(json.dumps(plan_data), encoding="utf-8")
        return plan_path

    happy_result = RunResult(
        project_id="fix43-local120b",
        total_tickets=1,
        passed=1,
        failed=0,
        definition_of_done_met=True,
    )

    def fake_run_plan(plan_path, repo_path, coder_config, **kw):
        captured_configs.append(("run_plan", coder_config))
        return happy_result

    fake_session = MagicMock()
    fake_session.backend = "local-120b"
    fake_session.last_plan_path = None
    fake_session.last_evidence_dir = None

    with patch("saturnday.interactive.select_backend", return_value="local-120b"), \
         patch("saturnday.run.planner.generate_plan", fake_generate_plan), \
         patch("saturnday.ticket_runner.run_plan", fake_run_plan), \
         patch("saturnday.interactive._run_dod_gate", return_value=True), \
         patch("saturnday.interactive.show_run_summary"), \
         patch("saturnday.interactive.save_session"), \
         patch("saturnday.interactive._enrich_brief", return_value=""), \
         patch("saturnday.interactive.input", return_value=""):
        # Ensure repo has a .git dir (FU-01 guard)
        (tmp_path / ".git").mkdir()
        guided_run(tmp_path, goal="build something", session=fake_session)

    assert len(captured_configs) >= 1
    for _stage, cfg in captured_configs:
        assert cfg.backend == "openai", f"backend must be 'openai', got {cfg.backend!r}"
        assert cfg.base_url == "http://localhost:8000/v1"
        assert cfg.model == "openai/gpt-oss-120b"
        assert cfg.api_key == "EMPTY"
