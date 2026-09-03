"""Tests for fingerprint-based ratchet enforcement (src/saturnday/ratchet.py).

Covers:
- Fingerprint computation and stability
- Baseline I/O
- Ratchet comparison modes (block_new, ratchet_down)
- Waiver awareness
- Enforcement date escalation
- Edge cases (empty baseline, swapped findings, line shifts)
"""

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

from saturnday.ratchet import (
    BASELINE_SCHEMA_VERSION,
    Baseline,
    FindingFingerprint,
    RatchetResult,
    _hash_snippet,
    _normalise_path,
    _normalise_snippet,
    _get_symbol_at_line,
    compare_findings,
    compute_fingerprint,
    fingerprint_check_results,
    generate_baseline,
    load_baseline,
    save_baseline,
)


# ---------------------------------------------------------------------------
# Fingerprint stability
# ---------------------------------------------------------------------------

class TestFingerprintStability:
    """Fingerprints must be stable across line shifts and whitespace changes."""

    def test_same_finding_different_lines_same_fingerprint(self):
        """Same code at different line numbers → same fingerprint."""
        fp1 = compute_fingerprint(
            rule_id="SEC-013",
            path="src/views.py",
            severity="warning",
            snippet="f'<h1>{user_input}</h1>'",
            line=10,
            source="",
        )
        fp2 = compute_fingerprint(
            rule_id="SEC-013",
            path="src/views.py",
            severity="warning",
            snippet="f'<h1>{user_input}</h1>'",
            line=42,
            source="",
        )
        assert fp1 == fp2

    def test_different_rule_different_fingerprint(self):
        """Different rule IDs → different fingerprints."""
        fp1 = compute_fingerprint("SEC-013", "src/views.py", "warning", "snippet")
        fp2 = compute_fingerprint("SEC-015", "src/views.py", "warning", "snippet")
        assert fp1 != fp2

    def test_different_path_different_fingerprint(self):
        """Different file paths → different fingerprints."""
        fp1 = compute_fingerprint("SEC-013", "src/views.py", "warning", "snippet")
        fp2 = compute_fingerprint("SEC-013", "src/api.py", "warning", "snippet")
        assert fp1 != fp2

    def test_path_normalisation(self):
        """Paths with different separators normalise to same fingerprint."""
        fp1 = compute_fingerprint("SEC-013", "src/views.py", "warning", "snippet")
        fp2 = compute_fingerprint("SEC-013", "src\\views.py", "warning", "snippet")
        assert fp1 == fp2

    def test_leading_dot_slash_normalised(self):
        fp1 = compute_fingerprint("SEC-013", "./src/views.py", "warning", "x")
        fp2 = compute_fingerprint("SEC-013", "src/views.py", "warning", "x")
        assert fp1 == fp2

    def test_snippet_whitespace_normalised(self):
        """Whitespace differences in snippets don't change fingerprint."""
        fp1 = compute_fingerprint("SEC-013", "a.py", "warning", 'f"<h1>{ x }</h1>"')
        fp2 = compute_fingerprint("SEC-013", "a.py", "warning", 'f"<h1>{  x  }</h1>"')
        assert fp1 == fp2

    def test_snippet_string_literal_normalised(self):
        """String literal contents don't affect fingerprint."""
        fp1 = compute_fingerprint("SEC-001", "a.py", "error", 'SECRET = "abc123"')
        fp2 = compute_fingerprint("SEC-001", "a.py", "error", 'SECRET = "xyz789"')
        assert fp1 == fp2

    def test_snippet_numeric_normalised(self):
        fp1 = compute_fingerprint("SEC-001", "a.py", "error", "random.randint(0, 999)")
        fp2 = compute_fingerprint("SEC-001", "a.py", "error", "random.randint(0, 12345)")
        assert fp1 == fp2

    def test_fingerprint_frozen(self):
        """Fingerprints must be hashable (frozen dataclass)."""
        fp = compute_fingerprint("SEC-013", "a.py", "warning", "test")
        assert hash(fp) is not None
        s = {fp}
        assert len(s) == 1


class TestSymbolResolution:
    """AST-based symbol resolution for fingerprints."""

    def test_function_scope(self):
        source = "def render_page():\n    html = f'<h1>{x}</h1>'\n    return html\n"
        symbol = _get_symbol_at_line(source, 2)
        assert symbol == "render_page"

    def test_class_scope(self):
        source = "class MyView:\n    def get(self):\n        return 'ok'\n"
        symbol = _get_symbol_at_line(source, 3)
        assert symbol == "get"

    def test_module_level(self):
        source = "SECRET = 'abc123'\n\ndef foo():\n    pass\n"
        symbol = _get_symbol_at_line(source, 1)
        assert symbol == ""

    def test_syntax_error_returns_empty(self):
        symbol = _get_symbol_at_line("def broken(:", 1)
        assert symbol == ""


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------

class TestBaselineIO:
    """Baseline serialization round-trips correctly."""

    def test_save_and_load_roundtrip(self, tmp_path):
        fps = {
            FindingFingerprint("SEC-013", "src/views.py", "render_page", "abc123", "warning"),
            FindingFingerprint("SEC-001", "src/auth.py", "", "def456", "error"),
        }
        baseline = generate_baseline(
            fingerprints=fps,
            repo_sha="abc123def",
            enforcement_date="2026-06-15",
            ratchet_mode="block_new",
        )

        path = tmp_path / ".saturnday-baseline.json"
        save_baseline(path, baseline)

        loaded = load_baseline(path)
        assert loaded.schema_version == BASELINE_SCHEMA_VERSION
        assert loaded.enforcement_date == "2026-06-15"
        assert loaded.ratchet_mode == "block_new"
        assert loaded.repo_sha == "abc123def"
        assert loaded.findings == fps

    def test_baseline_json_schema(self, tmp_path):
        fps = {FindingFingerprint("SEC-001", "a.py", "", "hash1", "error")}
        baseline = generate_baseline(fps, repo_sha="sha1")
        path = tmp_path / "baseline.json"
        save_baseline(path, baseline)

        data = json.loads(path.read_text())
        assert "schema_version" in data
        assert "created_utc" in data
        assert "findings" in data
        assert isinstance(data["findings"], list)
        assert data["findings"][0]["rule_id"] == "SEC-001"

    def test_empty_baseline(self, tmp_path):
        baseline = generate_baseline(set())
        path = tmp_path / "empty.json"
        save_baseline(path, baseline)
        loaded = load_baseline(path)
        assert len(loaded.findings) == 0


# ---------------------------------------------------------------------------
# Ratchet comparison: block_new mode
# ---------------------------------------------------------------------------

class TestBlockNewMode:
    """block_new mode: new non-waived fingerprints block."""

    def _make_fp(self, rule_id, path="a.py", snippet_hash="h1"):
        return FindingFingerprint(rule_id, path, "", snippet_hash, "warning")

    def test_no_change_passes(self):
        fps = {self._make_fp("SEC-013")}
        baseline = Baseline(findings=fps, ratchet_mode="block_new")
        result = compare_findings(fps, baseline)
        assert result.disposition == "PASS"
        assert len(result.new_findings) == 0
        assert len(result.legacy_findings) == 1

    def test_new_finding_fails(self):
        baseline_fps = {self._make_fp("SEC-013")}
        current_fps = {self._make_fp("SEC-013"), self._make_fp("SEC-015", snippet_hash="h2")}
        baseline = Baseline(findings=baseline_fps, ratchet_mode="block_new")
        result = compare_findings(current_fps, baseline)
        assert result.disposition == "FAIL"
        assert len(result.new_findings) == 1

    def test_swapped_finding_fails(self):
        """One finding removed, one added at same count → FAIL (different fingerprint)."""
        old_fp = self._make_fp("SEC-013", snippet_hash="old")
        new_fp = self._make_fp("SEC-013", snippet_hash="new")
        baseline = Baseline(findings={old_fp}, ratchet_mode="block_new")
        result = compare_findings({new_fp}, baseline)
        assert result.disposition == "FAIL"
        assert len(result.new_findings) == 1
        assert len(result.resolved_findings) == 1

    def test_waived_new_finding_passes(self):
        """New finding that is waived → PASS."""
        baseline_fps = {self._make_fp("SEC-013")}
        new_fp = self._make_fp("SEC-015", snippet_hash="h2")
        current_fps = {self._make_fp("SEC-013"), new_fp}
        baseline = Baseline(findings=baseline_fps, ratchet_mode="block_new")
        result = compare_findings(current_fps, baseline, waived_fingerprints={new_fp})
        assert result.disposition == "PASS"

    def test_resolved_finding_tracked(self):
        """Finding removed from current → appears in resolved_findings."""
        baseline_fps = {self._make_fp("SEC-013"), self._make_fp("SEC-015", snippet_hash="h2")}
        current_fps = {self._make_fp("SEC-013")}
        baseline = Baseline(findings=baseline_fps, ratchet_mode="block_new")
        result = compare_findings(current_fps, baseline)
        assert result.disposition == "PASS"
        assert len(result.resolved_findings) == 1

    def test_empty_baseline_new_finding_fails(self):
        """Baseline with zero findings + new finding → FAIL."""
        current_fps = {self._make_fp("SEC-013")}
        baseline = Baseline(findings=set(), ratchet_mode="block_new")
        result = compare_findings(current_fps, baseline)
        assert result.disposition == "FAIL"

    def test_enforcement_date_legacy_fails(self):
        """After enforcement_date: legacy findings → FAIL."""
        fp = self._make_fp("SEC-013")
        baseline = Baseline(
            findings={fp},
            ratchet_mode="block_new",
            enforcement_date="2026-01-01",
        )
        result = compare_findings({fp}, baseline, today=date(2026, 6, 15))
        assert result.disposition == "FAIL"
        assert "legacy" in result.reasons[0].lower()

    def test_enforcement_date_not_reached_passes(self):
        """Before enforcement_date: legacy findings → PASS."""
        fp = self._make_fp("SEC-013")
        baseline = Baseline(
            findings={fp},
            ratchet_mode="block_new",
            enforcement_date="2026-06-15",
        )
        result = compare_findings({fp}, baseline, today=date(2026, 3, 1))
        assert result.disposition == "PASS"

    def test_introduced_finding_blocks_ratchet(self):
        """Introducing a new finding causes ratchet FAIL — C11 MVP proof."""
        baseline_fp = self._make_fp("SEC-001", path="existing.py")
        baseline = Baseline(findings={baseline_fp}, ratchet_mode="block_new")

        new_fp = self._make_fp("SEC-015", path="new_vuln.py", snippet_hash="introduced")
        current_fps = {baseline_fp, new_fp}

        result = compare_findings(current_fps, baseline)

        assert result.disposition == "FAIL"
        assert new_fp in result.new_findings
        assert len(result.new_findings) == 1
        assert len(result.reasons) > 0

    def test_no_new_findings_passes_ratchet(self):
        """Matching baseline exactly yields PASS — complement to C11."""
        baseline_fp = self._make_fp("SEC-001", path="existing.py")
        baseline = Baseline(findings={baseline_fp}, ratchet_mode="block_new")
        current_fps = {baseline_fp}

        result = compare_findings(current_fps, baseline)

        assert result.disposition == "PASS"
        assert len(result.new_findings) == 0


# ---------------------------------------------------------------------------
# Ratchet comparison: ratchet_down mode
# ---------------------------------------------------------------------------

class TestRatchetDownMode:
    """ratchet_down mode: total non-waived count must not increase."""

    def _make_fp(self, rule_id, path="a.py", snippet_hash="h1"):
        return FindingFingerprint(rule_id, path, "", snippet_hash, "warning")

    def test_count_increase_fails(self):
        baseline_fps = {self._make_fp("SEC-013")}
        current_fps = {
            self._make_fp("SEC-013"),
            self._make_fp("SEC-015", snippet_hash="h2"),
        }
        baseline = Baseline(findings=baseline_fps, ratchet_mode="ratchet_down")
        result = compare_findings(current_fps, baseline)
        assert result.disposition == "FAIL"

    def test_count_decrease_passes(self):
        baseline_fps = {
            self._make_fp("SEC-013"),
            self._make_fp("SEC-015", snippet_hash="h2"),
        }
        current_fps = {self._make_fp("SEC-013")}
        baseline = Baseline(findings=baseline_fps, ratchet_mode="ratchet_down")
        result = compare_findings(current_fps, baseline)
        assert result.disposition == "PASS"

    def test_same_count_passes(self):
        fps = {self._make_fp("SEC-013")}
        baseline = Baseline(findings=fps, ratchet_mode="ratchet_down")
        result = compare_findings(fps, baseline)
        assert result.disposition == "PASS"

    def test_enforcement_date_ratchet_down(self):
        """After enforcement in ratchet_down: legacy findings FAIL."""
        fp = self._make_fp("SEC-013")
        baseline = Baseline(
            findings={fp},
            ratchet_mode="ratchet_down",
            enforcement_date="2026-01-01",
        )
        result = compare_findings({fp}, baseline, today=date(2026, 6, 15))
        assert result.disposition == "FAIL"


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

class TestNormalisation:
    def test_normalise_path_backslash(self):
        assert _normalise_path("src\\views.py") == "src/views.py"

    def test_normalise_path_leading_dot(self):
        assert _normalise_path("./src/views.py") == "src/views.py"

    def test_normalise_snippet_whitespace(self):
        assert _normalise_snippet("  a   b  ") == "a b"

    def test_normalise_snippet_strings(self):
        assert '""' in _normalise_snippet('x = "hello world"')

    def test_hash_snippet_deterministic(self):
        h1 = _hash_snippet("test snippet")
        h2 = _hash_snippet("test snippet")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Integration: fingerprint_check_results
# ---------------------------------------------------------------------------

class TestFingerprintCheckResults:
    def test_extracts_fingerprints_from_check_results(self):
        from saturnday.evidence import CheckResult

        cr = CheckResult(
            name="xss_check",
            status="FAIL",
            severity="warning",
            findings=[
                {"file": "src/views.py", "line": 10, "detail": "XSS: f-string in HTML"},
                {"file": "src/api.py", "line": 20, "message": "Reflected input"},
            ],
            files_checked=["src/views.py", "src/api.py"],
        )
        fps = fingerprint_check_results([cr])
        assert len(fps) == 2

    def test_skips_passing_checks(self):
        from saturnday.evidence import CheckResult

        cr = CheckResult(
            name="xss_check",
            status="PASS",
            severity="warning",
            findings=[],
        )
        fps = fingerprint_check_results([cr])
        assert len(fps) == 0
