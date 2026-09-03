"""Thin LLM client: CLI-first (subscription) + API fallback.

Six backends:
- ``codex-cli``: ChatGPT subscription via ``codex`` subprocess.
- ``claude-cli``: Claude subscription via ``claude`` subprocess.
- ``openclaude``: Claude Code fork with OpenAI-compatible shim via ``openclaude`` subprocess.
- ``cursor-cli``: Cursor multi-model agent via ``agent`` subprocess.
- ``openai``: OpenAI-compatible HTTP API (also covers Groq, Together, vLLM).
- ``anthropic``: Anthropic Messages API.

CLI backends are the primary path (no API cost).  API backends are the
fallback for CI/server use or users who prefer API keys.

All HTTP is stdlib ``urllib`` — no extra dependencies.

Shell policy limitation (CLI backends)
---------------------------------------
The ``codex-cli`` and ``claude-cli`` backends launch as autonomous agent
sub-processes inside the repo directory using ``subprocess.run``.  Once
launched, those agents execute commands in their own shells and are NOT
constrained at the process level by ``saturnday.run.safe_shell``.  The shell
policy wrapper (``safe_subprocess_run``) only covers commands that the ticket
runner itself issues (git operations).  Full isolation of CLI coder processes
requires container or seccomp sandboxing, which is outside the current scope
and tracked as a future capability.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from saturnday._exceptions import CoderAPIError
from saturnday._types import CoderConfig
from saturnday.shared.backend_auth import (
    capability_matrix,
    validate_auth,
)

logger = logging.getLogger(__name__)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _validate_url_scheme(url: str) -> None:
    """Reject non-HTTP(S) URL schemes to prevent file:// or other SSRF vectors."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise CoderAPIError(
            f"Unsupported URL scheme {scheme!r} in {url!r}. "
            "Only http:// and https:// are allowed."
        )


# Auth-failure substrings recognised in CLI stderr output.
# Kept conservative: only match patterns that unambiguously indicate a login
# problem rather than a transient or unrelated error.
_AUTH_FAILURE_HINTS: tuple[str, ...] = (
    "not logged in",
    "login required",
    "authentication required",
    "unauthenticated",
    "auth failed",
    "please log in",
    "please login",
    "unauthorized",
)

# Default base URLs and models for API backends
_API_DEFAULTS: dict[str, dict[str, str]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-4-20250514",
    },
}

# Backends where the CLI agent writes files directly to disk
CLI_BACKENDS = frozenset({"codex-cli", "claude-cli", "openclaude", "cursor-cli"})

# Warn-once flag for the openclaude backend quality caveat
_OPENCLAUDE_WARNED = False


def is_cli_backend(config: CoderConfig) -> bool:
    """Check if the backend is a CLI agent that writes files directly."""
    return config.backend in CLI_BACKENDS


def call_coder(
    config: CoderConfig,
    messages: list[dict[str, str]],
    repo_path: Path,
    *,
    agent_mode: bool = False,
) -> str:
    """Route a code-generation request to the configured backend.

    Args:
        config: Coder backend configuration.
        messages: OpenAI-format messages (system/user roles).
        repo_path: Repository directory (used by CLI backends as cwd).
        agent_mode: When ``True``, CLI backends run as full agents that
            edit files directly (no ``--print`` flag).  The response is
            the agent's conversational output, NOT file content.  Callers
            must NOT write the response to disk.  Default ``False`` uses
            ``--print`` for backward compatibility.

    Returns:
        Raw text response from the coder.

    Raises:
        CoderAPIError: On any backend failure.
    """
    # ------------------------------------------------------------------
    # Auth preflight — must succeed before any subprocess or HTTP call.
    # ------------------------------------------------------------------
    auth = validate_auth(config)
    logger.info(
        "Backend: %s | Auth mode: %s",
        config.backend,
        auth.mode,
    )
    if not auth.valid:
        raise CoderAPIError(
            f"Auth preflight failed for backend '{config.backend}': {auth.reason}"
        )

    # Log capability matrix so operators can see what the backend supports.
    caps = capability_matrix(config.backend)
    if caps:
        logger.info(
            "Backend '%s' capabilities: %s",
            config.backend,
            ", ".join(f"{k}={v}" for k, v in sorted(caps.items())),
        )

    if config.backend == "codex-cli":
        prompt_text = _messages_to_text(messages)
        return _call_codex_cli(config, prompt_text, repo_path, agent_mode=agent_mode)
    elif config.backend == "claude-cli":
        prompt_text = _messages_to_text(messages)
        return _call_claude_cli(config, prompt_text, repo_path, agent_mode=agent_mode)
    elif config.backend == "openclaude":
        prompt_text = _messages_to_text(messages)
        return _call_openclaude(config, prompt_text, repo_path, agent_mode=agent_mode)
    elif config.backend == "cursor-cli":
        prompt_text = _messages_to_text(messages)
        return _call_cursor_cli(config, prompt_text, repo_path, agent_mode=agent_mode)
    elif config.backend == "anthropic":
        return _call_anthropic(config, messages)
    elif config.backend == "openai":
        return _call_openai_compatible(config, messages)
    else:
        raise CoderAPIError(f"Unknown backend: {config.backend!r}")


# ---------------------------------------------------------------------------
# CLI backends (subscription-based, no API cost)
# ---------------------------------------------------------------------------

def _call_codex_cli(
    config: CoderConfig,
    prompt_text: str,
    repo_path: Path,
    *,
    agent_mode: bool = False,
) -> str:
    """Invoke ``codex`` CLI with the full prompt.

    When ``agent_mode`` is ``False``, runs ``codex exec`` for non-interactive
    execution and captures stdout.

    When ``agent_mode`` is ``True``, runs ``codex`` in full-auto mode as an
    agent that edits files directly.  The return value is the agent's output —
    callers must NOT write it to disk as file content.
    """
    if agent_mode:
        # Use `codex exec` for nested/repair calls — runs non-interactively
        # without needing a TTY. Bypass sandbox to avoid permission errors
        # when spawned as a nested subprocess.
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt_text,
        ]
    else:
        # Also bypass sandbox for non-agent mode — Codex's sandbox
        # blocks network access to its own API when spawned as subprocess
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt_text,
        ]
    logger.info("Calling codex-cli in %s (%d char prompt, agent_mode=%s)", repo_path, len(prompt_text), agent_mode)
    _env = {**os.environ, "SATURNDAY_IN_SUBPROCESS": "1"}
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=not agent_mode,
            text=True,
            timeout=config.timeout_s,
            check=False,
            env=_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoderAPIError(
            f"codex-cli timed out after {config.timeout_s}s",
        ) from exc
    except FileNotFoundError as exc:
        raise CoderAPIError(
            "codex CLI not found. Install it or use an API backend.",
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr or "" if not agent_mode else ""
        if stderr and _is_auth_failure(stderr):
            raise CoderAPIError(
                "Not logged in. Run `codex login` first.",
                raw_body=stderr[:1000],
            )
        if not agent_mode:
            raise CoderAPIError(
                f"codex-cli exited with code {result.returncode}",
                raw_body=stderr[:1000],
            )
        # In agent_mode, non-zero exit may just mean partial completion
        logger.warning("codex-cli agent exited with code %d", result.returncode)

    response = (result.stdout or "").strip() if not agent_mode else "codex agent completed"
    if not response:
        raise CoderAPIError("codex-cli returned empty response")

    logger.info("codex-cli response: %d chars", len(response))
    return response


def _call_cursor_cli(
    config: CoderConfig,
    prompt_text: str,
    repo_path: Path,
    *,
    agent_mode: bool = False,
) -> str:
    """Invoke Cursor CLI agent.

    When ``agent_mode`` is ``False``, runs ``agent -p --force`` for
    non-interactive execution that captures output and can modify files.

    When ``agent_mode`` is ``True``, runs ``agent -p --force`` without capture
    so the agent can work interactively.  The return value is the agent's
    conversational output — callers must NOT write it to disk as file content.

    Args:
        config: Coder backend configuration.
        prompt_text: Flattened prompt string for the agent.
        repo_path: Repository directory used as cwd.
        agent_mode: When ``True``, do not capture subprocess output.

    Returns:
        Agent response text, or ``"cursor agent completed"`` in agent_mode.

    Raises:
        CoderAPIError: On timeout, binary-not-found, or non-zero exit in
            non-agent mode.
    """
    cmd = ["agent", "-p", "--force", prompt_text]

    logger.info(
        "Calling cursor-cli in %s (%d char prompt, agent_mode=%s)",
        repo_path,
        len(prompt_text),
        agent_mode,
    )
    _env = {**os.environ, "SATURNDAY_IN_SUBPROCESS": "1"}
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=not agent_mode,
            text=True,
            timeout=config.timeout_s,
            check=False,
            env=_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoderAPIError(
            f"cursor-cli timed out after {config.timeout_s}s",
        ) from exc
    except FileNotFoundError as exc:
        raise CoderAPIError(
            "Cursor CLI ('agent') not found. Install from cursor.com/cli.",
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr or "" if not agent_mode else ""
        if not agent_mode:
            raise CoderAPIError(
                f"cursor-cli exited with code {result.returncode}",
                raw_body=stderr[:1000],
            )
        logger.warning("cursor-cli agent exited with code %d", result.returncode)

    response = (result.stdout or "").strip() if not agent_mode else "cursor agent completed"
    logger.info("cursor-cli response: %d chars", len(response))
    return response


def _call_claude_cli(
    config: CoderConfig,
    prompt_text: str,
    repo_path: Path,
    *,
    agent_mode: bool = False,
) -> str:
    """Invoke ``claude`` CLI in the repo directory.

    When ``agent_mode`` is ``False`` (default), runs with ``--print`` which
    outputs text to stdout without editing files.

    When ``agent_mode`` is ``True``, runs WITHOUT ``--print`` so Claude
    operates as a full agent that reads files, writes files, and runs
    commands directly.  The return value is the agent's conversational
    output — callers must NOT write it to disk as file content.
    """
    cmd = ["claude", "--dangerously-skip-permissions"]
    if not agent_mode:
        cmd.insert(1, "--print")
    logger.info("Calling claude-cli in %s (%d char prompt)", repo_path, len(prompt_text))
    _env = {**os.environ, "SATURNDAY_IN_SUBPROCESS": "1"}
    try:
        result = subprocess.run(
            cmd,
            input=prompt_text,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=config.timeout_s,
            check=False,
            env=_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoderAPIError(
            f"claude-cli timed out after {config.timeout_s}s",
        ) from exc
    except FileNotFoundError as exc:
        raise CoderAPIError(
            "claude CLI not found. Install it or use an API backend.",
        ) from exc

    if result.returncode != 0:
        if _is_auth_failure(result.stderr):
            raise CoderAPIError(
                "Not logged in. Run `claude login` first.",
                raw_body=result.stderr[:1000],
            )
        logger.warning("claude-cli stderr: %s", result.stderr[:500])

    response = result.stdout.strip()
    if not response:
        raise CoderAPIError(
            "claude-cli returned empty response",
            raw_body=result.stderr[:1000],
        )
    logger.info("claude-cli finished: %d chars response", len(response))
    return response


def _call_openclaude(
    config: CoderConfig,
    prompt_text: str,
    repo_path: Path,
    *,
    agent_mode: bool = False,
) -> str:
    """Invoke the ``openclaude`` CLI (Claude Code fork with OpenAI-compatible shim).

    Same invocation pattern as ``claude-cli`` but uses the ``openclaude`` binary.
    Model quality varies depending on the upstream provider configured in OpenClaude,
    so callers should expect weaker tool-calling from non-frontier models.

    When ``agent_mode`` is ``False`` (default), runs with ``--print`` which
    outputs text to stdout without editing files.

    When ``agent_mode`` is ``True``, runs WITHOUT ``--print`` so openclaude
    operates as a full agent that reads files, writes files, and runs
    commands directly.  The return value is the agent's conversational
    output — callers must NOT write it to disk as file content.

    Args:
        config: Coder backend configuration.
        prompt_text: Flattened prompt string for the binary.
        repo_path: Repository directory used as cwd.
        agent_mode: When ``True``, do not pass ``--print``.

    Returns:
        Agent response text.

    Raises:
        CoderAPIError: On binary-not-found, timeout, or non-zero exit in
            non-agent mode.
    """
    global _OPENCLAUDE_WARNED
    if not _OPENCLAUDE_WARNED:
        logger.warning(
            "openclaude backend active — model quality depends on upstream provider. "
            "Weaker models may produce lower-quality repairs and ticket execution."
        )
        _OPENCLAUDE_WARNED = True

    cmd = ["openclaude", "--dangerously-skip-permissions"]
    if not agent_mode:
        cmd.insert(1, "--print")
    logger.info("Calling openclaude in %s (%d char prompt, agent_mode=%s)", repo_path, len(prompt_text), agent_mode)
    _env = {**os.environ, "SATURNDAY_IN_SUBPROCESS": "1"}
    try:
        result = subprocess.run(
            cmd,
            input=prompt_text,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=config.timeout_s,
            check=False,
            env=_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoderAPIError(
            f"openclaude timed out after {config.timeout_s}s",
        ) from exc
    except FileNotFoundError as exc:
        raise CoderAPIError(
            "openclaude binary not found. Install OpenClaude: npm install -g @gitlawb/openclaude",
        ) from exc

    if result.returncode != 0:
        if _is_auth_failure(result.stderr):
            raise CoderAPIError(
                "Not logged in. Configure OpenClaude credentials first.",
                raw_body=result.stderr[:1000],
            )
        logger.warning("openclaude stderr: %s", result.stderr[:500])

    response = result.stdout.strip()
    logger.info("openclaude finished: %d chars response", len(response))
    return response


# ---------------------------------------------------------------------------
# API backends (pay-per-token)
# ---------------------------------------------------------------------------

def _call_openai_compatible(
    config: CoderConfig,
    messages: list[dict[str, str]],
) -> str:
    """POST to an OpenAI-compatible ``/chat/completions`` endpoint.

    Covers: OpenAI, GPT, Groq, Together, Mistral, local vLLM.
    """
    defaults = _API_DEFAULTS.get("openai", {})
    base_url = (config.base_url or defaults.get("base_url", "")).rstrip("/")
    model = config.model or defaults.get("model", "gpt-4o")
    url = f"{base_url}/chat/completions"
    _validate_url_scheme(url)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }

    data = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    logger.info("POST %s model=%s", url, model)

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_s) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            pass
        if exc.code == 401:
            raise CoderAPIError(
                "API key not set or invalid. Set OPENAI_API_KEY / ANTHROPIC_API_KEY.",
                http_status=exc.code,
                raw_body=raw[:1000],
            ) from exc
        raise CoderAPIError(
            f"OpenAI-compatible API error (HTTP {exc.code})",
            http_status=exc.code,
            raw_body=raw[:1000],
        ) from exc
    except Exception as exc:
        raise CoderAPIError(f"OpenAI-compatible request failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CoderAPIError(
            "Invalid JSON in OpenAI-compatible response",
            http_status=status,
            raw_body=body[:500],
        ) from exc

    return _extract_openai_content(parsed)


def _call_anthropic(
    config: CoderConfig,
    messages: list[dict[str, str]],
) -> str:
    """POST to the Anthropic Messages API, translating from OpenAI format."""
    defaults = _API_DEFAULTS.get("anthropic", {})
    base_url = (config.base_url or defaults.get("base_url", "")).rstrip("/")
    model = config.model or defaults.get("model", "claude-sonnet-4-20250514")
    url = f"{base_url}/v1/messages"
    _validate_url_scheme(url)

    # Translate OpenAI format → Anthropic format
    system_text = ""
    anthropic_messages: list[dict[str, str]] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            system_text = content
        else:
            anthropic_messages.append({"role": role, "content": content})

    # Anthropic requires first message to be user role
    if anthropic_messages and anthropic_messages[0]["role"] != "user":
        anthropic_messages.insert(0, {"role": "user", "content": "Please proceed."})

    anthropic_payload: dict = {
        "model": model,
        "max_tokens": config.max_tokens,
        "messages": anthropic_messages,
    }
    if system_text:
        anthropic_payload["system"] = system_text
    if config.temperature is not None:
        anthropic_payload["temperature"] = config.temperature

    data = json.dumps(anthropic_payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": config.api_key,
        "anthropic-version": "2023-06-01",
    }

    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    logger.info("POST %s model=%s", url, model)

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_s) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            pass
        if exc.code == 401:
            raise CoderAPIError(
                "API key not set or invalid. Set OPENAI_API_KEY / ANTHROPIC_API_KEY.",
                http_status=exc.code,
                raw_body=raw[:1000],
            ) from exc
        raise CoderAPIError(
            f"Anthropic API error (HTTP {exc.code})",
            http_status=exc.code,
            raw_body=raw[:1000],
        ) from exc
    except Exception as exc:
        raise CoderAPIError(f"Anthropic request failed: {exc}") from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CoderAPIError(
            "Invalid JSON in Anthropic response",
            http_status=status,
            raw_body=body[:500],
        ) from exc

    return _extract_anthropic_content(parsed)


# ---------------------------------------------------------------------------
# Response extraction helpers
# ---------------------------------------------------------------------------

def _is_auth_failure(stderr: str) -> bool:
    """Return True if stderr content looks like a login / auth failure.

    Uses a conservative substring match against known CLI error phrases.
    False-negatives (unrecognised auth errors) fall through to the generic
    non-zero exit-code error, which is safer than false-positives.

    Args:
        stderr: Standard error output from a CLI subprocess.

    Returns:
        True when an auth-failure hint is found in stderr (case-insensitive).
    """
    lower = stderr.lower()
    return any(hint in lower for hint in _AUTH_FAILURE_HINTS)


def _messages_to_text(messages: list[dict[str, str]]) -> str:
    """Flatten OpenAI-format messages into a single prompt string for CLI backends."""
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def _extract_openai_content(parsed: dict) -> str:
    """Extract text content from an OpenAI-format response."""
    choices = parsed.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content
            text = first.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise CoderAPIError(
        "No content in OpenAI-compatible response",
        raw_body=json.dumps(parsed)[:500],
    )


def _extract_anthropic_content(parsed: dict) -> str:
    """Extract text content from an Anthropic Messages API response."""
    content_blocks = parsed.get("content", [])
    if isinstance(content_blocks, list):
        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        if texts:
            return "\n".join(texts)
    raise CoderAPIError(
        "No content in Anthropic response",
        raw_body=json.dumps(parsed)[:500],
    )
