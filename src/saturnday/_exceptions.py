"""Exception hierarchy for saturnday.

Every exception inherits from ``CloudCoreError`` so callers can catch the
entire family with a single handler when needed.
"""

from __future__ import annotations


class CloudCoreError(Exception):
    """Base exception for all saturnday errors."""


class PlanValidationError(CloudCoreError):
    """Raised when a project plan fails validation.

    Attributes:
        errors: Individual validation failure descriptions.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        joined = "; ".join(errors)
        super().__init__(f"Plan validation failed: {joined}")


class CoderAPIError(CloudCoreError):
    """Raised when a coder backend call fails.

    Attributes:
        http_status: HTTP status code, if available.
        raw_body: Truncated response body for diagnostics.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        raw_body: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.raw_body = raw_body


class PatchExtractionError(CloudCoreError):
    """Raised when no valid code patch can be extracted from a coder response.

    Attributes:
        response: The raw backend response text captured before the error was
            raised.  Preserved so that error-path evidence can record what the
            coder actually returned, even when no file changes were detected.
    """

    def __init__(self, message: str, *, response: str = "") -> None:
        super().__init__(message)
        self.response = response


class PatchApplicationError(CloudCoreError):
    """Raised when a patch cannot be applied to the repository."""


class ScopeViolationError(CloudCoreError):
    """Fix 70: Raised when a CLI-backend coder modified files outside the
    declared ticket scope (``allowed_globs`` / ``forbidden_globs``).

    The API-backend code path enforces scope at apply-time via
    :func:`patch_extractor._validate_path`; CLI backends write files (and may
    commit) directly, so the runner enforces scope post-hoc against
    ``git status`` / new commits.

    Attributes:
        violating_paths: Tuple of relative paths that failed scope checks.
        response: Coder response text (carried for evidence).
    """

    def __init__(
        self,
        message: str,
        *,
        violating_paths: tuple[str, ...] = (),
        response: str = "",
    ) -> None:
        super().__init__(message)
        self.violating_paths = violating_paths
        self.response = response


class GovernanceError(CloudCoreError):
    """Raised when governance check execution itself fails (not a FAIL disposition)."""


class PolicySchemaError(CloudCoreError):
    """Raised when ``.saturnday-policy.yaml`` contains exemption entries that
    do not match the current schema.

    The policy loader refuses to return a partially-filtered exemption list
    when any entry is invalid, because running governance with silently
    dropped exemptions produces a misleading FAIL disposition.  Callers
    (governance CLI, repair CLI) are expected to catch this, surface the
    operator-facing message, and exit non-zero without running governance.

    Attributes:
        invalid_entries: Tuple of per-entry detail dicts with keys
            ``index`` (int), ``present_keys`` (tuple[str, ...]), and
            ``reason`` (str).  Preserved so callers can emit structured
            evidence if they want.
    """

    def __init__(
        self,
        message: str,
        *,
        invalid_entries: tuple[dict, ...] = (),
    ) -> None:
        super().__init__(message)
        self.invalid_entries = invalid_entries


class TicketFailedError(CloudCoreError):
    """Raised when a ticket exhausts all repair attempts.

    Attributes:
        ticket_id: The failing ticket identifier.
        attempt: Final attempt number when the ticket was abandoned.
    """

    def __init__(self, ticket_id: str, attempt: int) -> None:
        self.ticket_id = ticket_id
        self.attempt = attempt
        super().__init__(
            f"Ticket {ticket_id!r} failed after {attempt} attempt(s)"
        )


# Sentinel string embedded in GitStateError messages.
# Detected by failure_classifier to distinguish git-state failures from
# genuine "coder wrote nothing" failures.
GIT_STATE_ERROR_MARKER = "git-tracking precondition failure"


class GitStateError(CloudCoreError):
    """Raised when git-based change detection cannot operate.

    Distinct from PatchExtractionError, which means the coder ran
    successfully but made no detectable changes.  GitStateError means
    the precondition for git-based change detection is absent or invalid.

    Attributes:
        response: The raw backend response text captured before the error was
            raised.  Preserved so that error-path evidence can record what the
            coder returned even when git-state detection failed.
    """

    def __init__(self, message: str, *, response: str = "") -> None:
        super().__init__(message)
        self.response = response
