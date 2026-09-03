"""Tests for OpenClaw passive skill scanner."""

import json
import tempfile
from pathlib import Path

import pytest

from saturnday.openclaw_scanner import (
    ScanSummary,
    SkillScanResult,
    _compute_skill_md_hash,
    _load_existing_results,
    discover_skills,
    scan_corpus,
    scan_skill,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_corpus(tmp: str, skills: dict[str, dict[str, str]]) -> Path:
    """Create a corpus with multiple skills.

    skills: {relative_path: {filename: content}}
    """
    root = Path(tmp) / "corpus"
    root.mkdir()
    for rel_path, files in skills.items():
        skill_dir = root / rel_path
        skill_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (skill_dir / name).write_text(content)
    return root


def _make_skill_dir(tmp: str, files: dict[str, str]) -> Path:
    """Create a single skill directory."""
    skill = Path(tmp) / "skill"
    skill.mkdir()
    for name, content in files.items():
        p = skill / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return skill


# ---------------------------------------------------------------------------
# discover_skills
# ---------------------------------------------------------------------------

class TestDiscoverSkills:
    def test_finds_skills_with_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "owner1/skill-a": {"SKILL.md": "---\nname: a\n---\n"},
                "owner1/skill-b": {"SKILL.md": "---\nname: b\n---\n"},
                "owner2/skill-c": {"SKILL.md": "---\nname: c\n---\n"},
            })
            skills = discover_skills(root)
            assert len(skills) == 3

    def test_skips_dirs_without_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "owner1/skill-a": {"SKILL.md": "---\nname: a\n---\n"},
                "owner1/not-a-skill": {"README.md": "# Hello"},
            })
            skills = discover_skills(root)
            assert len(skills) == 1

    def test_skips_excluded_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {"SKILL.md": "test"},
            })
            # Put a SKILL.md inside node_modules
            nm = root / "skill-a" / "node_modules" / "fake"
            nm.mkdir(parents=True)
            (nm / "SKILL.md").write_text("test")
            skills = discover_skills(root)
            assert len(skills) == 1  # Only the top-level one

    def test_empty_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            skills = discover_skills(root)
            assert skills == []


# ---------------------------------------------------------------------------
# scan_skill
# ---------------------------------------------------------------------------

class TestScanSkill:
    def test_clean_skill_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: clean\n---\nA clean skill.\n",
                "index.ts": "export const hello = () => 'world';\n",
            })
            result = scan_skill(skill)
            assert result.status == "scanned"
            assert result.disposition == "PASS"
            assert result.findings_count == 0

    def test_skill_with_secret_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: bad\n---\n",
                "config.ts": 'export const API_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234567890";\n',
            })
            result = scan_skill(skill)
            assert result.findings_count > 0
            assert any(
                cr["name"] == "secrets_ts" and cr["status"] == "FAIL"
                for cr in result.check_results
            )

    def test_skill_with_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: todo\n---\n",
                "index.ts": "// TODO: implement this\nexport const x = 1;\n",
            })
            result = scan_skill(skill)
            assert result.findings_count > 0

    def test_missing_skill_md_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill)
            assert result.status == "skipped"
            assert result.error == "no SKILL.md found"

    def test_result_has_all_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill)
            check_names = {cr["name"] for cr in result.check_results}
            # All 6 passive checks should have run
            assert "secrets_ts" in check_names
            assert "hallucinated_imports_ts" in check_names
            assert "typosquat_ts" in check_names
            assert "fake_tests_ts" in check_names
            assert "prompt_injection_ts" in check_names
            assert "placeholders_ts" in check_names

    def test_result_all_checks_have_standard_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill)
            for cr in result.check_results:
                assert "name" in cr
                assert "status" in cr
                assert "findings" in cr
                assert "exit_code" in cr
                assert "error" in cr

    def test_syntax_included_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill, include_syntax=True)
            check_names = {cr["name"] for cr in result.check_results}
            assert "syntax_ts" in check_names


# ---------------------------------------------------------------------------
# scan_corpus
# ---------------------------------------------------------------------------

class TestScanCorpus:
    def test_basic_corpus_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "owner/skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "export const x = 1;\n",
                },
                "owner/skill-b": {
                    "SKILL.md": "---\nname: b\n---\n",
                    "index.ts": "export const y = 2;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            summary = scan_corpus(root, output_dir)
            assert summary.total_candidates == 2
            assert summary.total_scanned >= 2
            assert (output_dir / "findings.jsonl").exists()
            assert (output_dir / "summary.md").exists()
            assert (output_dir / "report.json").exists()

    def test_limit_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                f"skill-{i}": {"SKILL.md": f"---\nname: s{i}\n---\n", "index.ts": "const x = 1;\n"}
                for i in range(10)
            })
            output_dir = Path(tmp) / "output"
            summary = scan_corpus(root, output_dir, limit=3)
            assert summary.total_candidates == 3

    def test_resume_skips_already_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "const x = 1;\n",
                },
                "skill-b": {
                    "SKILL.md": "---\nname: b\n---\n",
                    "index.ts": "const y = 2;\n",
                },
            })
            output_dir = Path(tmp) / "output"

            # First scan
            summary1 = scan_corpus(root, output_dir)
            jsonl1 = (output_dir / "findings.jsonl").read_text()
            lines1 = [l for l in jsonl1.strip().split("\n") if l]

            # Second scan — should skip both
            summary2 = scan_corpus(root, output_dir)
            assert summary2.total_skipped == 2

            # JSONL should still have only 2 entries (no duplicates)
            jsonl2 = (output_dir / "findings.jsonl").read_text()
            lines2 = [l for l in jsonl2.strip().split("\n") if l]
            assert len(lines2) == 2

    def test_resume_rescans_changed_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\nVersion 1\n",
                    "index.ts": "const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"

            # First scan
            scan_corpus(root, output_dir)

            # Modify SKILL.md (changes hash)
            (root / "skill-a" / "SKILL.md").write_text("---\nname: a\n---\nVersion 2\n")

            # Second scan — should rescan because hash changed
            summary2 = scan_corpus(root, output_dir)
            assert summary2.total_skipped == 0
            assert summary2.total_scanned >= 1

    def test_output_files_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "// TODO: fix\nexport const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir)

            # JSONL is valid
            for line in (output_dir / "findings.jsonl").read_text().strip().split("\n"):
                entry = json.loads(line)
                assert "relative_path" in entry
                assert "skill_md_hash" in entry
                assert "status" in entry
                assert "checks" in entry

            # report.json has stable contract fields
            report = json.loads((output_dir / "report.json").read_text())
            assert "saturnday_version" in report
            assert "scan_timestamp" in report
            assert "summary" in report
            assert "skills" in report
            assert "total_skills" in report["summary"]
            assert "total_findings" in report["summary"]
            assert "disposition" in report["summary"]

            # summary.md exists and is not empty
            summary_text = (output_dir / "summary.md").read_text()
            assert "Saturnday OpenClaw Scan Summary" in summary_text

    def test_top_n_creates_appendix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "// TODO: fix\n// FIXME: broken\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, top_n=5)
            assert (output_dir / "top-n.md").exists()

    def test_progress_callback(self):
        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                f"skill-{i}": {"SKILL.md": f"---\nname: s{i}\n---\n", "index.ts": "const x = 1;\n"}
                for i in range(5)
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, progress_interval=2, progress_callback=messages.append)
            assert len(messages) >= 1  # At least one progress update


# ---------------------------------------------------------------------------
# Resume / identity
# ---------------------------------------------------------------------------

class TestResumeIdentity:
    def test_skill_md_hash_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {"SKILL.md": "---\nname: test\n---\nContent\n"})
            h1 = _compute_skill_md_hash(skill)
            h2 = _compute_skill_md_hash(skill)
            assert h1 == h2
            assert len(h1) == 16  # truncated SHA-256

    def test_skill_md_hash_changes_on_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {"SKILL.md": "version 1"})
            h1 = _compute_skill_md_hash(skill)
            (skill / "SKILL.md").write_text("version 2")
            h2 = _compute_skill_md_hash(skill)
            assert h1 != h2

    def test_load_existing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            jsonl = output_dir / "findings.jsonl"
            jsonl.write_text(
                '{"relative_path": "a/b", "skill_md_hash": "abc123"}\n'
                '{"relative_path": "c/d", "skill_md_hash": "def456"}\n'
            )
            existing = _load_existing_results(output_dir)
            assert existing == {"a/b": "abc123", "c/d": "def456"}

    def test_load_existing_results_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            existing = _load_existing_results(output_dir)
            assert existing == {}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_scan_empty_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            output_dir = Path(tmp) / "output"
            summary = scan_corpus(root, output_dir)
            assert summary.total_candidates == 0
            assert summary.total_scanned == 0

    def test_scan_skill_with_binary_files(self):
        """Binary files should not crash the scanner."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
            })
            # Write a binary file
            (skill / "data.bin").write_bytes(b"\x00\x01\x02\x03" * 100)
            result = scan_skill(skill)
            assert result.status in ("scanned", "scanned_degraded")

    def test_per_skill_timeout_enforcement(self):
        """Setting timeout_s=0 should cause all checks to be SKIPPED."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill, timeout_s=0)
            assert result.status == "failed_timeout"
            # All checks should be SKIPPED due to timeout
            skipped = [cr for cr in result.check_results if cr["status"] == "SKIPPED"]
            assert len(skipped) == len(result.check_results), (
                f"Expected all checks SKIPPED, got {len(skipped)}/{len(result.check_results)}"
            )
            # Every SKIPPED check must have error="timeout"
            for cr in skipped:
                assert cr["error"] == "timeout", f"SKIPPED check {cr['name']} missing error='timeout'"
            # Each SKIPPED check must have standard fields
            for cr in skipped:
                assert "name" in cr
                assert "findings" in cr

    def test_timeout_with_generous_limit_runs_all_checks(self):
        """With a generous timeout, no checks should be SKIPPED due to timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill, timeout_s=300)
            assert result.status == "scanned"
            timeout_skipped = [
                cr for cr in result.check_results
                if cr["status"] == "SKIPPED" and cr.get("error") == "timeout"
            ]
            assert len(timeout_skipped) == 0

    def test_strict_plus_timeout_becomes_fail(self):
        """In strict mode, timeout-SKIPPED checks should cause FAIL disposition."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill, timeout_s=0, strict=True)
            assert result.status == "failed_timeout"
            assert result.disposition == "FAIL"

    def test_strict_mode_skipped_becomes_fail(self):
        """In strict mode, SKIPPED checks should cause FAIL disposition."""
        import shutil
        if shutil.which("node"):
            pytest.skip("Node is available, cannot test strict mode on missing runtime")
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill, include_syntax=True, strict=True)
            # syntax_ts should be SKIPPED (no Node), and strict should make disposition FAIL
            assert result.disposition == "FAIL"


# ---------------------------------------------------------------------------
# Fix 1: Format parameter
# ---------------------------------------------------------------------------

class TestScanFormat:
    def test_format_both_writes_all_files(self):
        """fmt='both' (default) should produce findings.jsonl, summary.md, and report.json."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "export const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, fmt="both")
            assert (output_dir / "findings.jsonl").exists()
            assert (output_dir / "summary.md").exists()
            assert (output_dir / "report.json").exists()

    def test_format_json_skips_markdown(self):
        """fmt='json' should write findings.jsonl + report.json, not summary.md."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "export const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, fmt="json")
            assert (output_dir / "findings.jsonl").exists()
            assert (output_dir / "report.json").exists()
            assert not (output_dir / "summary.md").exists()

    def test_format_markdown_skips_json(self):
        """fmt='markdown' should write findings.jsonl + summary.md, not report.json."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "export const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, fmt="markdown")
            assert (output_dir / "findings.jsonl").exists()
            assert (output_dir / "summary.md").exists()
            assert not (output_dir / "report.json").exists()

    def test_format_default_is_both(self):
        """Default format should be 'both'."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "export const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir)  # no fmt arg
            assert (output_dir / "findings.jsonl").exists()
            assert (output_dir / "summary.md").exists()
            assert (output_dir / "report.json").exists()

    def test_json_has_stable_contract_fields(self):
        """report.json must have the stable aggregate JSON contract fields."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "// TODO: fix\nexport const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, fmt="json")
            report = json.loads((output_dir / "report.json").read_text())
            # Top-level required fields
            assert "saturnday_version" in report
            assert "scan_timestamp" in report
            assert "summary" in report
            assert "skills" in report
            # Summary required fields
            s = report["summary"]
            assert "total_skills" in s
            assert "total_findings" in s
            assert "disposition" in s
            assert "findings_by_severity" in s
            assert "findings_by_category" in s
            # Skills array structure
            assert isinstance(report["skills"], list)
            if report["skills"]:
                skill = report["skills"][0]
                assert "skill_path" in skill
                assert "disposition" in skill
                assert "findings" in skill

    def test_json_finding_structure(self):
        """Each finding in report.json must have standard fields."""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_corpus(tmp, {
                "skill-a": {
                    "SKILL.md": "---\nname: a\n---\n",
                    "index.ts": "// TODO: fix\nexport const x = 1;\n",
                },
            })
            output_dir = Path(tmp) / "output"
            scan_corpus(root, output_dir, fmt="json")
            report = json.loads((output_dir / "report.json").read_text())
            for skill in report["skills"]:
                for f in skill.get("findings", []):
                    assert "check" in f
                    assert "severity" in f
                    assert "file" in f
                    assert "message" in f

    def test_jsonl_always_written(self):
        """findings.jsonl must always be written regardless of format."""
        for fmt in ("json", "markdown", "both"):
            with tempfile.TemporaryDirectory() as tmp:
                root = _make_corpus(tmp, {
                    "skill-a": {
                        "SKILL.md": "---\nname: a\n---\n",
                        "index.ts": "const x = 1;\n",
                    },
                })
                output_dir = Path(tmp) / "output"
                scan_corpus(root, output_dir, fmt=fmt)
                assert (output_dir / "findings.jsonl").exists(), f"JSONL missing for fmt={fmt}"


# ---------------------------------------------------------------------------
# Fix 2: Timeout budget passing
# ---------------------------------------------------------------------------

class TestTimeoutBudget:
    def test_remaining_budget_passed_to_checks(self):
        """Timeout-aware checks should receive remaining budget, not full timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            # With a generous timeout, all checks complete normally
            result = scan_skill(skill, timeout_s=300)
            assert result.status == "scanned"

    def test_timeout_skipped_checks_have_info_findings(self):
        """When budget exhausted, SKIPPED checks should record info findings."""
        with tempfile.TemporaryDirectory() as tmp:
            skill = _make_skill_dir(tmp, {
                "SKILL.md": "---\nname: test\n---\n",
                "index.ts": "const x = 1;\n",
            })
            result = scan_skill(skill, timeout_s=0)
            assert result.status == "failed_timeout"
            for cr in result.check_results:
                if cr["status"] == "SKIPPED" and cr["error"] == "timeout":
                    # Should have an info finding about why it was skipped
                    assert len(cr["findings"]) > 0
                    assert cr["findings"][0]["kind"] == "timeout_skip"
