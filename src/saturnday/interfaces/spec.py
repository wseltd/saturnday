"""SpecVerifierExt protocol for the Saturnday public/premium split.

Defines the exact call signature that a premium spec-verification
implementation must satisfy in order to be registered with the
capability registry under the ``"spec_verifier"`` key.

Premium implementation:

- ``saturnday.run.spec_verifier.generate_spec_assertions`` /
  ``run_spec_assertions`` (consolidated into ``generate_assertions`` +
  ``run_assertions``)
- ``saturnday.run.property_tests.run_property_tests`` (consolidated into
  ``run_property_tests`` -- premium wraps target identification, test
  generation, and execution into a single call)
- ``saturnday.run.dataflow_checker.check_cross_function_flow`` (wrapped as
  ``check_dataflow`` with ``state`` typed as ``Any`` because
  ``ProjectState`` is an implementation detail)

Return types are intentionally ``list[dict]`` throughout -- the protocol
defines shape, not schema.  Schema validation is the premium
implementation's responsibility.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from saturnday._types import TicketSpec

__all__ = ["SpecVerifierExt", "STAGE_NAME"]

logger = logging.getLogger(__name__)

STAGE_NAME: str = "spec_verifier"
"""Registry key used to register and look up a SpecVerifierExt handler."""


@runtime_checkable
class SpecVerifierExt(Protocol):
    """Protocol for premium spec verification and dataflow checking.

    A conforming implementation must supply all four methods below.
    Register it at import time via::

        from saturnday.capability_registry import register
        from saturnday.interfaces.spec import SpecVerifierExt, STAGE_NAME

        register(STAGE_NAME, MySpecVerifier())
    """

    def generate_assertions(
        self,
        ticket: TicketSpec,
        repo_path: Path,
    ) -> list[dict]:
        """Generate executable assertion dicts from ticket acceptance criteria.

        Args:
            ticket: The ticket whose acceptance criteria are analysed.
            repo_path: Absolute path to the repository root.

        Returns:
            A list of assertion dicts ready for ``run_assertions``.
        """
        ...

    def run_assertions(
        self,
        assertions: list[dict],
        repo_path: Path,
    ) -> list[dict]:
        """Execute assertion dicts and return pass/fail results.

        Args:
            assertions: Assertion dicts produced by ``generate_assertions``.
            repo_path: Absolute path to the repository root.

        Returns:
            The same dicts enriched with ``"passed": bool`` and
            ``"error": str | None`` fields.
        """
        ...

    def run_property_tests(
        self,
        changed_files: list[str],
        repo_path: Path,
    ) -> list[dict]:
        """Identify targets, generate property tests, and run them.

        Premium implementation wraps target identification, test generation,
        and execution into a single call.

        Args:
            changed_files: Repo-relative paths of files changed by the ticket.
            repo_path: Absolute path to the repository root.

        Returns:
            A list of result dicts, one per property test executed.
        """
        ...

    def check_dataflow(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> list[dict]:
        """Detect cross-function data flow mismatches in changed files.

        Args:
            changed_files: Repo-relative paths of files changed by the ticket.
            repo_path: Absolute path to the repository root.
            state: Optional project state object (``ProjectState | None``).
                Typed as ``Any`` to avoid coupling the protocol to the
                internal dataclass.

        Returns:
            A list of dataflow finding dicts.  Empty list means no issues.
        """
        ...
