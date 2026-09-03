"""Tests for Phase 5 — T017 (promote_rules), T018 (check_enforced_rules),
T019 (compare_metrics / format_metrics_report).
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from saturnday.run.lessons import (
    Rule,
    init_memory_db,
    promote_rules,
    promote_to_enforced,
    store_rule,
)
from saturnday.run.memory_enforcement import check_enforced_rules
from saturnday.run.metrics import (
    RunMetrics,
    compare_metrics,
    format_metrics_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_db() -> sqlite3.Connection:
    """In-memory SQLite DB with full Phase 1 memory schema."""
    return init_memory_db(Path(":memory:"))


def _make_rule(
    rule_id: str = "TEST-001",
    status: str = "enforced",
    trigger_conditions: dict | None = None,
    forbidden_patterns: list[str] | None = None,
    required_patterns: list[str] | None = None,
    severity: str = "medium",
    human_rule: str = "Do not use eval()",
    trigger_count: int = 0,
) -> Rule:
    """Build a minimal Rule for testing."""
    now = datetime.now(timezone.utc).isoformat()
    return Rule(
        rule_id=rule_id,
        created_at=now,
        human_rule=human_rule,
        status=status,
        trigger_conditions=trigger_conditions or {},
        forbidden_patterns=forbidden_patterns or [],
        required_patterns=required_patterns or [],
        severity=severity,
        trigger_count=trigger_count,
    )


def _make_run_metrics(
    repeated_failure_count: int = 0,
    governance_regression_count: int = 0,
    lessons_extracted: int = 0,
    rules_generated: int = 0,
    passed: int = 2,
    failed: int = 1,
) -> RunMetrics:
    """Build a minimal RunMetrics for testing."""
    return RunMetrics(
        run_id="test-proj",
        total_tickets=passed + failed,
        passed=passed,
        failed=failed,
        coded_ungoverned=0,
        repeated_failure_count=repeated_failure_count,
        out_of_scope_edit_count=0,
        governance_regression_count=governance_regression_count,
        lessons_extracted=lessons_extracted,
        rules_generated=rules_generated,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# T018: check_enforced_rules
# ---------------------------------------------------------------------------


def test_check_enforced_rules_finds_forbidden_pattern(mem_db, tmp_path):
    """An enforced rule with a matching forbidden_pattern produces a finding."""
    rule = _make_rule(
        rule_id="SEC-EVAL",
        status="enforced",
        forbidden_patterns=[r"eval\("],
        required_patterns=[],
        severity="error",
        human_rule="Do not use eval()",
    )
    store_rule(mem_db, rule)
    promote_to_enforced(mem_db, rule.rule_id)

    target = tmp_path / "bad_code.py"
    target.write_text("result = eval(user_input)\n", encoding="utf-8")

    findings = check_enforced_rules(mem_db, ["bad_code.py"], tmp_path)

    assert len(findings) == 1
    f = findings[0]
    assert f["file"] == "bad_code.py"
    assert f["line"] == 1
    assert f["kind"] == "enforced_rule_SEC-EVAL"
    assert f["severity"] == "error"
    assert f["rule_id"] == "SEC-EVAL"
    assert "eval" in f["detail"].lower()


def test_check_enforced_rules_passes_when_required_present(mem_db, tmp_path):
    """Finding is suppressed when a required_pattern is also present in the file."""
    rule = _make_rule(
        rule_id="SHELL-CHECK",
        status="enforced",
        forbidden_patterns=[r"subprocess\.run\("],
        required_patterns=[r"shell=False"],
        severity="error",
        human_rule="subprocess.run must use shell=False",
    )
    store_rule(mem_db, rule)
    promote_to_enforced(mem_db, rule.rule_id)

    target = tmp_path / "runner.py"
    # Contains forbidden pattern BUT also the required safeguard
    target.write_text(
        "import subprocess\nsubprocess.run(['ls'], shell=False)\n",
        encoding="utf-8",
    )

    findings = check_enforced_rules(mem_db, ["runner.py"], tmp_path)

    assert findings == [], f"Expected no findings but got: {findings}"


def test_check_enforced_rules_no_rules(mem_db, tmp_path):
    """Empty rules table produces empty findings list."""
    target = tmp_path / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")

    findings = check_enforced_rules(mem_db, ["foo.py"], tmp_path)
    assert findings == []


def test_check_enforced_rules_non_matching_file(mem_db, tmp_path):
    """Rule with file_patterns trigger only fires for matching files."""
    rule = _make_rule(
        rule_id="PY-ONLY",
        status="enforced",
        trigger_conditions={"file_patterns": ["*.py"]},
        forbidden_patterns=[r"import os"],
        required_patterns=[],
        severity="medium",
        human_rule="Do not use os module",
    )
    store_rule(mem_db, rule)
    promote_to_enforced(mem_db, rule.rule_id)

    # A .js file — should not match *.py pattern
    target = tmp_path / "script.js"
    target.write_text("import os\n", encoding="utf-8")

    findings = check_enforced_rules(mem_db, ["script.js"], tmp_path)
    # *.py pattern does not match script.js
    assert findings == []


def test_check_enforced_rules_candidate_rule_does_not_fire(mem_db, tmp_path):
    """Candidate rules (not enforced) must not produce findings."""
    rule = _make_rule(
        rule_id="CAND-001",
        status="candidate",
        forbidden_patterns=[r"eval\("],
        severity="error",
        human_rule="Do not use eval()",
    )
    store_rule(mem_db, rule)
    # Do NOT promote to enforced

    target = tmp_path / "evil.py"
    target.write_text("eval(code)\n", encoding="utf-8")

    findings = check_enforced_rules(mem_db, ["evil.py"], tmp_path)
    assert findings == []


def test_check_enforced_rules_increments_trigger_count(mem_db, tmp_path):
    """trigger_count on a rule is incremented when it fires."""
    rule = _make_rule(
        rule_id="COUNT-TEST",
        status="enforced",
        forbidden_patterns=[r"secret"],
        severity="medium",
        human_rule="No hardcoded secrets",
        trigger_count=0,
    )
    store_rule(mem_db, rule)
    promote_to_enforced(mem_db, rule.rule_id)

    target = tmp_path / "config.py"
    target.write_text("password = 'secret'\n", encoding="utf-8")

    check_enforced_rules(mem_db, ["config.py"], tmp_path)

    row = mem_db.execute(
        "SELECT trigger_count FROM rules WHERE rule_id = ?", ("COUNT-TEST",)
    ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_check_enforced_rules_empty_changed_files(mem_db, tmp_path):
    """Empty changed_files list always returns empty findings."""
    rule = _make_rule(
        rule_id="EMPTY-FILES",
        status="enforced",
        forbidden_patterns=[r"eval\("],
        severity="error",
    )
    store_rule(mem_db, rule)
    promote_to_enforced(mem_db, rule.rule_id)

    findings = check_enforced_rules(mem_db, [], tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# T017: promote_rules / promote_to_enforced
# ---------------------------------------------------------------------------


def test_promote_rules_threshold(mem_db):
    """Candidate rules with trigger_count >= 3 are promoted to active."""
    rule = _make_rule(
        rule_id="PROMOTE-ME",
        status="candidate",
        trigger_count=3,
    )
    store_rule(mem_db, rule)
    # Manually set trigger_count since store_rule uses upsert
    mem_db.execute(
        "UPDATE rules SET trigger_count = 3 WHERE rule_id = ?", ("PROMOTE-ME",)
    )
    mem_db.commit()

    promoted_count = promote_rules(mem_db)

    assert promoted_count == 1
    row = mem_db.execute(
        "SELECT status FROM rules WHERE rule_id = ?", ("PROMOTE-ME",)
    ).fetchone()
    assert row[0] == "active"


def test_promote_rules_below_threshold(mem_db):
    """Candidate rules with trigger_count < 3 are not promoted."""
    rule = _make_rule(
        rule_id="NOT-YET",
        status="candidate",
        trigger_count=2,
    )
    store_rule(mem_db, rule)
    mem_db.execute(
        "UPDATE rules SET trigger_count = 2 WHERE rule_id = ?", ("NOT-YET",)
    )
    mem_db.commit()

    promoted_count = promote_rules(mem_db)

    assert promoted_count == 0
    row = mem_db.execute(
        "SELECT status FROM rules WHERE rule_id = ?", ("NOT-YET",)
    ).fetchone()
    assert row[0] == "candidate"


def test_promote_rules_already_active_not_re_promoted(mem_db):
    """Active rules are not double-counted or altered by promote_rules."""
    rule = _make_rule(
        rule_id="ALREADY-ACTIVE",
        status="active",
        trigger_count=5,
    )
    store_rule(mem_db, rule)
    mem_db.execute(
        "UPDATE rules SET status = 'active', trigger_count = 5 WHERE rule_id = ?",
        ("ALREADY-ACTIVE",),
    )
    mem_db.commit()

    promoted_count = promote_rules(mem_db)
    assert promoted_count == 0  # already active, not a candidate


def test_promote_to_enforced(mem_db):
    """promote_to_enforced sets the rule status to enforced."""
    rule = _make_rule(rule_id="FORCE-ENFORCE", status="active")
    store_rule(mem_db, rule)
    mem_db.execute(
        "UPDATE rules SET status = 'active' WHERE rule_id = ?", ("FORCE-ENFORCE",)
    )
    mem_db.commit()

    promote_to_enforced(mem_db, "FORCE-ENFORCE")

    row = mem_db.execute(
        "SELECT status FROM rules WHERE rule_id = ?", ("FORCE-ENFORCE",)
    ).fetchone()
    assert row[0] == "enforced"


# ---------------------------------------------------------------------------
# T019: compare_metrics / format_metrics_report
# ---------------------------------------------------------------------------


def test_compare_metrics_improvement():
    """When current repeated failures are lower than historical avg, trend is decreasing."""
    history = [
        _make_run_metrics(repeated_failure_count=5),
        _make_run_metrics(repeated_failure_count=4),
    ]
    current = _make_run_metrics(repeated_failure_count=1)

    result = compare_metrics(current, history)

    assert result["repeated_failure_trend"] == "decreasing"
    assert result["improvement_score"] < 1.0


def test_compare_metrics_regression():
    """When current repeated failures are higher than historical avg, trend is increasing."""
    history = [
        _make_run_metrics(repeated_failure_count=1),
        _make_run_metrics(repeated_failure_count=1),
    ]
    current = _make_run_metrics(repeated_failure_count=5)

    result = compare_metrics(current, history)

    assert result["repeated_failure_trend"] == "increasing"
    assert result["improvement_score"] > 1.0


def test_compare_metrics_no_history():
    """With no history, compare_metrics returns stable trends and score 1.0."""
    current = _make_run_metrics(repeated_failure_count=3)
    result = compare_metrics(current, [])

    assert result["repeated_failure_trend"] == "stable"
    assert result["improvement_score"] == 1.0
    assert result["avg_historical_repeated_failures"] == 0.0
    assert result["lessons_extracted_total"] == current.lessons_extracted


def test_compare_metrics_cumulative_totals():
    """Cumulative lessons and rules totals include both history and current."""
    history = [
        _make_run_metrics(lessons_extracted=3, rules_generated=1),
        _make_run_metrics(lessons_extracted=2, rules_generated=0),
    ]
    current = _make_run_metrics(lessons_extracted=4, rules_generated=2)

    result = compare_metrics(current, history)

    assert result["lessons_extracted_total"] == 3 + 2 + 4
    assert result["rules_generated_total"] == 1 + 0 + 2


def test_compare_metrics_stable():
    """Near-identical repeated failures across history produces stable trend."""
    history = [_make_run_metrics(repeated_failure_count=3)] * 4
    current = _make_run_metrics(repeated_failure_count=3)

    result = compare_metrics(current, history)
    assert result["repeated_failure_trend"] == "stable"


def test_format_metrics_report():
    """format_metrics_report produces a string containing key sections."""
    history = [_make_run_metrics(repeated_failure_count=4)]
    current = _make_run_metrics(repeated_failure_count=2, lessons_extracted=5)
    comparison = compare_metrics(current, history)

    report = format_metrics_report(comparison)

    assert "## Run Performance" in report
    assert "Repeated failures" in report
    assert "Improvement score" in report
    assert "Cumulative lessons" in report


def test_format_metrics_report_empty_comparison():
    """format_metrics_report handles an empty dict without raising."""
    report = format_metrics_report({})
    assert isinstance(report, str)
    assert len(report) > 0
