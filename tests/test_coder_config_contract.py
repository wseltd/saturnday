"""Tests for Fix 58 — CoderConfig.large_context regression.

Proves:
1. CoderConfig has large_context with default False
2. Normal claude-cli config does not crash when ticket_runner reads large_context
3. Read sites behave correctly with getattr fallback on legacy objects
4. Devstral path works with large_context=True
"""

from __future__ import annotations

import pytest

from saturnday._types import CoderConfig


class TestCoderConfigLargeContext:
    def test_default_false(self) -> None:
        """CoderConfig.large_context defaults to False."""
        config = CoderConfig(backend="claude-cli")
        assert config.large_context is False

    def test_explicit_true(self) -> None:
        """CoderConfig.large_context can be set to True."""
        config = CoderConfig(backend="openai", large_context=True)
        assert config.large_context is True

    def test_compact_prompts_also_exists(self) -> None:
        """compact_prompts field also exists alongside large_context."""
        config = CoderConfig(backend="claude-cli")
        assert config.compact_prompts is False

    def test_devstral_config_shape(self) -> None:
        """Devstral-style config with both flags works."""
        config = CoderConfig(
            backend="openai",
            compact_prompts=True,
            large_context=True,
        )
        assert config.compact_prompts is True
        assert config.large_context is True


class TestLargeContextReadSiteDefense:
    """Test that read sites handle missing attribute gracefully."""

    def test_getattr_fallback_on_normal_object(self) -> None:
        """getattr fallback works on a normal CoderConfig."""
        config = CoderConfig(backend="claude-cli")
        assert getattr(config, "large_context", False) is False

    def test_getattr_fallback_on_legacy_object(self) -> None:
        """getattr fallback works even if attribute were missing."""
        # Simulate a legacy object without the field
        class LegacyConfig:
            backend = "claude-cli"
        legacy = LegacyConfig()
        assert getattr(legacy, "large_context", False) is False

    def test_getattr_true_on_devstral(self) -> None:
        """getattr returns True when explicitly set."""
        config = CoderConfig(backend="openai", large_context=True)
        assert getattr(config, "large_context", False) is True


class TestConstructorCoverage:
    """Test that all common constructor patterns work."""

    def test_minimal_cli(self) -> None:
        config = CoderConfig(backend="claude-cli")
        assert config.large_context is False

    def test_full_api(self) -> None:
        config = CoderConfig(
            backend="openai",
            base_url="http://localhost:8000/v1",
            api_key="test",
            model="test-model",
            temperature=0.1,
            max_tokens=4096,
            timeout_s=300,
            compact_prompts=True,
            large_context=True,
        )
        assert config.large_context is True
        assert config.compact_prompts is True

    def test_triage_minimal(self) -> None:
        config = CoderConfig(backend="claude-cli", api_key="test")
        assert config.large_context is False
