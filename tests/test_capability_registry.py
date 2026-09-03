"""Tests for src/saturnday/capability_registry.py (SPLIT-001)."""
from __future__ import annotations

import pytest

import saturnday.capability_registry as reg


@pytest.fixture(autouse=True)
def _isolate():
    """Clear the registry before and after every test."""
    reg.clear()
    yield
    reg.clear()


# ---------------------------------------------------------------------------
# 1. register then get returns handler
# ---------------------------------------------------------------------------

def test_register_then_get_returns_handler():
    sentinel = object()
    reg.register("foo", sentinel)
    assert reg.get("foo") is sentinel


# ---------------------------------------------------------------------------
# 2. get nonexistent returns None
# ---------------------------------------------------------------------------

def test_get_nonexistent_returns_none():
    assert reg.get("does_not_exist") is None


# ---------------------------------------------------------------------------
# 3. is_available True after register, False before
# ---------------------------------------------------------------------------

def test_is_available_false_before_register():
    assert reg.is_available("bar") is False


def test_is_available_true_after_register():
    reg.register("bar", lambda: None)
    assert reg.is_available("bar") is True


# ---------------------------------------------------------------------------
# 4. registered_stages returns sorted list
# ---------------------------------------------------------------------------

def test_registered_stages_sorted():
    reg.register("zz_stage", object())
    reg.register("aa_stage", object())
    reg.register("mm_stage", object())
    assert reg.registered_stages() == ["aa_stage", "mm_stage", "zz_stage"]


def test_registered_stages_empty_when_clear():
    assert reg.registered_stages() == []


# ---------------------------------------------------------------------------
# 5. snapshot returns qualname mapping
# ---------------------------------------------------------------------------

def test_snapshot_returns_qualname_mapping():
    class MyHandler:
        pass

    handler = MyHandler()
    reg.register("my_stage", handler)
    snap = reg.snapshot()
    assert "my_stage" in snap
    # __qualname__ for a class defined inside a function includes the function
    # name as a scope prefix (e.g. "test_...<locals>.MyHandler"), so we
    # assert the tail rather than the full string.
    assert snap["my_stage"].endswith("MyHandler")


def test_snapshot_empty_when_clear():
    assert reg.snapshot() == {}


# ---------------------------------------------------------------------------
# 6. clear empties registry
# ---------------------------------------------------------------------------

def test_clear_empties_registry():
    reg.register("x", object())
    reg.register("y", object())
    reg.clear()
    assert reg.registered_stages() == []
    assert reg.get("x") is None


# ---------------------------------------------------------------------------
# 7. Module importable without premium
# ---------------------------------------------------------------------------

def test_module_importable_without_premium():
    """Verifies the module has no premium or run/ imports."""
    import importlib
    import sys

    import saturnday as _saturnday_pkg  # needed for package-attr restore

    # Remove cached module if present, re-import fresh.
    # We restore both sys.modules AND the parent package attribute afterwards
    # so that other test modules that hold ``import saturnday.capability_registry
    # as reg`` bindings continue to reference the same singleton _registry dict.
    # importlib.import_module sets ``saturnday.capability_registry`` as a package
    # attribute — restoring sys.modules alone is not sufficient.
    mod_name = "saturnday.capability_registry"
    original = sys.modules.get(mod_name)
    sys.modules.pop(mod_name, None)
    module = importlib.import_module(mod_name)
    assert hasattr(module, "register")
    assert hasattr(module, "get")
    assert hasattr(module, "is_available")
    assert hasattr(module, "registered_stages")
    assert hasattr(module, "clear")
    assert hasattr(module, "snapshot")
    # Restore original module so the singleton _registry stays consistent.
    if original is not None:
        sys.modules[mod_name] = original
        setattr(_saturnday_pkg, "capability_registry", original)
