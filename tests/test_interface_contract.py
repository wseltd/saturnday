"""Tests for the interface-contract plan checks (IC-001, IC-002)."""

from __future__ import annotations

import json

import pytest

from saturnday.interface_contract import (
    check_duplicate_provides,
    check_interface_contract,
    check_verify_cmd_tools,
    collect_declared_tools,
    extract_executables,
)


def _plan(tickets: list[dict]) -> dict:
    return {"tickets": tickets}


# ---------------------------------------------------------------------------
# IC-001: duplicate provides
# ---------------------------------------------------------------------------

class TestDuplicateProvides:
    def test_two_tickets_same_file_is_error(self):
        plan = _plan([
            {"ticket_id": "T031", "provides": {"files": ["tests/loader.ts"]}},
            {"ticket_id": "T049", "provides": {"files": ["tests/loader.ts"]}},
        ])
        findings = check_duplicate_provides(plan)
        assert len(findings) == 1
        assert findings[0].rule_id == "IC-001"
        assert findings[0].severity == "error"
        assert findings[0].ticket_ids == ["T031", "T049"]

    def test_same_export_different_signatures_still_collides(self):
        """Ownership compares the symbol, not the full signature string."""
        plan = _plan([
            {"ticket_id": "T031", "provides": {
                "exports": ["defineFixture(f: Shape): Shape"]}},
            {"ticket_id": "T048", "provides": {
                "exports": ["defineFixture(fixture: Shape): Shape"]}},
        ])
        findings = check_duplicate_provides(plan)
        assert len(findings) == 1
        assert "defineFixture" in findings[0].detail

    def test_three_way_collision_reported_once(self):
        plan = _plan([
            {"ticket_id": "T031", "provides": {"exports": ["Fixture {a}"]}},
            {"ticket_id": "T048", "provides": {"exports": ["Fixture {b}"]}},
            {"ticket_id": "T049", "provides": {"exports": ["Fixture {c}"]}},
        ])
        findings = check_duplicate_provides(plan)
        assert len(findings) == 1
        assert findings[0].ticket_ids == ["T031", "T048", "T049"]

    def test_path_normalisation_detects_collision(self):
        plan = _plan([
            {"ticket_id": "T1", "provides": {"files": ["./src/a.ts"]}},
            {"ticket_id": "T2", "provides": {"files": ["src/a.ts"]}},
        ])
        assert len(check_duplicate_provides(plan)) == 1

    def test_distinct_owners_produce_nothing(self):
        plan = _plan([
            {"ticket_id": "T1", "provides": {"files": ["a.ts"], "exports": ["foo()"]}},
            {"ticket_id": "T2", "provides": {"files": ["b.ts"], "exports": ["bar()"]}},
        ])
        assert check_duplicate_provides(plan) == []

    def test_plan_without_provides_produces_nothing(self):
        """Backward compatibility: pre-existing plans must be unaffected."""
        plan = _plan([
            {"ticket_id": "T1", "goal": "do a thing", "verify_cmd": "echo ok"},
            {"ticket_id": "T2", "goal": "do another", "verify_cmd": "echo ok"},
        ])
        assert check_duplicate_provides(plan) == []

    @pytest.mark.parametrize("bad", [None, "string", 42, []])
    def test_malformed_provides_does_not_crash(self, bad):
        plan = _plan([{"ticket_id": "T1", "provides": bad}])
        assert check_duplicate_provides(plan) == []

    def test_malformed_plan_does_not_crash(self):
        assert check_duplicate_provides({}) == []
        assert check_duplicate_provides({"tickets": "nope"}) == []
        assert check_duplicate_provides({"tickets": [None, 7]}) == []

    def test_empty_and_blank_entries_ignored(self):
        plan = _plan([
            {"ticket_id": "T1", "provides": {"files": ["", "  "], "exports": [""]}},
            {"ticket_id": "T2", "provides": {"files": ["", "  "], "exports": [""]}},
        ])
        assert check_duplicate_provides(plan) == []


# ---------------------------------------------------------------------------
# extract_executables
# ---------------------------------------------------------------------------

class TestExtractExecutables:
    def test_simple_command(self):
        assert extract_executables("pytest tests/test_a.py") == ["pytest"]

    def test_env_assignment_is_skipped(self):
        assert extract_executables("CI=1 FOO=bar pytest tests") == ["pytest"]

    def test_shell_operators_split(self):
        assert extract_executables("npm run build && node dist/x.js") == ["npm", "node"]

    def test_pipe_splits(self):
        assert extract_executables("cat a.json | jq .") == ["cat", "jq"]

    def test_runner_resolves_to_real_tool(self):
        assert extract_executables("pnpm exec tsx tests/gen.ts") == ["tsx"]
        assert extract_executables("npx --yes vitest run") == ["vitest"]
        assert extract_executables("uv run ruff check .") == ["ruff"]

    def test_path_prefix_stripped(self):
        assert extract_executables("./node_modules/.bin/eslint src") == ["eslint"]

    def test_duplicates_collapsed(self):
        assert extract_executables("echo a && echo b") == ["echo"]

    @pytest.mark.parametrize("cmd", ["", "   ", None])
    def test_empty_is_safe(self, cmd):
        assert extract_executables(cmd) == []


# ---------------------------------------------------------------------------
# IC-002: verify_cmd tool resolution
# ---------------------------------------------------------------------------

class TestVerifyCmdTools:
    def test_undeclared_tool_is_warning(self, tmp_path):
        plan = _plan([{
            "ticket_id": "T052",
            "verify_cmd": "pnpm exec definitelynotarealtool9x --out dir",
        }])
        findings = check_verify_cmd_tools(plan, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule_id == "IC-002"
        assert findings[0].severity == "warning"
        assert findings[0].ticket_ids == ["T052"]
        assert "definitelynotarealtool9x" in findings[0].detail

    def test_baseline_tool_is_not_reported(self, tmp_path):
        plan = _plan([{"ticket_id": "T1", "verify_cmd": "python -m pytest tests"}])
        assert check_verify_cmd_tools(plan, tmp_path) == []

    def test_tool_declared_in_package_json_is_accepted(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"definitelynotarealtool9x": "^1.0.0"}}),
            encoding="utf-8",
        )
        plan = _plan([{
            "ticket_id": "T1",
            "verify_cmd": "pnpm exec definitelynotarealtool9x run",
        }])
        assert check_verify_cmd_tools(plan, tmp_path) == []

    def test_scoped_npm_package_basename_accepted(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"@acme/zzztool": "^1.0.0"}}),
            encoding="utf-8",
        )
        plan = _plan([{"ticket_id": "T1", "verify_cmd": "npx zzztool build"}])
        assert check_verify_cmd_tools(plan, tmp_path) == []

    def test_tool_declared_in_pyproject_is_accepted(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["zzzlint>=1.0"]\n', encoding="utf-8"
        )
        plan = _plan([{"ticket_id": "T1", "verify_cmd": "zzzlint check ."}])
        assert check_verify_cmd_tools(plan, tmp_path) == []

    def test_missing_verify_cmd_is_safe(self, tmp_path):
        plan = _plan([
            {"ticket_id": "T1"},
            {"ticket_id": "T2", "verify_cmd": ""},
            {"ticket_id": "T3", "verify_cmd": None},
        ])
        assert check_verify_cmd_tools(plan, tmp_path) == []

    def test_no_repo_path_still_works(self):
        plan = _plan([{"ticket_id": "T1", "verify_cmd": "echo ok"}])
        assert check_verify_cmd_tools(plan, None) == []

    def test_missing_manifests_yield_no_declared_tools(self, tmp_path):
        assert collect_declared_tools(tmp_path) == set()

    def test_corrupt_package_json_does_not_crash(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
        assert collect_declared_tools(tmp_path) == set()

    def test_nonexistent_repo_path_is_safe(self, tmp_path):
        assert collect_declared_tools(tmp_path / "nope") == set()


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

class TestCheckInterfaceContract:
    def test_returns_errors_before_warnings(self, tmp_path):
        plan = _plan([
            {"ticket_id": "T1", "provides": {"files": ["a.ts"]},
             "verify_cmd": "definitelynotarealtool9x go"},
            {"ticket_id": "T2", "provides": {"files": ["a.ts"]}},
        ])
        findings = check_interface_contract(plan, tmp_path)
        assert [f.rule_id for f in findings] == ["IC-001", "IC-002"]

    def test_clean_plan_yields_nothing(self, tmp_path):
        plan = _plan([{"ticket_id": "T1", "verify_cmd": "echo ok"}])
        assert check_interface_contract(plan, tmp_path) == []

    def test_format_line_includes_rule_and_tickets(self, tmp_path):
        plan = _plan([
            {"ticket_id": "T1", "provides": {"files": ["a.ts"]}},
            {"ticket_id": "T2", "provides": {"files": ["a.ts"]}},
        ])
        line = check_interface_contract(plan, tmp_path)[0].format_line()
        assert line.startswith("[IC-001] T1, T2:")


# ---------------------------------------------------------------------------
# Regression: existing plan loading must be untouched
# ---------------------------------------------------------------------------

class TestNoRegressionOnLoadPlan:
    def test_validate_plan_ignores_provides_collisions(self):
        """IC-001 must NOT leak into validate_plan, which load_plan raises on."""
        from saturnday.plan_parser import validate_plan

        raw = {
            "project_id": "p",
            "tickets": [
                {"ticket_id": "T1", "goal": "g1", "verify_cmd": "echo ok",
                 "provides": {"files": ["a.ts"]}},
                {"ticket_id": "T2", "goal": "g2", "verify_cmd": "echo ok",
                 "provides": {"files": ["a.ts"]}},
            ],
        }
        errors = validate_plan(raw)
        assert not any("IC-001" in e or "provides" in e for e in errors)

    def test_unknown_provides_key_does_not_break_parsing(self):
        """Additive keys must survive _parse_tickets untouched."""
        from saturnday.plan_parser import _parse_tickets

        tickets = _parse_tickets([
            {"ticket_id": "T1", "goal": "g", "provides": {"files": ["a.ts"]},
             "consumes": ["T0.foo"], "requires_tools": ["tsx"]},
        ])
        assert len(tickets) == 1
        assert tickets[0].ticket_id == "T1"
