"""TriageHook protocol for the Saturnday public/premium split.

Defines the exact call signature that a premium security triage implementation
must satisfy in order to be registered with the capability registry under the
key ``"security_triage"``.

Premium implementation:
    ``saturnday.security_triage.triage_security_findings``

Public orchestration code should never import that module directly.  Instead
it calls ``capability_registry.get("security_triage")`` and invokes
``filter_findings``, falling back gracefully when no handler is registered.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["TriageHook", "STAGE_NAME"]

logger = logging.getLogger(__name__)

STAGE_NAME: str = "security_triage"


@runtime_checkable
class TriageHook(Protocol):
    """Protocol for the premium security triage stage.

    A conforming implementation wraps the triage logic that filters raw scan
    findings down to the set that should block or warn a run.

    Premium implementation: ``saturnday.security_triage.triage_security_findings``

    The protocol contract:

    - ``filter_findings`` receives the full list of raw scan findings, the
      repository root, and the coder configuration object.
    - It returns the subset of findings that survive triage (i.e. were not
      suppressed by policy, waiver, or noise rules).
    - Callers must not assume anything about findings that are absent from the
      returned list -- they may have been suppressed or deferred, not fixed.
    """

    def filter_findings(
        self,
        findings: list[dict],
        repo_path: Path,
        coder_config: Any,
    ) -> list[dict]:
        """Filter *findings* down to those that should affect the current run.

        Args:
            findings:     Full list of raw scan findings, each as a dict.
            repo_path:    Absolute path to the repository root.
            coder_config: Coder configuration object (implementation detail of
                          the caller; typed as ``Any`` to avoid coupling this
                          protocol to the run-layer dataclass).

        Returns:
            The subset of *findings* that survived triage.  Order and shape of
            surviving entries are preserved from the input.
        """
        ...  # pragma: no cover
