"""Fix 12.B: Truthfulness guards — fabrication pattern detection.

Tests for 7 new checks in review.py:
  1. constant_risk_suppression
  2. schema_write_read_coherence
  3. lookup_table_fabrication
  4. same_name_shape_divergence
  5. eval_preseeding
  6. test_vs_real_shape_parity
  7. doc_code_response_shape
"""

from __future__ import annotations

from pathlib import Path

import pytest

from saturnday.review import (
    _check_constant_risk_suppression,
    _check_doc_code_response_shape,
    _check_eval_preseeding,
    _check_lookup_table_fabrication,
    _check_same_name_shape_divergence,
    _check_schema_write_read_coherence,
    _check_test_vs_real_shape_parity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> str:
    """Write content to a file and return the relative path string."""
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return name


def _result_shape(result: dict, check_name: str) -> None:
    """Assert the standard check result shape is present."""
    assert result["name"] == check_name
    assert result["status"] in ("PASS", "FAIL")
    assert result["severity"] == "warning"
    assert isinstance(result["findings"], list)
    assert isinstance(result["files_checked"], list)


# ---------------------------------------------------------------------------
# 1. _check_constant_risk_suppression
# ---------------------------------------------------------------------------


def test_crs_detects_skip_governance_true(tmp_path: Path) -> None:
    """SKIP_GOVERNANCE = True at module level must be flagged."""
    f = _write(tmp_path, "src/module.py", "SKIP_GOVERNANCE = True\n")
    result = _check_constant_risk_suppression(tmp_path, [f])
    _result_shape(result, "constant_risk_suppression")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "constant_risk_suppression" for d in result["findings"])


def test_crs_detects_disable_auth_true(tmp_path: Path) -> None:
    """DISABLE_AUTH = True at module level must be flagged."""
    f = _write(tmp_path, "src/auth.py", "DISABLE_AUTH = True\n")
    result = _check_constant_risk_suppression(tmp_path, [f])
    assert result["status"] == "FAIL"


def test_crs_ignores_false_value(tmp_path: Path) -> None:
    """SKIP_SECURITY = False must NOT be flagged (value is False)."""
    f = _write(tmp_path, "src/cfg.py", "SKIP_SECURITY = False\n")
    result = _check_constant_risk_suppression(tmp_path, [f])
    assert result["status"] == "PASS"


def test_crs_ignores_non_risk_name(tmp_path: Path) -> None:
    """VERBOSE = True must NOT be flagged (no risk keyword in name)."""
    f = _write(tmp_path, "src/cfg.py", "VERBOSE = True\n")
    result = _check_constant_risk_suppression(tmp_path, [f])
    assert result["status"] == "PASS"


def test_crs_excludes_test_files(tmp_path: Path) -> None:
    """SKIP_GOVERNANCE = True inside a test file must NOT be flagged."""
    f = _write(tmp_path, "tests/test_cfg.py", "SKIP_GOVERNANCE = True\n")
    result = _check_constant_risk_suppression(tmp_path, [f])
    assert result["status"] == "PASS"


def test_crs_requires_module_level(tmp_path: Path) -> None:
    """SKIP_GOVERNANCE = True inside a function must NOT be flagged (not module-level)."""
    code = "def setup():\n    SKIP_GOVERNANCE = True\n"
    f = _write(tmp_path, "src/module.py", code)
    result = _check_constant_risk_suppression(tmp_path, [f])
    assert result["status"] == "PASS"


def test_crs_result_shape(tmp_path: Path) -> None:
    """Return shape is correct when no files are provided."""
    result = _check_constant_risk_suppression(tmp_path, [])
    _result_shape(result, "constant_risk_suppression")


# ---------------------------------------------------------------------------
# 2. _check_schema_write_read_coherence
# ---------------------------------------------------------------------------


def test_swr_detects_optional_field_with_no_write_site(tmp_path: Path) -> None:
    """Optional field that is read but never written in corpus must be flagged."""
    # The dataclass defines 'risk_score: Optional[float] = None'.
    # The same file reads obj.risk_score but nothing ever assigns it.
    code = (
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "@dataclass\n"
        "class AnalysisResult:\n"
        "    status: str\n"
        "    risk_score: Optional[float] = None\n"
        "\n"
        "def display(obj: AnalysisResult) -> str:\n"
        "    return f'risk={obj.risk_score}'\n"
    )
    f = _write(tmp_path, "src/analysis.py", code)
    result = _check_schema_write_read_coherence(tmp_path, [f])
    _result_shape(result, "schema_write_read_coherence")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "schema_write_gap" for d in result["findings"])
    assert any("risk_score" in d["detail"] for d in result["findings"])


def test_swr_pass_when_field_written_via_attribute(tmp_path: Path) -> None:
    """Optional field assigned via obj.field = ... must NOT be flagged."""
    code = (
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "@dataclass\n"
        "class Result:\n"
        "    risk_score: Optional[float] = None\n"
        "\n"
        "def compute(obj):\n"
        "    obj.risk_score = 0.9\n"
        "    return obj.risk_score\n"
    )
    f = _write(tmp_path, "src/compute.py", code)
    result = _check_schema_write_read_coherence(tmp_path, [f])
    assert result["status"] == "PASS"


def test_swr_pass_when_field_written_via_constructor_kwarg(tmp_path: Path) -> None:
    """Optional field populated as constructor keyword arg must NOT be flagged."""
    code = (
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "@dataclass\n"
        "class Result:\n"
        "    risk_score: Optional[float] = None\n"
        "\n"
        "def make_result(score):\n"
        "    return Result(risk_score=score)\n"
    )
    f = _write(tmp_path, "src/factory.py", code)
    result = _check_schema_write_read_coherence(tmp_path, [f])
    assert result["status"] == "PASS"


def test_swr_pass_when_field_neither_read_nor_written(tmp_path: Path) -> None:
    """Optional field defined but unused entirely must NOT be flagged (not actively orphaned)."""
    code = (
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "@dataclass\n"
        "class Result:\n"
        "    risk_score: Optional[float] = None\n"
    )
    f = _write(tmp_path, "src/schema.py", code)
    result = _check_schema_write_read_coherence(tmp_path, [f])
    assert result["status"] == "PASS"


def test_swr_excludes_non_optional_fields(tmp_path: Path) -> None:
    """Non-Optional field that is never written must NOT be flagged."""
    code = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Result:\n"
        "    name: str\n"
        "\n"
        "def display(obj):\n"
        "    return obj.name\n"
    )
    f = _write(tmp_path, "src/schema.py", code)
    result = _check_schema_write_read_coherence(tmp_path, [f])
    assert result["status"] == "PASS"


def test_swr_excludes_test_files(tmp_path: Path) -> None:
    """Schema class in test file must NOT be inspected."""
    code = (
        "from dataclasses import dataclass\n"
        "from typing import Optional\n"
        "@dataclass\n"
        "class Result:\n"
        "    risk_score: Optional[float] = None\n"
        "\n"
        "def show(obj):\n"
        "    return obj.risk_score\n"
    )
    f = _write(tmp_path, "tests/test_schema.py", code)
    result = _check_schema_write_read_coherence(tmp_path, [f])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# 3. _check_lookup_table_fabrication
# ---------------------------------------------------------------------------


def test_ltf_detects_static_dict_return(tmp_path: Path) -> None:
    """Analytical function body = dict assign + return name must be flagged."""
    code = (
        "def check_security(code):\n"
        "    RESULT = {'status': 'pass', 'findings': []}\n"
        "    return RESULT\n"
    )
    f = _write(tmp_path, "src/checks.py", code)
    result = _check_lookup_table_fabrication(tmp_path, [f])
    _result_shape(result, "lookup_table_fabrication")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "lookup_table_fabrication" for d in result["findings"])


def test_ltf_detects_inline_dict_return(tmp_path: Path) -> None:
    """Analytical function that returns a dict literal directly must be flagged."""
    code = (
        "def analyze_risk(text):\n"
        "    return {'risk': 0, 'action': 'none'}\n"
    )
    f = _write(tmp_path, "src/analysis.py", code)
    result = _check_lookup_table_fabrication(tmp_path, [f])
    assert result["status"] == "FAIL"


def test_ltf_pass_when_has_conditional_logic(tmp_path: Path) -> None:
    """Analytical function with if/else logic must NOT be flagged."""
    code = (
        "def check_security(code):\n"
        "    if 'eval' in code:\n"
        "        return {'status': 'fail', 'findings': ['eval']}\n"
        "    return {'status': 'pass', 'findings': []}\n"
    )
    f = _write(tmp_path, "src/checks.py", code)
    result = _check_lookup_table_fabrication(tmp_path, [f])
    assert result["status"] == "PASS"


def test_ltf_pass_when_has_function_call(tmp_path: Path) -> None:
    """Analytical function that calls other code must NOT be flagged."""
    code = (
        "def check_security(code):\n"
        "    issues = _scan(code)\n"
        "    return {'status': 'fail' if issues else 'pass', 'findings': issues}\n"
    )
    f = _write(tmp_path, "src/checks.py", code)
    result = _check_lookup_table_fabrication(tmp_path, [f])
    assert result["status"] == "PASS"


def test_ltf_ignores_non_analytical_prefix(tmp_path: Path) -> None:
    """Non-analytical function (get_, create_, etc.) is not inspected."""
    code = (
        "def get_defaults(cfg):\n"
        "    RESULT = {'debug': False, 'timeout': 30}\n"
        "    return RESULT\n"
    )
    f = _write(tmp_path, "src/config.py", code)
    result = _check_lookup_table_fabrication(tmp_path, [f])
    assert result["status"] == "PASS"


def test_ltf_excludes_test_files(tmp_path: Path) -> None:
    """Fabrication pattern in test file must NOT be flagged."""
    code = (
        "def check_x(code):\n"
        "    RESULT = {'status': 'pass'}\n"
        "    return RESULT\n"
    )
    f = _write(tmp_path, "tests/test_checks.py", code)
    result = _check_lookup_table_fabrication(tmp_path, [f])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# 4. _check_same_name_shape_divergence
# ---------------------------------------------------------------------------


def test_snd_detects_incompatible_shapes(tmp_path: Path) -> None:
    """Same class name with divergent field sets (≥2 difference) must be flagged."""
    fa = _write(tmp_path, "src/models_a.py",
                "class Widget:\n    def __init__(self, id, name, price, stock): pass\n")
    fb = _write(tmp_path, "src/models_b.py",
                "class Widget:\n    def __init__(self, uid, label, cost, available, sku): pass\n")
    result = _check_same_name_shape_divergence(tmp_path, [fa, fb])
    _result_shape(result, "same_name_shape_divergence")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "same_name_shape_divergence" for d in result["findings"])


def test_snd_pass_when_shapes_match(tmp_path: Path) -> None:
    """Same class name with identical field sets must NOT be flagged."""
    fa = _write(tmp_path, "src/models_a.py",
                "class Widget:\n    def __init__(self, id, name, price): pass\n")
    fb = _write(tmp_path, "src/models_b.py",
                "class Widget:\n    def __init__(self, id, name, price): pass\n")
    result = _check_same_name_shape_divergence(tmp_path, [fa, fb])
    assert result["status"] == "PASS"


def test_snd_pass_when_difference_is_small(tmp_path: Path) -> None:
    """Divergence of only 1 field must NOT be flagged (noise threshold)."""
    fa = _write(tmp_path, "src/models_a.py",
                "class Widget:\n    def __init__(self, id, name, price): pass\n")
    fb = _write(tmp_path, "src/models_b.py",
                "class Widget:\n    def __init__(self, id, name, price, extra): pass\n")
    result = _check_same_name_shape_divergence(tmp_path, [fa, fb])
    assert result["status"] == "PASS"


def test_snd_excludes_test_files(tmp_path: Path) -> None:
    """Class divergence inside test files must NOT be flagged."""
    fa = _write(tmp_path, "src/models.py",
                "class Widget:\n    def __init__(self, id, name, price, stock): pass\n")
    fb = _write(tmp_path, "tests/test_models.py",
                "class Widget:\n    def __init__(self, uid, label, cost, available, sku): pass\n")
    result = _check_same_name_shape_divergence(tmp_path, [fa, fb])
    assert result["status"] == "PASS"


def test_snd_uses_annotated_fields(tmp_path: Path) -> None:
    """Divergence detected via class-level annotations (dataclass style)."""
    fa = _write(tmp_path, "src/schema_a.py",
                "class Record:\n    id: int\n    name: str\n    score: float\n    rank: int\n")
    fb = _write(tmp_path, "src/schema_b.py",
                "class Record:\n    uid: str\n    label: str\n    weight: float\n    tier: str\n")
    result = _check_same_name_shape_divergence(tmp_path, [fa, fb])
    assert result["status"] == "FAIL"


# ---------------------------------------------------------------------------
# 5. _check_eval_preseeding
# ---------------------------------------------------------------------------


def test_ep_detects_expected_var_as_arg(tmp_path: Path) -> None:
    """expected_output loaded from json.load and passed to a function must be flagged."""
    code = (
        "import json\n"
        "def test_model():\n"
        "    with open('f.json') as fh:\n"
        "        expected_output = json.load(fh)\n"
        "    result = model.predict(input_data=expected_output)\n"
        "    assert result == expected_output\n"
    )
    f = _write(tmp_path, "tests/test_eval.py", code)
    result = _check_eval_preseeding(tmp_path, [f])
    _result_shape(result, "eval_preseeding")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "eval_preseeding" for d in result["findings"])


def test_ep_pass_when_only_in_assertion(tmp_path: Path) -> None:
    """expected_output only used in assertion (not as function arg) must NOT be flagged."""
    code = (
        "import json\n"
        "def test_model():\n"
        "    with open('f.json') as fh:\n"
        "        expected_output = json.load(fh)\n"
        "    result = model.predict(input_data='raw input')\n"
        "    assert result == expected_output\n"
    )
    f = _write(tmp_path, "tests/test_eval.py", code)
    result = _check_eval_preseeding(tmp_path, [f])
    assert result["status"] == "PASS"


def test_ep_pass_when_no_preseed_names(tmp_path: Path) -> None:
    """Regular test without preseed-named variables must NOT be flagged."""
    code = (
        "def test_basic():\n"
        "    data = load_fixture('data.json')\n"
        "    result = compute(data)\n"
        "    assert result['status'] == 'pass'\n"
    )
    f = _write(tmp_path, "tests/test_basic.py", code)
    result = _check_eval_preseeding(tmp_path, [f])
    assert result["status"] == "PASS"


def test_ep_ignores_non_test_source_files(tmp_path: Path) -> None:
    """Source file with expected_ vars used as args must NOT be flagged."""
    code = (
        "def process():\n"
        "    expected_output = load('data.json')\n"
        "    result = run(expected_output)\n"
    )
    f = _write(tmp_path, "src/processor.py", code)
    result = _check_eval_preseeding(tmp_path, [f])
    assert result["status"] == "PASS"


def test_ep_ground_truth_prefix_detected(tmp_path: Path) -> None:
    """ground_truth_ variables loaded from a file and passed as args must be flagged."""
    code = (
        "def test_benchmark():\n"
        "    ground_truth_labels = load_dataset('labels.json')\n"
        "    preds = classifier.predict(inputs=ground_truth_labels)\n"
    )
    f = _write(tmp_path, "tests/test_benchmark.py", code)
    result = _check_eval_preseeding(tmp_path, [f])
    assert result["status"] == "FAIL"


# ---------------------------------------------------------------------------
# 6. _check_test_vs_real_shape_parity
# ---------------------------------------------------------------------------


def test_tvr_detects_ghost_key_in_mock(tmp_path: Path) -> None:
    """Mock with ghost_key not in real function returns must be flagged."""
    test_code = (
        "from unittest.mock import patch\n"
        "def test_x():\n"
        "    with patch('mymod.do_check') as mock_check:\n"
        "        mock_check.return_value = {'status': 'pass', 'ghost_key': True}\n"
        "        result = do_check('code')\n"
    )
    src_code = (
        "def do_check(code):\n"
        "    return {'status': 'pass', 'findings': []}\n"
    )
    tf = _write(tmp_path, "tests/test_checks.py", test_code)
    sf = _write(tmp_path, "src/mymod.py", src_code)
    result = _check_test_vs_real_shape_parity(tmp_path, [tf, sf])
    _result_shape(result, "test_vs_real_shape_parity")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "mock_shape_mismatch" for d in result["findings"])
    assert any("ghost_key" in d["detail"] for d in result["findings"])


def test_tvr_pass_when_mock_is_subset_of_real(tmp_path: Path) -> None:
    """Mock keys that are a subset of real keys must NOT be flagged."""
    test_code = (
        "from unittest.mock import patch\n"
        "def test_x():\n"
        "    with patch('mymod.do_check') as mock_check:\n"
        "        mock_check.return_value = {'status': 'pass'}\n"
        "        result = do_check('code')\n"
    )
    src_code = (
        "def do_check(code):\n"
        "    return {'status': 'pass', 'findings': [], 'extra': None}\n"
    )
    tf = _write(tmp_path, "tests/test_checks.py", test_code)
    sf = _write(tmp_path, "src/mymod.py", src_code)
    result = _check_test_vs_real_shape_parity(tmp_path, [tf, sf])
    assert result["status"] == "PASS"


def test_tvr_pass_when_no_patches(tmp_path: Path) -> None:
    """Test file with no patch() calls must not flag anything."""
    test_code = (
        "def test_x():\n"
        "    result = compute(42)\n"
        "    assert result > 0\n"
    )
    tf = _write(tmp_path, "tests/test_x.py", test_code)
    result = _check_test_vs_real_shape_parity(tmp_path, [tf])
    assert result["status"] == "PASS"


def test_tvr_pass_when_real_function_not_in_changed_files(tmp_path: Path) -> None:
    """If real function not scanned, no false positive — conservative skip."""
    test_code = (
        "from unittest.mock import patch\n"
        "def test_x():\n"
        "    with patch('other.module.func') as mock_func:\n"
        "        mock_func.return_value = {'only_in_mock': True}\n"
        "        func()\n"
    )
    tf = _write(tmp_path, "tests/test_x.py", test_code)
    # Real function is NOT in changed_files → check skips comparison
    result = _check_test_vs_real_shape_parity(tmp_path, [tf])
    assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# 7. _check_doc_code_response_shape
# ---------------------------------------------------------------------------


def test_dcr_detects_missing_documented_key(tmp_path: Path) -> None:
    """Docstring Returns with {'claimed_key': ...} not in actual return must be flagged."""
    code = (
        'def get_result(x):\n'
        '    """Get the result.\n\n'
        '    Returns: {"status": str, "claimed_key": str}\n'
        '    """\n'
        "    return {'status': 'ok'}\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_doc_code_response_shape(tmp_path, [f])
    _result_shape(result, "doc_code_response_shape")
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "doc_shape_drift" for d in result["findings"])
    assert any("claimed_key" in d["detail"] for d in result["findings"])


def test_dcr_pass_when_documented_keys_present(tmp_path: Path) -> None:
    """Docstring keys that appear in actual return dict must NOT be flagged."""
    code = (
        'def get_result(x):\n'
        '    """Get the result.\n\n'
        '    Returns: {"status": str, "findings": list}\n'
        '    """\n'
        "    return {'status': 'ok', 'findings': []}\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_doc_code_response_shape(tmp_path, [f])
    assert result["status"] == "PASS"


def test_dcr_pass_when_no_dict_in_returns_section(tmp_path: Path) -> None:
    """Narrative-only Returns section without explicit dict must NOT be flagged."""
    code = (
        'def get_result(x):\n'
        '    """Get the result.\n\n'
        '    Returns: A string indicating the outcome.\n'
        '    """\n'
        "    return {'status': 'ok'}\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_doc_code_response_shape(tmp_path, [f])
    assert result["status"] == "PASS"


def test_dcr_pass_when_no_return_dicts_in_function(tmp_path: Path) -> None:
    """Function with no return dict literals is skipped (not enough data)."""
    code = (
        'def get_result(x):\n'
        '    """Get the result.\n\n'
        '    Returns: {"key": str}\n'
        '    """\n'
        "    return result_obj\n"
    )
    f = _write(tmp_path, "src/service.py", code)
    result = _check_doc_code_response_shape(tmp_path, [f])
    assert result["status"] == "PASS"


def test_dcr_excludes_test_files(tmp_path: Path) -> None:
    """Drift in test file docstrings must NOT be flagged."""
    code = (
        'def check_something(x):\n'
        '    """Check something.\n\n'
        '    Returns: {"status": str, "ghost": str}\n'
        '    """\n'
        "    return {'status': 'ok'}\n"
    )
    f = _write(tmp_path, "tests/test_service.py", code)
    result = _check_doc_code_response_shape(tmp_path, [f])
    assert result["status"] == "PASS"


def test_dcr_result_shape_empty_input(tmp_path: Path) -> None:
    """Correct result shape when called with no files."""
    result = _check_doc_code_response_shape(tmp_path, [])
    _result_shape(result, "doc_code_response_shape")
    assert result["status"] == "PASS"
