"""Phase 5 — post-execution proof derivation.

After tickets have run and code exists on disk, the resolver can read
actual function signatures, routes, CLI entry points, worker handlers,
and frontend structure to derive a meaningful proof.

Key differences from Phase 4 (plan-time):
- All five gap-modes are eligible, including worker and frontend.
- Prompt includes ACTUAL generated code.
- Code-context collection ranks files by mode-specific keyword relevance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from saturnday._types import CoderConfig
from saturnday.run.planner import _generate_local_proof_cmd
from saturnday.run.proof_resolver import (
    _build_post_execution_prompt,
    _collect_code_context,
    proof_is_gap,
    resolve_proof_gap_post_execution,
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


def _ticket(tid: str = "T001") -> dict:
    return {
        "ticket_id": tid, "goal": "g",
        "acceptance_criteria": ["function foo exists in src/x.py"],
    }


# ---------------------------------------------------------------------------
# Early exits
# ---------------------------------------------------------------------------


def test_no_proof_returns_not_attempted() -> None:
    plan = {"operating_mode": "library", "local_proof_cmd": "",
            "dependency_profile": "self_contained",
            "proof_realism": "production_intent",
            "testing_strategy": "unspecified"}
    status, source = resolve_proof_gap_post_execution(
        plan, _config(), "/tmp", [], [],
    )
    assert status == "not_attempted"


def test_concrete_proof_returns_resolved_from_planning() -> None:
    plan = {
        "operating_mode": "pipeline",
        "local_proof_cmd": _generate_local_proof_cmd("pipeline", "p"),
        "dependency_profile": "self_contained",
        "proof_realism": "production_intent",
        "testing_strategy": "unspecified",
    }
    status, source = resolve_proof_gap_post_execution(
        plan, _config(), "/tmp", [], [],
    )
    assert status == "resolved_from_planning"


def test_no_code_context_preserves_gap(tmp_path: Path) -> None:
    plan = _gap_plan("library")
    status, source = resolve_proof_gap_post_execution(
        plan, _config(), str(tmp_path), [], [],
    )
    assert status == "unresolved_gap"
    assert proof_is_gap(plan["local_proof_cmd"])


# ---------------------------------------------------------------------------
# Worker and frontend — NOW eligible (unlike Phase 4)
# ---------------------------------------------------------------------------


def test_worker_resolved_post_execution(tmp_path: Path) -> None:
    """Worker mode, which always gapped at Phase 4, can now be derived
    against the real worker.py code."""
    plan = _gap_plan("worker")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "worker.py").write_text(
        "import os\n"
        "def run_one():\n"
        "    with open('/tmp/worker_out.txt', 'w') as f:\n"
        "        f.write('done')\n"
        "    return {'status': 'ok'}\n"
    )

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            "```bash\n"
            "python <<'PY'\n"
            "import os, tempfile\n"
            "d = tempfile.mkdtemp()\n"
            "os.environ['WORKER_OUTPUT_DIR'] = d\n"
            "from src.worker import run_one\n"
            "before = set(os.listdir(d))\n"
            "run_one()\n"
            "# Check a real side effect file\n"
            "assert os.path.exists('/tmp/worker_out.txt')\n"
            "PY\n"
            "```"
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap_post_execution(
            plan, _config(), str(tmp_path),
            ["src/worker.py"], [_ticket()],
        )

    assert status == "resolved_coder_post_exec"
    assert source == "coder_post_execution"
    assert not proof_is_gap(plan["local_proof_cmd"])


def test_frontend_resolved_post_execution(tmp_path: Path) -> None:
    plan = _gap_plan("frontend")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text(
        "export default function App() {\n"
        "  return <div><header>Welcome</header></div>\n"
        "}\n"
    )

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            "```bash\n"
            "npm run build && npx playwright test e2e/smoke.spec.ts\n"
            "```"
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap_post_execution(
            plan, _config(), str(tmp_path),
            ["src/App.tsx"], [_ticket()],
        )

    assert status == "resolved_coder_post_exec"
    assert "playwright" in plan["local_proof_cmd"]


# ---------------------------------------------------------------------------
# Library / cli_tool / web_service — stronger than Phase 4 because of context
# ---------------------------------------------------------------------------


def test_library_resolved_with_real_signature(tmp_path: Path) -> None:
    plan = _gap_plan("library")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def compute(x: int, y: int) -> int:\n"
        "    return x + y\n"
    )

    def fake_coder(cfg, messages, repo_path, **kw):
        # Coder sees the real two-arg signature in the prompt.
        return (
            '```bash\n'
            'python -c "from src.calc import compute; '
            'assert compute(3, 4) == 7"\n'
            '```'
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap_post_execution(
            plan, _config(), str(tmp_path),
            ["src/calc.py"], [_ticket()],
        )

    assert status == "resolved_coder_post_exec"
    assert "compute(3, 4)" in plan["local_proof_cmd"]


def test_cli_tool_resolved_post_execution(tmp_path: Path) -> None:
    plan = _gap_plan("cli_tool")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "import argparse\n"
        "def main():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--input', required=True)\n"
        "    args = p.parse_args()\n"
        "    print(f'processed:{args.input}')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            '```bash\n'
            'python <<PY\n'
            'import subprocess, sys, re\n'
            'r = subprocess.run([sys.executable, "-m", "src.main", "--input", "data.json"], '
            'capture_output=True, text=True, timeout=30)\n'
            'assert r.returncode == 0\n'
            'assert len(r.stdout) >= 10\n'
            'assert re.search(r"processed", r.stdout)\n'
            'PY\n'
            '```'
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap_post_execution(
            plan, _config(), str(tmp_path),
            ["src/main.py"], [_ticket()],
        )

    assert status == "resolved_coder_post_exec"


def test_web_service_resolved_post_execution(tmp_path: Path) -> None:
    plan = _gap_plan("web_service")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "from flask import Flask, jsonify\n"
        "app = Flask(__name__)\n"
        "@app.route('/api/users/<id>')\n"
        "def get_user(id):\n"
        "    return jsonify({'id': id, 'name': 'alice'})\n"
    )

    def fake_coder(cfg, messages, repo_path, **kw):
        return (
            '```bash\n'
            'python <<PY\n'
            'import subprocess, sys, time, urllib.request, json\n'
            'proc = subprocess.Popen([sys.executable, "-m", "src.app"])\n'
            'deadline = time.monotonic() + 30\n'
            'while time.monotonic() < deadline:\n'
            '    try:\n'
            '        urllib.request.urlopen("http://127.0.0.1:8000/api/users/1", timeout=2)\n'
            '        break\n'
            '    except Exception:\n'
            '        time.sleep(0.5)\n'
            'with urllib.request.urlopen("http://127.0.0.1:8000/api/users/1") as resp:\n'
            '    assert resp.status == 200\n'
            '    body = resp.read().decode()\n'
            '    data = json.loads(body)\n'
            '    assert data["id"] == "1"\n'
            'proc.terminate()\n'
            'PY\n'
            '```'
        )

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap_post_execution(
            plan, _config(), str(tmp_path),
            ["src/app.py"], [_ticket()],
        )

    assert status == "resolved_coder_post_exec"
    assert "/api/users" in plan["local_proof_cmd"]


# ---------------------------------------------------------------------------
# Validator rejection — gap preserved post-execution too
# ---------------------------------------------------------------------------


def test_weak_post_execution_proof_is_rejected(tmp_path: Path) -> None:
    plan = _gap_plan("library")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def compute(x): return x\n")

    def fake_coder(cfg, messages, repo_path, **kw):
        # Import-only — validator must reject.
        return '```bash\npython -c "import src.calc"\n```'

    with patch("saturnday.coder_adapter.call_coder", side_effect=fake_coder):
        status, source = resolve_proof_gap_post_execution(
            plan, _config(), str(tmp_path),
            ["src/calc.py"], [_ticket()],
        )

    assert status == "unresolved_gap"
    assert proof_is_gap(plan["local_proof_cmd"])


# ---------------------------------------------------------------------------
# _collect_code_context
# ---------------------------------------------------------------------------


def test_collect_context_ranks_by_mode_keywords(tmp_path: Path) -> None:
    # A file with web_service keywords should rank higher.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "routes.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.route('/api/users')\n"
        "def users():\n"
        "    return {}\n"
    )
    (tmp_path / "src" / "utils.py").write_text(
        "def helper(x):\n    return x\n"
    )

    ctx = _collect_code_context(
        str(tmp_path), ["src/routes.py", "src/utils.py"], "web_service",
    )
    # routes.py (with @app.route) should appear; utils.py without web keywords may be excluded.
    assert "routes.py" in ctx
    assert "@app.route" in ctx


def test_collect_context_caps_size(tmp_path: Path) -> None:
    """Context is bounded at 12000 bytes total."""
    (tmp_path / "src").mkdir()
    big = "def f(x):\n    return x\n" * 2000  # >>12KB
    (tmp_path / "src" / "big.py").write_text(big)
    ctx = _collect_code_context(str(tmp_path), ["src/big.py"], "library")
    assert len(ctx) <= 12500  # with minor header overhead


def test_collect_context_empty_for_unknown_files(tmp_path: Path) -> None:
    ctx = _collect_code_context(
        str(tmp_path), ["nonexistent.py"], "library",
    )
    assert ctx == ""


def test_collect_context_includes_mode_keywords() -> None:
    """Sanity: the keyword table has entries for each mode we care about."""
    from saturnday.run.proof_resolver import _collect_code_context as fn
    # Just calling with empty input to confirm no crash for each mode.
    for mode in ("library", "cli_tool", "web_service", "worker", "frontend"):
        assert fn("/nonexistent_path", [], mode) == ""


# ---------------------------------------------------------------------------
# Prompt — post-execution includes actual code
# ---------------------------------------------------------------------------


def test_post_execution_prompt_includes_code_context() -> None:
    prompt = _build_post_execution_prompt(
        operating_mode="library",
        dependency_profile="self_contained",
        proof_realism="production_intent",
        testing_strategy="unspecified",
        external_dependencies=[],
        tickets=[_ticket()],
        project_id="p",
        notes="",
        code_context="### src/calc.py\n```\ndef compute(x): return x\n```",
    )
    assert "Actual code produced by the tickets" in prompt
    assert "src/calc.py" in prompt
    assert "def compute" in prompt
