"""Shared fixtures for release test suite."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_capability_registry():
    """Clear the capability registry before and after each test.

    Prevents premium handler registration from leaking between tests when
    saturnday-premium is installed in the development environment.
    """
    from saturnday import capability_registry

    capability_registry.clear()
    yield
    capability_registry.clear()
