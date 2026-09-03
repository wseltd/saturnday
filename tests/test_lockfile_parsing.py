"""Tests for lockfile parsing (_parse_lockfile_packages)."""

import json
import tempfile
from pathlib import Path

import pytest

from saturnday.review_ts import _parse_lockfile_packages


class TestPackageLockV2:
    def test_v2_packages_key(self):
        """package-lock.json v2 uses packages with node_modules/ prefix."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "package-lock.json"
            lock.write_text(json.dumps({
                "lockfileVersion": 2,
                "packages": {
                    "": {"name": "my-app"},
                    "node_modules/express": {"version": "4.18.0"},
                    "node_modules/lodash": {"version": "4.17.21"},
                    "node_modules/@nestjs/core": {"version": "10.0.0"},
                },
            }))
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert "express" in pkgs
            assert "lodash" in pkgs
            assert "@nestjs/core" in pkgs

    def test_v2_nested_deps(self):
        """Nested node_modules should resolve to the leaf package name."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "package-lock.json"
            lock.write_text(json.dumps({
                "lockfileVersion": 2,
                "packages": {
                    "node_modules/express/node_modules/debug": {"version": "2.6.9"},
                },
            }))
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert "debug" in pkgs


class TestPackageLockV1:
    def test_v1_dependencies_key(self):
        """package-lock.json v1 uses top-level dependencies dict."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "package-lock.json"
            lock.write_text(json.dumps({
                "lockfileVersion": 1,
                "dependencies": {
                    "express": {"version": "4.18.0"},
                    "lodash": {"version": "4.17.21"},
                },
            }))
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert "express" in pkgs
            assert "lodash" in pkgs


class TestYarnLock:
    def test_yarn_v1_format(self):
        """yarn.lock v1 uses quoted package@version format."""
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "yarn.lock"
            lock.write_text('''\
# yarn lockfile v1

"express@^4.18.0":
  version "4.18.2"
  resolved "https://registry.yarnpkg.com/express/-/express-4.18.2.tgz"

"lodash@^4.17.0":
  version "4.17.21"
''')
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert "express" in pkgs
            assert "lodash" in pkgs

    def test_yarn_scoped_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "yarn.lock"
            lock.write_text('''\
"@nestjs/core@^10.0.0":
  version "10.0.0"
''')
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert "@nestjs/core" in pkgs


class TestNoLockfile:
    def test_no_lockfile_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert pkgs == frozenset()


class TestPackageLockPreferredOverYarn:
    def test_package_lock_takes_priority(self):
        """When both lockfiles exist, package-lock.json should be used."""
        with tempfile.TemporaryDirectory() as tmp:
            # package-lock.json with express
            lock = Path(tmp) / "package-lock.json"
            lock.write_text(json.dumps({
                "lockfileVersion": 2,
                "packages": {
                    "node_modules/express": {"version": "4.18.0"},
                },
            }))
            # yarn.lock with lodash (different package to distinguish)
            yarn = Path(tmp) / "yarn.lock"
            yarn.write_text('"lodash@^4.17.0":\n  version "4.17.21"\n')

            pkgs = _parse_lockfile_packages(Path(tmp))
            assert "express" in pkgs
            # yarn.lock should NOT be parsed if package-lock.json exists
            assert "lodash" not in pkgs


class TestMalformedLockfile:
    def test_invalid_json_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "package-lock.json"
            lock.write_text("not valid json")
            # Should not crash, just return what it can
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert isinstance(pkgs, frozenset)

    def test_empty_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "package-lock.json"
            lock.write_text("{}")
            pkgs = _parse_lockfile_packages(Path(tmp))
            assert pkgs == frozenset()
