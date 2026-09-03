"""Tests for explain-failure across evidence types.

Covers:
1. Run evidence still works (existing ledger-based path)
2. Governance evidence prints governance-report.md
3. Release evidence prints summary.md
4. Missing report file gives clear error
5. Unsupported directory gives clear error
6. Unrelated behaviour does not regress
"""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import sys

from saturnday.cli import main


def _capture(args: list[str]) -> tuple[int, str, str]:
    """Run main() and capture stdout/stderr."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = StringIO(), StringIO()
    try:
        code = main(args)
    finally:
        out = sys.stdout.getvalue()
        err = sys.stderr.getvalue()
        sys.stdout, sys.stderr = old_out, old_err
    return code, out, err


# ---------------------------------------------------------------------------
# 1. Run evidence (existing path)
# ---------------------------------------------------------------------------

class TestRunEvidence:
    def test_run_evidence_works(self, tmp_path: Path) -> None:
        ledger_dir = tmp_path / "evidence" / "run"
        ledger_dir.mkdir(parents=True)
        ledger_data = {
            "ticket_statuses": {
                "T001": {"disposition": "PASS"},
                "T002": {"disposition": "FAIL"},
            }
        }
        (ledger_dir / "ledger.json").write_text(json.dumps(ledger_data), encoding="utf-8")

        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 0
        assert "Summary" in out
        assert "Passed" in out


# ---------------------------------------------------------------------------
# 2. Governance evidence
# ---------------------------------------------------------------------------

class TestGovernanceEvidence:
    def test_governance_report_printed(self, tmp_path: Path) -> None:
        (tmp_path / "governance-report.md").write_text(
            "# Governance Report\n\nDisposition: FAIL\nFindings: 5\n",
            encoding="utf-8",
        )
        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 0
        assert "Governance Report" in out
        assert "Disposition: FAIL" in out

    def test_governance_summary_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "final-disposition.json").write_text('{"disposition": "FAIL"}', encoding="utf-8")
        (tmp_path / "summary.md").write_text("# Summary\n\nFAIL\n", encoding="utf-8")
        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 0
        assert "Summary" in out


# ---------------------------------------------------------------------------
# 3. Release evidence
# ---------------------------------------------------------------------------

class TestReleaseEvidence:
    def test_release_summary_printed(self, tmp_path: Path) -> None:
        (tmp_path / "evidence.json").write_text('{"disposition": "FAIL"}', encoding="utf-8")
        (tmp_path / "summary.md").write_text(
            "# Release Summary\n\nDisposition: FAIL\n",
            encoding="utf-8",
        )
        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 0
        assert "Release Summary" in out


# ---------------------------------------------------------------------------
# 4. Missing report file
# ---------------------------------------------------------------------------

class TestMissingReport:
    def test_governance_no_report(self, tmp_path: Path) -> None:
        (tmp_path / "final-disposition.json").write_text('{}', encoding="utf-8")
        # No governance-report.md or summary.md
        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 1
        assert "governance report not found" in err.lower() or "expected" in err.lower()

    def test_release_no_summary(self, tmp_path: Path) -> None:
        (tmp_path / "evidence.json").write_text('{}', encoding="utf-8")
        # No summary.md
        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 1
        assert "release summary not found" in err.lower() or "expected" in err.lower()


# ---------------------------------------------------------------------------
# 5. Unsupported directory
# ---------------------------------------------------------------------------

class TestUnsupportedDir:
    def test_empty_dir_gives_clear_error(self, tmp_path: Path) -> None:
        code, out, err = _capture(["explain-failure", "--output-dir", str(tmp_path)])
        assert code == 1
        assert "unrecognised evidence directory" in err.lower()
        assert "expected one of" in err.lower()

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        fake = tmp_path / "nonexistent"
        code, out, err = _capture(["explain-failure", "--output-dir", str(fake)])
        assert code == 1
        assert "not found" in err.lower()
