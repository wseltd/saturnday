"""Release governance interface protocol (RS-015).

Defines the public contract that a premium release-governance implementation
must satisfy in order to be registered with the capability registry under the
``"release_governance"`` stage name.

Public orchestration code (``saturnday.release.orchestrator``) queries the
registry and degrades gracefully when no premium handler is registered.
Premium code imports this protocol, implements it, and calls
``capability_registry.register(STAGE_NAME, adapter_instance)`` at bootstrap.

Usage (public / registry side)::

    from saturnday.capability_registry import get, is_available
    from saturnday.interfaces.release import STAGE_NAME

    if is_available(STAGE_NAME):
        handler = get(STAGE_NAME)
        policy_result = handler.apply_policy(check_results, policy)
    else:
        # No premium governance — record in evidence and continue.
        policy_result = None

Usage (premium / implementation side)::

    from saturnday.interfaces.release import ReleaseGovernanceExt, STAGE_NAME
    from saturnday import capability_registry

    class ReleaseGovernanceAdapter:
        def apply_policy(self, check_results, policy):
            ...
        def approve(self, args):
            ...
        def record_exception(self, exception, evidence_dir):
            ...

    capability_registry.register(STAGE_NAME, ReleaseGovernanceAdapter())
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ReleaseGovernanceExt", "STAGE_NAME"]

#: Registry key for the release-governance stage.
STAGE_NAME: str = "release_governance"


@runtime_checkable
class ReleaseGovernanceExt(Protocol):
    """Protocol for premium release governance capabilities.

    A conforming object is registered under ``"release_governance"`` in the
    capability registry.  The public orchestrator calls :meth:`apply_policy`
    after the base check results have been computed.  The ``release-approve``
    CLI stub delegates to :meth:`approve`.  Exception recording is handled
    via :meth:`record_exception`.

    Premium absence degrades gracefully: the orchestrator skips this stage
    and records it in the evidence pack's ``skipped_premium_stages`` field
    (golden rule).
    """

    def apply_policy(self, check_results: list[Any], policy: dict) -> list[Any]:
        """Apply org-level release policy to check results.

        Evaluates an org-level policy manifest against the existing check
        results and returns an updated (or extended) list.  The implementation
        may add new :class:`~saturnday.release.evidence.ReleaseCheckResult`
        instances for each policy rule evaluated, or it may return the input
        list unchanged if the policy passes without new findings.

        The implementation must never remove existing check results — it is
        additive only.

        Args:
            check_results: Current list of
                :class:`~saturnday.release.evidence.ReleaseCheckResult` from
                the public check pipeline.  Must be returned intact (plus any
                additions) in the output list.
            policy: Parsed org-level release policy dict.  Structure is an
                internal premium concern; the public orchestrator treats it
                as opaque.

        Returns:
            Updated list of check results.  Must contain all entries from the
            input *check_results* plus any policy-specific results.
        """
        ...

    def approve(self, args: Any) -> int:
        """Handle the ``release-approve`` CLI command.

        Called by the ``release-approve`` CLI stub in ``cli.py`` when a
        premium handler is registered.  The implementation records the
        approval (approver identity, timestamp, artefact hash) in the
        evidence directory and enforces any minimum-approvers policy.

        Args:
            args: Parsed :class:`argparse.Namespace` from the
                ``release-approve`` subparser.  Expected attributes include
                ``evidence_dir``, ``approver``, ``notes``, and ``signoff_id``.

        Returns:
            Exit code for the CLI process.  ``0`` on success; ``1`` on
            policy violation or error.
        """
        ...

    def record_exception(self, exception: dict, evidence_dir: Any) -> None:
        """Record a release exception with an immutable audit trail.

        Writes a signed exception record to the evidence directory.  The
        record includes the exception reason, approver, timestamp, and the
        artefact SHA-256 at time of exception.  Exceptions do not suppress
        findings — they annotate them for downstream review.

        Args:
            exception: Exception record dict with at minimum:
                ``reason`` (str), ``approver`` (str), and
                ``artefact_sha256`` (str).  Additional fields are
                implementation-defined.
            evidence_dir: Path-like object pointing to the release evidence
                directory for this run.

        Returns:
            ``None``.  Raises :exc:`OSError` if the record cannot be written.
        """
        ...
