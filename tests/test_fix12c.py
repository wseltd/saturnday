"""Fix 12.C: Live-path call-graph reachability.

Tests for _check_live_path_reachability in review.py.

Coverage requirements:
  1. Entrypoint that genuinely reaches its implementation is not flagged.
  2. Entrypoint whose claimed implementation is not reachable is flagged.
  3. CLI-style (Click) entrypoint case is covered.
  4. Generic private helpers are not flagged as live-path findings.
  5. Ambiguous / dynamic cases are handled conservatively (not flagged).
  6. Existing check results are correctly shaped.
"""

from __future__ import annotations

from pathlib import Path

from saturnday.review import _check_live_path_reachability


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return name


def _shape(result: dict) -> None:
    assert result["name"] == "live_path_reachability"
    assert result["status"] in ("PASS", "FAIL")
    assert result["severity"] == "warning"
    assert isinstance(result["findings"], list)
    assert isinstance(result["files_checked"], list)


# ---------------------------------------------------------------------------
# Requirement 1: Reachable implementation must NOT be flagged
# ---------------------------------------------------------------------------


def test_lpr_pass_impl_directly_called(tmp_path: Path) -> None:
    """Route handler that calls process_ implementation must PASS."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/analyze')\n"
        "def analyze_route():\n"
        "    return process_analysis('data')\n"
        "\n"
        "def process_analysis(code):\n"
        "    result = scan(code)\n"
        "    issues = check_issues(result)\n"
        "    return format_output(issues)\n"
    )
    f = _write(tmp_path, "src/routes.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    _shape(result)
    assert result["status"] == "PASS"


def test_lpr_pass_impl_reachable_via_intermediate(tmp_path: Path) -> None:
    """Implementation reachable through an intermediate function must PASS."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/run')\n"
        "def run_route():\n"
        "    return orchestrate('input')\n"
        "\n"
        "def orchestrate(data):\n"
        "    return execute_pipeline(data)\n"
        "\n"
        "def execute_pipeline(data):\n"
        "    step1 = transform(data)\n"
        "    step2 = invoke_worker(step1)\n"
        "    return format_result(step2)\n"
    )
    f = _write(tmp_path, "src/runner.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Requirement 2: Unreachable claimed implementation must be flagged
# ---------------------------------------------------------------------------


def test_lpr_fail_process_impl_bypassed(tmp_path: Path) -> None:
    """Route that returns a fake answer while process_ impl exists must FAIL."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/analyze')\n"
        "def analyze_route():\n"
        "    return {'status': 'fake'}\n"
        "\n"
        "def process_analysis(code):\n"
        "    result = scan(code)\n"
        "    findings = check_results(result)\n"
        "    return {'findings': findings, 'count': len(findings)}\n"
    )
    f = _write(tmp_path, "src/routes.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    _shape(result)
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "live_path_unreachable" for d in result["findings"])
    assert any("process_analysis" in d["detail"] for d in result["findings"])


def test_lpr_fail_execute_impl_bypassed(tmp_path: Path) -> None:
    """Route that calls nothing while execute_ impl exists must FAIL."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.post('/submit')\n"
        "def submit_route():\n"
        "    return {'queued': True}\n"
        "\n"
        "def execute_submission(payload):\n"
        "    validated = validate(payload)\n"
        "    stored = store_record(validated)\n"
        "    return notify_downstream(stored)\n"
    )
    f = _write(tmp_path, "src/api.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "FAIL"
    assert any("execute_submission" in d["detail"] for d in result["findings"])


# ---------------------------------------------------------------------------
# Requirement 3: CLI-style (Click) entrypoint case
# ---------------------------------------------------------------------------


def test_lpr_cli_click_command_unreachable(tmp_path: Path) -> None:
    """CLI command that bypasses handle_ impl must FAIL."""
    code = (
        "import click\n"
        "\n"
        "@click.command()\n"
        "def run():\n"
        "    print('done')\n"
        "\n"
        "def handle_run_request(args):\n"
        "    validated = validate(args)\n"
        "    result = execute_pipeline(validated)\n"
        "    return dispatch_output(result)\n"
    )
    f = _write(tmp_path, "src/cli_app.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "FAIL"
    assert any("handle_run_request" in d["detail"] for d in result["findings"])


def test_lpr_cli_command_reachable(tmp_path: Path) -> None:
    """CLI command that calls its handle_ impl must PASS."""
    code = (
        "import click\n"
        "\n"
        "@click.command()\n"
        "def run():\n"
        "    handle_run_request({})\n"
        "\n"
        "def handle_run_request(args):\n"
        "    validated = validate(args)\n"
        "    result = execute_pipeline(validated)\n"
        "    return dispatch_output(result)\n"
    )
    f = _write(tmp_path, "src/cli_app.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Requirement 4: Private helpers are not flagged
# ---------------------------------------------------------------------------


def test_lpr_private_helper_not_flagged(tmp_path: Path) -> None:
    """Private underscore-prefixed functions must NOT be flagged."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/go')\n"
        "def go_route():\n"
        "    return {'ok': True}\n"
        "\n"
        "def _handle_internal(data):\n"
        "    result = scan(data)\n"
        "    issues = check_issues(result)\n"
        "    return format_issues(issues)\n"
    )
    f = _write(tmp_path, "src/routes.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


def test_lpr_trivial_stub_not_flagged(tmp_path: Path) -> None:
    """Function with fewer than 3 body statements must NOT be flagged."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/go')\n"
        "def go_route():\n"
        "    return 'ok'\n"
        "\n"
        "def process_stub(x):\n"
        "    return x\n"
    )
    f = _write(tmp_path, "src/routes.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Requirement 5: Conservative handling of ambiguous / dynamic cases
# ---------------------------------------------------------------------------


def test_lpr_no_entrypoints_not_examined(tmp_path: Path) -> None:
    """Files with no route/command decorators are not examined; any impl inside PASS."""
    code = (
        "def handle_request(data):\n"
        "    result = process_data(data)\n"
        "    validated = execute_validation(result)\n"
        "    return dispatch_response(validated)\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


def test_lpr_name_without_impl_verb_not_flagged(tmp_path: Path) -> None:
    """Function with no impl verb in name (e.g. compute_hash) is not flagged."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/hash')\n"
        "def hash_route():\n"
        "    return {'hash': 'fake'}\n"
        "\n"
        "def compute_hash(data):\n"  # 'compute' is NOT in _IMPL_VERBS
        "    step1 = prepare(data)\n"
        "    step2 = hash_bytes(step1)\n"
        "    return encode(step2)\n"
    )
    f = _write(tmp_path, "src/routes.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


def test_lpr_excludes_test_files(tmp_path: Path) -> None:
    """Test files with route-like decorators must not produce findings."""
    code = (
        "@app.route('/test')\n"
        "def test_route():\n"
        "    return 'ok'\n"
        "\n"
        "def handle_test(x):\n"
        "    a = process(x)\n"
        "    b = execute(a)\n"
        "    return dispatch(b)\n"
    )
    f = _write(tmp_path, "tests/test_routes.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# Requirement 6: Result shape and edge cases
# ---------------------------------------------------------------------------


def test_lpr_result_shape_empty_input(tmp_path: Path) -> None:
    """Correct result shape when called with no files."""
    result = _check_live_path_reachability(tmp_path, [])
    _shape(result)
    assert result["status"] == "PASS"
    assert result["findings"] == []
    assert result["files_checked"] == []


def test_lpr_finding_contains_entrypoint_name(tmp_path: Path) -> None:
    """Finding detail must name the entrypoint that failed to reach the implementation."""
    code = (
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "\n"
        "@app.route('/data')\n"
        "def data_route():\n"
        "    return {}\n"
        "\n"
        "def dispatch_data(payload):\n"
        "    validated = validate(payload)\n"
        "    stored = persist(validated)\n"
        "    return confirm(stored)\n"
    )
    f = _write(tmp_path, "src/api.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "FAIL"
    finding = result["findings"][0]
    assert "data_route" in finding["detail"]
    assert "dispatch_data" in finding["detail"]


def test_lpr_fastapi_router_decorator(tmp_path: Path) -> None:
    """FastAPI @router.post decorator is recognised as an entrypoint."""
    code = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/items')\n"
        "def create_item():\n"
        "    return {'id': 1}\n"
        "\n"
        "def process_item_creation(data):\n"
        "    validated = validate_schema(data)\n"
        "    saved = save_to_db(validated)\n"
        "    return build_response(saved)\n"
    )
    f = _write(tmp_path, "src/items.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "FAIL"
    assert any("process_item_creation" in d["detail"] for d in result["findings"])


# ---------------------------------------------------------------------------
# Fix 33: FastAPI Depends() and async route handler fixes
# ---------------------------------------------------------------------------


def test_lpr_sync_route_depends_not_flagged(tmp_path: Path) -> None:
    """Sync route using Depends(dep_fn) does not falsely flag the provider."""
    code = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "def process_item_creation(data):\n"
        "    validated = validate_schema(data)\n"
        "    saved = save_to_db(validated)\n"
        "    return build_response(saved)\n"
        "\n"
        "@router.post('/items')\n"
        "def create_item(proc=Depends(process_item_creation)):\n"
        "    return proc\n"
    )
    f = _write(tmp_path, "src/items.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS", (
        "process_item_creation is injected via Depends — must not be flagged"
    )


def test_lpr_sync_route_annotated_depends_not_flagged(tmp_path: Path) -> None:
    """Sync route using Annotated[T, Depends(dep_fn)] does not falsely flag the provider."""
    code = (
        "from __future__ import annotations\n"
        "from typing import Annotated\n"
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "def process_item_creation(data):\n"
        "    validated = validate_schema(data)\n"
        "    saved = save_to_db(validated)\n"
        "    return build_response(saved)\n"
        "\n"
        "@router.post('/items')\n"
        "def create_item(proc: Annotated[object, Depends(process_item_creation)]):\n"
        "    return proc\n"
    )
    f = _write(tmp_path, "src/items.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS", (
        "process_item_creation is injected via Annotated+Depends — must not be flagged"
    )


def test_lpr_async_route_recognised_as_entrypoint(tmp_path: Path) -> None:
    """async def route handlers are recognised as entrypoints."""
    code = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.post('/items')\n"
        "async def create_item():\n"
        "    return {'id': 1}\n"
        "\n"
        "def process_item_creation(data):\n"
        "    validated = validate_schema(data)\n"
        "    saved = save_to_db(validated)\n"
        "    return build_response(saved)\n"
    )
    f = _write(tmp_path, "src/items.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    # async route is now an entrypoint — file is examined and the unreachable impl flagged
    assert result["status"] == "FAIL"
    assert any("process_item_creation" in d["detail"] for d in result["findings"])
    assert any(d["kind"] == "live_path_unreachable" for d in result["findings"])


def test_lpr_async_only_file_not_silently_skipped(tmp_path: Path) -> None:
    """An async-only FastAPI file is no longer silently skipped by this rule."""
    code = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/health')\n"
        "async def health_check():\n"
        "    return {'status': 'ok'}\n"
        "\n"
        "def handle_health_logic():\n"
        "    validated = check_db()\n"
        "    result = compute_status(validated)\n"
        "    return format_response(result)\n"
    )
    f = _write(tmp_path, "src/health.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    # File must be examined (not skipped) — entrypoint found from async def
    assert f in result["files_checked"], "async-only file must appear in files_checked"


def test_lpr_async_route_with_depends_not_flagged(tmp_path: Path) -> None:
    """Async route using Depends(dep_fn) does not flag the provider."""
    code = (
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "\n"
        "def process_item_creation(data):\n"
        "    validated = validate_schema(data)\n"
        "    saved = save_to_db(validated)\n"
        "    return build_response(saved)\n"
        "\n"
        "@router.post('/items')\n"
        "async def create_item(proc=Depends(process_item_creation)):\n"
        "    return proc\n"
    )
    f = _write(tmp_path, "src/items.py", code)
    result = _check_live_path_reachability(tmp_path, [f])
    assert result["status"] == "PASS", (
        "process_item_creation injected via Depends into async route — must not be flagged"
    )
