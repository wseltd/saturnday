"""EvidenceAppender Protocol -- additive premium evidence stage interface.

Defines the public contract that a premium evidence-appender implementation
must satisfy in order to be registered with the capability registry.

Premium evidence is additive -- it appends to the base pack, never replaces.

The merge invariant: premium may add keys but must never remove or overwrite
base keys.  The base evidence pack is authoritative; premium fields supplement
it with triage rationale, memory items, impact reports, claim verdicts, licence
state, etc.

Usage (public / registry side)::

    from saturnday.capability_registry import get, is_available
    from saturnday.interfaces.evidence import STAGE_NAME

    if is_available(STAGE_NAME):
        appender = get(STAGE_NAME)
        full_pack = appender.append(base_pack, artifacts)
    else:
        full_pack = base_pack
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

__all__ = ["EvidenceAppender", "STAGE_NAME"]

logger = logging.getLogger(__name__)

#: Registry key for the evidence-appender stage.
STAGE_NAME: str = "evidence_appender"


@runtime_checkable
class EvidenceAppender(Protocol):
    """Protocol for a premium evidence-appender stage handler.

    A conforming object is registered under ``"evidence_appender"`` in the
    capability registry.  The public orchestrator calls :meth:`append` after
    the base evidence pack has been built, passing both the base pack and any
    premium-stage artifacts accumulated during the run.

    Premium evidence is additive -- it appends to the base pack, never replaces.

    Merge invariant: the returned dict must contain all keys from
    ``base_pack`` with their original values unchanged.  Premium may add new
    keys; it must never remove or overwrite base keys.
    """

    def append(
        self,
        base_pack: dict,
        artifacts: dict,
    ) -> dict:
        """Append premium evidence fields to the base evidence pack.

        :param base_pack: The evidence dict produced by the public orchestrator
            (run-summary fields, governance status, etc.).  Must be returned
            intact in the result.
        :param artifacts: Premium-stage artifacts accumulated during the run
            (triage rationale, memory items, impact report, claim verdicts,
            licence state, etc.).  The structure is an internal premium concern.
        :returns: A new dict containing all keys from ``base_pack`` plus any
            additional premium fields contributed by the appender.  No key
            present in ``base_pack`` may be absent or overwritten in the result.
        """
        ...
