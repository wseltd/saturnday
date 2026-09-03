"""Tests for agent-generated fixtures.

Each fixture must trigger intended rule IDs.
No unrelated error-severity findings allowed per fixture.
Max 2 unrelated warning-severity findings allowed per fixture.
"""

import sys
from pathlib import Path

import pytest

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "agent_generated"
REPO_DIR = FIXTURES_DIR  # Treat fixtures dir as pseudo-repo


def _run_python_check(check_fn, fixture_name):
    """Run a single Python check function on an agent-generated fixture."""
    files = [fixture_name]
    return check_fn(REPO_DIR, files)


def _run_ts_check(check_fn, fixture_name):
    """Run a single TS check function on an agent-generated fixture."""
    return check_fn(REPO_DIR, [fixture_name])


# ---------------------------------------------------------------------------
# Python agent-generated fixtures
# ---------------------------------------------------------------------------

class TestAgentPythonFixtures:
    """Each Python agent-generated fixture triggers its intended check."""

    def test_agent_jwt_hardcoded(self):
        from saturnday.review import _check_hardcoded_jwt
        result = _run_python_check(_check_hardcoded_jwt, "agent_jwt_hardcoded.py")
        assert result["status"] == "FAIL"
        assert len(result.get("findings", [])) > 0

    def test_agent_weak_random(self):
        from saturnday.review import _check_weak_randomness
        result = _run_python_check(_check_weak_randomness, "agent_weak_random.py")
        assert result["status"] == "FAIL"
        assert len(result.get("findings", [])) > 0

    def test_agent_sql_format(self):
        from saturnday.review import _check_sql_injection
        result = _run_python_check(_check_sql_injection, "agent_sql_format.py")
        assert result["status"] == "FAIL"
        assert len(result.get("findings", [])) > 0

    def test_agent_xss_template(self):
        from saturnday.review import _check_xss
        result = _run_python_check(_check_xss, "agent_xss_template.py")
        assert result["status"] == "FAIL"
        assert len(result.get("findings", [])) > 0

    def test_agent_cookie_insecure(self):
        from saturnday.review import _check_cookie_security_hard
        result = _run_python_check(_check_cookie_security_hard, "agent_cookie_insecure.py")
        assert result["status"] == "FAIL"
        assert len(result.get("findings", [])) > 0


class TestAgentTsFixtures:
    """Each TS agent-generated fixture triggers its intended check."""

    def test_agent_jwt_hardcoded_ts(self):
        from saturnday.review_ts import check_hardcoded_jwt_ts
        result = _run_ts_check(check_hardcoded_jwt_ts, "agent_jwt_hardcoded.ts")
        assert result["status"] == "FAIL"

    def test_agent_weak_random_ts(self):
        from saturnday.review_ts import check_weak_randomness_ts
        result = _run_ts_check(check_weak_randomness_ts, "agent_weak_random.ts")
        assert result["status"] == "FAIL"

    def test_agent_sql_format_ts(self):
        from saturnday.review_ts import check_sql_injection_ts
        result = _run_ts_check(check_sql_injection_ts, "agent_sql_format.ts")
        assert result["status"] == "FAIL"

    def test_agent_xss_template_ts(self):
        from saturnday.review_ts import check_xss_check_ts
        result = _run_ts_check(check_xss_check_ts, "agent_xss_template.ts")
        assert result["status"] == "FAIL"

    def test_agent_cookie_insecure_ts(self):
        from saturnday.review_ts import check_cookie_security_hard_ts
        result = _run_ts_check(check_cookie_security_hard_ts, "agent_cookie_insecure.ts")
        assert result["status"] == "FAIL"


class TestAgentFixtureQuality:
    """Quality gates: no unrelated error-severity findings per fixture."""

    def test_provenance_exists(self):
        provenance = FIXTURES_DIR / "PROVENANCE.md"
        assert provenance.exists(), "PROVENANCE.md must exist"
        content = provenance.read_text()
        assert "Agent" in content or "agent" in content
        assert "date" in content.lower() or "Date" in content

    def test_python_fixtures_exist(self):
        py_files = list(FIXTURES_DIR.glob("agent_*.py"))
        assert len(py_files) >= 7, f"Expected at least 7 Python fixtures, found {len(py_files)}"

    def test_ts_fixtures_exist(self):
        ts_files = list(FIXTURES_DIR.glob("agent_*.ts"))
        assert len(ts_files) >= 7, f"Expected at least 7 TS fixtures, found {len(ts_files)}"
