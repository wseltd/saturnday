"""Tests for impact_analysis.py — Phase 3 of the Saturnday memory system.

Covers T012 (deterministic impact) and T013 (LLM impact explanation).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from saturnday._types import CoderConfig, TicketScope, TicketSpec
from saturnday.run.impact_analysis import (
    ImpactCandidate,
    ImpactReport,
    compute_impact,
    explain_impact_with_llm,
    format_impact_for_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Create a minimal fake repo layout for impact analysis."""
    # Source file that will be "changed"
    src = tmp_path / "src" / "mypackage"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "foo.py").write_text(
        "def public_func():\n    pass\n\nclass PublicClass:\n    pass\n",
        encoding="utf-8",
    )
    # Another source file that imports foo
    (src / "bar.py").write_text(
        "from mypackage.foo import public_func\n\ndef bar():\n    return public_func()\n",
        encoding="utf-8",
    )
    # Test file
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_foo.py").write_text(
        "from mypackage.foo import public_func\n\ndef test_basic():\n    assert public_func() is None\n",
        encoding="utf-8",
    )
    (tests / "test_bar.py").write_text(
        "from mypackage.bar import bar\n\ndef test_bar():\n    pass\n",
        encoding="utf-8",
    )
    # Config files
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mypackage'\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def basic_ticket() -> TicketSpec:
    """Minimal TicketSpec for testing."""
    return TicketSpec(
        ticket_id="T001",
        goal="Update public_func in src/mypackage/foo.py",
        scope=TicketScope(allowed_globs=("src/mypackage/foo.py",)),
    )


@pytest.fixture()
def api_coder_config() -> CoderConfig:
    """API-backend coder config for LLM tests (avoids spawning real process)."""
    return CoderConfig(
        backend="anthropic",
        api_key="test-key",
        model="claude-test",
    )


# ---------------------------------------------------------------------------
# T012 tests: compute_impact
# ---------------------------------------------------------------------------


class TestComputeImpactImportDependency:
    """test_compute_impact_import_dependency — bar.py imports from foo.py."""

    def test_direct_import_found(self, repo: Path) -> None:
        """bar.py imports from foo.py — should appear in import_dependents."""
        report = compute_impact(
            ["src/mypackage/foo.py"],
            repo_path=repo,
            state=None,
        )
        dependent_files = {c.file for c in report.import_dependents}
        # bar.py and test_foo.py both import from foo
        assert any("bar.py" in f for f in dependent_files), (
            f"Expected bar.py in import_dependents, got {dependent_files}"
        )

    def test_impact_type_is_import_dependency(self, repo: Path) -> None:
        """All import_dependents must have the correct impact_type."""
        report = compute_impact(["src/mypackage/foo.py"], repo_path=repo, state=None)
        for candidate in report.import_dependents:
            assert candidate.impact_type == "import_dependency"

    def test_touched_files_preserved(self, repo: Path) -> None:
        """touched_files must exactly mirror the input."""
        files = ["src/mypackage/foo.py"]
        report = compute_impact(files, repo_path=repo, state=None)
        assert report.touched_files == files


class TestComputeImpactTestLinkage:
    """test_compute_impact_test_linkage — tests/test_foo.py linked to foo.py."""

    def test_test_file_flagged_when_not_updated(self, repo: Path) -> None:
        """tests/test_foo.py exists and is NOT in changed_files — must be flagged."""
        report = compute_impact(
            ["src/mypackage/foo.py"],
            repo_path=repo,
            state=None,
        )
        test_files = {c.file for c in report.linked_tests}
        assert any("test_foo" in f for f in test_files), (
            f"Expected test_foo.py in linked_tests, got {test_files}"
        )

    def test_test_file_not_flagged_when_updated(self, repo: Path) -> None:
        """If the test file is already in changed_files, do not flag it."""
        report = compute_impact(
            ["src/mypackage/foo.py", "tests/test_foo.py"],
            repo_path=repo,
            state=None,
        )
        test_files = {c.file for c in report.linked_tests}
        # test_foo.py is in the change set — should NOT appear as a concern
        assert not any("test_foo.py" in f for f in test_files)

    def test_test_linkage_impact_type(self, repo: Path) -> None:
        """Linked test candidates must have impact_type = 'test_coverage'."""
        report = compute_impact(["src/mypackage/foo.py"], repo_path=repo, state=None)
        for candidate in report.linked_tests:
            assert candidate.impact_type == "test_coverage"


class TestComputeImpactConfigDetection:
    """test_compute_impact_config_detection — pyproject.toml in change set."""

    def test_pyproject_flagged_as_build(self, repo: Path) -> None:
        """pyproject.toml in changed_files -> config_impacts with type 'build'."""
        report = compute_impact(["pyproject.toml"], repo_path=repo, state=None)
        assert len(report.config_impacts) == 1
        assert report.config_impacts[0].impact_type == "build"
        assert report.config_impacts[0].risk_level == "medium"

    def test_dockerfile_flagged_as_deploy(self, tmp_path: Path) -> None:
        """Dockerfile in changed_files -> config_impacts with type 'deploy'."""
        (tmp_path / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
        report = compute_impact(["Dockerfile"], repo_path=tmp_path, state=None)
        assert any(c.impact_type == "deploy" for c in report.config_impacts)

    def test_github_workflow_flagged_as_deploy(self, tmp_path: Path) -> None:
        """A .github/workflows/*.yml file -> config_impacts with type 'deploy'."""
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("on: push\n", encoding="utf-8")
        report = compute_impact([".github/workflows/ci.yml"], repo_path=tmp_path, state=None)
        assert any(c.impact_type == "deploy" for c in report.config_impacts)

    def test_policy_file_flagged(self, tmp_path: Path) -> None:
        """A .saturnday-policy.yaml file -> config_impacts with type 'policy'."""
        report = compute_impact([".saturnday-policy.yaml"], repo_path=tmp_path, state=None)
        assert any(c.impact_type == "policy" for c in report.config_impacts)
        assert any(c.risk_level == "high" for c in report.config_impacts)

    def test_requirements_txt_flagged(self, tmp_path: Path) -> None:
        """requirements.txt -> config_impacts (build)."""
        report = compute_impact(["requirements.txt"], repo_path=tmp_path, state=None)
        assert any("requirements" in c.file for c in report.config_impacts)


class TestComputeImpactArchitectureCrossing:
    """test_compute_impact_architecture_crossing — spans src/ and tests/."""

    def test_single_directory_no_crossing(self, repo: Path) -> None:
        """Changes within one top-level dir should not produce crossings."""
        report = compute_impact(
            ["src/mypackage/foo.py", "src/mypackage/bar.py"],
            repo_path=repo,
            state=None,
        )
        assert report.architecture_crossings == []

    def test_multi_directory_crossing_detected(self, repo: Path) -> None:
        """Changes spanning src/ and tests/ should produce a crossing entry."""
        report = compute_impact(
            ["src/mypackage/foo.py", "tests/test_foo.py"],
            repo_path=repo,
            state=None,
        )
        assert len(report.architecture_crossings) == 1
        assert "src" in report.architecture_crossings[0]
        assert "tests" in report.architecture_crossings[0]

    def test_three_directory_crossing(self, repo: Path) -> None:
        """Three boundaries must mention all three in the crossing string."""
        report = compute_impact(
            ["src/mypackage/foo.py", "tests/test_foo.py", "pyproject.toml"],
            repo_path=repo,
            state=None,
        )
        assert report.architecture_crossings
        crossing_text = report.architecture_crossings[0]
        assert "3" in crossing_text


class TestComputeImpactEmptyChanges:
    """test_compute_impact_empty_changes — safe on empty input."""

    def test_empty_list_returns_zero_radius(self, tmp_path: Path) -> None:
        """Empty changed_files returns a report with total_blast_radius=0."""
        report = compute_impact([], repo_path=tmp_path, state=None)
        assert report.total_blast_radius == 0

    def test_empty_list_all_lists_empty(self, tmp_path: Path) -> None:
        """All candidate lists must be empty for empty input."""
        report = compute_impact([], repo_path=tmp_path, state=None)
        assert report.import_dependents == []
        assert report.symbol_references == []
        assert report.linked_tests == []
        assert report.config_impacts == []
        assert report.architecture_crossings == []


class TestComputeImpactBlastRadius:
    """total_blast_radius must count unique affected files."""

    def test_blast_radius_is_non_negative(self, repo: Path) -> None:
        report = compute_impact(["src/mypackage/foo.py"], repo_path=repo, state=None)
        assert report.total_blast_radius >= 0

    def test_blast_radius_counts_unique_files(self, repo: Path) -> None:
        """If the same file appears in multiple candidate lists, count it once."""
        report = compute_impact(["src/mypackage/foo.py"], repo_path=repo, state=None)
        all_candidate_files = (
            {c.file for c in report.import_dependents}
            | {c.file for c in report.symbol_references}
            | {c.file for c in report.linked_tests}
            | {c.file for c in report.config_impacts}
        )
        assert report.total_blast_radius == len(all_candidate_files)


# ---------------------------------------------------------------------------
# T012 tests: format_impact_for_prompt
# ---------------------------------------------------------------------------


class TestFormatImpactForPromptSize:
    """test_format_impact_for_prompt_size — output < 1500 chars."""

    def test_output_under_limit(self, repo: Path) -> None:
        """format_impact_for_prompt output must be <= 1500 chars."""
        report = compute_impact(
            ["src/mypackage/foo.py"],
            repo_path=repo,
            state=None,
        )
        text = format_impact_for_prompt(report)
        assert len(text) <= 1500, f"Output too long: {len(text)} chars"

    def test_empty_report_produces_empty_string(self) -> None:
        """An empty ImpactReport returns an empty string."""
        report = ImpactReport()
        text = format_impact_for_prompt(report)
        assert text == ""

    def test_output_contains_blast_radius(self, repo: Path) -> None:
        """Output must contain the blast radius count."""
        report = compute_impact(["src/mypackage/foo.py"], repo_path=repo, state=None)
        text = format_impact_for_prompt(report)
        if report.total_blast_radius > 0:
            assert str(report.total_blast_radius) in text


class TestFormatImpactHighRiskFirst:
    """test_format_impact_high_risk_first — high-risk entries appear before low."""

    def test_high_risk_before_low_risk(self) -> None:
        """High-risk candidates must appear before low-risk in the output."""
        report = ImpactReport(
            touched_files=["foo.py"],
            config_impacts=[
                ImpactCandidate(
                    file="Dockerfile",
                    impact_type="deploy",
                    detail="Dockerfile is a deploy file",
                    risk_level="high",
                ),
            ],
            symbol_references=[
                ImpactCandidate(
                    file="utils.py",
                    impact_type="symbol_reference",
                    detail="references symbol 'foo'",
                    risk_level="low",
                ),
            ],
            total_blast_radius=2,
        )
        text = format_impact_for_prompt(report)
        high_pos = text.find("Dockerfile")
        low_pos = text.find("utils.py")
        assert high_pos != -1, "High-risk file not in output"
        assert low_pos != -1, "Low-risk file not in output"
        assert high_pos < low_pos, "High-risk must appear before low-risk"

    def test_architecture_crossing_near_top(self) -> None:
        """Architecture crossings must appear near the top of the output."""
        report = ImpactReport(
            touched_files=["src/foo.py"],
            architecture_crossings=["Change spans 2 boundaries: src, tests"],
            import_dependents=[
                ImpactCandidate(
                    file="tests/test_foo.py",
                    impact_type="import_dependency",
                    detail="imports src.foo",
                    risk_level="medium",
                ),
            ],
            total_blast_radius=1,
        )
        text = format_impact_for_prompt(report)
        crossing_pos = text.find("Change spans")
        import_pos = text.find("tests/test_foo.py")
        assert crossing_pos != -1
        assert import_pos != -1
        assert crossing_pos < import_pos, "Architecture crossing must appear before dependents"


# ---------------------------------------------------------------------------
# T013 tests: explain_impact_with_llm
# ---------------------------------------------------------------------------


class TestExplainImpactWithLlm:
    """test_explain_impact_with_llm — WARNING-only, never blocks."""

    def test_skips_trivial_changes(self, basic_ticket: TicketSpec, api_coder_config: CoderConfig, tmp_path: Path) -> None:
        """blast_radius <= 3 must return empty string without calling LLM."""
        report = ImpactReport(touched_files=["foo.py"], total_blast_radius=2)
        with patch("saturnday.run.impact_analysis.invoke_role") as mock_invoke:
            result = explain_impact_with_llm(report, basic_ticket, api_coder_config, tmp_path)
        assert result == ""
        mock_invoke.assert_not_called()

    def test_calls_llm_for_large_blast_radius(
        self, basic_ticket: TicketSpec, api_coder_config: CoderConfig, tmp_path: Path
    ) -> None:
        """blast_radius > 3 should invoke the code_reviewer role."""
        report = ImpactReport(
            touched_files=["foo.py"],
            import_dependents=[
                ImpactCandidate("a.py", "import_dependency", "imports foo", "medium"),
                ImpactCandidate("b.py", "import_dependency", "imports foo", "medium"),
                ImpactCandidate("c.py", "import_dependency", "imports foo", "medium"),
                ImpactCandidate("d.py", "import_dependency", "imports foo", "low"),
            ],
            total_blast_radius=4,
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "auth module may break due to changed interface"
        with patch("saturnday.run.impact_analysis.invoke_role", return_value=mock_result) as mock_invoke:
            result = explain_impact_with_llm(report, basic_ticket, api_coder_config, tmp_path)
        mock_invoke.assert_called_once()
        assert "auth module" in result

    def test_returns_empty_on_llm_failure(
        self, basic_ticket: TicketSpec, api_coder_config: CoderConfig, tmp_path: Path
    ) -> None:
        """LLM failure must return empty string, not raise."""
        report = ImpactReport(
            touched_files=["foo.py"],
            import_dependents=[
                ImpactCandidate("a.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("b.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("c.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("d.py", "import_dependency", "imports foo", "high"),
            ],
            total_blast_radius=4,
        )
        with patch("saturnday.run.impact_analysis.invoke_role", side_effect=RuntimeError("backend down")):
            result = explain_impact_with_llm(report, basic_ticket, api_coder_config, tmp_path)
        assert result == ""

    def test_truncates_long_response(
        self, basic_ticket: TicketSpec, api_coder_config: CoderConfig, tmp_path: Path
    ) -> None:
        """LLM response is capped at 500 chars."""
        report = ImpactReport(
            touched_files=["foo.py"],
            import_dependents=[
                ImpactCandidate("x.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("y.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("z.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("w.py", "import_dependency", "imports foo", "high"),
            ],
            total_blast_radius=4,
        )
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "x" * 1000  # very long response
        with patch("saturnday.run.impact_analysis.invoke_role", return_value=mock_result):
            result = explain_impact_with_llm(report, basic_ticket, api_coder_config, tmp_path)
        assert len(result) <= 500

    def test_returns_empty_when_invoke_role_not_successful(
        self, basic_ticket: TicketSpec, api_coder_config: CoderConfig, tmp_path: Path
    ) -> None:
        """When invoke_role.success is False, return empty string."""
        report = ImpactReport(
            touched_files=["foo.py"],
            import_dependents=[
                ImpactCandidate("a.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("b.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("c.py", "import_dependency", "imports foo", "high"),
                ImpactCandidate("d.py", "import_dependency", "imports foo", "high"),
            ],
            total_blast_radius=4,
        )
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.output = ""
        with patch("saturnday.run.impact_analysis.invoke_role", return_value=mock_result):
            result = explain_impact_with_llm(report, basic_ticket, api_coder_config, tmp_path)
        assert result == ""
