"""ReviewerExt Protocol -- code review stage interface.

Defines the public contract that a premium code-reviewer implementation must
satisfy in order to be registered with the capability registry.

Premium implementation: wraps ``saturnday.run.role_modes.invoke_role`` with
role ``"code_reviewer"`` and maps the resulting ``RoleResult`` to the dict
shape documented on :meth:`ReviewerExt.review`.

Usage (public / registry side)::

    from saturnday.capability_registry import get, is_available
    from saturnday.interfaces.reviewer import STAGE_NAME

    if is_available(STAGE_NAME):
        reviewer = get(STAGE_NAME)
        result = reviewer.review(task, coder_config, repo_path)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["ReviewerExt", "STAGE_NAME"]

logger = logging.getLogger(__name__)

#: Registry key for the code-reviewer stage.
STAGE_NAME: str = "code_reviewer"


@runtime_checkable
class ReviewerExt(Protocol):
    """Protocol for a premium code-review stage handler.

    A conforming object is registered under ``"code_reviewer"`` in the
    capability registry.  The public orchestrator calls :meth:`review` after
    the coder agent has applied changes to the repository.

    Return dict shape::

        {
            "success":      bool,   # True if review completed without error
            "output":       str,    # Human-readable review output / rationale
            "has_concerns": bool,   # True if the reviewer raised issues
        }

    Premium implementation wraps
    ``invoke_role("code_reviewer", task, coder_config=..., repo_path=...)``
    and maps the resulting ``RoleResult`` to this dict.
    """

    def review(
        self,
        task: str,
        coder_config: Any,
        repo_path: Path,
    ) -> dict:
        """Run a code review for the given task.

        :param task: The task description / diff context passed to the
            reviewer LLM prompt.
        :param coder_config: Opaque coder configuration dict forwarded to the
            role invocation (model, temperature, etc.).
        :param repo_path: Absolute path to the repository root.
        :returns: Dict with keys ``success``, ``output``, ``has_concerns``.
        """
        ...
