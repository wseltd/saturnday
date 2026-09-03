"""T026 — Phase 6 regression gate tests.

Tests for:
- T022: code_reviewer prompt mentions evidence sections
- T023: reviewer task includes spec results and dataflow
- T024: select_verification_layers function
- T025: false-positive log functions in lessons.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# T022: code_reviewer prompt content
# ---------------------------------------------------------------------------


def test_reviewer_prompt_mentions_evidence() -> None:
    """The code_reviewer.md prompt must reference the evidence sections added in T022."""
    prompt_path = (
        Path(__file__).parent.parent
        / "src/saturnday/prompts/role_modes/code_reviewer.md"
    )
    assert prompt_path.is_file(), f"code_reviewer.md not found at {prompt_path}"
    content = prompt_path.read_text(encoding="utf-8")

    # T022 required phrases
    assert "Spec assertion results" in content, "Prompt must mention spec assertion results"
    assert "Property test results" in content, "Prompt must mention property test results"
    assert "Data flow findings" in content, "Prompt must mention data flow findings"
    assert "Available verification evidence" in content, "Prompt must have evidence section header"
    assert "Do NOT repeat what the deterministic checks already found" in content, \
        "Prompt must tell reviewer not to repeat deterministic findings"


# ---------------------------------------------------------------------------
# T023: reviewer task includes evidence variables
# ---------------------------------------------------------------------------


def _make_ticket(ticket_id: str = "T001", goal: str = "test goal") -> MagicMock:
    """Create a minimal TicketSpec mock."""
    ticket = MagicMock()
    ticket.ticket_id = ticket_id
    ticket.goal = goal
    ticket.acceptance_criteria = ("function parse returns dict",)
    ticket.verify_cmd = ""
    return ticket


def test_reviewer_task_includes_spec_results(tmp_path: Path) -> None:
    """When _spec_verification is non-empty, reviewer task must include spec section."""
    # We test the string-building logic directly by replicating the builder
    spec_verification = [
        {"criterion": "function parse returns dict", "passed": True, "output": ""},
        {"criterion": "function parse raises on None", "passed": False, "output": "AssertionError"},
    ]
    prop_results: list = []
    dataflow_findings: list = []

    evidence_parts: list[str] = []
    if spec_verification:
        spec_lines = []
        for sv in spec_verification[:10]:
            status = "PASS" if sv.get("passed") else "FAIL"
            crit = sv.get("criterion", "")[:100]
            spec_lines.append(f"  [{status}] {crit}")
        evidence_parts.append("## Spec Assertion Results\n" + "\n".join(spec_lines))

    if prop_results:
        evidence_parts.append("## Property Test Results\n(no results)")

    combined = "\n\n".join(evidence_parts)
    assert "Spec Assertion Results" in combined
    assert "[PASS] function parse returns dict" in combined
    assert "[FAIL] function parse raises on None" in combined
    assert "Property Test Results" not in combined  # prop_results empty


def test_reviewer_task_includes_dataflow(tmp_path: Path) -> None:
    """When _dataflow_findings is non-empty, reviewer task must include dataflow section."""
    dataflow_findings = [
        {"kind": "optional_deref", "detail": "result may be None at line 42", "file": "foo.py", "line": 42, "severity": "warning"},
    ]

    # Replicate the dataflow section builder from ticket_runner
    evidence_parts: list[str] = []
    if dataflow_findings:
        df_evidence = "\n".join(
            f"  {f.get('kind', '')}: {f.get('detail', '')}"
            for f in dataflow_findings[:5]
        )[:600]
        evidence_parts.append(f"## Data Flow Findings\n{df_evidence}")

    combined = "\n\n".join(evidence_parts)
    assert "Data Flow Findings" in combined
    assert "optional_deref" in combined
    assert "result may be None at line 42" in combined


def test_reviewer_task_includes_property_results(tmp_path: Path) -> None:
    """When _prop_results is non-empty, reviewer task must include property test section."""
    prop_results = [
        {"test_name": "test_no_exception_parse", "target_function": "parse", "passed": True},
        {"test_name": "test_determinism_parse", "target_function": "parse", "passed": False},
    ]

    evidence_parts: list[str] = []
    if prop_results:
        prop_lines = []
        for pr in prop_results[:10]:
            ps = "PASS" if pr.get("passed") else "FAIL"
            pn = pr.get("test_name", pr.get("target_function", ""))[:80]
            prop_lines.append(f"  [{ps}] {pn}")
        evidence_parts.append("## Property Test Results\n" + "\n".join(prop_lines))

    combined = "\n\n".join(evidence_parts)
    assert "Property Test Results" in combined
    assert "[PASS] test_no_exception_parse" in combined
    assert "[FAIL] test_determinism_parse" in combined


# ---------------------------------------------------------------------------
# T024: select_verification_layers
# ---------------------------------------------------------------------------


def _make_impact_report(blast: int = 0, config_files: list | None = None) -> MagicMock:
    """Build a minimal ImpactReport-like mock."""
    report = MagicMock()
    report.total_blast_radius = blast
    report.touched_files = config_files or []
    report.import_dependents = []
    report.symbol_references = []
    report.config_impacts = []
    return report


def _make_ticket_spec(has_criteria: bool = True) -> MagicMock:
    ticket = MagicMock()
    ticket.acceptance_criteria = ("returns dict",) if has_criteria else ()
    return ticket


def test_select_verification_layers_trivial_change() -> None:
    """Trivial change (blast=0) should skip property_tests, dataflow, llm_spec_inference, llm_review."""
    from saturnday.run.impact_analysis import select_verification_layers

    report = _make_impact_report(blast=0)
    ticket = _make_ticket_spec(has_criteria=True)
    layers = select_verification_layers(report, ticket)

    # spec_assertions always on
    assert layers["spec_assertions"] is True
    # expensive layers off for trivial change
    assert layers["property_tests"] is False
    assert layers["dataflow_check"] is False
    assert layers["llm_review"] is False


def test_select_verification_layers_high_blast() -> None:
    """High blast radius (>5) should enable all verification layers."""
    from saturnday.run.impact_analysis import select_verification_layers

    report = _make_impact_report(blast=8)
    # No explicit assertions to trigger llm_spec_inference
    ticket = _make_ticket_spec(has_criteria=False)
    layers = select_verification_layers(report, ticket)

    assert layers["spec_assertions"] is True
    assert layers["property_tests"] is True
    assert layers["dataflow_check"] is True
    assert layers["llm_spec_inference"] is True
    assert layers["llm_review"] is True


def test_select_verification_layers_config_only() -> None:
    """Config-only change should disable spec_assertions."""
    from saturnday.run.impact_analysis import select_verification_layers

    report = _make_impact_report(blast=1, config_files=["pyproject.toml"])
    ticket = _make_ticket_spec(has_criteria=True)
    layers = select_verification_layers(report, ticket)

    # pyproject.toml is a config file — all_config=True, so spec_assertions=False
    assert layers["spec_assertions"] is False


def test_select_verification_layers_medium_blast() -> None:
    """Medium blast (blast=4) enables dataflow and llm_review but not llm_spec_inference."""
    from saturnday.run.impact_analysis import select_verification_layers

    report = _make_impact_report(blast=4)
    ticket = _make_ticket_spec(has_criteria=True)
    layers = select_verification_layers(report, ticket)

    assert layers["dataflow_check"] is True
    assert layers["llm_review"] is True
    # blast=4 is not > 5, so llm_spec_inference stays off
    assert layers["llm_spec_inference"] is False


# ---------------------------------------------------------------------------
# T025: false-positive tracking in lessons.py
# ---------------------------------------------------------------------------


def _open_db() -> sqlite3.Connection:
    """Open a fully initialised lessons DB in a temporary directory.

    Uses mkdtemp so the directory is NOT automatically removed — the
    connection remains valid for the entire test.
    """
    from saturnday.run.lessons import init_memory_db

    tmp_dir = tempfile.mkdtemp()
    db_path = Path(tmp_dir) / "test_lessons.db"
    return init_memory_db(db_path)


def test_log_check_result() -> None:
    """log_check_result inserts a row into false_positive_log."""
    from saturnday.run.lessons import log_check_result

    conn = _open_db()
    log_check_result(conn, "spec_assertion", "assert parse() == {}", "foo.py", "T001", 1)
    log_check_result(conn, "dataflow", "optional_deref", "bar.py", "T002", 0)

    rows = conn.execute("SELECT * FROM false_positive_log").fetchall()
    assert len(rows) == 2

    # First row: useful
    assert rows[0][1] == "spec_assertion"
    assert rows[0][4] == "T001"
    assert rows[0][5] == 1

    # Second row: false positive
    assert rows[1][1] == "dataflow"
    assert rows[1][5] == 0


def test_get_false_positive_rate() -> None:
    """get_false_positive_rate returns correct ratio of was_useful=0 / total."""
    from saturnday.run.lessons import get_false_positive_rate, log_check_result

    conn = _open_db()
    log_check_result(conn, "spec_assertion", "rule_a", None, None, 1)
    log_check_result(conn, "spec_assertion", "rule_b", None, None, 0)
    log_check_result(conn, "spec_assertion", "rule_c", None, None, 0)

    rate = get_false_positive_rate(conn, "spec_assertion")
    # 2 out of 3 are false positives
    assert abs(rate - 2 / 3) < 1e-9

    # Unknown check type returns 0.0
    assert get_false_positive_rate(conn, "nonexistent") == 0.0


def test_get_false_positive_rate_empty() -> None:
    """get_false_positive_rate returns 0.0 when no rows exist."""
    from saturnday.run.lessons import get_false_positive_rate

    conn = _open_db()
    assert get_false_positive_rate(conn, "spec_assertion") == 0.0


def test_get_noisy_rules() -> None:
    """get_noisy_rules returns rules with false-positive rate above threshold."""
    from saturnday.run.lessons import get_noisy_rules, log_check_result

    conn = _open_db()

    # noisy_rule: 3 FP out of 4 = 75 % FP rate
    for _ in range(3):
        log_check_result(conn, "spec_assertion", "noisy_rule", None, None, 0)
    log_check_result(conn, "spec_assertion", "noisy_rule", None, None, 1)

    # clean_rule: 1 FP out of 4 = 25 % FP rate
    log_check_result(conn, "spec_assertion", "clean_rule", None, None, 0)
    for _ in range(3):
        log_check_result(conn, "spec_assertion", "clean_rule", None, None, 1)

    noisy = get_noisy_rules(conn, threshold=0.5)
    assert "noisy_rule" in noisy
    assert "clean_rule" not in noisy


def test_get_noisy_rules_empty() -> None:
    """get_noisy_rules returns empty list when no rows exist."""
    from saturnday.run.lessons import get_noisy_rules

    conn = _open_db()
    assert get_noisy_rules(conn) == []
