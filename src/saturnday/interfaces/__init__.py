"""Protocol surface between the Saturnday public and premium editions.

Each protocol in this package defines the exact call signature that a premium
implementation must satisfy in order to be registered with the capability
registry.  Public code queries the registry and falls back gracefully when a
premium handler is absent; premium code imports the relevant protocol,
implements it, and calls ``capability_registry.register`` at import time.

Protocols defined here (each in its own module):

- TriageHook            -- src/saturnday/interfaces/triage.py         (SPLIT-003)
- MemoryProvider        -- src/saturnday/interfaces/memory.py         (SPLIT-004)
- SpecVerifierExt       -- src/saturnday/interfaces/spec.py           (SPLIT-005)
- ImpactAnalysisExt     -- src/saturnday/interfaces/impact.py         (SPLIT-006)
- ReviewerExt           -- src/saturnday/interfaces/reviewer.py       (SPLIT-007)
- DocPostGlobalExt      -- src/saturnday/interfaces/doc_stages.py     (SPLIT-008)
- EvidenceAppender      -- src/saturnday/interfaces/evidence.py       (SPLIT-009)
- ReleaseGovernanceExt  -- src/saturnday/interfaces/release.py        (RS-015)
"""
from __future__ import annotations

from saturnday.interfaces.triage import TriageHook
from saturnday.interfaces.memory import MemoryProvider
from saturnday.interfaces.spec import SpecVerifierExt
from saturnday.interfaces.impact import ImpactAnalysisExt
from saturnday.interfaces.reviewer import ReviewerExt
from saturnday.interfaces.doc_stages import DocPostGlobalExt
from saturnday.interfaces.evidence import EvidenceAppender
from saturnday.interfaces.release import ReleaseGovernanceExt

__all__ = [
    "TriageHook",
    "MemoryProvider",
    "SpecVerifierExt",
    "ImpactAnalysisExt",
    "ReviewerExt",
    "DocPostGlobalExt",
    "EvidenceAppender",
    "ReleaseGovernanceExt",
]
