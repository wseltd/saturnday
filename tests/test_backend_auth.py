"""Tests for saturnday.shared.backend_auth."""

import os

from saturnday._types import CoderConfig
from saturnday.shared.backend_auth import (
    capability_matrix,
    detect_auth_mode,
    validate_auth,
)


class TestDetectAuthMode:
    def test_cli_local(self) -> None:
        config = CoderConfig(backend="claude-cli")
        mode = detect_auth_mode(config)
        assert mode == "local_interactive"

    def test_api_with_key(self) -> None:
        config = CoderConfig(backend="openai", api_key="sk-test")
        mode = detect_auth_mode(config)
        assert mode == "api_key"

    def test_api_without_key(self) -> None:
        config = CoderConfig(backend="anthropic")
        mode = detect_auth_mode(config)
        assert mode == "unsupported"


class TestValidateAuth:
    def test_valid_cli(self) -> None:
        config = CoderConfig(backend="codex-cli")
        result = validate_auth(config)
        assert result.valid is True
        assert result.mode == "local_interactive"

    def test_invalid_api_no_key(self) -> None:
        config = CoderConfig(backend="openai")
        result = validate_auth(config)
        assert result.valid is False
        assert "API key" in result.reason

    def test_valid_api_with_key(self) -> None:
        config = CoderConfig(backend="anthropic", api_key="sk-ant-test")
        result = validate_auth(config)
        assert result.valid is True


class TestCapabilityMatrix:
    def test_codex_cli_caps(self) -> None:
        caps = capability_matrix("codex-cli")
        assert caps["local_interactive"] is True
        assert caps["api_key"] is False
        assert caps["file_writes"] is True

    def test_openai_caps(self) -> None:
        caps = capability_matrix("openai")
        assert caps["api_key"] is True
        assert caps["file_writes"] is False
        assert caps["multi_turn"] is True

    def test_unknown_backend(self) -> None:
        caps = capability_matrix("unknown")
        assert caps == {}


class TestOpenClaudeAuth:
    def test_openclaude_detect_auth_mode(self) -> None:
        """openclaude must be treated as a CLI backend and return local_interactive."""
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        mode = detect_auth_mode(config)
        assert mode == "local_interactive"

    def test_openclaude_validate_auth(self) -> None:
        """openclaude must pass auth validation in non-CI environments."""
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        result = validate_auth(config)
        assert result.valid is True
        assert result.mode == "local_interactive"

    def test_openclaude_capability_matrix(self) -> None:
        """capability_matrix must return a non-empty dict for openclaude."""
        caps = capability_matrix("openclaude")
        assert caps, "openclaude must have a non-empty capability entry"
        assert "file_writes" in caps
        assert caps["file_writes"] is True
        assert "local_interactive" in caps
        assert caps["local_interactive"] is True

    def test_openclaude_capability_matrix_keys(self) -> None:
        """openclaude capability entry must contain the same keys as other CLI backends."""
        caps = capability_matrix("openclaude")
        expected_keys = {"local_interactive", "local_subscription", "api_key", "ci", "streaming", "file_writes", "multi_turn"}
        assert expected_keys <= set(caps.keys())
