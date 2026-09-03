"""Backend adapter auth mode formalisation.

Explicit auth modes for each coder backend, with detection and validation.

Auth modes:
- ``local_interactive``: User present, subscription auth (ChatGPT/Claude.ai login)
- ``local_subscription``: Headless but subscription-authenticated
- ``api_key``: Direct API billing via API key
- ``ci``: API key via CI secrets (GitHub Actions, etc.)
- ``unsupported``: Backend cannot satisfy auth requirement
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from saturnday._types import CoderConfig

logger = logging.getLogger(__name__)

AuthMode = Literal[
    "local_interactive",
    "local_subscription",
    "api_key",
    "ci",
    "unsupported",
]


@dataclass(frozen=True, slots=True)
class AuthValidation:
    """Result of auth mode validation.

    Attributes:
        valid: Whether auth is valid for the backend.
        mode: The detected auth mode.
        reason: Human-readable explanation.
    """

    valid: bool
    mode: AuthMode
    reason: str


def detect_auth_mode(config: CoderConfig) -> AuthMode:
    """Detect the auth mode for the given backend configuration.

    Args:
        config: Coder backend configuration.

    Returns:
        The detected auth mode.
    """
    is_ci = _is_ci_environment()

    if config.backend in ("codex-cli", "claude-cli", "openclaude", "cursor-cli"):
        # CLI backends use subscription/local auth.
        # openclaude manages its own API keys via environment variables —
        # Saturnday does not validate those; the binary handles them.
        if is_ci:
            # CLI in CI is generally unsupported (needs interactive login)
            return "unsupported"
        return "local_interactive"

    if config.backend in ("openai", "anthropic"):
        if config.api_key:
            return "ci" if is_ci else "api_key"
        return "unsupported"

    return "unsupported"


def validate_auth(config: CoderConfig) -> AuthValidation:
    """Validate that the backend has valid auth.

    Args:
        config: Coder backend configuration.

    Returns:
        AuthValidation with validity, mode, and reason.
    """
    mode = detect_auth_mode(config)

    if mode == "unsupported":
        if config.backend in ("codex-cli", "claude-cli", "openclaude", "cursor-cli") and _is_ci_environment():
            return AuthValidation(
                valid=False,
                mode=mode,
                reason=(
                    f"Backend '{config.backend}' requires interactive login "
                    f"and cannot be used in CI. Use an API backend instead."
                ),
            )
        if config.backend in ("openai", "anthropic") and not config.api_key:
            return AuthValidation(
                valid=False,
                mode=mode,
                reason=(
                    f"Backend '{config.backend}' requires an API key. "
                    f"Set --api-key or SATURNDAY_CLOUD_API_KEY."
                ),
            )
        return AuthValidation(
            valid=False,
            mode=mode,
            reason=f"Backend '{config.backend}' auth mode is unsupported.",
        )

    return AuthValidation(
        valid=True,
        mode=mode,
        reason=f"Auth mode '{mode}' is valid for backend '{config.backend}'.",
    )


def capability_matrix(backend: str) -> dict[str, bool]:
    """Return capability flags for a backend.

    Args:
        backend: Backend name.

    Returns:
        Dict of capability name to boolean.
    """
    caps: dict[str, dict[str, bool]] = {
        "codex-cli": {
            "local_interactive": True,
            "local_subscription": True,
            "api_key": False,
            "ci": False,
            "streaming": False,
            "file_writes": True,
            "multi_turn": False,
        },
        "claude-cli": {
            "local_interactive": True,
            "local_subscription": True,
            "api_key": False,
            "ci": False,
            "streaming": True,
            "file_writes": True,
            "multi_turn": False,
        },
        "openclaude": {
            # Claude Code fork with OpenAI-compatible shim.
            # Model quality varies by upstream provider — weaker models may
            # produce lower-quality governance repairs and ticket execution.
            "local_interactive": True,
            "local_subscription": False,
            "api_key": True,
            "ci": False,
            "streaming": True,
            "file_writes": True,
            "multi_turn": False,
        },
        "cursor-cli": {
            "local_interactive": True,
            "local_subscription": True,
            "api_key": True,
            "ci": True,
            "streaming": True,
            "file_writes": True,
            "multi_turn": False,
        },
        "openai": {
            "local_interactive": False,
            "local_subscription": False,
            "api_key": True,
            "ci": True,
            "streaming": True,
            "file_writes": False,
            "multi_turn": True,
        },
        "anthropic": {
            "local_interactive": False,
            "local_subscription": False,
            "api_key": True,
            "ci": True,
            "streaming": True,
            "file_writes": False,
            "multi_turn": True,
        },
    }
    return caps.get(backend, {})


def _is_ci_environment() -> bool:
    """Detect whether we're running in a CI environment."""
    ci_vars = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "CIRCLECI")
    return any(os.environ.get(v) for v in ci_vars)
