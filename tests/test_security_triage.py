"""Tests for saturnday.security_triage.

Tests cover:
- _is_security_finding: security vs non-security identification
- _parse_triage_response: JSON verdict parsing from LLM response
- triage_security_findings: end-to-end filtering with mocked call_coder

No real LLM calls are made; call_coder is always mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import CoderConfig
from saturnday.security_triage import (
    _is_security_finding,
    _parse_triage_response,
    triage_security_findings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config() -> CoderConfig:
    return CoderConfig(backend="claude-cli")


def _security_finding(
    kind: str = "sql_injection",
    file: str = "src/db.py",
    line: int = 10,
    rule_id: str = "",
) -> dict:
    f: dict = {"file": file, "line": line, "kind": kind, "detail": "test detail"}
    if rule_id:
        f["rule_id"] = rule_id
    return f


def _non_security_finding(kind: str = "missing_license") -> dict:
    return {"file": "LICENSE", "line": 0, "kind": kind, "detail": "no license"}


def _verdict_response(verdicts: list[dict]) -> str:
    """Build a plain JSON array response as the LLM would return."""
    return json.dumps(verdicts)


def _fp_verdict(index: int, reason: str = "safe") -> dict:
    return {"finding_index": index, "verdict": "FALSE_POSITIVE", "reason": reason}


def _tp_verdict(index: int, reason: str = "real") -> dict:
    return {"finding_index": index, "verdict": "TRUE_POSITIVE", "reason": reason}


def _uncertain_verdict(index: int) -> dict:
    return {"finding_index": index, "verdict": "UNCERTAIN", "reason": "not sure"}


# ---------------------------------------------------------------------------
# _is_security_finding
# ---------------------------------------------------------------------------

class TestIsSecurityFinding:
    def test_sql_injection_kind_is_security(self) -> None:
        assert _is_security_finding({"kind": "sql_injection"}) is True

    def test_csrf_missing_kind_is_security(self) -> None:
        assert _is_security_finding({"kind": "csrf_missing"}) is True

    def test_jwt_literal_secret_is_security(self) -> None:
        assert _is_security_finding({"kind": "jwt_literal_secret"}) is True

    def test_missing_token_expiry_is_security(self) -> None:
        assert _is_security_finding({"kind": "missing_token_expiry"}) is True

    def test_route_no_auth_is_security(self) -> None:
        assert _is_security_finding({"kind": "route_no_auth"}) is True

    def test_xss_reflected_is_security(self) -> None:
        assert _is_security_finding({"kind": "xss_reflected"}) is True

    def test_weak_randomness_is_security(self) -> None:
        assert _is_security_finding({"kind": "weak_randomness"}) is True

    def test_idor_pattern_is_security(self) -> None:
        assert _is_security_finding({"kind": "idor_pattern"}) is True

    def test_rule_id_sec_prefix_is_security(self) -> None:
        assert _is_security_finding({"kind": "unknown_kind", "rule_id": "SEC-042"}) is True

    def test_rule_id_sec_fe_prefix_is_security(self) -> None:
        assert _is_security_finding({"kind": "unknown_kind", "rule_id": "SEC-FE-001"}) is True

    def test_missing_license_is_not_security(self) -> None:
        assert _is_security_finding({"kind": "missing_license"}) is False

    def test_missing_readme_is_not_security(self) -> None:
        assert _is_security_finding({"kind": "missing_readme"}) is False

    def test_tests_failing_is_not_security(self) -> None:
        assert _is_security_finding({"kind": "tests_failing"}) is False

    def test_empty_kind_is_not_security(self) -> None:
        assert _is_security_finding({"kind": ""}) is False

    def test_never_triage_missing_security_logging(self) -> None:
        assert _is_security_finding({"kind": "missing_security_logging"}) is False

    def test_never_triage_bare_logging(self) -> None:
        assert _is_security_finding({"kind": "bare_logging"}) is False

    def test_never_triage_missing_rate_limit(self) -> None:
        assert _is_security_finding({"kind": "missing_rate_limit"}) is False

    def test_never_triage_missing_auth(self) -> None:
        assert _is_security_finding({"kind": "missing_auth"}) is False

    def test_missing_security_logging_ts_not_triaged(self) -> None:
        assert _is_security_finding({"kind": "missing_security_logging_ts"}) is False

    def test_never_triage_bare_logging_ts(self) -> None:
        assert _is_security_finding({"kind": "bare_logging_ts"}) is False

    def test_env_fallback_secret_is_security(self) -> None:
        assert _is_security_finding({"kind": "env_fallback_secret"}) is True

    def test_client_trusted_logic_is_security(self) -> None:
        assert _is_security_finding({"kind": "client_trusted_logic"}) is True

    def test_token_no_revoke_is_security(self) -> None:
        assert _is_security_finding({"kind": "token_no_revoke"}) is True

    def test_finding_missing_kind_key_is_not_security(self) -> None:
        # dict without "kind" key — getattr returns ""
        assert _is_security_finding({}) is False

    def test_non_sec_rule_id_prefix_not_security(self) -> None:
        # rule_id that doesn't start with SEC- should not make it security
        assert _is_security_finding({"kind": "missing_license", "rule_id": "Q-001"}) is False


# ---------------------------------------------------------------------------
# _parse_triage_response
# ---------------------------------------------------------------------------

class TestParseTriageResponse:
    """Tests for _parse_triage_response."""

    def _findings(self, n: int = 2) -> list[tuple[int, dict]]:
        """Build a file_findings list of length n."""
        return [
            (i, {"kind": "sql_injection", "file": "db.py", "line": i})
            for i in range(n)
        ]

    def test_false_positive_verdict_returns_global_index(self) -> None:
        findings = self._findings(2)
        response = _verdict_response([_fp_verdict(0)])
        result = _parse_triage_response(response, findings)
        assert 0 in result

    def test_true_positive_not_in_false_positives(self) -> None:
        findings = self._findings(2)
        response = _verdict_response([_tp_verdict(0)])
        result = _parse_triage_response(response, findings)
        assert 0 not in result

    def test_uncertain_not_in_false_positives(self) -> None:
        findings = self._findings(2)
        response = _verdict_response([_uncertain_verdict(0)])
        result = _parse_triage_response(response, findings)
        assert 0 not in result

    def test_multiple_false_positives_all_returned(self) -> None:
        findings = self._findings(3)
        response = _verdict_response([_fp_verdict(0), _fp_verdict(1), _tp_verdict(2)])
        result = _parse_triage_response(response, findings)
        assert result == {0, 1}

    def test_empty_response_returns_empty_set(self) -> None:
        findings = self._findings(2)
        result = _parse_triage_response("", findings)
        assert result == set()

    def test_malformed_json_returns_empty_set(self) -> None:
        findings = self._findings(2)
        result = _parse_triage_response("not json at all {{{", findings)
        assert result == set()

    def test_json_object_not_array_returns_empty_set(self) -> None:
        findings = self._findings(1)
        result = _parse_triage_response('{"verdict": "FALSE_POSITIVE"}', findings)
        assert result == set()

    def test_markdown_json_block_parsed(self) -> None:
        findings = self._findings(1)
        response = "```json\n" + _verdict_response([_fp_verdict(0)]) + "\n```"
        result = _parse_triage_response(response, findings)
        assert 0 in result

    def test_plain_markdown_block_parsed(self) -> None:
        findings = self._findings(1)
        response = "```\n" + _verdict_response([_fp_verdict(0)]) + "\n```"
        result = _parse_triage_response(response, findings)
        assert 0 in result

    def test_out_of_range_index_ignored(self) -> None:
        findings = self._findings(2)
        response = _verdict_response([{"finding_index": 99, "verdict": "FALSE_POSITIVE", "reason": "x"}])
        result = _parse_triage_response(response, findings)
        assert result == set()

    def test_negative_index_ignored(self) -> None:
        findings = self._findings(2)
        response = _verdict_response([{"finding_index": -1, "verdict": "FALSE_POSITIVE", "reason": "x"}])
        result = _parse_triage_response(response, findings)
        assert result == set()

    def test_non_int_index_ignored(self) -> None:
        findings = self._findings(2)
        response = _verdict_response([{"finding_index": "0", "verdict": "FALSE_POSITIVE", "reason": "x"}])
        result = _parse_triage_response(response, findings)
        assert result == set()

    def test_missing_finding_index_ignored(self) -> None:
        findings = self._findings(1)
        response = _verdict_response([{"verdict": "FALSE_POSITIVE", "reason": "x"}])
        result = _parse_triage_response(response, findings)
        assert result == set()

    def test_global_index_mapping_correct(self) -> None:
        # file_findings[local_idx][0] is the global index
        # local_idx=1 → global_idx=5 (when global_idx in the tuple is 5)
        findings = [(3, {"kind": "sql_injection"}), (5, {"kind": "csrf_missing"})]
        response = _verdict_response([_fp_verdict(1)])
        result = _parse_triage_response(response, findings)
        assert 5 in result
        assert 3 not in result

    def test_uppercase_false_positive_verdict_matches(self) -> None:
        findings = self._findings(1)
        response = _verdict_response([{"finding_index": 0, "verdict": "false_positive", "reason": "x"}])
        # Verdict is uppercased inside _parse_triage_response, so lowercase should work
        result = _parse_triage_response(response, findings)
        assert 0 in result

    def test_response_with_preamble_text_still_parsed(self) -> None:
        findings = self._findings(1)
        json_payload = _verdict_response([_fp_verdict(0)])
        response = "Here is my analysis:\n\n" + json_payload
        result = _parse_triage_response(response, findings)
        assert 0 in result

    def test_non_dict_items_in_array_ignored(self) -> None:
        findings = self._findings(2)
        response = '[0, "bad", {"finding_index": 1, "verdict": "FALSE_POSITIVE", "reason": "ok"}]'
        result = _parse_triage_response(response, findings)
        assert 1 in result

    def test_empty_findings_list_returns_empty(self) -> None:
        response = _verdict_response([_fp_verdict(0)])
        result = _parse_triage_response(response, [])
        assert result == set()


# ---------------------------------------------------------------------------
# triage_security_findings
# ---------------------------------------------------------------------------

class TestTriageSecurityFindings:
    """Tests for triage_security_findings — call_coder is always mocked."""

    def test_empty_findings_returns_empty(self, tmp_path: Path) -> None:
        config = _make_config()
        result = triage_security_findings([], tmp_path, config)
        assert result == []

    def test_no_security_findings_returned_unchanged(self, tmp_path: Path) -> None:
        findings = [_non_security_finding("missing_license"), _non_security_finding("missing_readme")]
        config = _make_config()
        result = triage_security_findings(findings, tmp_path, config)
        assert result == findings

    def test_call_coder_not_called_when_no_security_findings(self, tmp_path: Path) -> None:
        findings = [_non_security_finding()]
        config = _make_config()
        with patch("saturnday.security_triage._triage_file") as mock_triage:
            triage_security_findings(findings, tmp_path, config)
            mock_triage.assert_not_called()

    def test_security_finding_kept_when_classified_true_positive(self, tmp_path: Path) -> None:
        f = _security_finding("sql_injection", "src/db.py", 10)
        # Create the file so _triage_file can read it
        src = tmp_path / "src"
        src.mkdir()
        (src / "db.py").write_text("query = f'SELECT * FROM users WHERE id={user_id}'\n", encoding="utf-8")
        config = _make_config()
        response = _verdict_response([_tp_verdict(0)])
        with patch("saturnday.coder_adapter.call_coder", return_value=response):
            with patch("saturnday.role_modes.load_role_prompt", return_value="system prompt"):
                result = triage_security_findings([f], tmp_path, config)
        assert f in result

    def test_security_finding_removed_when_false_positive(self, tmp_path: Path) -> None:
        f = _security_finding("sql_injection", "src/db.py", 1)
        src = tmp_path / "src"
        src.mkdir()
        (src / "db.py").write_text("x = parameterized_query(user_id)\n", encoding="utf-8")
        config = _make_config()
        response = _verdict_response([_fp_verdict(0)])
        with patch("saturnday.coder_adapter.call_coder", return_value=response):
            with patch("saturnday.role_modes.load_role_prompt", return_value="system prompt"):
                result = triage_security_findings([f], tmp_path, config)
        assert f not in result

    def test_non_security_findings_never_filtered(self, tmp_path: Path) -> None:
        sec_f = _security_finding("sql_injection", "src/db.py", 1)
        non_sec = _non_security_finding("missing_license")
        src = tmp_path / "src"
        src.mkdir()
        (src / "db.py").write_text("x = 1\n", encoding="utf-8")
        config = _make_config()
        # LLM says the security finding is FP — non-security must still be kept
        response = _verdict_response([_fp_verdict(0)])
        with patch("saturnday.coder_adapter.call_coder", return_value=response):
            with patch("saturnday.role_modes.load_role_prompt", return_value="system prompt"):
                result = triage_security_findings([sec_f, non_sec], tmp_path, config)
        assert non_sec in result
        assert sec_f not in result

    def test_triage_failure_keeps_all_findings(self, tmp_path: Path) -> None:
        f = _security_finding("sql_injection", "src/db.py", 1)
        src = tmp_path / "src"
        src.mkdir()
        (src / "db.py").write_text("x = 1\n", encoding="utf-8")
        config = _make_config()
        with patch("saturnday.coder_adapter.call_coder", side_effect=RuntimeError("LLM down")):
            with patch("saturnday.role_modes.load_role_prompt", return_value="system prompt"):
                result = triage_security_findings([f], tmp_path, config)
        assert f in result

    def test_missing_file_does_not_crash(self, tmp_path: Path) -> None:
        # File path does not exist — _triage_file returns empty set
        f = _security_finding("sql_injection", "no/such/file.py", 1)
        config = _make_config()
        result = triage_security_findings([f], tmp_path, config)
        # Finding kept (cannot triage without source)
        assert f in result

    def test_never_triage_kinds_pass_through_as_non_security(self, tmp_path: Path) -> None:
        f = {"kind": "missing_security_logging", "file": "app.py", "line": 5, "detail": "x"}
        config = _make_config()
        with patch("saturnday.security_triage._triage_file") as mock_triage:
            result = triage_security_findings([f], tmp_path, config)
            mock_triage.assert_not_called()
        assert f in result

    def test_multiple_files_each_triaged_separately(self, tmp_path: Path) -> None:
        f1 = _security_finding("sql_injection", "src/a.py", 1)
        f2 = _security_finding("csrf_missing", "src/b.py", 1)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x=1\n", encoding="utf-8")
        (tmp_path / "src" / "b.py").write_text("x=1\n", encoding="utf-8")
        config = _make_config()
        # FP for first file, TP for second
        call_count = {"n": 0}
        def fake_call_coder(cfg, messages, path):
            call_count["n"] += 1
            # First call (a.py): FP; second call (b.py): TP
            if call_count["n"] == 1:
                return _verdict_response([_fp_verdict(0)])
            return _verdict_response([_tp_verdict(0)])

        with patch("saturnday.coder_adapter.call_coder", side_effect=fake_call_coder):
            with patch("saturnday.role_modes.load_role_prompt", return_value="system prompt"):
                result = triage_security_findings([f1, f2], tmp_path, config)

        assert f1 not in result  # FP — removed
        assert f2 in result      # TP — kept

    def test_malformed_llm_response_keeps_findings(self, tmp_path: Path) -> None:
        f = _security_finding("sql_injection", "src/db.py", 1)
        src = tmp_path / "src"
        src.mkdir()
        (src / "db.py").write_text("x = 1\n", encoding="utf-8")
        config = _make_config()
        with patch("saturnday.coder_adapter.call_coder", return_value="not valid json!!!"):
            with patch("saturnday.role_modes.load_role_prompt", return_value="system prompt"):
                result = triage_security_findings([f], tmp_path, config)
        assert f in result

    def test_mixed_findings_order_preserved_for_non_security(self, tmp_path: Path) -> None:
        non_sec1 = _non_security_finding("missing_license")
        non_sec2 = _non_security_finding("missing_readme")
        config = _make_config()
        result = triage_security_findings([non_sec1, non_sec2], tmp_path, config)
        assert result == [non_sec1, non_sec2]
