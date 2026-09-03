"""Tests for context_capsule.py — Phase 2 of the Saturnday memory system.

Covers T008 (capsule generator), T009 (outlier selector), T010 (knowledge
check), and T011 (residual fetch gating).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from saturnday._types import TicketScope, TicketSpec
from saturnday.run.context_capsule import (
    ContextCapsule,
    KnowledgeCheck,
    build_context_capsule,
    capsule_to_prompt,
    check_residual_gates,
    fetch_residual_evidence,
    format_knowledge_check_for_prompt,
    generate_knowledge_check,
    requires_residual_fetch,
    select_outliers,
)
from saturnday.run.lessons import init_memory_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """Open an in-memory memory DB in a temp directory."""
    return init_memory_db(tmp_path / "lessons.db")


@pytest.fixture()
def basic_ticket() -> TicketSpec:
    """A minimal TicketSpec for testing."""
    return TicketSpec(
        ticket_id="T001",
        goal="Create foo_bar function in src/foo.py",
        scope=TicketScope(
            allowed_globs=("src/foo.py", "tests/test_foo.py"),
            forbidden_globs=("**/secrets/**",),
        ),
    )


@pytest.fixture()
def ticket_with_wide_scope() -> TicketSpec:
    """A TicketSpec with wide scope."""
    return TicketSpec(
        ticket_id="T002",
        goal="Refactor the auth module for compliance",
        scope=TicketScope(
            allowed_globs=("src/auth/**",),
            forbidden_globs=(),
        ),
    )


# ---------------------------------------------------------------------------
# T008: build_context_capsule
# ---------------------------------------------------------------------------


def test_build_capsule_basic(basic_ticket: TicketSpec) -> None:
    """build_context_capsule with no DB returns a valid minimal capsule."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)

    assert isinstance(capsule, ContextCapsule)
    assert capsule.ticket_id == "T001"
    assert capsule.ticket_goal == "Create foo_bar function in src/foo.py"
    assert capsule.ticket_class == "generation"
    assert capsule.scope["allowed_globs"] == ["src/foo.py", "tests/test_foo.py"]
    assert capsule.scope["out_of_scope_globs"] == ["**/secrets/**"]
    assert capsule.active_rules == []
    assert capsule.repo_invariants == []
    assert capsule.recent_relevant_lessons == []
    assert capsule.residual_refs == []


def test_build_capsule_with_rules_and_lessons(
    basic_ticket: TicketSpec, db: sqlite3.Connection
) -> None:
    """Capsule loads rules and lessons from the DB."""
    # Insert a rule into the DB
    now = "2026-03-29T00:00:00+00:00"
    db.execute(
        "INSERT INTO rules (rule_id, created_at, human_rule, status, trigger_count) "
        "VALUES (?, ?, ?, ?, ?)",
        ("R001", now, "Never use os.system() directly.", "enforced", 5),
    )
    # Insert a lesson into the DB
    import uuid
    db.execute(
        "INSERT INTO memory_items "
        "(item_id, memory_type, created_at, status, failure_mode, "
        "corrective_rule_text, files_touched) "
        "VALUES (?, 'lesson', ?, 'active', 'governance_fail', ?, ?)",
        (
            str(uuid.uuid4()),
            now,
            "Always validate inputs in foo.py",
            '["src/foo.py"]',
        ),
    )
    db.commit()

    capsule = build_context_capsule(basic_ticket, memory_conn=db)

    assert any("Never use os.system" in r for r in capsule.active_rules)
    assert any("Always validate inputs" in l for l in capsule.recent_relevant_lessons)


def test_build_capsule_repair_class(db: sqlite3.Connection) -> None:
    """Ticket with 'fix' in goal is classified as 'repair'."""
    ticket = TicketSpec(
        ticket_id="T003",
        goal="Fix the broken auth handler in src/auth.py",
    )
    capsule = build_context_capsule(ticket, memory_conn=db)
    assert capsule.ticket_class == "repair"


def test_build_capsule_remediation_class(db: sqlite3.Connection) -> None:
    """Ticket with 'refactor' in goal is classified as 'remediation'."""
    ticket = TicketSpec(
        ticket_id="T004",
        goal="Refactor and migrate legacy handler",
    )
    capsule = build_context_capsule(ticket, memory_conn=db)
    assert capsule.ticket_class == "remediation"


# ---------------------------------------------------------------------------
# T008: capsule_to_prompt size and structure
# ---------------------------------------------------------------------------


def test_capsule_to_prompt_size(basic_ticket: TicketSpec, db: sqlite3.Connection) -> None:
    """capsule_to_prompt output must be under 3000 chars (hard limit 2000)."""
    # Stuff the DB with many rules and lessons
    now = "2026-03-29T00:00:00+00:00"
    import uuid
    for i in range(20):
        db.execute(
            "INSERT OR IGNORE INTO rules (rule_id, created_at, human_rule, status, trigger_count) "
            "VALUES (?, ?, ?, 'enforced', ?)",
            (f"R{i:03d}", now, f"Rule {i}: " + "x" * 180, i),
        )
        db.execute(
            "INSERT INTO memory_items "
            "(item_id, memory_type, created_at, status, corrective_rule_text, files_touched) "
            "VALUES (?, 'lesson', ?, 'active', ?, '[]')",
            (str(uuid.uuid4()), now, f"Lesson {i}: " + "y" * 180),
        )
    db.commit()

    capsule = build_context_capsule(basic_ticket, memory_conn=db)
    result = capsule_to_prompt(capsule)

    assert len(result) < 3000, f"Prompt too large: {len(result)} chars"
    assert len(result) <= 2000, f"Exceeds hard cap of 2000: {len(result)} chars"


def test_capsule_to_prompt_contains_ticket_id(basic_ticket: TicketSpec) -> None:
    """capsule_to_prompt always includes the ticket ID."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    result = capsule_to_prompt(capsule)
    assert "T001" in result


def test_capsule_to_prompt_scope_in_output(basic_ticket: TicketSpec) -> None:
    """capsule_to_prompt renders scope globs."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    result = capsule_to_prompt(capsule)
    assert "src/foo.py" in result


def test_capsule_to_prompt_empty_db(basic_ticket: TicketSpec, db: sqlite3.Connection) -> None:
    """capsule_to_prompt with empty DB returns valid minimal text."""
    capsule = build_context_capsule(basic_ticket, memory_conn=db)
    result = capsule_to_prompt(capsule)
    assert len(result) > 0
    assert "T001" in result


# ---------------------------------------------------------------------------
# T009: select_outliers
# ---------------------------------------------------------------------------


def test_select_outliers_test_deletion(basic_ticket: TicketSpec) -> None:
    """Test file in changed_files triggers an outlier warning."""
    outliers = select_outliers(
        ticket=basic_ticket,
        changed_files=["tests/test_foo.py"],
        findings=[],
    )
    assert any("Test file" in o for o in outliers), f"Expected test outlier, got: {outliers}"


def test_select_outliers_build_file(basic_ticket: TicketSpec) -> None:
    """pyproject.toml triggers a build file outlier."""
    outliers = select_outliers(
        ticket=basic_ticket,
        changed_files=["pyproject.toml"],
        findings=[],
    )
    assert any("Build/deploy" in o or "build" in o.lower() for o in outliers), (
        f"Expected build outlier, got: {outliers}"
    )


def test_select_outliers_security(basic_ticket: TicketSpec) -> None:
    """Security findings trigger a security outlier."""
    findings = [
        {
            "path": "src/auth.py",
            "message": "Hardcoded credential detected",
            "check_name": "credential_exposure",
            "severity": "error",
        }
    ]
    outliers = select_outliers(
        ticket=basic_ticket,
        changed_files=["src/auth.py"],
        findings=findings,
    )
    assert any("security" in o.lower() or "finding" in o.lower() for o in outliers), (
        f"Expected security outlier, got: {outliers}"
    )


def test_select_outliers_none_for_safe_changes(basic_ticket: TicketSpec) -> None:
    """Safe changes (no special files, no security findings) produce no outliers."""
    outliers = select_outliers(
        ticket=basic_ticket,
        changed_files=["src/foo.py"],
        findings=[],
    )
    assert outliers == [], f"Expected no outliers for safe change, got: {outliers}"


def test_select_outliers_api_file(basic_ticket: TicketSpec) -> None:
    """__init__.py change triggers a public API outlier."""
    outliers = select_outliers(
        ticket=basic_ticket,
        changed_files=["src/__init__.py"],
        findings=[],
    )
    assert any("API" in o or "interface" in o.lower() for o in outliers), (
        f"Expected API outlier, got: {outliers}"
    )


def test_select_outliers_dockerfile(basic_ticket: TicketSpec) -> None:
    """Dockerfile change triggers a build file outlier."""
    outliers = select_outliers(
        ticket=basic_ticket,
        changed_files=["Dockerfile"],
        findings=[],
    )
    assert any("Build" in o or "deploy" in o.lower() for o in outliers), (
        f"Expected build outlier for Dockerfile, got: {outliers}"
    )


# ---------------------------------------------------------------------------
# T010: generate_knowledge_check and format_knowledge_check_for_prompt
# ---------------------------------------------------------------------------


def test_generate_knowledge_check_structure(basic_ticket: TicketSpec) -> None:
    """generate_knowledge_check returns a KnowledgeCheck with expected fields."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    check = generate_knowledge_check(capsule)

    assert isinstance(check, KnowledgeCheck)
    assert "src/foo.py" in check.files_in_scope or "tests/test_foo.py" in check.files_in_scope
    assert "**/secrets/**" in check.files_out_of_scope
    # Symbols extracted from goal — 'foo_bar' should appear
    assert any("foo" in s.lower() or "bar" in s.lower() for s in check.symbols_to_touch), (
        f"Expected 'foo_bar' symbol, got: {check.symbols_to_touch}"
    )


def test_format_knowledge_check_structure(basic_ticket: TicketSpec) -> None:
    """format_knowledge_check_for_prompt includes all section labels."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    check = generate_knowledge_check(capsule)
    result = format_knowledge_check_for_prompt(check)

    assert "BEFORE YOU CODE" in result
    assert "Files in scope" in result
    assert "Files out of scope" in result
    assert "Active rules" in result
    assert "Relevant prior lessons" in result
    assert "What could break" in result


def test_format_knowledge_check_size(basic_ticket: TicketSpec) -> None:
    """format_knowledge_check_for_prompt output must be under 1000 chars."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    # Inject many rules and lessons to stress-test the cap
    capsule.active_rules = [f"Rule {i}: " + "x" * 100 for i in range(20)]
    capsule.recent_relevant_lessons = [f"Lesson {i}: " + "y" * 100 for i in range(20)]
    check = generate_knowledge_check(capsule)
    result = format_knowledge_check_for_prompt(check)

    assert len(result) <= 1000, f"Knowledge check too large: {len(result)} chars"


def test_generate_knowledge_check_with_repo_path(
    basic_ticket: TicketSpec, tmp_path: Path
) -> None:
    """generate_knowledge_check expands globs when repo_path is provided."""
    # Create actual files
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("# foo")

    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    check = generate_knowledge_check(capsule, repo_path=tmp_path)
    # When glob expansion works, 'src/foo.py' should appear
    assert any("foo.py" in f for f in check.files_in_scope)


# ---------------------------------------------------------------------------
# T011: requires_residual_fetch
# ---------------------------------------------------------------------------


def test_requires_residual_fetch_test_file() -> None:
    """Test file in changed_files triggers residual fetch."""
    assert requires_residual_fetch("generation", ["tests/test_auth.py"]) is True


def test_requires_residual_fetch_config_file() -> None:
    """pyproject.toml in changed_files triggers residual fetch."""
    assert requires_residual_fetch("generation", ["pyproject.toml"]) is True


def test_requires_residual_fetch_safe_file() -> None:
    """Safe file does not trigger residual fetch."""
    assert requires_residual_fetch("generation", ["src/utils.py"]) is False


def test_requires_residual_fetch_by_action_type() -> None:
    """High-risk action_type triggers residual fetch regardless of files."""
    assert requires_residual_fetch("exemption", []) is True
    assert requires_residual_fetch("security", []) is True
    assert requires_residual_fetch("build", []) is True
    assert requires_residual_fetch("interface", []) is True
    assert requires_residual_fetch("test_deletion", []) is True


def test_requires_residual_fetch_init_py() -> None:
    """__init__.py triggers residual fetch as a public interface."""
    assert requires_residual_fetch("generation", ["src/__init__.py"]) is True


def test_requires_residual_fetch_dockerfile() -> None:
    """Dockerfile triggers residual fetch."""
    assert requires_residual_fetch("generation", ["Dockerfile"]) is True


def test_requires_residual_fetch_empty() -> None:
    """No files and safe action_type does not trigger residual fetch."""
    assert requires_residual_fetch("generation", []) is False


# ---------------------------------------------------------------------------
# T011: fetch_residual_evidence
# ---------------------------------------------------------------------------


def test_fetch_residual_evidence_reads_file(tmp_path: Path) -> None:
    """fetch_residual_evidence returns file content."""
    (tmp_path / "foo.py").write_text("def foo(): pass")
    result = fetch_residual_evidence("foo.py", tmp_path)
    assert "def foo" in result


def test_fetch_residual_evidence_caps_at_3000(tmp_path: Path) -> None:
    """fetch_residual_evidence caps output at 3000 chars."""
    (tmp_path / "large.py").write_text("x = 1\n" * 1000)
    result = fetch_residual_evidence("large.py", tmp_path)
    assert len(result) <= 3100  # small buffer for truncation message


def test_fetch_residual_evidence_missing_file(tmp_path: Path) -> None:
    """fetch_residual_evidence returns an error string for missing files."""
    result = fetch_residual_evidence("nonexistent.py", tmp_path)
    assert "does not exist" in result or "could not read" in result


# ---------------------------------------------------------------------------
# T011: check_residual_gates
# ---------------------------------------------------------------------------


def test_check_residual_gates_test_file(
    basic_ticket: TicketSpec, tmp_path: Path
) -> None:
    """Test file in changed_files triggers a residual gate warning."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    warnings = check_residual_gates(capsule, ["tests/test_foo.py"], tmp_path)
    assert any("Test file" in w for w in warnings), f"Expected gate, got: {warnings}"


def test_check_residual_gates_build_file(
    basic_ticket: TicketSpec, tmp_path: Path
) -> None:
    """pyproject.toml in changed_files triggers a build gate warning."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    warnings = check_residual_gates(capsule, ["pyproject.toml"], tmp_path)
    assert any("Build" in w or "build" in w.lower() for w in warnings), (
        f"Expected build gate, got: {warnings}"
    )


def test_check_residual_gates_interface_file(
    basic_ticket: TicketSpec, tmp_path: Path
) -> None:
    """__init__.py in changed_files triggers an interface gate warning."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    warnings = check_residual_gates(capsule, ["src/__init__.py"], tmp_path)
    assert any("interface" in w.lower() or "Public" in w for w in warnings), (
        f"Expected interface gate, got: {warnings}"
    )


def test_check_residual_gates_empty(
    basic_ticket: TicketSpec, tmp_path: Path
) -> None:
    """No changed files produces no gate warnings."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    warnings = check_residual_gates(capsule, [], tmp_path)
    assert warnings == []


def test_check_residual_gates_safe_file(
    basic_ticket: TicketSpec, tmp_path: Path
) -> None:
    """A safe source file produces no gate warnings."""
    capsule = build_context_capsule(basic_ticket, memory_conn=None)
    warnings = check_residual_gates(capsule, ["src/utils.py"], tmp_path)
    assert warnings == []
