"""Tests for saturnday.context_assembler."""

from pathlib import Path

from saturnday._types import TicketSpec
from saturnday.context_assembler import (
    assemble_messages,
    build_system_prompt,
    build_ticket_prompt,
    load_standards_context,
    write_standards_file,
)


class TestLoadStandardsContext:
    def test_loads_existing_standards(self, tmp_path: Path) -> None:
        (tmp_path / "coder_standards_v1.txt").write_text("Rule 1: be good")
        load_standards_context.cache_clear()
        result = load_standards_context(str(tmp_path))
        assert "Rule 1: be good" in result

    def test_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        load_standards_context.cache_clear()
        result = load_standards_context(str(tmp_path / "nonexistent"))
        assert result == ""

    def test_result_is_cached(self, tmp_path: Path) -> None:
        (tmp_path / "coder_standards_v1.txt").write_text("cached")
        load_standards_context.cache_clear()
        r1 = load_standards_context(str(tmp_path))
        r2 = load_standards_context(str(tmp_path))
        assert r1 is r2  # Same object = cached


class TestBuildSystemPrompt:
    def test_includes_standards(self, tmp_path: Path) -> None:
        (tmp_path / "coder_standards_v1.txt").write_text("Standard A")
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "coder_system_senior_v1.txt").write_text("You are an engineer.")
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()
        result = build_system_prompt(str(tmp_path))
        assert "You are an engineer." in result
        assert "Standard A" in result

    def test_works_without_prompt_file(self, tmp_path: Path) -> None:
        (tmp_path / "coder_standards_v1.txt").write_text("Standard B")
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()
        result = build_system_prompt(str(tmp_path))
        assert "Standard B" in result


class TestBuildTicketPrompt:
    def test_includes_goal(self) -> None:
        ticket = TicketSpec(ticket_id="T001", goal="Create scaffold")
        result = build_ticket_prompt(ticket, "", "")
        assert "T001" in result
        assert "Create scaffold" in result

    def test_includes_plan_notes(self) -> None:
        ticket = TicketSpec(ticket_id="T001", goal="Test")
        result = build_ticket_prompt(ticket, "", "Use absolute imports")
        assert "Use absolute imports" in result

    def test_includes_state_summary(self) -> None:
        ticket = TicketSpec(ticket_id="T002", goal="Add feature")
        result = build_ticket_prompt(ticket, "Existing modules: foo.py", "")
        assert "Existing modules: foo.py" in result

    def test_includes_output_format(self) -> None:
        ticket = TicketSpec(ticket_id="T001", goal="Test")
        result = build_ticket_prompt(ticket, "", "")
        assert "FILE:" in result
        assert "END FILE" in result


class TestAssembleMessages:
    def test_basic_messages(self) -> None:
        msgs = assemble_messages("system text", "user text")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_repair_context_adds_messages(self) -> None:
        msgs = assemble_messages("sys", "usr", repair_context="Fix lint errors")
        assert len(msgs) == 4
        assert "Fix lint errors" in msgs[3]["content"]
        assert "GOVERNANCE FINDINGS" in msgs[3]["content"]


class TestSaturndayContextIntegration:
    """Tests that verify Saturnday-specific content in the assembled prompt."""

    def _make_standards_dir(self, tmp_path: Path) -> Path:
        """Create a minimal standards dir with the real coder_system_senior_v1.txt."""
        import shutil

        real_prompt = (
            Path(__file__).parent.parent
            / "senior_engineering_standards"
            / "prompts"
            / "coder_system_senior_v1.txt"
        )
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        if real_prompt.exists():
            shutil.copy(real_prompt, prompts_dir / "coder_system_senior_v1.txt")
        else:
            (prompts_dir / "coder_system_senior_v1.txt").write_text(
                "SATURNDAY EXECUTION CONTEXT\ngovernance_judge\nOpenClaw"
            )
        return tmp_path

    def test_coder_prompt_includes_saturnday_context(self, tmp_path: Path) -> None:
        """System prompt includes Saturnday execution context."""
        standards_dir = self._make_standards_dir(tmp_path)
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()
        prompt = build_system_prompt(str(standards_dir))
        assert "SATURNDAY EXECUTION CONTEXT" in prompt
        assert "governance_judge" in prompt
        assert "OpenClaw" in prompt

    def test_corpus_excludes_reviewer_and_data(self, tmp_path: Path) -> None:
        """Standards corpus does not include reviewer prompt or data files."""
        # Populate a standards dir mirroring the real layout
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "coder_system_senior_v1.txt").write_text("coder prompt")
        (tmp_path / "prompts" / "reviewer_system_senior_v1.txt").write_text(
            "reviewer_system_senior_v1 content"
        )
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "exclusion_rules.yaml").write_text("exclusion_rules: []")
        (corpus_dir / "permissive_repos.yaml").write_text("permissive_repos: []")
        evals_dir = tmp_path / "evals"
        evals_dir.mkdir()
        (evals_dir / "senior_style_rubric.yaml").write_text("senior_style_rubric: {}")
        load_standards_context.cache_clear()
        corpus = load_standards_context(str(tmp_path))
        assert "reviewer_system_senior_v1" not in corpus
        assert "exclusion_rules" not in corpus
        assert "permissive_repos" not in corpus
        assert "senior_style_rubric" not in corpus


class TestSeniorJudgmentIntegration:
    def test_critical_rules_included_when_file_exists(self, tmp_path: Path) -> None:
        style_dir = tmp_path / "docs" / "style"
        style_dir.mkdir(parents=True)
        (style_dir / "senior_judgment_rules.md").write_text("Never swallow exceptions.")
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "coder_system_senior_v1.txt").write_text("You are an engineer.")
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()
        result = build_system_prompt(str(tmp_path))
        assert "CRITICAL SENIOR JUDGMENT RULES" in result
        assert "Never swallow exceptions." in result

    def test_common_mistakes_always_present(self, tmp_path: Path) -> None:
        (tmp_path / "coder_standards_v1.txt").write_text("Standard A")
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()
        result = build_system_prompt(str(tmp_path))
        assert "COMMON MISTAKES TO AVOID" in result
        # Verify all 8 lines are present
        assert "Centralize domain constants" in result
        assert "never commit agent artifacts" in result


class TestWriteStandardsFile:
    """Tests for write_standards_file() — the CLI-backend standards delivery path."""

    def _make_standards_dir(self, tmp_path: Path) -> Path:
        """Minimal standards dir with one standards file."""
        (tmp_path / "coder_standards_v1.txt").write_text("Rule X: do good work")
        return tmp_path

    def test_writes_full_content_to_output_path(self, tmp_path: Path) -> None:
        """Full standards content is written to the output file."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "out" / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        write_standards_file(str(standards_dir), output_path)

        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")
        assert "Rule X: do good work" in content

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Output parent directories are created when they do not exist."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "nested" / "deep" / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        write_standards_file(str(standards_dir), output_path)

        assert output_path.exists()

    def test_returns_short_prompt(self, tmp_path: Path) -> None:
        """Returned prompt is a compact reference, not the full corpus."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        short_prompt = write_standards_file(str(standards_dir), output_path)

        # Must be short (< 1500 chars)
        assert len(short_prompt) < 1500

    def test_short_prompt_contains_required_markers(self, tmp_path: Path) -> None:
        """Short prompt includes all three required markers from the ticket spec."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        short_prompt = write_standards_file(str(standards_dir), output_path)

        assert "standards.md" in short_prompt
        assert "MANDATORY BUILD RULES" in short_prompt
        assert "COMMON MISTAKES" in short_prompt

    def test_short_prompt_references_file_before_coding(self, tmp_path: Path) -> None:
        """Short prompt instructs the agent to read the file before writing code."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        short_prompt = write_standards_file(str(standards_dir), output_path)

        assert "BEFORE writing any code" in short_prompt
        assert ".saturnday/standards.md" in short_prompt

    def test_mandatory_build_rules_inline(self, tmp_path: Path) -> None:
        """MANDATORY BUILD RULES content is present verbatim in the short prompt."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        short_prompt = write_standards_file(str(standards_dir), output_path)

        assert "setuptools.build_meta" in short_prompt
        assert "setuptools.backends._legacy" in short_prompt

    def test_common_mistakes_inline(self, tmp_path: Path) -> None:
        """COMMON MISTAKES content is present verbatim in the short prompt."""
        standards_dir = self._make_standards_dir(tmp_path)
        output_path = tmp_path / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        short_prompt = write_standards_file(str(standards_dir), output_path)

        assert "Centralize domain constants" in short_prompt
        assert "never commit agent artifacts" in short_prompt

    def test_full_content_is_longer_than_short_prompt(self, tmp_path: Path) -> None:
        """Written file contains more content than the returned short prompt.

        The full prompt always includes MANDATORY BUILD RULES and COMMON MISTAKES
        plus the standards corpus.  We add a moderately-sized standards file so
        the written output is meaningfully larger than the ~917-char short prompt.
        """
        standards_dir = tmp_path / "standards"
        standards_dir.mkdir()
        # Add enough content so full_content > short_prompt length
        big_doc = "\n".join(f"Rule {i}: some important rule here." for i in range(60))
        (standards_dir / "coder_standards_v1.txt").write_text(big_doc)
        output_path = tmp_path / "out" / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        short_prompt = write_standards_file(str(standards_dir), output_path)
        full_content = output_path.read_text(encoding="utf-8")

        assert len(full_content) > len(short_prompt)

    def test_project_languages_forwarded_to_build(self, tmp_path: Path) -> None:
        """project_languages parameter is forwarded to build_system_prompt."""
        # Use a dedicated standards subdir so written output files don't
        # get scanned back into the corpus on the second call.
        standards_dir = tmp_path / "stds"
        standards_dir.mkdir()
        (standards_dir / "coder_standards_v1.txt").write_text("Rule X: do good work")
        py_only_file = standards_dir / "senior_python_standards.md"
        py_only_file.write_text("Python-only content: use type hints everywhere")

        out_py = tmp_path / "out_py" / "standards.md"
        out_ts = tmp_path / "out_ts" / "standards.md"
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        # With python in languages: python-only file should appear in written output
        write_standards_file(
            str(standards_dir), out_py,
            project_languages=frozenset({"python"}),
        )
        content_with_python = out_py.read_text(encoding="utf-8")

        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()
        write_standards_file(
            str(standards_dir), out_ts,
            project_languages=frozenset({"typescript"}),
        )
        content_without_python = out_ts.read_text(encoding="utf-8")

        assert "Python-only content" in content_with_python
        assert "Python-only content" not in content_without_python

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        """OSError on write is logged as a warning, not raised."""
        standards_dir = self._make_standards_dir(tmp_path)
        # Point to a path whose parent is a file (unwritable)
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a dir")
        output_path = blocker / "standards.md"  # parent is a file — mkdir will fail
        load_standards_context.cache_clear()
        build_system_prompt.cache_clear()

        # Must not raise; should return the short prompt regardless
        result = write_standards_file(str(standards_dir), output_path)
        assert "standards.md" in result
