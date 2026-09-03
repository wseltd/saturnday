"""Tests for saturnday.coder_adapter."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from saturnday._exceptions import CoderAPIError
from saturnday._types import CoderConfig
from saturnday.coder_adapter import (
    CLI_BACKENDS,
    _extract_anthropic_content,
    _extract_openai_content,
    _is_auth_failure,
    _messages_to_text,
    call_coder,
    is_cli_backend,
)
from saturnday.shared.backend_auth import AuthValidation


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _valid_auth(mode: str = "local_interactive") -> AuthValidation:
    """Return an AuthValidation that represents a passing preflight."""
    return AuthValidation(valid=True, mode=mode, reason="ok")  # type: ignore[arg-type]


def _invalid_auth(mode: str = "unsupported", reason: str = "no creds") -> AuthValidation:
    """Return an AuthValidation that represents a failing preflight."""
    return AuthValidation(valid=False, mode=mode, reason=reason)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _messages_to_text
# ---------------------------------------------------------------------------

class TestMessagesToText:
    def test_combines_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are an engineer."},
            {"role": "user", "content": "Build a config module."},
        ]
        text = _messages_to_text(messages)
        assert "You are an engineer." in text
        assert "Build a config module." in text

    def test_skips_empty_content(self) -> None:
        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "Do stuff."},
        ]
        text = _messages_to_text(messages)
        assert text == "Do stuff."


# ---------------------------------------------------------------------------
# _extract_openai_content
# ---------------------------------------------------------------------------

class TestExtractOpenAIContent:
    def test_extracts_message_content(self) -> None:
        parsed = {"choices": [{"message": {"content": "hello world"}}]}
        assert _extract_openai_content(parsed) == "hello world"

    def test_extracts_text_field(self) -> None:
        parsed = {"choices": [{"text": "alt response"}]}
        assert _extract_openai_content(parsed) == "alt response"

    def test_raises_on_empty(self) -> None:
        with pytest.raises(CoderAPIError, match="No content"):
            _extract_openai_content({"choices": []})

    def test_raises_on_missing_choices(self) -> None:
        with pytest.raises(CoderAPIError, match="No content"):
            _extract_openai_content({})


# ---------------------------------------------------------------------------
# _extract_anthropic_content
# ---------------------------------------------------------------------------

class TestExtractAnthropicContent:
    def test_extracts_text_blocks(self) -> None:
        parsed = {"content": [{"type": "text", "text": "response text"}]}
        assert _extract_anthropic_content(parsed) == "response text"

    def test_joins_multiple_blocks(self) -> None:
        parsed = {
            "content": [
                {"type": "text", "text": "part1"},
                {"type": "text", "text": "part2"},
            ]
        }
        result = _extract_anthropic_content(parsed)
        assert "part1" in result
        assert "part2" in result

    def test_raises_on_empty(self) -> None:
        with pytest.raises(CoderAPIError, match="No content"):
            _extract_anthropic_content({"content": []})


# ---------------------------------------------------------------------------
# _is_auth_failure
# ---------------------------------------------------------------------------

class TestIsAuthFailure:
    @pytest.mark.parametrize("stderr", [
        "Error: not logged in",
        "Authentication required to continue",
        "login required for this action",
        "Please log in before proceeding",
        "Please login to continue",
        "Unauthenticated request",
        "auth failed: invalid session",
        "401 Unauthorized",
    ])
    def test_returns_true_for_auth_stderr(self, stderr: str) -> None:
        assert _is_auth_failure(stderr) is True

    @pytest.mark.parametrize("stderr", [
        "codex: command not found",
        "timeout expired after 300s",
        "fatal: not a git repository",
        "",
    ])
    def test_returns_false_for_non_auth_stderr(self, stderr: str) -> None:
        assert _is_auth_failure(stderr) is False

    def test_case_insensitive(self) -> None:
        assert _is_auth_failure("NOT LOGGED IN") is True


# ---------------------------------------------------------------------------
# call_coder — routing (env-independent: validate_auth always mocked)
# ---------------------------------------------------------------------------

class TestCallCoderRouting:
    def test_unknown_backend_raises(self) -> None:
        """Unknown backend should raise even before auth — validate_auth also
        returns unsupported, so auth preflight fires first with a clear message."""
        config = CoderConfig(backend="unknown")  # type: ignore[arg-type]
        # No mock: validate_auth returns invalid for unknown backend, which is fine —
        # we just want any CoderAPIError.
        with pytest.raises(CoderAPIError):
            call_coder(config, [], MagicMock())

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_codex_cli")
    def test_routes_to_codex(
        self,
        mock_codex: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_codex.return_value = "response"
        config = CoderConfig(backend="codex-cli")
        result = call_coder(config, [{"role": "user", "content": "hi"}], MagicMock())
        assert result == "response"
        mock_codex.assert_called_once()

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_claude_cli")
    def test_routes_to_claude(
        self,
        mock_claude: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_claude.return_value = "response"
        config = CoderConfig(backend="claude-cli")
        result = call_coder(config, [{"role": "user", "content": "hi"}], MagicMock())
        assert result == "response"
        mock_claude.assert_called_once()

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_openai_compatible")
    def test_routes_to_openai(
        self,
        mock_oai: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        mock_validate.return_value = _valid_auth("api_key")
        mock_oai.return_value = "response"
        config = CoderConfig(backend="openai", api_key="sk-test")
        result = call_coder(config, [{"role": "user", "content": "hi"}], MagicMock())
        assert result == "response"
        mock_oai.assert_called_once()

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_anthropic")
    def test_routes_to_anthropic(
        self,
        mock_anth: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        mock_validate.return_value = _valid_auth("api_key")
        mock_anth.return_value = "response"
        config = CoderConfig(backend="anthropic", api_key="sk-test")
        result = call_coder(config, [{"role": "user", "content": "hi"}], MagicMock())
        assert result == "response"
        mock_anth.assert_called_once()


# ---------------------------------------------------------------------------
# call_coder — auth preflight
# ---------------------------------------------------------------------------

class TestCallCoderAuthPreflight:
    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_codex_cli")
    def test_validate_auth_called_before_backend(
        self,
        mock_codex: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """validate_auth must be called; backend should only run if auth passes."""
        mock_validate.return_value = _valid_auth()
        mock_codex.return_value = "ok"
        config = CoderConfig(backend="codex-cli")
        call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        mock_validate.assert_called_once_with(config)

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_codex_cli")
    def test_auth_failure_blocks_backend_call(
        self,
        mock_codex: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """Backend must NOT be invoked when auth validation fails."""
        mock_validate.return_value = _invalid_auth(
            reason="Backend 'codex-cli' requires interactive login and cannot be used in CI."
        )
        config = CoderConfig(backend="codex-cli")
        with pytest.raises(CoderAPIError, match="Auth preflight failed"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        mock_codex.assert_not_called()

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_openai_compatible")
    def test_api_key_missing_blocks_call(
        self,
        mock_oai: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """openai backend with no API key must fail auth preflight."""
        mock_validate.return_value = _invalid_auth(
            reason="Backend 'openai' requires an API key."
        )
        config = CoderConfig(backend="openai")
        with pytest.raises(CoderAPIError, match="Auth preflight failed"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        mock_oai.assert_not_called()

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_codex_cli")
    def test_auth_mode_logged(
        self,
        mock_codex: MagicMock,
        mock_validate: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Auth mode must appear in INFO logs before backend call."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_codex.return_value = "done"
        config = CoderConfig(backend="codex-cli")
        with caplog.at_level(logging.INFO, logger="saturnday.coder_adapter"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        assert any(
            "codex-cli" in r.message and "local_interactive" in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# call_coder — login error message formatting
# ---------------------------------------------------------------------------

class TestLoginErrorMessages:
    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_codex_cli_auth_failure_message(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
        tmp_path: MagicMock,
    ) -> None:
        """Codex CLI auth errors surface the `codex login` hint."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="Error: not logged in",
            stdout="",
        )
        config = CoderConfig(backend="codex-cli")
        with pytest.raises(CoderAPIError, match="codex login"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_claude_cli_auth_failure_message(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """Claude CLI auth errors surface the `claude login` hint."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="Authentication required",
            stdout="",
        )
        config = CoderConfig(backend="claude-cli")
        with pytest.raises(CoderAPIError, match="claude login"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_codex_non_auth_error_not_login_message(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """Non-auth codex errors must NOT claim login is the fix."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(
            returncode=2,
            stderr="segfault in codex binary",
            stdout="",
        )
        config = CoderConfig(backend="codex-cli")
        with pytest.raises(CoderAPIError) as exc_info:
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        assert "codex login" not in str(exc_info.value)
        assert "exited with code 2" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Fix 34: claude-cli empty-stdout hardening
# ---------------------------------------------------------------------------

class TestClaudeCliEmptyResponse:
    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_empty_stdout_raises(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """claude-cli empty stdout raises CoderAPIError, not silent empty string."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = CoderConfig(backend="claude-cli")
        with pytest.raises(CoderAPIError, match="empty response"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_empty_stdout_whitespace_only_raises(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """claude-cli whitespace-only stdout (strips to empty) also raises."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(returncode=0, stdout="   \n\t  ", stderr="")
        config = CoderConfig(backend="claude-cli")
        with pytest.raises(CoderAPIError, match="empty response"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_non_empty_stdout_returns_normally(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """claude-cli non-empty stdout is returned unchanged."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Here is the fix.\n", stderr=""
        )
        config = CoderConfig(backend="claude-cli")
        result = call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        assert result == "Here is the fix."

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_auth_failure_still_raises_login_hint(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """Auth failure on claude-cli still surfaces the claude login hint (not empty-response)."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(
            returncode=1, stderr="Authentication required", stdout=""
        )
        config = CoderConfig(backend="claude-cli")
        with pytest.raises(CoderAPIError, match="claude login"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())


# ---------------------------------------------------------------------------
# call_coder — capability display
# ---------------------------------------------------------------------------

class TestCapabilityDisplay:
    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_codex_cli")
    def test_capabilities_logged_for_known_backend(
        self,
        mock_codex: MagicMock,
        mock_validate: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Capability matrix must be logged at INFO level before the backend call."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_codex.return_value = "ok"
        config = CoderConfig(backend="codex-cli")
        with caplog.at_level(logging.INFO, logger="saturnday.coder_adapter"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        cap_logs = [r for r in caplog.records if "capabilities" in r.message]
        assert cap_logs, "Expected at least one capabilities log line"
        assert "codex-cli" in cap_logs[0].message

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_anthropic")
    def test_capabilities_logged_for_api_backend(
        self,
        mock_anth: MagicMock,
        mock_validate: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_validate.return_value = _valid_auth("api_key")
        mock_anth.return_value = "ok"
        config = CoderConfig(backend="anthropic", api_key="key")
        with caplog.at_level(logging.INFO, logger="saturnday.coder_adapter"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
        cap_logs = [r for r in caplog.records if "capabilities" in r.message]
        assert cap_logs
        assert "anthropic" in cap_logs[0].message


# ---------------------------------------------------------------------------
# openclaude backend
# ---------------------------------------------------------------------------

class TestOpenClaudeBackend:
    def test_openclaude_in_cli_backends(self) -> None:
        """openclaude must be a member of CLI_BACKENDS."""
        assert "openclaude" in CLI_BACKENDS

    def test_openclaude_is_cli_backend(self) -> None:
        """is_cli_backend must return True for openclaude."""
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        assert is_cli_backend(config) is True

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter._call_openclaude")
    def test_openclaude_routes_correctly(
        self,
        mock_openclaude: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """call_coder with backend='openclaude' must dispatch to _call_openclaude."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_openclaude.return_value = "openclaude response"
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        result = call_coder(config, [{"role": "user", "content": "fix it"}], MagicMock())
        assert result == "openclaude response"
        mock_openclaude.assert_called_once()

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_openclaude_command_construction_non_agent(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
        tmp_path: MagicMock,
    ) -> None:
        """Non-agent mode must include --print before --dangerously-skip-permissions."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        call_coder(config, [{"role": "user", "content": "go"}], MagicMock(), agent_mode=False)
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[0] == "openclaude"
        assert "--print" in called_cmd
        assert "--dangerously-skip-permissions" in called_cmd

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_openclaude_agent_mode_no_print(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """Agent mode must NOT include --print in the command."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        call_coder(config, [{"role": "user", "content": "go"}], MagicMock(), agent_mode=True)
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[0] == "openclaude"
        assert "--print" not in called_cmd
        assert "--dangerously-skip-permissions" in called_cmd

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_openclaude_binary_not_found_raises(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """FileNotFoundError from subprocess must raise CoderAPIError with install hint."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.side_effect = FileNotFoundError("No such file or directory: 'openclaude'")
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        with pytest.raises(CoderAPIError, match="openclaude binary not found"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())

    @patch("saturnday.coder_adapter.validate_auth")
    @patch("saturnday.coder_adapter.subprocess.run")
    def test_openclaude_binary_not_found_message_contains_install(
        self,
        mock_run: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        """FileNotFoundError error message must mention the npm install command."""
        mock_validate.return_value = _valid_auth("local_interactive")
        mock_run.side_effect = FileNotFoundError("No such file or directory: 'openclaude'")
        config = CoderConfig(backend="openclaude")  # type: ignore[arg-type]
        with pytest.raises(CoderAPIError, match="npm install"):
            call_coder(config, [{"role": "user", "content": "go"}], MagicMock())
