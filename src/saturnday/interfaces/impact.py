"""ImpactAnalysisExt protocol for the Saturnday public/premium split.

Defines the exact call signature that a premium impact-analysis implementation
must satisfy in order to be registered with the capability registry.

Premium implementation: ``saturnday.run.impact_analysis``.
- ``compute_impact`` wraps :func:`~saturnday.run.impact_analysis.compute_impact`
- ``select_verification_layers`` wraps
  :func:`~saturnday.run.impact_analysis.select_verification_layers`

At protocol level, ``compute_impact`` returns ``dict`` rather than the
``ImpactReport`` dataclass -- ``ImpactReport`` is an implementation detail.
Premium callers may use the result via ``dataclasses.asdict()`` or attribute
access; public callers treat it as an opaque mapping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from saturnday._types import TicketSpec

__all__ = ["ImpactAnalysisExt", "STAGE_NAME"]

logger = logging.getLogger(__name__)

STAGE_NAME: str = "impact_analysis"


@runtime_checkable
class ImpactAnalysisExt(Protocol):
    """Protocol for premium impact-analysis capability.

    An object satisfying this protocol can be registered with the capability
    registry under the key ``"impact_analysis"`` and will be invoked by the
    run orchestrator to compute blast radius and select verification layers.

    Implementors must provide both methods; neither has a default
    implementation so that missing coverage is a clear conformance failure
    rather than a silent no-op.
    """

    def compute_impact(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> dict:
        """Compute the blast radius for a set of changed files.

        Args:
            changed_files: Repository-relative paths of files that changed.
            repo_path: Root of the repository being analysed.
            state: Optional pre-computed project state (e.g. AST data).
                Pass ``None`` if unavailable; implementations must fall back
                to direct file scanning in that case.

        Returns:
            A dict representation of the impact report.  Keys include at
            minimum: ``total_blast_radius`` (int), ``touched_files``
            (list[str]), ``dependent_files`` (list[str]),
            ``test_files`` (list[str]).
        """
        ...

    def select_verification_layers(
        self,
        impact: dict,
        ticket: Any,
    ) -> dict[str, bool]:
        """Decide which verification layers should run for this ticket.

        Args:
            impact: The impact dict produced by :meth:`compute_impact`.
            ticket: The :class:`~saturnday._types.TicketSpec` being executed.
                Typed as ``Any`` to avoid coupling the protocol to internal
                dataclasses; premium implementations receive a ``TicketSpec``.

        Returns:
            A dict mapping layer name to ``True`` (run) or ``False`` (skip).
            All layers default to ``True``; expensive layers may be skipped
            for trivial or non-code changes.
        """
        ...
