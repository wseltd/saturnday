"""Tests for src/saturnday/run/contract_checker.py (U4).

Covers:
- extract_contracts: parsing acceptance criteria into Contract objects
- verify_contracts: checking contracts against a real repo directory
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from saturnday.run.contract_checker import (
    Contract,
    ContractResult,
    extract_contracts,
    format_contract_results,
    verify_contracts,
)


# ---------------------------------------------------------------------------
# extract_contracts — parsing tests
# ---------------------------------------------------------------------------

class TestExtractContracts:
    def test_empty_tuple_returns_empty(self):
        assert extract_contracts(()) == []

    def test_no_recognisable_patterns_returns_empty(self):
        criteria = ("The system should be fast", "Deploy to production")
        assert extract_contracts(criteria) == []

    def test_function_exists(self):
        criteria = ("function add exists",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        c = contracts[0]
        assert c.kind == "function"
        assert c.name == "add"
        assert c.file_hint == ""

    def test_function_exists_in_file(self):
        criteria = ("function add exists in math.py",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        c = contracts[0]
        assert c.kind == "function"
        assert c.name == "add"
        assert c.file_hint == "math.py"

    def test_function_case_insensitive(self):
        criteria = ("Function compute_total exists in billing.py",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        assert contracts[0].name == "compute_total"

    def test_class_exists(self):
        criteria = ("class MyHandler exists",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        c = contracts[0]
        assert c.kind == "class"
        assert c.name == "MyHandler"

    def test_class_exists_in_file(self):
        criteria = ("class Config exists in settings.py",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        c = contracts[0]
        assert c.kind == "class"
        assert c.name == "Config"
        assert c.file_hint == "settings.py"

    def test_file_exists(self):
        criteria = ("file config.yaml exists",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        c = contracts[0]
        assert c.kind == "file"
        assert c.name == "config.yaml"

    def test_file_with_path(self):
        criteria = ("file src/utils/helpers.py exists",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        assert contracts[0].name == "src/utils/helpers.py"

    def test_test_exists_with_prefix(self):
        criteria = ("test test_add_numbers exists",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        c = contracts[0]
        assert c.kind == "test"
        assert c.name == "test_add_numbers"

    def test_test_exists_without_prefix(self):
        # "test add_numbers exists" should normalise to test_add_numbers
        criteria = ("test add_numbers exists",)
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1
        assert contracts[0].name == "test_add_numbers"

    def test_test_function_keyword(self):
        # "test function test_compute_total exists" may also match _RE_FUNCTION
        # for "function test_compute_total".  Both contracts are valid;
        # verify that at least the test contract is extracted.
        criteria = ("test function test_compute_total exists in tests/test_billing.py",)
        contracts = extract_contracts(criteria)
        assert len(contracts) >= 1
        test_contracts = [c for c in contracts if c.kind == "test"]
        assert len(test_contracts) == 1
        c = test_contracts[0]
        assert c.name == "test_compute_total"
        assert c.file_hint == "tests/test_billing.py"

    def test_multiple_criteria(self):
        criteria = (
            "function add exists in math.py",
            "class Calculator exists in math.py",
            "file config.json exists",
        )
        contracts = extract_contracts(criteria)
        assert len(contracts) == 3
        kinds = {c.kind for c in contracts}
        assert kinds == {"function", "class", "file"}

    def test_deduplication(self):
        # Same function mentioned twice should only produce one contract
        criteria = (
            "function add exists",
            "function add exists in math.py",
        )
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1

    def test_source_preserved(self):
        criterion = "function my_func exists in utils.py"
        contracts = extract_contracts((criterion,))
        assert contracts[0].source == criterion

    def test_non_string_criterion_skipped(self):
        # Robustness: tuple with a non-string entry should not crash
        criteria = ("function add exists", None, 42)  # type: ignore[arg-type]
        contracts = extract_contracts(criteria)
        assert len(contracts) == 1


# ---------------------------------------------------------------------------
# verify_contracts — against real tmp directories
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal repo structure for contract verification tests."""
    # src/math_utils.py with a function and a class
    src = tmp_path / "src"
    src.mkdir()
    (src / "math_utils.py").write_text(
        textwrap.dedent("""\
            def add(a, b):
                return a + b

            def subtract(a, b):
                return a - b

            class Calculator:
                def compute(self, x, y):
                    return x + y
        """),
        encoding="utf-8",
    )

    # tests/test_math_utils.py
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_math_utils.py").write_text(
        textwrap.dedent("""\
            from src.math_utils import add

            def test_add():
                assert add(1, 2) == 3

            def test_subtract():
                assert True
        """),
        encoding="utf-8",
    )

    # data/config.yaml
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("key: value\n", encoding="utf-8")

    return tmp_path


class TestVerifyContracts:
    def test_function_exists_by_hint(self, tmp_repo: Path):
        c = Contract(kind="function", name="add", file_hint="src/math_utils.py")
        results = verify_contracts([c], tmp_repo)
        assert len(results) == 1
        assert results[0].verified is True

    def test_function_missing(self, tmp_repo: Path):
        c = Contract(kind="function", name="multiply")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False

    def test_function_found_by_repo_scan(self, tmp_repo: Path):
        # No file_hint — scanner should find it in src/math_utils.py
        c = Contract(kind="function", name="subtract")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_class_exists(self, tmp_repo: Path):
        c = Contract(kind="class", name="Calculator", file_hint="src/math_utils.py")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_class_missing(self, tmp_repo: Path):
        c = Contract(kind="class", name="MissingClass")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False

    def test_file_exists(self, tmp_repo: Path):
        c = Contract(kind="file", name="data/config.yaml")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_file_missing(self, tmp_repo: Path):
        c = Contract(kind="file", name="data/missing.yaml")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False

    def test_test_exists(self, tmp_repo: Path):
        c = Contract(kind="test", name="test_add")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_test_missing(self, tmp_repo: Path):
        c = Contract(kind="test", name="test_multiply")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False

    def test_test_found_with_file_hint(self, tmp_repo: Path):
        c = Contract(
            kind="test",
            name="test_subtract",
            file_hint="tests/test_math_utils.py",
        )
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_multiple_contracts(self, tmp_repo: Path):
        contracts = [
            Contract(kind="function", name="add"),
            Contract(kind="class", name="Calculator"),
            Contract(kind="file", name="data/config.yaml"),
            Contract(kind="function", name="nonexistent"),
        ]
        results = verify_contracts(contracts, tmp_repo)
        assert len(results) == 4
        assert results[0].verified is True   # add
        assert results[1].verified is True   # Calculator
        assert results[2].verified is True   # config.yaml
        assert results[3].verified is False  # nonexistent

    def test_unknown_kind_returns_fail(self, tmp_repo: Path):
        c = Contract(kind="unknown_kind", name="something")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False

    def test_empty_contracts_list(self, tmp_repo: Path):
        results = verify_contracts([], tmp_repo)
        assert results == []

    def test_parse_error_file_returns_fail(self, tmp_repo: Path):
        # Write a file with invalid Python syntax
        bad_py = tmp_repo / "src" / "bad_syntax.py"
        bad_py.write_text("def )(invalid:\n    pass\n", encoding="utf-8")
        c = Contract(kind="function", name="invalid", file_hint="src/bad_syntax.py")
        results = verify_contracts([c], tmp_repo)
        # Can't find it in the bad file — fallback scan finds nothing
        assert results[0].verified is False


# ---------------------------------------------------------------------------
# format_contract_results
# ---------------------------------------------------------------------------

class TestFormatContractResults:
    def test_empty_results(self):
        assert format_contract_results([]) == ""

    def test_pass_result(self):
        c = Contract(kind="function", name="add")
        r = ContractResult(contract=c, verified=True, detail="found in src/math.py")
        text = format_contract_results([r])
        assert "[PASS]" in text
        assert "add" in text

    def test_fail_result(self):
        c = Contract(kind="file", name="missing.yaml")
        r = ContractResult(contract=c, verified=False, detail="file not found")
        text = format_contract_results([r])
        assert "[FAIL]" in text
        assert "missing.yaml" in text

    def test_multiple_results(self):
        c1 = Contract(kind="function", name="a")
        c2 = Contract(kind="function", name="b")
        results = [
            ContractResult(contract=c1, verified=True, detail="ok"),
            ContractResult(contract=c2, verified=False, detail="missing"),
        ]
        text = format_contract_results(results)
        lines = text.splitlines()
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# Round-trip: extract + verify
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_criteria_to_verification(self, tmp_repo: Path):
        criteria = (
            "function add exists in src/math_utils.py",
            "class Calculator exists in src/math_utils.py",
            "file data/config.yaml exists",
            "test test_add exists",
        )
        contracts = extract_contracts(criteria)
        assert len(contracts) == 4
        results = verify_contracts(contracts, tmp_repo)
        assert all(r.verified for r in results), [r.detail for r in results]

    def test_failing_criteria(self, tmp_repo: Path):
        criteria = (
            "function nonexistent_fn exists",
            "class NonexistentClass exists",
            "file nonexistent/path.txt exists",
        )
        contracts = extract_contracts(criteria)
        results = verify_contracts(contracts, tmp_repo)
        assert all(not r.verified for r in results)


# ---------------------------------------------------------------------------
# Fix 2: file basename fallback and dotted-name tolerance
# ---------------------------------------------------------------------------

class TestFileBasenameFallback:
    """_verify_file falls back to basename search when exact path misses."""

    def test_file_at_different_path_found_by_basename(self, tmp_repo: Path):
        # Planner says "file math_utils.py exists" but it's at src/math_utils.py
        c = Contract(kind="file", name="math_utils.py")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True
        assert "basename match" in results[0].detail

    def test_file_exact_path_preferred_over_basename(self, tmp_repo: Path):
        # Exact path match should not say "basename match"
        c = Contract(kind="file", name="src/math_utils.py")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True
        assert "basename match" not in results[0].detail

    def test_truly_missing_file_still_fails(self, tmp_repo: Path):
        c = Contract(kind="file", name="does_not_exist.py")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False

    def test_non_python_basename_fallback(self, tmp_repo: Path):
        # Planner says "file config.yaml exists" but it's at data/config.yaml
        c = Contract(kind="file", name="config.yaml")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_multiple_basename_matches_fail_as_ambiguous(self, tmp_repo: Path):
        # Create a second math_utils.py in a different directory
        other = tmp_repo / "lib"
        other.mkdir()
        (other / "math_utils.py").write_text("# duplicate\n", encoding="utf-8")
        c = Contract(kind="file", name="math_utils.py")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False
        assert "ambiguous" in results[0].detail


class TestDottedNameTolerance:
    """Dotted qualified names like 'module.func' resolve to bare AST name."""

    def test_dotted_function_name_resolves(self, tmp_repo: Path):
        # Planner says "function math_utils.add exists"
        c = Contract(kind="function", name="math_utils.add")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_dotted_class_name_resolves(self, tmp_repo: Path):
        # Planner says "class src.math_utils.Calculator exists"
        c = Contract(kind="class", name="src.math_utils.Calculator")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_dotted_test_name_resolves(self, tmp_repo: Path):
        # Planner says "test tests.test_math_utils.test_add exists"
        c = Contract(kind="test", name="tests.test_math_utils.test_add")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_bare_name_still_works(self, tmp_repo: Path):
        # Undotted names must still work as before
        c = Contract(kind="function", name="add")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is True

    def test_dotted_name_missing_function_still_fails(self, tmp_repo: Path):
        c = Contract(kind="function", name="math_utils.nonexistent")
        results = verify_contracts([c], tmp_repo)
        assert results[0].verified is False
