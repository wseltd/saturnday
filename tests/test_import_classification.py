"""Tests for import classification (Stage 1 of hallucinated imports pipeline)."""

import json
import tempfile
from pathlib import Path

import pytest

from saturnday.review_ts import (
    ClassifiedImport,
    _classify_import,
    _load_tsconfig_paths,
    _resolve_locally,
)


class TestClassifyImport:
    def test_relative_import(self):
        imp = _classify_import("./utils", "index.ts", 1)
        assert imp.import_type == "relative"
        assert imp.base_package is None

    def test_relative_parent(self):
        imp = _classify_import("../models/user", "src/handler.ts", 5)
        assert imp.import_type == "relative"

    def test_absolute_path(self):
        imp = _classify_import("/absolute/path", "index.ts", 1)
        assert imp.import_type == "relative"

    def test_node_builtin(self):
        imp = _classify_import("fs", "index.ts", 1)
        assert imp.import_type == "builtin"
        assert imp.base_package == "fs"

    def test_node_prefixed_builtin(self):
        imp = _classify_import("node:crypto", "index.ts", 1)
        assert imp.import_type == "builtin"
        assert imp.base_package == "crypto"

    def test_bun_prefixed(self):
        imp = _classify_import("bun:test", "index.ts", 1)
        assert imp.import_type == "builtin"
        assert imp.base_package == "test"

    def test_at_slash_alias(self):
        imp = _classify_import("@/lib/utils", "index.ts", 1)
        assert imp.import_type == "alias"
        assert imp.base_package is None

    def test_tilde_alias(self):
        imp = _classify_import("~/utils", "index.ts", 1)
        assert imp.import_type == "alias"
        assert imp.base_package is None

    def test_url_import_https(self):
        imp = _classify_import("https://esm.sh/react", "index.ts", 1)
        assert imp.import_type == "url"
        assert imp.base_package is None

    def test_url_import_http(self):
        imp = _classify_import("http://cdn.example.com/lib.js", "index.ts", 1)
        assert imp.import_type == "url"

    def test_data_url(self):
        imp = _classify_import("data:text/javascript,export default 42", "index.ts", 1)
        assert imp.import_type == "url"

    def test_scoped_package(self):
        imp = _classify_import("@nestjs/core", "index.ts", 1)
        assert imp.import_type == "scoped"
        assert imp.base_package == "@nestjs/core"

    def test_scoped_with_subpath(self):
        imp = _classify_import("@nestjs/core/decorators", "index.ts", 1)
        assert imp.import_type == "scoped"
        assert imp.base_package == "@nestjs/core"

    def test_subpath_import(self):
        imp = _classify_import("lodash/fp", "index.ts", 1)
        assert imp.import_type == "subpath"
        assert imp.base_package == "lodash"

    def test_subpath_react_dom(self):
        imp = _classify_import("react-dom/client", "index.ts", 1)
        assert imp.import_type == "subpath"
        assert imp.base_package == "react-dom"

    def test_external_package(self):
        imp = _classify_import("express", "index.ts", 1)
        assert imp.import_type == "external"
        assert imp.base_package == "express"

    def test_tsconfig_path_alias(self):
        aliases = {"#/", "lib/"}
        imp = _classify_import("#/utils", "index.ts", 1, tsconfig_aliases=aliases)
        assert imp.import_type == "alias"
        assert imp.base_package is None

    def test_tsconfig_alias_exact_match(self):
        """Alias without trailing / should match exact import."""
        aliases = {"components/"}
        imp = _classify_import("components", "index.ts", 1, tsconfig_aliases=aliases)
        assert imp.import_type == "alias"

    def test_empty_specifier(self):
        imp = _classify_import("", "index.ts", 1)
        assert imp.import_type == "external"
        assert imp.base_package is None


class TestLoadTsconfigPaths:
    def test_no_tsconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert _load_tsconfig_paths(Path(tmp)) == set()

    def test_basic_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsconfig = Path(tmp) / "tsconfig.json"
            tsconfig.write_text(json.dumps({
                "compilerOptions": {
                    "paths": {
                        "@/*": ["./src/*"],
                        "~/": ["./lib/"],
                    }
                }
            }))
            aliases = _load_tsconfig_paths(Path(tmp))
            assert "@/" in aliases
            assert "~/" in aliases

    def test_jsonc_with_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsconfig = Path(tmp) / "tsconfig.json"
            tsconfig.write_text("""{
                // This is a comment
                "compilerOptions": {
                    /* Block comment */
                    "paths": {
                        "#/*": ["./src/*"]
                    }
                }
            }""")
            aliases = _load_tsconfig_paths(Path(tmp))
            assert "#/" in aliases

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsconfig = Path(tmp) / "tsconfig.json"
            tsconfig.write_text("not json at all")
            assert _load_tsconfig_paths(Path(tmp)) == set()

    def test_no_paths_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsconfig = Path(tmp) / "tsconfig.json"
            tsconfig.write_text('{"compilerOptions": {"strict": true}}')
            assert _load_tsconfig_paths(Path(tmp)) == set()


class TestResolveLocally:
    def _make_imp(self, pkg: str) -> ClassifiedImport:
        return ClassifiedImport("pkg", "index.ts", 1, "external", pkg, resolved=False)

    def test_resolve_from_package_json(self):
        imp = self._make_imp("my-pkg")
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset({"my-pkg"}),
            lockfile_pkgs=frozenset(),
            node_modules_pkgs=frozenset(),
        )
        assert resolved.resolved is True

    def test_resolve_from_lockfile(self):
        imp = self._make_imp("locked-pkg")
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset(),
            lockfile_pkgs=frozenset({"locked-pkg"}),
            node_modules_pkgs=frozenset(),
        )
        assert resolved.resolved is True

    def test_resolve_from_node_modules(self):
        imp = self._make_imp("installed-pkg")
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset(),
            lockfile_pkgs=frozenset(),
            node_modules_pkgs=frozenset({"installed-pkg"}),
        )
        assert resolved.resolved is True

    def test_node_modules_case_insensitive(self):
        imp = self._make_imp("MyPkg")
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset(),
            lockfile_pkgs=frozenset(),
            node_modules_pkgs=frozenset({"mypkg"}),
        )
        assert resolved.resolved is True

    def test_unresolved(self):
        imp = self._make_imp("ghost-pkg")
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset(),
            lockfile_pkgs=frozenset(),
            node_modules_pkgs=frozenset(),
        )
        assert resolved.resolved is False

    def test_already_resolved_skipped(self):
        imp = ClassifiedImport("pkg", "index.ts", 1, "external", "pkg", resolved=True)
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset(),
            lockfile_pkgs=frozenset(),
            node_modules_pkgs=frozenset(),
        )
        assert resolved.resolved is True

    def test_no_base_package_skipped(self):
        imp = ClassifiedImport("./foo", "index.ts", 1, "relative", None)
        resolved = _resolve_locally(
            imp,
            pkg_json_deps=frozenset(),
            lockfile_pkgs=frozenset(),
            node_modules_pkgs=frozenset(),
        )
        assert resolved.resolved is False
