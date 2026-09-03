"""DocPostGlobalExt protocol for the Saturnday public/premium split.

Defines the exact call signatures that a premium document post-global stage
implementation must satisfy in order to be registered with the capability
registry under the key ``"doc_post_global"``.

Premium implementations derive from the following modules:

- ``extract_claims`` --
  ``saturnday.document.claim_verifier.run_claim_analysis(sections_content,
  spec, repo_path, coder_config) -> list[DocumentClaim]``
- ``verify_claims`` --
  ``saturnday.document.claim_verifier.verify_claims(claims, source_contents,
  coder_config, repo_path) -> list[DocumentClaim]``
- ``evaluate_publishability`` --
  ``saturnday.document.provisional.evaluate_publishability(section_results,
  spec, approvals) -> tuple[bool, list[str]]``
- ``check_signoff`` --
  ``saturnday.document.signoff.check_signoff_requirements(document_id, spec,
  approvals) -> tuple[bool, list[str]]``

At protocol level, return types use ``list[dict]`` rather than ``DocumentClaim``
because the protocol defines the public contract, not the internal schema.
Premium implementations map dataclass results to dicts before returning.

``spec`` and ``coder_config`` are typed as ``Any`` throughout to avoid coupling
this protocol to internal dataclasses from the document or run layers.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["DocPostGlobalExt", "STAGE_NAME"]

logger = logging.getLogger(__name__)

STAGE_NAME: str = "doc_post_global"


@runtime_checkable
class DocPostGlobalExt(Protocol):
    """Protocol for the premium document post-global stage.

    An object satisfying this protocol can be registered with the capability
    registry under the key ``"doc_post_global"`` and will be invoked by the
    document orchestrator to perform claim extraction/verification, signoff
    gating, and publishability evaluation across the full document.

    All four methods are required; none has a default implementation so that
    partial conformance is a clear protocol failure rather than a silent no-op.

    Return types use ``list[dict]`` / ``tuple[bool, list[str]]`` at this
    boundary.  Premium may use richer internal types but must serialise to
    dicts before returning to the public orchestrator.
    """

    def extract_claims(
        self,
        sections_content: dict[str, str],
        spec: Any,
        repo_path: Path,
        coder_config: Any,
    ) -> list[dict]:
        """Extract verifiable claims from all section content.

        Args:
            sections_content: Mapping of section name to rendered text.
            spec:             Document specification (``DocumentSpec`` in the
                              premium implementation; ``Any`` here to avoid
                              coupling).
            repo_path:        Absolute path to the repository root.
            coder_config:     Coder configuration object; typed ``Any`` to
                              avoid coupling this protocol to the run-layer
                              dataclass.

        Returns:
            A list of claim dicts.  Each dict contains at minimum:
            ``{"claim_id": str, "text": str, "section": str}``.
        """
        ...  # pragma: no cover

    def verify_claims(
        self,
        claims: list[dict],
        source_contents: dict[str, str],
        coder_config: Any,
        repo_path: Path,
    ) -> list[dict]:
        """Verify each claim against source evidence.

        Args:
            claims:          List of claim dicts produced by :meth:`extract_claims`.
            source_contents: Mapping of source-file path to its text content.
            coder_config:    Coder configuration object.
            repo_path:       Absolute path to the repository root.

        Returns:
            A list of verified claim dicts, each extended with a
            ``"verdict"`` key (``"PASS"``, ``"FAIL"``, or ``"UNVERIFIABLE"``)
            and a ``"rationale"`` key.
        """
        ...  # pragma: no cover

    def evaluate_publishability(
        self,
        section_results: list[dict],
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        """Evaluate whether the document is publishable given current results.

        Args:
            section_results: List of per-section result dicts, each containing
                             at minimum ``{"section": str, "passed": bool}``.
            spec:            Document specification.
            approvals:       List of approval records already collected.

        Returns:
            A two-tuple: ``(publishable: bool, blocking_reasons: list[str])``.
            ``publishable`` is ``True`` only when all policy gates are met.
            ``blocking_reasons`` is empty when publishable; non-empty when
            there are outstanding blockers.
        """
        ...  # pragma: no cover

    def check_signoff(
        self,
        document_id: str,
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        """Check whether all required signoffs have been collected.

        Args:
            document_id: Identifier for the document being checked.
            spec:        Document specification (used to derive required roles
                         and approver counts).
            approvals:   List of approval records already collected.  Each dict
                         contains at minimum ``{"approver": str, "role": str}``.

        Returns:
            A two-tuple: ``(satisfied: bool, missing: list[str])``.
            ``satisfied`` is ``True`` when all required signoffs are present.
            ``missing`` lists the outstanding roles or approvers that must still
            sign off; empty when ``satisfied`` is ``True``.
        """
        ...  # pragma: no cover
