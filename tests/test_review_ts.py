"""Tests for TypeScript/JavaScript review checks."""

import tempfile
from pathlib import Path
from unittest.mock import patch

_GH_TOKEN = "ghp_" + "0" * 36


import pytest

from saturnday.npm_registry import NpmPackageInfo
from saturnday.review_ts import (
    _extract_package_name,
    check_fake_tests_ts,
    check_hallucinated_imports_ts,
    check_placeholders_ts,
    check_prompt_injection_ts,
    check_secrets_ts,
    check_syntax_ts,
    check_typosquat_ts,
    run_all_ts_checks,
    run_passive_ts_checks,
    NODE_BUILTINS,
)


def _mock_registry_404(name: str, **kwargs) -> NpmPackageInfo:
    """Mock registry response: package does not exist."""
    return NpmPackageInfo(name=name, exists=False)


def _mock_registry_exists(name: str, **kwargs) -> NpmPackageInfo:
    """Mock registry response: package exists."""
    return NpmPackageInfo(name=name, exists=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill(tmp: str, files: dict[str, str]) -> Path:
    """Create a temporary skill directory with given files."""
    repo = Path(tmp)
    for name, content in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return repo


# ---------------------------------------------------------------------------
# _extract_package_name
# ---------------------------------------------------------------------------

class TestExtractPackageName:
    def test_relative_import(self):
        assert _extract_package_name("./foo") is None
        assert _extract_package_name("../bar") is None
        assert _extract_package_name("/absolute/path") is None

    def test_simple_package(self):
        assert _extract_package_name("express") == "express"
        assert _extract_package_name("lodash") == "lodash"

    def test_subpath(self):
        assert _extract_package_name("lodash/fp") == "lodash"
        assert _extract_package_name("react-dom/client") == "react-dom"

    def test_scoped_package(self):
        assert _extract_package_name("@scope/name") == "@scope/name"
        assert _extract_package_name("@scope/name/sub") == "@scope/name"

    def test_node_prefix(self):
        assert _extract_package_name("node:fs") == "fs"
        assert _extract_package_name("node:path") == "path"

    def test_bun_prefix(self):
        assert _extract_package_name("bun:test") == "test"
        assert _extract_package_name("bun:sqlite") == "sqlite"

    def test_path_alias_at_slash(self):
        """@/ imports are path aliases, not packages."""
        assert _extract_package_name("@/lib/utils") is None
        assert _extract_package_name("@/components/Header") is None
        assert _extract_package_name("@/types") is None

    def test_path_alias_tilde(self):
        """~/ imports are path aliases, not packages."""
        assert _extract_package_name("~/utils") is None
        assert _extract_package_name("~/lib/db") is None

    def test_empty(self):
        assert _extract_package_name("") is None


# ---------------------------------------------------------------------------
# secrets_ts
# ---------------------------------------------------------------------------

class TestSecrets:
    def test_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            result = check_secrets_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert result["findings"] == []
            assert result["name"] == "secrets_ts"

    def test_api_key_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "config.ts": 'const API_KEY = "sk-abcdefghijklmnopqrstuvwxyz1234567890";\n'
            })
            result = check_secrets_ts(repo, ["config.ts"])
            assert result["status"] == "FAIL"
            assert len(result["findings"]) >= 1
            assert any(f["kind"] in ("api_key", "openai_key", "generic_secret") for f in result["findings"])

    def test_github_token_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "deploy.js": f'const token = "{_GH_TOKEN}";\n'
            })
            result = check_secrets_ts(repo, ["deploy.js"])
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "github_token" for f in result["findings"])

    def test_env_file_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                ".env": 'API_KEY="sk-abcdefghijklmnopqrstuvwxyz1234567890"\n'
            })
            result = check_secrets_ts(repo, [".env"])
            assert result["status"] == "FAIL"

    def test_skill_md_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "SKILL.md": f'---\nname: test\n---\ntoken = "{_GH_TOKEN}"\n'
            })
            result = check_secrets_ts(repo, ["SKILL.md"])
            assert result["status"] == "FAIL"

    def test_non_ts_files_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "readme.md": 'api_key = "sk-abcdefghijklmnopqrstuvwxyz1234567890"\n'
            })
            result = check_secrets_ts(repo, ["readme.md"])
            assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# hallucinated_imports_ts
# ---------------------------------------------------------------------------

class TestHallucinatedImports:
    def test_known_package_passes(self):
        """Packages in package.json should not be flagged (locally resolved)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "package.json": '{"dependencies": {"express": "^4.18.0"}}',
                "index.ts": 'import express from "express";\n',
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert result["name"] == "hallucinated_imports_ts"

    def test_unknown_package_fails(self):
        """Package not locally resolved + 404 on npm → hallucinated."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import foo from "nonexistent-fake-pkg-xyz";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert len(result["findings"]) == 1
            f = result["findings"][0]
            assert f["kind"] == "hallucinated_import"
            assert f["package"] == "nonexistent-fake-pkg-xyz"
            assert f["confidence"] == "high"

    def test_unknown_locally_but_exists_on_npm(self):
        """Package not in package.json but exists on npm → PASS (undeclared, not hallucinated)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import express from "express";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_relative_import_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import utils from "./utils";\n'
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_node_builtin_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": (
                    'import fs from "fs";\n'
                    'import path from "path";\n'
                    'import { createServer } from "http";\n'
                    'import crypto from "node:crypto";\n'
                )
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_subpath_resolves_via_registry(self):
        """lodash/fp → base_package=lodash → check registry for lodash."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import fp from "lodash/fp";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_scoped_package_unknown(self):
        """Scoped package that doesn't exist on npm → medium confidence."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import pkg from "@fake-scope/nonexistent";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["confidence"] == "medium"  # scoped packages get medium

    def test_package_json_deps_treated_as_known(self):
        """Packages declared in package.json should be locally resolved."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "package.json": '{"dependencies": {"my-custom-pkg": "^1.0.0"}}',
                "index.ts": 'import pkg from "my-custom-pkg";\n',
            })
            # No registry mock needed — should resolve locally
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_require_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.js": 'const fake = require("nonexistent-fake-pkg-xyz");\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404):
                result = check_hallucinated_imports_ts(repo, ["index.js"])
            assert result["status"] == "FAIL"

    def test_path_alias_not_flagged(self):
        """@/ and ~/ path aliases should not be flagged as hallucinated imports."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": (
                    'import { utils } from "@/lib/utils";\n'
                    'import { db } from "~/lib/db";\n'
                ),
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_bun_prefix_not_flagged(self):
        """bun: imports should be treated like node: built-ins."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import { test } from "bun:test";\n'
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_deduplication(self):
        """Same package imported twice should produce only one finding."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "a.ts": 'import fake from "nonexistent-fake-pkg-xyz";\n',
                "b.ts": 'import fake from "nonexistent-fake-pkg-xyz";\n',
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404):
                result = check_hallucinated_imports_ts(repo, ["a.ts", "b.ts"])
            assert result["status"] == "FAIL"
            assert len(result["findings"]) == 1  # deduplicated

    def test_url_import_not_flagged(self):
        """https: imports should be classified as URL and skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import React from "https://esm.sh/react";\n'
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_lockfile_resolution(self):
        """Packages in lockfile should be locally resolved."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "package-lock.json": _json.dumps({
                    "lockfileVersion": 2,
                    "packages": {
                        "node_modules/obscure-pkg": {"version": "1.0.0"},
                    },
                }),
                "index.ts": 'import pkg from "obscure-pkg";\n',
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_tsconfig_alias_not_flagged(self):
        """tsconfig.json path aliases should not be flagged."""
        import json as _json
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "tsconfig.json": _json.dumps({
                    "compilerOptions": {
                        "paths": {"#/*": ["./src/*"]}
                    }
                }),
                "index.ts": 'import { foo } from "#/utils";\n',
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_template_literal_not_flagged(self):
        """require('${variable}') should be skipped — template literal, not a package."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": "const mod = require('${variable}');\n",
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0

    def test_template_literal_path_not_flagged(self):
        """require('${ovaPath}') should be skipped — dynamic path interpolation."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": "const ova = require('${ovaPath}');\n",
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0

    def test_uppercase_variable_not_flagged(self):
        """require('MASTER_ENCRYPTION_PASSWORD') should be skipped — variable name pattern."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": "const crypto = require('MASTER_ENCRYPTION_PASSWORD');\n",
            })
            result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0

    def test_real_package_still_flagged(self):
        """Regression guard: real-looking package that doesn't exist should still be flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import foo from "nonexistent-fake-pkg";\n',
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert len(result["findings"]) == 1
            assert result["findings"][0]["package"] == "nonexistent-fake-pkg"

    def test_scoped_package_not_filtered(self):
        """@scope/package should NOT be caught by the uppercase filter (has '/')."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import sdk from "@openclaw/sdk";\n',
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert len(result["findings"]) == 1

    def test_lowercase_with_hyphens_not_filtered(self):
        """Normal lowercase-with-hyphens package should proceed through pipeline normally."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import pkg from "some-real-package";\n',
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists):
                result = check_hallucinated_imports_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# typosquat_ts
# ---------------------------------------------------------------------------

def _mock_downloads(name: str, **kwargs) -> int:
    """Mock download counts: express gets 30M, unknown gets 50."""
    known = {
        "express": 30_000_000,
        "react": 25_000_000,
        "lodash": 50_000_000,
        "axios": 45_000_000,
    }
    return known.get(name, 50)


def _mock_downloads_unavailable(name: str, **kwargs) -> int:
    """Mock download counts: all unavailable."""
    return -1


def _mock_downloads_popular(name: str, **kwargs) -> int:
    """Mock download counts: everything is popular (>1000)."""
    return 5_000_000


class TestTyposquat:
    def test_known_package_passes(self):
        """A top npm package should not be flagged as typosquat."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import express from "express";\n'
            })
            result = check_typosquat_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert result["name"] == "typosquat_ts"

    def test_near_match_flagged_when_low_downloads(self):
        """Package close to popular package, exists, low downloads → typosquat."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import exp from "expres";\n'  # edit distance 1 from express
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads):
                result = check_typosquat_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["kind"] == "typosquat"
            assert f["similar_to"] == "express"
            assert f["distance"] <= 2
            assert f["risk_score"] >= 0.5
            assert f["candidate_downloads"] < 1000
            assert f["target_downloads"] > 100_000

    def test_popular_package_never_flagged(self):
        """Package with >1000 downloads should NEVER be flagged even if close to popular."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import exp from "expres";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads_popular):
                result = check_typosquat_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_near_match_not_flagged_when_not_exists(self):
        """Package close to a popular package but doesn't exist → not typosquat."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import exp from "expres";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_404), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads):
                result = check_typosquat_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_downloads_unavailable_not_flagged(self):
        """When download data is unavailable, NEVER flag (no false positives)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import exp from "expres";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads_unavailable):
                result = check_typosquat_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_short_names_not_checked(self):
        """Packages with names < 3 chars should not trigger typosquat."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import x from "xy";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads):
                result = check_typosquat_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_declared_in_package_json_lower_score(self):
        """Declared packages get lower risk score (no undeclared penalty)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "package.json": '{"dependencies": {"expres": "^1.0.0"}}',
                "index.ts": 'import exp from "expres";\n',
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads):
                result = check_typosquat_ts(repo, ["index.ts"])
            # Score is 0.6 (dist 1) + 0.0 (declared) = 0.6, still >= 0.5
            if result["status"] == "FAIL":
                assert result["findings"][0]["undeclared"] is False

    def test_finding_includes_download_counts(self):
        """Findings should include candidate_downloads and target_downloads."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'import exp from "expres";\n'
            })
            with patch("saturnday.review_ts.npm_registry.check_package_exists", side_effect=_mock_registry_exists), \
                 patch("saturnday.review_ts.npm_registry.get_weekly_downloads", side_effect=_mock_downloads):
                result = check_typosquat_ts(repo, ["index.ts"])
            if result["findings"]:
                f = result["findings"][0]
                assert "risk_score" in f
                assert "undeclared" in f
                assert "candidate_downloads" in f
                assert "target_downloads" in f


# ---------------------------------------------------------------------------
# fake_tests_ts
# ---------------------------------------------------------------------------

class TestFakeTests:
    def test_no_tests_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            result = check_fake_tests_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert result["name"] == "fake_tests_ts"

    def test_empty_test_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "app.test.ts": 'test("should work", () => {})\n'
            })
            result = check_fake_tests_ts(repo, ["app.test.ts"])
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "empty_test" for f in result["findings"])

    def test_tautological_assertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "app.test.ts": 'test("taut", () => { expect(true).toBe(true) })\n'
            })
            result = check_fake_tests_ts(repo, ["app.test.ts"])
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "tautological_assertion" for f in result["findings"])

    def test_skipped_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "app.test.ts": 'test.skip("todo", () => { expect(1).toBe(1) })\n'
            })
            result = check_fake_tests_ts(repo, ["app.test.ts"])
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "skipped_test" for f in result["findings"])

    def test_empty_describe(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "app.test.ts": 'describe("suite", () => {})\n'
            })
            result = check_fake_tests_ts(repo, ["app.test.ts"])
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "empty_describe" for f in result["findings"])

    def test_non_test_file_skipped(self):
        """Non-test files should not be checked for fake tests."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'test("should work", () => {})\n'
            })
            result = check_fake_tests_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_real_test_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "app.test.ts": (
                    'test("adds numbers", () => {\n'
                    '  expect(add(1, 2)).toBe(3);\n'
                    '});\n'
                )
            })
            result = check_fake_tests_ts(repo, ["app.test.ts"])
            assert result["status"] == "PASS"


# ---------------------------------------------------------------------------
# prompt_injection_ts
# ---------------------------------------------------------------------------

class TestPromptInjection:
    def test_no_injection_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": "const x = 1;\n"
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert result["name"] == "prompt_injection_ts"

    def test_template_literal_with_prompt_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'const system_prompt = `You are ${role}`;\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["kind"] == "template_injection"
            assert f["confidence"] == "high"

    def test_string_concat_near_prompt_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'const prompt = "You are " + user_input;\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            f = result["findings"][0]
            assert f["kind"] == "concat_injection"
            assert f["confidence"] == "medium"

    def test_llm_api_with_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'fetch("https://api.openai.com/v1/chat/completions", { body: `${data}` });\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert any(f["kind"] == "api_template_injection" for f in result["findings"])

    def test_skill_md_external_url_not_flagged(self):
        """external_prompt_url heuristic was removed (0% precision in calibration).
        SKILL.md URLs should no longer produce findings."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "SKILL.md": '---\nname: test\nprompt_url: https://evil.com/prompt.txt\n---\nSome content\n',
                "index.ts": "const x = 1;\n",
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert len(result["findings"]) == 0

    def test_console_log_not_flagged(self):
        """Console logging with template literals near prompt keywords should not trigger."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'console.log(`system_prompt: ${prompt}`);\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_assert_not_flagged(self):
        """Test assertions with template literals should not trigger."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'expect(`system_prompt is ${val}`).toBe(true);\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_no_false_positive_on_normal_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'const greeting = `Hello ${name}`;\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"

    def test_generic_words_not_flagged(self):
        """Generic words like 'messages', 'role', 'system' should not trigger.
        Only LLM-specific compound terms like 'system_prompt' should match."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": (
                    'const messages = `Total: ${count} messages`;\n'
                    'const role = `User role: ${user.role}`;\n'
                    'const system = `System: ${os.type()}`;\n'
                ),
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            assert result["status"] == "PASS", (
                f"Generic words should not trigger: {result['findings']}"
            )

    def test_report_wording(self):
        """Findings should use honest heuristic language, never 'vulnerable to'."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'const system_prompt = `You are ${role}`;\n'
            })
            result = check_prompt_injection_ts(repo, ["index.ts"])
            for f in result["findings"]:
                detail = f["detail"].lower()
                assert "vulnerable" not in detail
                assert "confirmed exploit" not in detail
                assert "suspicious" in detail or "heuristic" in detail


# ---------------------------------------------------------------------------
# placeholders_ts
# ---------------------------------------------------------------------------

class TestPlaceholders:
    def test_no_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            result = check_placeholders_ts(repo, ["index.ts"])
            assert result["status"] == "PASS"
            assert result["name"] == "placeholders_ts"

    def test_todo_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": "// TODO: implement this\nconst x = 1;\n"
            })
            result = check_placeholders_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert result["findings"][0]["kind"] == "todo_comment"

    def test_fixme_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": "// FIXME: broken\nconst x = 1;\n"
            })
            result = check_placeholders_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"

    def test_not_implemented_throw(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'throw new Error("Not implemented");\n'
            })
            result = check_placeholders_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert result["findings"][0]["kind"] == "not_implemented"

    def test_console_todo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {
                "index.ts": 'console.log("TODO: fix this");\n'
            })
            result = check_placeholders_ts(repo, ["index.ts"])
            assert result["status"] == "FAIL"
            assert result["findings"][0]["kind"] == "console_todo"


# ---------------------------------------------------------------------------
# syntax_ts
# ---------------------------------------------------------------------------

class TestSyntax:
    def test_skipped_when_node_absent(self):
        """When Node is not found, result should be SKIPPED, not missing."""
        import shutil
        if shutil.which("node"):
            pytest.skip("Node is available, cannot test absence")
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            result = check_syntax_ts(repo, ["index.ts"])
            assert result["status"] == "SKIPPED"
            assert result["error"] == "node_not_found"
            assert result["findings"] == []
            assert result["exit_code"] == 0
            assert result["name"] == "syntax_ts"

    def test_result_always_has_full_fields(self):
        """Regardless of outcome, the result must have all standard fields."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            result = check_syntax_ts(repo, ["index.ts"])
            assert "name" in result
            assert "status" in result
            assert "findings" in result
            assert "exit_code" in result
            assert "error" in result
            assert "raw_output" in result


# ---------------------------------------------------------------------------
# run_all / run_passive
# ---------------------------------------------------------------------------

class TestRunAll:
    def test_run_all_returns_all_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            results = run_all_ts_checks(repo, ["index.ts"])
            assert len(results) == 27  # 7 original + 19 security governance + 1 type_check_ts
            names = {r["name"] for r in results}
            # Original 7
            assert "secrets_ts" in names
            assert "hallucinated_imports_ts" in names
            assert "typosquat_ts" in names
            assert "fake_tests_ts" in names
            assert "prompt_injection_ts" in names
            assert "placeholders_ts" in names
            assert "syntax_ts" in names
            # Security governance (spot check)
            assert "hardcoded_jwt_ts" in names
            assert "websocket_auth_ts" in names
            assert "xss_check_ts" in names
            assert "security_event_logging_ts" in names

    def test_run_passive_returns_6_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            results = run_passive_ts_checks(repo, ["index.ts"])
            assert len(results) == 6
            names = {r["name"] for r in results}
            assert "syntax_ts" not in names  # syntax is not passive

    def test_all_results_have_standard_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"index.ts": "const x = 1;\n"})
            results = run_all_ts_checks(repo, ["index.ts"])
            for r in results:
                assert "name" in r
                assert "status" in r
                assert "findings" in r
                assert "exit_code" in r
                assert "error" in r


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_non_ts_files_skipped(self):
        """Non-TS/JS files should be skipped by all checks."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_skill(tmp, {"readme.md": "# Hello\n"})
            for check in [
                check_secrets_ts,
                check_hallucinated_imports_ts,
                check_typosquat_ts,
                check_fake_tests_ts,
                check_prompt_injection_ts,
                check_placeholders_ts,
            ]:
                result = check(repo, ["readme.md"])
                assert result["status"] == "PASS", f"{check.__name__} failed on non-TS file"

    def test_missing_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            result = check_secrets_ts(repo, ["ghost.ts"])
            assert result["status"] == "PASS"

    def test_large_file_skipped(self):
        """Files > 1MB should be skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            large = repo / "big.ts"
            large.write_text("// " + "x" * 1_100_000 + "\n")
            result = check_secrets_ts(repo, ["big.ts"])
            assert result["status"] == "PASS"  # File too large, skipped

    def test_node_builtins_complete(self):
        """Verify key Node built-ins are in the set."""
        for mod in ["fs", "path", "http", "crypto", "child_process", "os", "url", "stream"]:
            assert mod in NODE_BUILTINS
