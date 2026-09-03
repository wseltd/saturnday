"""Tests for saturnday.post_checks."""

from pathlib import Path
from textwrap import dedent

from saturnday.post_checks import (
    check_domain_duplicates,
    check_per_request_rebuild,
    check_readme_sections,
    check_shape_only_tests,
    check_silent_swallow,
    check_terminology_inflation,
)


# ---------------------------------------------------------------------------
# check_silent_swallow
# ---------------------------------------------------------------------------


class TestCheckSilentSwallow:
    def test_catches_except_exception_pass(self, tmp_path: Path) -> None:
        py = tmp_path / "bad.py"
        py.write_text(dedent("""\
            try:
                do_stuff()
            except Exception:
                pass
        """))
        findings = check_silent_swallow(tmp_path, ["bad.py"])
        assert len(findings) == 1
        assert "Silent exception swallowing" in findings[0]["message"]

    def test_allows_logged_exception(self, tmp_path: Path) -> None:
        py = tmp_path / "ok.py"
        py.write_text(dedent("""\
            import logging
            logger = logging.getLogger(__name__)
            try:
                do_stuff()
            except Exception as exc:
                logger.warning("Failed: %s", exc)
        """))
        findings = check_silent_swallow(tmp_path, ["ok.py"])
        assert findings == []

    def test_ignores_non_python(self, tmp_path: Path) -> None:
        txt = tmp_path / "notes.txt"
        txt.write_text("except Exception: pass")
        findings = check_silent_swallow(tmp_path, ["notes.txt"])
        assert findings == []


# ---------------------------------------------------------------------------
# check_readme_sections
# ---------------------------------------------------------------------------


class TestCheckReadmeSections:
    def test_passes_with_all_headings(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text(dedent("""\
            # My Project
            ## Trade-offs
            Some trade-offs here.
            ## Limitations
            Some limitations.
            ## Non-goals
            Things we don't do.
        """))
        findings = check_readme_sections(tmp_path, ["README.md"])
        assert findings == []

    def test_fails_when_limitations_missing(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text(dedent("""\
            # My Project
            ## Trade-offs
            Some trade-offs.
            ## Non-goals
            Not doing this.
        """))
        findings = check_readme_sections(tmp_path, ["README.md"])
        assert len(findings) == 1
        assert "Limitations" in findings[0]["message"]

    def test_ignores_non_readme(self, tmp_path: Path) -> None:
        (tmp_path / "CHANGELOG.md").write_text("# Changes")
        findings = check_readme_sections(tmp_path, ["CHANGELOG.md"])
        assert findings == []


# ---------------------------------------------------------------------------
# check_domain_duplicates
# ---------------------------------------------------------------------------


class TestCheckDomainDuplicates:
    def test_flags_overlapping_frozensets(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "module_a.py").write_text(dedent("""\
            CATEGORIES = frozenset({"alpha", "beta", "gamma"})
        """))
        (src / "module_b.py").write_text(dedent("""\
            CATEGORIES = frozenset({"alpha", "beta", "delta"})
        """))
        findings = check_domain_duplicates(
            tmp_path, ["src/module_a.py", "src/module_b.py"],
        )
        assert len(findings) == 1
        assert "Overlapping domain vocabulary" in findings[0]["message"]

    def test_ignores_test_files(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        tests = src / "tests"
        tests.mkdir()
        (src / "module_a.py").write_text(dedent("""\
            CATEGORIES = frozenset({"alpha", "beta", "gamma"})
        """))
        (tests / "test_module.py").write_text(dedent("""\
            CATEGORIES = frozenset({"alpha", "beta", "delta"})
        """))
        findings = check_domain_duplicates(
            tmp_path, ["src/module_a.py", "src/tests/test_module.py"],
        )
        assert findings == []

    def test_no_flag_for_identical_sets(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "module_a.py").write_text('TAGS = frozenset({"x", "y"})\n')
        (src / "module_b.py").write_text('TAGS = frozenset({"x", "y"})\n')
        findings = check_domain_duplicates(
            tmp_path, ["src/module_a.py", "src/module_b.py"],
        )
        assert findings == []


# ---------------------------------------------------------------------------
# check_terminology_inflation
# ---------------------------------------------------------------------------


class TestCheckTerminologyInflation:
    def test_flags_safety_in_substring_function(self, tmp_path: Path) -> None:
        py = tmp_path / "checker.py"
        py.write_text(dedent("""\
            BANNED = {"rm", "sudo", "chmod"}

            def safety_check(cmd: str) -> bool:
                \"\"\"Check command safety.\"\"\"
                return cmd.lower() in BANNED
        """))
        findings = check_terminology_inflation(tmp_path, ["checker.py"])
        assert len(findings) == 1
        assert "Terminology inflation" in findings[0]["message"]

    def test_ignores_guard_in_complex_function(self, tmp_path: Path) -> None:
        py = tmp_path / "guard.py"
        py.write_text(dedent("""\
            import hashlib

            def guard_payload(payload: bytes) -> bool:
                \"\"\"Validate payload integrity.\"\"\"
                digest = hashlib.sha256(payload).hexdigest()
                return verify_signature(digest, get_expected())
        """))
        findings = check_terminology_inflation(tmp_path, ["guard.py"])
        assert findings == []

    def test_ignores_non_assurance_term(self, tmp_path: Path) -> None:
        py = tmp_path / "filter.py"
        py.write_text(dedent("""\
            WORDS = {"bad", "ugly"}
            def word_filter(text: str) -> bool:
                return text.lower() in WORDS
        """))
        findings = check_terminology_inflation(tmp_path, ["filter.py"])
        assert findings == []


# ---------------------------------------------------------------------------
# check_per_request_rebuild
# ---------------------------------------------------------------------------


class TestCheckPerRequestRebuild:
    def test_flags_build_knowledge_store_in_service(self, tmp_path: Path) -> None:
        py = tmp_path / "query_service.py"
        py.write_text(dedent("""\
            def handle_query(request):
                store = build_knowledge_store()
                return store.search(request.text)
        """))
        findings = check_per_request_rebuild(tmp_path, ["query_service.py"])
        assert len(findings) == 1
        assert "Per-request rebuild" in findings[0]["message"]
        assert "build_knowledge_store" in findings[0]["message"]

    def test_ignores_build_in_init(self, tmp_path: Path) -> None:
        py = tmp_path / "search_service.py"
        py.write_text(dedent("""\
            class SearchService:
                def __init__(self):
                    self.store = build_knowledge_store()

                def search(self, query):
                    return self.store.search(query)
        """))
        findings = check_per_request_rebuild(tmp_path, ["search_service.py"])
        assert findings == []

    def test_ignores_non_service_file(self, tmp_path: Path) -> None:
        py = tmp_path / "utils.py"
        py.write_text(dedent("""\
            def setup():
                store = build_knowledge_store()
                return store
        """))
        findings = check_per_request_rebuild(tmp_path, ["utils.py"])
        assert findings == []

    def test_ignores_test_files(self, tmp_path: Path) -> None:
        py = tmp_path / "test_service_reply.py"
        py.write_text(dedent("""\
            def test_suggest_reply():
                store = KnowledgeStore()
                result = store.search("hello")
                assert result is not None
        """))
        findings = check_per_request_rebuild(tmp_path, ["test_service_reply.py"])
        assert findings == []


# ---------------------------------------------------------------------------
# check_shape_only_tests
# ---------------------------------------------------------------------------


class TestCheckShapeOnlyTests:
    def test_flags_all_isinstance_assertions(self, tmp_path: Path) -> None:
        """A test with only isinstance / is not None assertions should be flagged."""
        py = tmp_path / "test_example.py"
        py.write_text(dedent("""\
            def test_result_shape():
                result = build_result()
                assert isinstance(result, dict)
                assert result is not None
        """))
        findings = check_shape_only_tests(tmp_path, ["test_example.py"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "shape_only_test"
        assert "test_result_shape" in findings[0]["message"]

    def test_clears_function_with_value_assertion(self, tmp_path: Path) -> None:
        """One value assertion clears the entire function — no finding."""
        py = tmp_path / "test_example.py"
        py.write_text(dedent("""\
            def test_compute():
                result = compute(2, 3)
                assert isinstance(result, int)
                assert result == 5
        """))
        findings = check_shape_only_tests(tmp_path, ["test_example.py"])
        assert findings == []

    def test_flags_zero_assertion_function(self, tmp_path: Path) -> None:
        """A test function with no assertions at all should be flagged."""
        py = tmp_path / "test_empty.py"
        py.write_text(dedent("""\
            def test_nothing():
                x = do_something()
        """))
        findings = check_shape_only_tests(tmp_path, ["test_empty.py"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "shape_only_test"
        assert "test_nothing" in findings[0]["message"]

    def test_ignores_non_test_file(self, tmp_path: Path) -> None:
        """Shape assertions in a non-test file are not flagged."""
        py = tmp_path / "helpers.py"
        py.write_text(dedent("""\
            def test_something():
                assert isinstance(x, str)
        """))
        findings = check_shape_only_tests(tmp_path, ["helpers.py"])
        assert findings == []

    def test_flags_type_check_only(self, tmp_path: Path) -> None:
        """A test using only ``type(x) == T`` is shape-only."""
        py = tmp_path / "test_typing.py"
        py.write_text(dedent("""\
            def test_type_check():
                result = make_thing()
                assert type(result) == MyClass
        """))
        findings = check_shape_only_tests(tmp_path, ["test_typing.py"])
        assert len(findings) == 1
        assert findings[0]["kind"] == "shape_only_test"

    def test_clears_string_membership_assertion(self, tmp_path: Path) -> None:
        """``assert "expected" in result`` is a value assertion — clears the function."""
        py = tmp_path / "test_content.py"
        py.write_text(dedent("""\
            def test_content():
                result = render()
                assert isinstance(result, str)
                assert "hello" in result
        """))
        findings = check_shape_only_tests(tmp_path, ["test_content.py"])
        assert findings == []
