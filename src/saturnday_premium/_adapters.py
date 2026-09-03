"""Adapter classes for saturnday-premium capability registration.

Each adapter class wraps a concrete premium module function and exposes the
method signatures required by the corresponding Protocol defined in
``saturnday.interfaces``.

These were moved from ``saturnday.premium_registration`` in SPLIT-037.
The internal imports (e.g. ``from saturnday.security_triage import ...``)
still reference the public ``saturnday`` package, which retains all premium
module implementations until the repo split in Step 10+.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Any

__all__ = [
    "_TriageAdapter",
    "_MemoryAdapter",
    "_SpecVerifierAdapter",
    "_ImpactAdapter",
    "_ReviewerAdapter",
    "_DocPostGlobalAdapter",
    "_EvidenceAppenderAdapter",
    "_ReleaseGovernanceAdapter",
]

_logger = logging.getLogger(__name__)


class _TriageAdapter:
    """TriageHook adapter wrapping ``security_triage.triage_security_findings``."""

    def filter_findings(
        self,
        findings: list[dict],
        repo_path: Path,
        coder_config: Any,
    ) -> list[dict]:
        """Delegate to ``triage_security_findings``."""
        from saturnday.security_triage import triage_security_findings

        return triage_security_findings(findings, repo_path, coder_config)


class _MemoryAdapter:
    """MemoryProvider adapter wrapping memory_retrieval + memory_enforcement."""

    def retrieve(
        self,
        conn: Any,  # sqlite3.Connection
        ticket: Any,
        repo_path: Path,
        top_k: int = 5,
    ) -> list[dict]:
        """Filter and rank memory items for *ticket*."""
        from saturnday.run.memory_retrieval import filter_relevant_items, rank_and_select

        items = filter_relevant_items(conn, ticket)
        ranked = rank_and_select(items, ticket, limit=top_k, conn=conn)
        return [dataclasses.asdict(i) if dataclasses.is_dataclass(i) else dict(i) for i in ranked]

    def enforce(
        self,
        conn: Any,  # sqlite3.Connection
        changed_files: list[str],
        repo_path: Path,
    ) -> list[dict]:
        """Check enforced rules against changed files."""
        from saturnday.run.memory_enforcement import check_enforced_rules

        return check_enforced_rules(conn, changed_files, repo_path)

    def cleanup(
        self,
        conn: Any,  # sqlite3.Connection
        repo_path: Path,
    ) -> int:
        """Expire stale memory entries; return count removed."""
        from saturnday.run.memory_retrieval import run_staleness_cleanup

        return run_staleness_cleanup(conn, repo_path)


class _SpecVerifierAdapter:
    """SpecVerifierExt adapter wrapping spec_verifier + property_tests + dataflow_checker."""

    def generate_assertions(
        self,
        ticket: Any,
        repo_path: Path,
    ) -> list[dict]:
        """Generate assertion dicts from ticket acceptance criteria."""
        from saturnday.run.spec_verifier import generate_spec_assertions

        # generate_spec_assertions takes (ticket, changed_files, repo_path).
        # At protocol boundary changed_files is derived from ticket scope.
        changed_files: list[str] = list(getattr(getattr(ticket, "scope", None), "allowed_globs", []) or [])
        return generate_spec_assertions(ticket, changed_files, repo_path)

    def run_assertions(
        self,
        assertions: list[dict],
        repo_path: Path,
    ) -> list[dict]:
        """Execute assertion dicts; return pass/fail results."""
        from saturnday.run.spec_verifier import run_spec_assertions

        return run_spec_assertions(assertions, repo_path)

    def run_property_tests(
        self,
        changed_files: list[str],
        repo_path: Path,
    ) -> list[dict]:
        """Identify targets, generate property tests, and execute them."""
        from saturnday.run.property_tests import (
            generate_property_tests,
            identify_eligible_targets,
            run_property_tests,
        )

        targets = identify_eligible_targets(changed_files, repo_path)
        if not targets:
            return []
        test_content = generate_property_tests(targets, repo_path)
        return run_property_tests(test_content, repo_path)

    def check_dataflow(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> list[dict]:
        """Detect cross-function data flow mismatches in changed files."""
        from saturnday.run.dataflow_checker import check_cross_function_flow

        return check_cross_function_flow(changed_files, repo_path, state)


class _ImpactAdapter:
    """ImpactAnalysisExt adapter wrapping impact_analysis functions.

    The adapter caches the last raw ``ImpactReport`` returned by
    ``compute_impact`` so that ``select_verification_layers`` can receive
    the original dataclass without lossy dict-roundtripping.  Typical usage
    calls ``compute_impact`` immediately before ``select_verification_layers``
    in the same request context, so the one-item cache is sufficient.
    """

    def __init__(self) -> None:
        self._last_report: Any = None

    def compute_impact(
        self,
        changed_files: list[str],
        repo_path: Path,
        state: Any,
    ) -> dict:
        """Compute blast radius; cache raw report; return as dict."""
        from saturnday.run.impact_analysis import compute_impact

        result = compute_impact(changed_files, repo_path, state)
        self._last_report = result  # cache for select_verification_layers
        if dataclasses.is_dataclass(result):
            return dataclasses.asdict(result)
        return dict(result)  # type: ignore[call-overload]

    def select_verification_layers(
        self,
        impact: dict,
        ticket: Any,
    ) -> dict[str, bool]:
        """Decide which verification layers should run.

        Uses the cached raw ``ImpactReport`` when available (same call
        sequence as ``ticket_runner.py``).  Falls back to a default
        all-layers-enabled dict when no cached report is present.
        """
        from saturnday.run.impact_analysis import select_verification_layers

        report = self._last_report if self._last_report is not None else impact
        return select_verification_layers(report, ticket)


class _ReviewerAdapter:
    """ReviewerExt adapter wrapping ``invoke_role("code_reviewer", ...)``.

    ``invoke_role`` is a PUBLIC function, but calling it with the
    ``"code_reviewer"`` role is a PREMIUM feature.  This adapter is the
    single place that performs that binding.
    """

    def review(
        self,
        task: str,
        coder_config: Any,
        repo_path: Path,
    ) -> dict:
        """Run a code review; return ``{success, output, has_concerns}``."""
        from saturnday.role_modes import invoke_role

        result = invoke_role(
            "code_reviewer",
            task,
            coder_config=coder_config,
            repo_path=repo_path,
        )
        return {
            "success": bool(getattr(result, "success", False)),
            "output": str(getattr(result, "output", "")),
            "has_concerns": bool(getattr(result, "has_concerns", False)),
        }


class _DocPostGlobalAdapter:
    """DocPostGlobalExt adapter wrapping claim_verifier + signoff + provisional."""

    def extract_claims(
        self,
        sections_content: dict[str, str],
        spec: Any,
        repo_path: Path,
        coder_config: Any,
    ) -> list[dict]:
        """Extract and verify claims; return as list of dicts."""
        from saturnday.document.claim_verifier import run_claim_analysis

        claims = run_claim_analysis(sections_content, spec, repo_path, coder_config)
        return [dataclasses.asdict(c) if dataclasses.is_dataclass(c) else dict(c) for c in claims]

    def verify_claims(
        self,
        claims: list[dict],
        source_contents: dict[str, str],
        coder_config: Any,
        repo_path: Path,
    ) -> list[dict]:
        """Verify claims against source evidence; return enriched dicts."""
        from saturnday.document._types import DocumentClaim
        from saturnday.document.claim_verifier import verify_claims

        # Re-hydrate dicts into DocumentClaim objects expected by verify_claims.
        claim_objects = [DocumentClaim(**c) if not isinstance(c, DocumentClaim) else c for c in claims]
        verified = verify_claims(claim_objects, source_contents, coder_config, repo_path)
        return [dataclasses.asdict(c) if dataclasses.is_dataclass(c) else dict(c) for c in verified]

    def evaluate_publishability(
        self,
        section_results: list[dict],
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        """Evaluate whether the document is publishable."""
        from saturnday.document.provisional import evaluate_publishability
        from saturnday.document.signoff import DocumentApproval

        approval_objects = [
            DocumentApproval(**a) if not isinstance(a, DocumentApproval) else a for a in approvals
        ]
        return evaluate_publishability(section_results, spec, approval_objects)

    def check_signoff(
        self,
        document_id: str,
        spec: Any,
        approvals: list[dict],
    ) -> tuple[bool, list[str]]:
        """Check whether all required signoffs have been collected."""
        from saturnday.document.signoff import DocumentApproval, check_signoff_requirements

        approval_objects = [
            DocumentApproval(**a) if not isinstance(a, DocumentApproval) else a for a in approvals
        ]
        return check_signoff_requirements(document_id, spec, approval_objects)


class _ReleaseGovernanceAdapter:
    """Adapter for the :class:`~saturnday.interfaces.release.ReleaseGovernanceExt` protocol.

    Implements org policy evaluation (RS-016), two-person signoff (RS-017),
    and exception recording (RS-027) via the corresponding premium modules.
    Registered under ``"release_governance"`` in the capability registry.
    """

    def apply_policy(
        self,
        check_results: list,
        policy: dict,
        artefact_path: str | None = None,
        artefact_sha256: str | None = None,
        evidence_dir: Any = None,
    ) -> list:
        """Apply org policy merged with *policy* to *check_results*.

        Loads the org-level policy from ``~/.saturnday/release-policy.yaml``,
        merges it with the manifest-supplied *policy* (manifest takes
        precedence on collisions), then delegates to
        :func:`~saturnday_premium._release_policy.evaluate_release_policy`.

        When ``require_provenance: true`` is set in the merged policy,
        provenance checks (RS-019,
        :func:`~saturnday_premium._provenance.evaluate_provenance`) are run
        and their result is appended to *check_results* before policy
        evaluation so that ``required_checks`` assertions can reference
        ``REL-PROV-001``.

        Args:
            check_results:   Existing check results from the public pipeline.
            policy:          Manifest-supplied policy dict (may be empty).
            artefact_path:   Optional path to the artefact — required for
                             provenance checks.  When absent and
                             ``require_provenance`` is True, a WARN result is
                             emitted instead.
            artefact_sha256: Optional SHA-256 of the artefact file.
            evidence_dir:    Optional evidence directory for attestation
                             auto-discovery.

        Returns:
            Input check results plus any provenance, policy, and registry results.
        """
        from saturnday_premium._release_policy import (
            evaluate_release_policy,
            load_release_policy,
        )
        from saturnday_premium._release_registry_checks import evaluate_registry_policy

        org_policy = load_release_policy()
        # Manifest policy takes precedence over org defaults.
        merged: dict = {**org_policy, **(policy or {})}
        results = list(check_results)

        # ------------------------------------------------------------------
        # RS-019: Provenance / attestation checks.
        # Run before policy evaluation so REL-PROV-001 is visible to
        # ``required_checks`` policy assertions.
        # ------------------------------------------------------------------
        require_provenance: bool = bool(merged.get("require_provenance", False))
        if require_provenance or (artefact_path and artefact_sha256):
            if artefact_path and artefact_sha256:
                try:
                    from saturnday_premium._provenance import evaluate_provenance

                    prov_result = evaluate_provenance(
                        artefact_path=artefact_path,
                        artefact_sha256=artefact_sha256,
                        evidence_dir=Path(evidence_dir) if evidence_dir else None,
                        policy=merged,
                    )
                    results.append(prov_result)
                    _logger.debug(
                        "Provenance check completed: status=%s", prov_result.status
                    )
                except Exception as exc:
                    _logger.warning(
                        "Provenance check raised an exception: %s", exc
                    )
            else:
                _logger.debug(
                    "Provenance check requested via policy but artefact_path/sha256"
                    " not provided — skipped"
                )

        # ------------------------------------------------------------------
        # RS-016: Org policy evaluation.
        # ------------------------------------------------------------------
        results = evaluate_release_policy(results, merged)

        # ------------------------------------------------------------------
        # RS-018: Registry-specific metadata quality checks.
        # ------------------------------------------------------------------
        inventory_metadata = merged.get("_inventory_metadata", {})
        artefact_type = merged.get("_artefact_type", "python")
        registry_result = evaluate_registry_policy(inventory_metadata, artefact_type)
        if registry_result.findings:
            results.append(registry_result)

        return results

    def approve(self, args: Any) -> int:
        """Handle the ``release-approve`` CLI command.

        Reads the artefact SHA-256 from ``evidence_dir/evidence.json`` and
        creates a signoff record.

        Args:
            args: Parsed :class:`argparse.Namespace` or dict.  Must provide
                ``evidence`` (or ``evidence_dir``) and ``approver``.

        Returns:
            ``0`` on success, ``1`` on error.
        """
        from saturnday_premium._release_signoff import create_signoff

        import json as _json

        # Support both Namespace and dict argument styles.
        def _get(key: str, fallback: str = "") -> Any:
            if isinstance(args, dict):
                return args.get(key, fallback)
            return getattr(args, key, fallback)

        evidence_raw = _get("evidence") or _get("evidence_dir")
        approver = _get("approver")
        notes = _get("notes", "")

        if not evidence_raw:
            _logger.error("approve: no evidence_dir provided in args")
            return 1
        if not approver:
            _logger.error("approve: no approver provided in args")
            return 1

        evidence_dir = Path(evidence_raw)
        evidence_json = evidence_dir / "evidence.json"
        if not evidence_json.is_file():
            _logger.error("approve: evidence.json not found in %s", evidence_dir)
            return 1

        try:
            data = _json.loads(evidence_json.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError) as exc:
            _logger.error("approve: cannot read evidence.json: %s", exc)
            return 1

        artefact_sha256 = data.get("artefact_sha256", "")
        if not artefact_sha256:
            _logger.warning("approve: artefact_sha256 not present in evidence.json — using empty")

        try:
            record = create_signoff(evidence_dir, artefact_sha256, approver, notes)
        except (ValueError, OSError) as exc:
            _logger.error("approve: failed to create signoff: %s", exc)
            return 1

        print("Release approval recorded:")
        print(f"  Signoff ID:  {record.signoff_id}")
        print(f"  Approver:    {record.approver}")
        print(f"  Artefact:    {record.artefact_sha256[:16]}...")
        print(f"  Approved at: {record.approved_at}")
        print(f"  Evidence:    {evidence_dir / 'release' / 'signoffs' / (record.signoff_id + '.json')}")
        return 0

    def record_exception(
        self,
        exception_dict: dict,
        evidence_dir: Any,
    ) -> int:
        """Record a release exception with an immutable audit trail.

        Delegates to
        :func:`~saturnday_premium._release_exception.record_exception`.

        Args:
            exception_dict: Exception parameters.  Recognised keys:
                ``rule_ids`` (list), ``rule`` (str, fallback for single rule),
                ``file_patterns`` (list), ``approver`` (str), ``reason`` (str),
                ``expires`` / ``expiry`` (str), ``artefact_sha256`` (str).
            evidence_dir: Path-like pointing to the release evidence directory.

        Returns:
            Exit code: 0 on success.

        Raises:
            OSError: If the record cannot be written.
        """
        from saturnday_premium._release_exception import record_exception as _record_exc

        evidence_path = Path(evidence_dir)
        rule_ids: list[str] = exception_dict.get(
            "rule_ids",
            [exception_dict["rule"]] if "rule" in exception_dict else [],
        )
        expiry = exception_dict.get("expires") or exception_dict.get("expiry")

        record = _record_exc(
            evidence_dir=evidence_path,
            rule_ids=rule_ids,
            file_patterns=exception_dict.get("file_patterns", []),
            approver=exception_dict.get("approver", ""),
            reason=exception_dict.get("reason", ""),
            expiry=expiry,
            artefact_sha256=exception_dict.get("artefact_sha256", ""),
        )

        print("Exception recorded:")
        print(f"  Exception ID: {record.exception_id}")
        print(f"  Rules:        {', '.join(record.rule_ids)}")
        print(f"  Approver:     {record.approver}")
        print(f"  Reason:       {record.reason}")
        print(f"  Expiry:       {record.expiry or 'none'}")
        print(f"  Evidence:     {evidence_path / 'release' / 'exceptions' / (record.exception_id + '.json')}")
        return 0


class _EvidenceAppenderAdapter:
    """EvidenceAppender adapter — merges premium fields into the base pack.

    The premium edition may replace this with a richer implementation that
    pulls in triage rationale, memory items, impact reports, claim verdicts,
    etc.  This adapter provides a safe additive baseline that satisfies the
    protocol.
    """

    def append(
        self,
        base_pack: dict,
        artifacts: dict,
    ) -> dict:
        """Append *artifacts* to *base_pack* without overwriting base keys."""
        merged = dict(base_pack)
        for key, value in artifacts.items():
            if key not in merged:
                merged[key] = value
        return merged
