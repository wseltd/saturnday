"""Tests for the runtime splitter scope-consistency guard in
``saturnday.ticket_splitter._parse_split_response``.

Defect being defended:
  Planner ticket was fine.  Runtime splitter asked the coder backend for
  sub-ticket JSON.  Returned sub-ticket ``goal`` said "write path X" but
  returned ``files`` list omitted X.  Runtime then enforced
  ``allowed_globs = tuple(files)`` (excluding X).  Coder wrote X.  Scope
  enforcement in ``patch_extractor._validate_path`` blocked the write
  with ``"Path X does not match any allowed glob"``.  Retries cascaded
  and the run stop-conditioned out.

The guard's job is to make that impossible at the contract boundary:

* goal-mentioned paths missing from ``files`` → augment ``allowed_globs``
  with the missing paths, log explicit fallback
* empty / malformed ``files`` with no extractable goal paths → fall back
  to parent ``allowed_globs``, log explicit fallback
* candidate paths that escape the parent ticket's ``allowed_globs`` →
  reject the whole split (runner runs the parent ticket unsplit)
* consistent payloads and vague-goal payloads are left unchanged
* single-root / non-split tickets are untouched (exercised by the
  existing splitter test file)
"""
from __future__ import annotations

import json
import logging

import pytest

from saturnday._exceptions import PatchApplicationError
from saturnday._types import TicketScope, TicketSpec
from saturnday.patch_extractor import _validate_path
from saturnday.ticket_splitter import (
    _extract_goal_paths,
    _parse_split_response,
)


def _split_payload(sub_tickets: list[dict]) -> str:
    """Build a JSON string matching the splitter response contract."""
    return json.dumps({"split": True, "sub_tickets": sub_tickets})


# ---------------------------------------------------------------------------
# _extract_goal_paths
# ---------------------------------------------------------------------------


class TestExtractGoalPaths:
    def test_single_python_path(self) -> None:
        goal = "Create app/__init__.py exporting the FastAPI instance."
        assert _extract_goal_paths(goal) == ["app/__init__.py"]

    def test_multiple_paths_deduped_in_first_seen_order(self) -> None:
        goal = (
            "Write src/acme/core.py and tests/test_core.py. "
            "Also update src/acme/core.py if needed."
        )
        assert _extract_goal_paths(goal) == ["src/acme/core.py", "tests/test_core.py"]

    def test_vague_goal_returns_empty(self) -> None:
        goal = "Refactor the auth package to share a common session helper."
        assert _extract_goal_paths(goal) == []

    def test_directory_reference_is_not_matched(self) -> None:
        goal = "Restructure the src/acme/ package so sub-modules are isolated."
        assert _extract_goal_paths(goal) == []

    def test_non_code_extensions_ignored(self) -> None:
        goal = "Also write README.md and pyproject.toml with project metadata."
        # README.md / pyproject.toml ARE covered by the extension list — this
        # is intentional since coders sometimes must modify them.
        assert set(_extract_goal_paths(goal)) == {"README.md", "pyproject.toml"}

    def test_bareword_with_init_not_confused_with_path(self) -> None:
        # ``__init__`` alone has no extension — must not match.
        assert _extract_goal_paths("add an __init__ for the package") == []

    def test_empty_goal(self) -> None:
        assert _extract_goal_paths("") == []


# ---------------------------------------------------------------------------
# _parse_split_response — scope-consistency guard
# ---------------------------------------------------------------------------


def _parent(allowed_globs: tuple[str, ...] = ("**",)) -> TicketSpec:
    return TicketSpec(
        ticket_id="T100",
        goal="parent",
        scope=TicketScope(
            allowed_globs=allowed_globs,
            max_files_changed=5,
            max_total_diff_lines=400,
            max_per_file_diff_lines=200,
        ),
    )


class TestParseSplitResponseConsistencyGuard:
    # ---- Hard case 1: single-file goal, files omits it -------------

    def test_goal_names_single_file_missing_from_files_augments_scope(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = _parent(("src/**",))
        response = _split_payload([
            {
                "id": "a",
                "goal": "Write src/acme/__init__.py that exposes the app factory.",
                "files": ["src/acme/app.py"],
            },
            {
                "id": "b",
                "goal": "Write src/acme/app.py with create_app().",
                "files": ["src/acme/app.py"],
            },
        ])

        with caplog.at_level(logging.WARNING, logger="saturnday.ticket_splitter"):
            result = _parse_split_response(response, original)

        assert result is not None
        assert len(result) == 2
        # The goal-mentioned path MUST appear in the first sub-ticket's scope.
        assert "src/acme/__init__.py" in result[0].scope.allowed_globs
        assert "src/acme/app.py" in result[0].scope.allowed_globs
        # Warning must name both the sub-ticket and the missing path.
        assert any(
            "splitter guard" in rec.message
            and "T100.a" in rec.message
            and "src/acme/__init__.py" in rec.message
            for rec in caplog.records
        )

    # ---- Hard case 2: multi-file goal, files covers only some ------

    def test_goal_names_multiple_files_partial_coverage_augments_scope(
        self,
    ) -> None:
        original = _parent(("src/**", "tests/**"))
        response = _split_payload([
            {
                "id": "a",
                "goal": (
                    "Write src/acme/core.py with add_user. Also write "
                    "tests/test_core.py. Also update src/acme/schema.py."
                ),
                "files": ["src/acme/core.py", "tests/test_core.py"],
            },
            {
                "id": "b",
                "goal": "Add src/acme/schema.py validation.",
                "files": ["src/acme/schema.py"],
            },
        ])

        result = _parse_split_response(response, original)
        assert result is not None
        globs_a = set(result[0].scope.allowed_globs)
        assert {
            "src/acme/core.py",
            "tests/test_core.py",
            "src/acme/schema.py",
        } <= globs_a

    # ---- Hard case 3: directory-scoped / vague paths ---------------

    def test_directory_scoped_goal_trusts_files_list(self) -> None:
        original = _parent(("src/**",))
        # No concrete file extensions in the goal — the guard should not
        # invent anything and must trust the splitter's file list.
        response = _split_payload([
            {"id": "a", "goal": "Refactor the src/acme/ package internals.", "files": ["src/acme/a.py"]},
            {"id": "b", "goal": "Split the src/acme/ helpers module.", "files": ["src/acme/b.py"]},
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        assert result[0].scope.allowed_globs == ("src/acme/a.py",)
        assert result[1].scope.allowed_globs == ("src/acme/b.py",)

    # ---- Hard case 4: vague goal, concrete files -------------------

    def test_vague_goal_passes_files_through_unchanged(self) -> None:
        original = _parent(("**",))
        response = _split_payload([
            {"id": "a", "goal": "Add a cache layer to the service.", "files": ["src/cache.py"]},
            {"id": "b", "goal": "Wire the cache into the routes.", "files": ["src/routes.py"]},
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        assert result[0].scope.allowed_globs == ("src/cache.py",)
        assert result[1].scope.allowed_globs == ("src/routes.py",)

    # ---- Hard case 5: malformed / empty files ----------------------

    def test_empty_files_with_no_goal_paths_falls_back_to_parent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = _parent(("src/**",))
        response = _split_payload([
            {"id": "a", "goal": "Refactor the service layer.", "files": []},
            {"id": "b", "goal": "Update the callers.", "files": []},
        ])
        with caplog.at_level(logging.WARNING, logger="saturnday.ticket_splitter"):
            result = _parse_split_response(response, original)
        assert result is not None
        # Both sub-tickets inherit parent's scope, not an empty tuple.
        assert result[0].scope.allowed_globs == ("src/**",)
        assert result[1].scope.allowed_globs == ("src/**",)
        assert any(
            "splitter guard" in rec.message
            and "falling back to parent scope" in rec.message
            for rec in caplog.records
        )

    def test_malformed_files_drops_non_strings_and_dupes(self) -> None:
        original = _parent(("**",))
        response = _split_payload([
            {
                "id": "a",
                "goal": "Write src/acme/one.py with helper_one.",
                "files": ["src/acme/one.py", "", None, 42, "src/acme/one.py"],
            },
            {
                "id": "b",
                "goal": "Write src/acme/two.py with helper_two.",
                "files": ["src/acme/two.py"],
            },
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        assert result[0].scope.allowed_globs == ("src/acme/one.py",)

    def test_files_is_not_a_list_falls_back_safely(self) -> None:
        original = _parent(("**",))
        response = _split_payload([
            {"id": "a", "goal": "Write src/acme/x.py.", "files": "src/acme/x.py"},
            {"id": "b", "goal": "Write src/acme/y.py.", "files": "src/acme/y.py"},
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        # String-valued ``files`` is treated as malformed; the guard
        # augments from the goal paths instead.
        assert "src/acme/x.py" in result[0].scope.allowed_globs
        assert "src/acme/y.py" in result[1].scope.allowed_globs

    # ---- Hard case 6: paths outside parent's scope -----------------

    def test_split_rejected_when_files_escape_parent_scope(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        original = _parent(("src/**",))
        response = _split_payload([
            {"id": "a", "goal": "Create src/acme/core.py.", "files": ["src/acme/core.py"]},
            {"id": "b", "goal": "Create /etc/evil.conf.", "files": ["/etc/evil.conf"]},
        ])
        with caplog.at_level(logging.WARNING, logger="saturnday.ticket_splitter"):
            result = _parse_split_response(response, original)
        # Entire split must be rejected — the second sub-ticket tried to
        # widen past the parent's src/** scope.
        assert result is None
        assert any(
            "rejecting split" in rec.message and "/etc/evil.conf" in rec.message
            for rec in caplog.records
        )

    def test_split_rejected_when_goal_escapes_parent_scope(self) -> None:
        original = _parent(("src/**",))
        # Goal mentions a path outside the parent scope; even if the
        # ``files`` list itself is compliant, the goal-mentioned escape
        # can't be honoured.  Uses a ``.toml`` extension so the
        # goal-path extractor catches it — the test is about parent
        # escape handling, not regex coverage.
        response = _split_payload([
            {"id": "a", "goal": "Write src/acme/core.py.", "files": ["src/acme/core.py"]},
            {
                "id": "b",
                "goal": "Patch /etc/app.toml to register acme.",
                "files": ["src/acme/conf_loader.py"],
            },
        ])
        result = _parse_split_response(response, original)
        assert result is None

    def test_wildcard_parent_scope_does_not_trigger_escape(self) -> None:
        original = _parent(("**",))
        response = _split_payload([
            {"id": "a", "goal": "Write foo.py.", "files": ["foo.py"]},
            {"id": "b", "goal": "Write /absolute/bar.py.", "files": ["/absolute/bar.py"]},
        ])
        result = _parse_split_response(response, original)
        # No escape check when parent is wildcard.  Both sub-tickets pass.
        assert result is not None
        assert len(result) == 2

    # ---- Hard case 7: inconsistent but salvageable -----------------

    def test_salvageable_inconsistency_records_reason_in_goal(self) -> None:
        original = _parent(("src/**", "tests/**"))
        response = _split_payload([
            {
                "id": "a",
                "goal": "Write src/acme/__init__.py and src/acme/app.py.",
                "files": ["src/acme/app.py"],
            },
            {
                "id": "b",
                "goal": "Write tests/test_app.py.",
                "files": ["tests/test_app.py"],
            },
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        assert "src/acme/__init__.py" in result[0].scope.allowed_globs
        # Fallback reason must be recorded in the sub-ticket goal so the
        # operator can see it in run-summary / evidence, not only in logs.
        assert "splitter-guard" in result[0].goal
        assert "src/acme/__init__.py" in result[0].goal

    # ---- Hard case 8: not safely salvageable → reject -------------

    def test_escape_path_blocks_split_even_if_other_subs_are_valid(self) -> None:
        # Parent allows only src/**.  The second sub-ticket tries to
        # silently broaden to ``lib/**`` which escapes the operator's
        # original contract — splitter guard must refuse the whole split.
        original = _parent(("src/**",))
        response = _split_payload([
            {"id": "a", "goal": "Write src/ok.py.", "files": ["src/ok.py"]},
            {"id": "b", "goal": "Write lib/bad.py.", "files": ["lib/bad.py"]},
        ])
        result = _parse_split_response(response, original)
        assert result is None


# ---------------------------------------------------------------------------
# End-to-end: splitter output → patch_extractor scope enforcement
# ---------------------------------------------------------------------------


class TestSplitToScopeEndToEnd:
    """Prove the original failure shape is fixed by the splitter guard.

    The shape: a split response whose ``files`` list excludes a path its
    own ``goal`` requires would formerly cause
    ``patch_extractor._validate_path`` to raise ``PatchApplicationError``
    for the goal-required path.  After the guard, the split's
    ``allowed_globs`` covers the goal path and the write is accepted.
    """

    def test_goal_required_path_is_accepted_by_validate_path_post_fix(
        self,
    ) -> None:
        original = _parent(("src/**",))
        response = _split_payload([
            {
                "id": "a",
                "goal": (
                    "Write src/acme/__init__.py that imports create_app "
                    "and re-exports it at package level."
                ),
                # Intentionally omitted from files — this is the exact
                # splitter-contract defect the guard defends against.
                "files": ["src/acme/app.py"],
            },
            {
                "id": "b",
                "goal": "Write src/acme/app.py with create_app().",
                "files": ["src/acme/app.py"],
            },
        ])

        result = _parse_split_response(response, original)
        assert result is not None
        sub_a = result[0]

        # This call used to raise before the guard was added, because
        # ``allowed_globs`` would have been only ``("src/acme/app.py",)``.
        _validate_path(
            "src/acme/__init__.py",
            sub_a.scope.allowed_globs,
            sub_a.scope.forbidden_globs,
        )

    def test_pre_fix_failure_shape_would_have_been_raised_without_guard(
        self,
    ) -> None:
        # Exact reproduction of the pre-fix scope — if a sub-ticket's
        # ``allowed_globs`` excludes the goal path, ``_validate_path``
        # must reject the write.  This test codifies the enforcement
        # surface so the guard is measured against the real failure mode.
        narrow_scope_from_broken_splitter = ("src/acme/app.py",)
        with pytest.raises(PatchApplicationError, match="does not match any allowed glob"):
            _validate_path(
                "src/acme/__init__.py",
                narrow_scope_from_broken_splitter,
                (),
            )

    def test_consistent_split_scope_behaviour_unchanged(self) -> None:
        # Parent scope covers both src and tests so the consistent
        # file paths below are all in-scope.
        original = TicketSpec(
            ticket_id="T200",
            goal="parent",
            scope=TicketScope(allowed_globs=("src/**", "tests/**")),
        )
        response = _split_payload([
            {
                "id": "a",
                "goal": "Write src/acme/core.py with helpers.",
                "files": ["src/acme/core.py"],
            },
            {
                "id": "b",
                "goal": "Write tests/test_core.py.",
                "files": ["tests/test_core.py"],
            },
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        assert result[0].scope.allowed_globs == ("src/acme/core.py",)
        assert result[1].scope.allowed_globs == ("tests/test_core.py",)
        # Goal text is unchanged — no guard annotation when payload was
        # consistent.
        assert "splitter-guard" not in result[0].goal
        assert "splitter-guard" not in result[1].goal

    def test_fallback_to_parent_scope_accepts_any_in_scope_path(self) -> None:
        original = _parent(("src/**",))
        response = _split_payload([
            {"id": "a", "goal": "Refactor the helpers.", "files": []},
            {"id": "b", "goal": "Refactor the services.", "files": []},
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        # After fallback, any path under src/** must be accepted.
        _validate_path("src/anything/under/here.py", result[0].scope.allowed_globs, ())
        # And a path outside parent scope must still be rejected.
        with pytest.raises(PatchApplicationError):
            _validate_path("outside/of/scope.py", result[0].scope.allowed_globs, ())


# ---------------------------------------------------------------------------
# Back-compat: existing splitter tests stay green
# ---------------------------------------------------------------------------


class TestPreExistingPathsUnchanged:
    def test_no_split_response_returns_none(self) -> None:
        original = _parent()
        assert _parse_split_response('{"split": false}', original) is None

    def test_single_sub_ticket_returns_none(self) -> None:
        original = _parent()
        response = _split_payload([{"id": "a", "goal": "Write src/a.py.", "files": ["src/a.py"]}])
        assert _parse_split_response(response, original) is None

    def test_invalid_json_returns_none(self) -> None:
        original = _parent()
        assert _parse_split_response("definitely not json", original) is None

    def test_sub_ticket_dependencies_preserved(self) -> None:
        original = _parent()
        response = _split_payload([
            {"id": "a", "goal": "Write src/a.py.", "files": ["src/a.py"]},
            {"id": "b", "goal": "Write src/b.py.", "files": ["src/b.py"], "depends_on": ["a"]},
        ])
        result = _parse_split_response(response, original)
        assert result is not None
        assert result[0].ticket_id == "T100.a"
        assert result[1].ticket_id == "T100.b"
        assert "T100.a" in result[1].dependencies
