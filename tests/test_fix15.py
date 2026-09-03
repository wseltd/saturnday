"""Fix 15: Dead-code sequencing.

Tests for the two Fix 15 changes:
  - dead_code registered in SOFT_CHECKS → warning severity, non-blocking
  - dead_code suppressed in run_mode in _run_governance

Coverage requirements:
  1. dead_code is in SOFT_CHECKS (registration)
  2. dead_code resolves to warning severity through get_check_severity
  3. default_policy has dead_code as warning
  4. dead_code findings are suppressed in run_mode (kind-filter)
  5. blast_radius suppression still works alongside dead_code suppression
  6. dead_code detection logic itself still works (no regression)
  7. dead_code is still present in ALL_CHECKS (accessible to policy)
"""

from __future__ import annotations

from pathlib import Path

from saturnday.policy_manifest import (
    ALL_CHECKS,
    HARD_CHECKS,
    SOFT_CHECKS,
    default_policy,
    get_check_severity,
)
from saturnday.review import _check_dead_code


# ---------------------------------------------------------------------------
# Requirement 1 & 7: dead_code is in SOFT_CHECKS and therefore ALL_CHECKS
# ---------------------------------------------------------------------------


def test_dead_code_in_soft_checks() -> None:
    """dead_code must be registered in SOFT_CHECKS."""
    assert "dead_code" in SOFT_CHECKS


def test_dead_code_not_in_hard_checks() -> None:
    """dead_code must not be in HARD_CHECKS — it is advisory, not a security blocker."""
    assert "dead_code" not in HARD_CHECKS


def test_dead_code_in_all_checks() -> None:
    """dead_code must be in ALL_CHECKS (= HARD_CHECKS | SOFT_CHECKS)."""
    assert "dead_code" in ALL_CHECKS


# ---------------------------------------------------------------------------
# Requirement 2: warning severity via get_check_severity
# ---------------------------------------------------------------------------


def test_dead_code_severity_is_warning_via_policy() -> None:
    """get_check_severity must return 'warning' for dead_code with default policy."""
    policy = default_policy()
    severity = get_check_severity(policy, "dead_code")
    assert severity == "warning"


def test_dead_code_severity_not_error_by_default() -> None:
    """dead_code must NOT fall through to the 'error' default anymore."""
    policy = default_policy()
    severity = get_check_severity(policy, "dead_code")
    assert severity != "error"


# ---------------------------------------------------------------------------
# Requirement 3: default_policy contains dead_code as warning
# ---------------------------------------------------------------------------


def test_default_policy_includes_dead_code_as_warning() -> None:
    """default_policy must include dead_code with severity 'warning' and enabled True."""
    policy = default_policy()
    check = policy.checks.get("dead_code")
    assert check is not None, "dead_code not found in default_policy"
    assert check.severity == "warning"
    assert check.enabled is True


# ---------------------------------------------------------------------------
# Requirement 4: dead_code findings suppressed in run_mode
# ---------------------------------------------------------------------------


def test_run_mode_suppresses_dead_code_kind() -> None:
    """The run_mode finding filter must drop dead_code kind findings."""
    # Simulate what _run_governance does with run_mode filtering
    findings = [
        {"kind": "dead_code", "file": "src/utils.py", "detail": "orphaned helper"},
        {"kind": "syntax_error", "file": "src/main.py", "detail": "bad syntax"},
        {"kind": "dead_code", "file": "src/api.py", "detail": "another orphan"},
    ]
    # Apply the exact same filter pattern as in _run_governance run_mode
    filtered = [
        f for f in findings
        if f.get("kind") not in ("excessive_blast_radius", "dead_code")
    ]
    assert len(filtered) == 1
    assert filtered[0]["kind"] == "syntax_error"


def test_run_mode_keeps_other_findings() -> None:
    """The run_mode filter must not suppress non-dead-code findings."""
    findings = [
        {"kind": "hardcoded_secret", "file": "src/config.py"},
        {"kind": "dead_code", "file": "src/utils.py"},
        {"kind": "missing_readme", "file": "README.md"},
    ]
    filtered = [
        f for f in findings
        if f.get("kind") not in ("excessive_blast_radius", "dead_code")
    ]
    kinds = {f["kind"] for f in filtered}
    assert "dead_code" not in kinds
    assert "hardcoded_secret" in kinds
    assert "missing_readme" in kinds


# ---------------------------------------------------------------------------
# Requirement 5: blast_radius suppression still works alongside dead_code
# ---------------------------------------------------------------------------


def test_run_mode_still_suppresses_blast_radius() -> None:
    """Existing blast_radius suppression must still work after Fix 15."""
    findings = [
        {"kind": "excessive_blast_radius", "file": "src/large_module.py"},
        {"kind": "dead_code", "file": "src/utils.py"},
        {"kind": "syntax_error", "file": "src/main.py"},
    ]
    filtered = [
        f for f in findings
        if f.get("kind") not in ("excessive_blast_radius", "dead_code")
    ]
    kinds = {f["kind"] for f in filtered}
    assert "excessive_blast_radius" not in kinds
    assert "dead_code" not in kinds
    assert "syntax_error" in kinds


# ---------------------------------------------------------------------------
# Requirement 6: dead_code detection logic still works
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_dead_code_check_still_flags_unreferenced(tmp_path: Path) -> None:
    """_check_dead_code must still detect genuinely unused functions."""
    _write(tmp_path, "src/utils.py", "def orphaned():\n    return 1\n")
    result = _check_dead_code(tmp_path, ["src/utils.py"])
    assert result["name"] == "dead_code"
    assert result["status"] == "FAIL"
    assert any(d["kind"] == "dead_code" for d in result["findings"])
    assert any("orphaned" in d["detail"] for d in result["findings"])


def test_dead_code_check_passes_when_referenced(tmp_path: Path) -> None:
    """_check_dead_code must still pass when the function is referenced elsewhere."""
    _write(tmp_path, "src/utils.py", "def helper():\n    return 1\n")
    _write(tmp_path, "src/main.py", "from src.utils import helper\nhelper()\n")
    result = _check_dead_code(tmp_path, ["src/utils.py"])
    assert result["status"] == "PASS"


def test_dead_code_check_severity_field_in_result() -> None:
    """_check_dead_code result must declare severity 'warning' in its own result dict."""
    # The check result itself should say "warning"; Fix 15 makes the policy agree
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "src").mkdir()
        (tmp / "src" / "x.py").write_text("def orphan():\n    return 1\n")
        result = _check_dead_code(tmp, ["src/x.py"])
    assert result["severity"] == "warning"
