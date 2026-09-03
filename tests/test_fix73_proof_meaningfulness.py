"""Fix 73 — mode-aware proof generation + meaningfulness validation.

Gap-aware correction: when the planner CANNOT infer concrete proof details
from ticket hints, it emits a ``FIX73_PROOF_GAP`` marker that deliberately
fails at runtime with a clear message.  The validator recognises the marker
and passes it through.  HONESTY BEATS FAKE PROOF.

When the planner CAN infer (function names from acceptance_criteria,
endpoints from goals, CLI flags from goals), it emits a CONCRETE template
exercising those hints.

``pipeline`` and ``storage_only`` always emit concrete proofs (reliable
conventions: ``--input/--output`` and ``write/read`` round-trip).
"""

from __future__ import annotations

import json
from pathlib import Path

from saturnday.plan_parser import (
    validate_plan,
    validate_proof_meaningfulness,
)
from saturnday.run.planner import (
    FIX73_PROOF_GAP_PREFIX,
    _extract_proof_hints,
    _generate_local_proof_cmd,
)


def _check(mode: str, cmd: str, *, profile: str = "self_contained",
           realism: str = "production_intent") -> list[str]:
    return validate_proof_meaningfulness(
        operating_mode=mode,
        proof_realism=realism,
        dependency_profile=profile,
        local_proof_cmd=cmd,
    )


# ---------------------------------------------------------------------------
# Universal rejections (unchanged from prior corrections)
# ---------------------------------------------------------------------------


def test_tautology_true_is_rejected() -> None:
    for mode in ("library", "cli_tool", "web_service", "worker",
                 "pipeline", "frontend", "storage_only"):
        errs = _check(mode, "true")
        assert errs, f"mode={mode!r} accepted 'true' as proof"
        assert any("tautology" in e for e in errs), errs


def test_echo_OK_is_rejected() -> None:
    assert any("tautology" in e for e in _check("library", "echo OK"))


def test_failure_swallowing_is_rejected() -> None:
    assert any("swallows failure" in e for e in _check("cli_tool", "python -m foo run || true"))


def test_pure_pytest_is_rejected_for_runnable_modes() -> None:
    for mode in ("cli_tool", "web_service", "worker", "pipeline",
                 "frontend", "storage_only"):
        errs = _check(mode, "pytest tests/ -q")
        assert errs, f"mode={mode!r} accepted bare pytest"


def test_import_only_rejected_for_non_library() -> None:
    for mode in ("cli_tool", "web_service", "worker", "pipeline",
                 "frontend", "storage_only"):
        errs = _check(mode, 'python -c "import foo"')
        assert errs, f"mode={mode!r} accepted import-only"


def test_import_only_rejected_for_library_too() -> None:
    errs = _check("library", 'python -c "import foo"')
    assert any("more than 'import'" in e for e in errs), errs


# ---------------------------------------------------------------------------
# Fix 73 correction v2 — previously-weak shapes now rejected
# ---------------------------------------------------------------------------


def test_library_dir_pkg_is_rejected() -> None:
    errs = _check("library", 'python -c "import foo; assert dir(foo)"')
    assert any("introspection" in e for e in errs), errs


def test_library_hasattr_only_is_rejected() -> None:
    errs = _check("library", 'python -c "import foo; assert hasattr(foo, \'bar\')"')
    assert any("introspection" in e for e in errs), errs


def test_cli_tool_version_test_n_is_rejected() -> None:
    errs = _check("cli_tool", 'OUT=$(python -m foo --version 2>&1) && test -n "$OUT"')
    assert any("meaningful output assertion" in e for e in errs), errs


def test_web_service_sleep_then_curl_is_rejected() -> None:
    cmd = '(python -m app &) && sleep 2 && curl http://127.0.0.1:8000/'
    errs = _check("web_service", cmd)
    assert any("readiness signal" in e for e in errs), errs


def test_frontend_serve_single_html_grep_is_rejected() -> None:
    cmd = ('npm run build && (npx serve dist -l 8080 -s &) && sleep 2 && '
           'curl -fsS http://127.0.0.1:8080/ | grep -q "<html"')
    errs = _check("frontend", cmd)
    assert any("structural" in e or "headless" in e for e in errs), errs


def test_worker_return_only_is_rejected() -> None:
    cmd = ('python -c "from app import worker; r = worker.run_one(); '
           'assert r is not None"')
    errs = _check("worker", cmd)
    assert any("side effect" in e for e in errs), errs


# ---------------------------------------------------------------------------
# Gap marker behaviour
# ---------------------------------------------------------------------------


def test_gap_markers_pass_validator() -> None:
    """Honest proof-generation gaps are NOT rejected by the validator.
    They fail at runtime, which is the correct behaviour (forces operator
    to supply a real proof)."""
    for mode in ("library", "cli_tool", "web_service", "worker", "frontend"):
        cmd = _generate_local_proof_cmd(mode, "foo")
        assert FIX73_PROOF_GAP_PREFIX in cmd, f"mode={mode!r} did not emit a gap"
        errs = _check(mode, cmd)
        assert errs == [], f"mode={mode!r} gap rejected by validator: {errs}"


def test_gap_markers_contain_mode_and_example() -> None:
    for mode in ("library", "cli_tool", "web_service", "worker", "frontend"):
        cmd = _generate_local_proof_cmd(mode, "foo")
        assert mode in cmd, f"gap for {mode!r} must name the mode"
        assert "Supply explicit local_proof_cmd" in cmd


def test_pipeline_and_storage_do_not_emit_gaps() -> None:
    for mode in ("pipeline", "storage_only"):
        cmd = _generate_local_proof_cmd(mode, "foo")
        assert FIX73_PROOF_GAP_PREFIX not in cmd, f"mode={mode!r} should not gap"
        errs = _check(mode, cmd)
        assert errs == [], f"mode={mode!r}: {errs}"


# ---------------------------------------------------------------------------
# Hint sufficiency — library / cli_tool / web_service ALWAYS gap
# ---------------------------------------------------------------------------
#
# The planner's hint quality is NEVER sufficient for a genuine concrete
# proof in these three modes.  A function name tells us WHAT exists, not
# what inputs to use or what to assert.  A flag tells us the CLI ACCEPTS
# a flag, not what value to pass.  An endpoint tells us the route EXISTS,
# not what request body or response shape to expect.  These modes ALWAYS
# gap, but the gap message includes any detected hints to help the operator.


def test_library_always_gaps_even_with_function_hint() -> None:
    """function name + module path alone is insufficient — the planner
    cannot infer meaningful input or expected output from acceptance_criteria."""
    tickets = [{"acceptance_criteria": ["function compute exists in src/calc.py"],
                "goal": "Implement compute"}]
    cmd = _generate_local_proof_cmd("library", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd, "library must gap even when function hint is present"
    assert "compute" in cmd, "gap should surface detected hint"
    assert "src.calc" in cmd, "gap should surface detected module"


def test_library_gaps_without_hints_too() -> None:
    tickets = [{"acceptance_criteria": ["tests pass"], "goal": "Build something"}]
    cmd = _generate_local_proof_cmd("library", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd


def test_cli_tool_always_gaps_even_with_flag_hint() -> None:
    """A flag alone is insufficient — the planner cannot infer what value
    to pass, what output to expect, or what business behaviour it exercises."""
    tickets = [{"acceptance_criteria": [], "goal": "CLI accepts --input flag for source file"}]
    cmd = _generate_local_proof_cmd("cli_tool", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd, "cli_tool must gap even when flag hint is present"
    assert "--input" in cmd, "gap should surface detected flag"


def test_cli_tool_gaps_without_hints_too() -> None:
    tickets = [{"acceptance_criteria": ["tests pass"], "goal": "Build tool"}]
    cmd = _generate_local_proof_cmd("cli_tool", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd


def test_web_service_always_gaps_even_with_endpoint_hint() -> None:
    """An endpoint path alone is insufficient — the planner cannot infer
    request body, response shape, or business-level assertion."""
    tickets = [{"acceptance_criteria": [], "goal": "Create POST /api/v1/users endpoint"}]
    cmd = _generate_local_proof_cmd("web_service", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd, "web_service must gap even with endpoint hint"
    assert "/api/v1/users" in cmd, "gap should surface detected endpoint"


def test_web_service_gaps_without_hints_too() -> None:
    tickets = [{"acceptance_criteria": ["tests pass"], "goal": "Build API"}]
    cmd = _generate_local_proof_cmd("web_service", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd


def test_worker_always_emits_gap() -> None:
    """Worker's work-item creation path is too varied to infer safely."""
    tickets = [{"acceptance_criteria": [], "goal": "Process jobs from queue"}]
    cmd = _generate_local_proof_cmd("worker", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd


def test_frontend_always_emits_gap() -> None:
    """Frontend operator paths are too varied to infer safely."""
    tickets = [{"acceptance_criteria": [], "goal": "Build React dashboard"}]
    cmd = _generate_local_proof_cmd("frontend", "foo", tickets)
    assert FIX73_PROOF_GAP_PREFIX in cmd


# ---------------------------------------------------------------------------
# _extract_proof_hints
# ---------------------------------------------------------------------------


def test_extract_function_hint_from_acceptance_criteria() -> None:
    tickets = [{"acceptance_criteria": ["function parse_event exists in src/parser.py"]}]
    hints = _extract_proof_hints(tickets, "library")
    assert "parse_event" in hints["function_names"]
    assert "src.parser" in hints["module_paths"]


def test_extract_class_hint_from_acceptance_criteria() -> None:
    tickets = [{"acceptance_criteria": ["class Event exists in src/models.py"]}]
    hints = _extract_proof_hints(tickets, "library")
    assert "Event" in hints["function_names"]
    assert "src.models" in hints["module_paths"]


def test_extract_endpoint_hint_from_goal() -> None:
    tickets = [{"acceptance_criteria": [], "goal": "Create GET /api/users endpoint"}]
    hints = _extract_proof_hints(tickets, "web_service")
    assert "/api/users" in hints["endpoints"]


def test_extract_cli_flag_from_goal() -> None:
    tickets = [{"acceptance_criteria": [], "goal": "CLI accepts --output-dir flag"}]
    hints = _extract_proof_hints(tickets, "cli_tool")
    assert "--output-dir" in hints["cli_args"]


def test_extract_ignores_help_version_flags() -> None:
    tickets = [{"acceptance_criteria": [], "goal": "CLI supports --help and --version"}]
    hints = _extract_proof_hints(tickets, "cli_tool")
    assert "--help" not in hints["cli_args"]
    assert "--version" not in hints["cli_args"]


# ---------------------------------------------------------------------------
# Operator-supplied concrete proofs still pass validator when genuinely strong
# ---------------------------------------------------------------------------


def test_library_real_call_with_input_is_accepted() -> None:
    """An operator who supplies a genuine call with concrete input + a
    meaningful assertion passes the validator."""
    cmd = 'python -c "from foo import compute; assert compute(1) == 1"'
    errs = _check("library", cmd)
    assert errs == [], errs


def test_cli_tool_real_invocation_with_args_is_accepted() -> None:
    cmd = ('python <<\'PY\'\nimport subprocess, sys, re\n'
           'r = subprocess.run([sys.executable, "-m", "foo", "--input", "f.json"], '
           'capture_output=True, text=True, timeout=30)\n'
           'assert len(r.stdout) >= 10\nPY')
    errs = _check("cli_tool", cmd)
    assert errs == [], errs


def test_web_service_readiness_loop_with_business_endpoint_is_accepted() -> None:
    """An operator who supplies a real endpoint + readiness loop + a
    meaningful response assertion passes the validator.  The PLANNER
    cannot generate this, but the operator can."""
    cmd = (
        "python <<'PY'\n"
        "import subprocess, sys, time, urllib.request\n"
        "proc = subprocess.Popen([sys.executable, '-m', 'foo'])\n"
        "deadline = time.monotonic() + 30\n"
        "while time.monotonic() < deadline:\n"
        "    try:\n"
        "        urllib.request.urlopen('http://127.0.0.1:8000/api/v1/users', timeout=2)\n"
        "        break\n"
        "    except Exception:\n"
        "        time.sleep(0.5)\n"
        "with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/users') as resp:\n"
        "    assert resp.status == 200\n"
        "    assert len(resp.read()) >= 20\n"
        "proc.terminate()\n"
        "PY"
    )
    errs = _check("web_service", cmd)
    assert errs == [], errs


def test_frontend_with_playwright_is_accepted() -> None:
    cmd = "npm run build && npx playwright test e2e/smoke.spec.ts"
    errs = _check("frontend", cmd)
    assert errs == [], errs


def test_pipeline_concrete_is_accepted() -> None:
    errs = _check("pipeline", _generate_local_proof_cmd("pipeline", "foo"))
    assert errs == [], errs


def test_storage_only_concrete_is_accepted() -> None:
    errs = _check("storage_only", _generate_local_proof_cmd("storage_only", "foo"))
    assert errs == [], errs


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


def test_legacy_unclassified_bypasses_validator() -> None:
    errs = validate_proof_meaningfulness(
        operating_mode="legacy_unclassified",
        proof_realism="production_intent",
        dependency_profile="self_contained",
        local_proof_cmd="pytest tests/ -q",
    )
    assert errs == []


# ---------------------------------------------------------------------------
# Integration: validate_plan rejects trivial proofs and accepts concrete
# ---------------------------------------------------------------------------


def _good_plan(mode: str, cmd: str) -> dict:
    return {
        "version": 1, "project_id": "p",
        "operating_mode": mode,
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "local_proof_cmd": cmd,
        "tickets": [{"ticket_id": "T001", "goal": "do",
                      "acceptance_criteria": ["function f exists in src/m.py"]}],
    }


def test_validate_plan_rejects_trivial_web_service_proof() -> None:
    raw = _good_plan("web_service", "pytest tests/ -q")
    errs = validate_plan(raw)
    assert any("bare pytest" in e for e in errs), errs


def test_validate_plan_accepts_gap_for_web_service() -> None:
    gap = _generate_local_proof_cmd("web_service", "p")
    raw = _good_plan("web_service", gap)
    errs = validate_plan(raw)
    assert errs == [], errs


def test_validate_plan_accepts_gap_for_library() -> None:
    """Library always gaps now — validate_plan must accept the gap."""
    gap = _generate_local_proof_cmd("library", "p")
    raw = _good_plan("library", gap)
    errs = validate_plan(raw)
    assert errs == [], errs


def test_validate_plan_accepts_operator_supplied_library_proof() -> None:
    """An operator who edits the plan to supply a real proof passes."""
    raw = _good_plan("library",
                     'python -c "from src.m import f; assert f(42) == 84"')
    errs = validate_plan(raw)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# seeded_demo evidence
# ---------------------------------------------------------------------------


def test_seeded_demo_run_summary_includes_completion_note(tmp_path: Path) -> None:
    from saturnday._types import RunResult
    from saturnday.run.evidence import write_run_summary
    rr = RunResult(project_id="x", local_proof_attempted=True, local_proof_passed=True)
    out = tmp_path / "out"
    summary_path = write_run_summary(rr, out, plan_data={
        "operating_mode": "web_service", "dependency_profile": "self_contained",
        "proof_realism": "seeded_demo",
        "operator_disclaimer": "Demo: uses seeds/demo.json.",
        "governing_goal": "g", "required_outcomes": [],
        "scoped_categories": [], "exclusions": [],
    })
    data = json.loads(summary_path.read_text())
    assert data["proof_realism"] == "seeded_demo"
    assert "SEEDED_DEMO" in data.get("proof_completion_note", "")


def test_production_intent_run_summary_omits_completion_note(tmp_path: Path) -> None:
    from saturnday._types import RunResult
    from saturnday.run.evidence import write_run_summary
    rr = RunResult(project_id="x", local_proof_attempted=True, local_proof_passed=True)
    out = tmp_path / "out"
    summary_path = write_run_summary(rr, out, plan_data={
        "operating_mode": "library", "dependency_profile": "self_contained",
        "proof_realism": "production_intent", "operator_disclaimer": "",
        "governing_goal": "g", "required_outcomes": [],
        "scoped_categories": [], "exclusions": [],
    })
    data = json.loads(summary_path.read_text())
    assert "proof_completion_note" not in data
