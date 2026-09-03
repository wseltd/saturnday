"""Document runner orchestrator for Saturnday Document Mode.

This is the main orchestrator for governed document generation — the
document-mode equivalent of ``ticket_runner.py`` for code mode.

Pipeline (in execution order):

1. Parse and validate doc-spec.yaml.
2. Generate document plan.
3. Create output directory structure.
4. For each section in plan order:
   a. Run with retry via :func:`~saturnday.document.section_retry.run_section_with_retry`.
   b. Persist partial summary (crash-safe).
   c. Log progress.
5. Run global cross-section checks.
6. Run claim analysis (extract + optionally verify).
7. Evaluate publishability.
8. Check sign-off gate.
9. Determine final document_status.
10. Write evidence pack.
11. Return :class:`~saturnday.document._types.DocumentRunResult`.

Error contract:
- Spec validation failure: ``ValueError`` raised immediately.
- Individual section failure: logged, section marked ``PROVISIONAL_UNVERIFIED``, run continues.
- Global check failure: findings logged, run continues.
- Claim verification failure: claims marked ``UNCERTAIN_REQUIRES_REVIEW``, run continues.
- Evidence pack write failure: logged, result returned without complete evidence path.
- ``KeyboardInterrupt``: partial state saved to progress log, then re-raised.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saturnday import capability_registry
from saturnday._types import CoderConfig
from saturnday.document._types import (
    DocumentApproval,
    DocumentClaim,
    DocumentFinding,
    DocumentPlan,
    DocumentRunResult,
    DocumentSection,
    DocumentSpec,
)

logger = logging.getLogger(__name__)

# Progress log filename written inside the output directory.
_PROGRESS_LOG_FILENAME = "document-progress.log"
# Partial run summary filename written after each section (crash-safe).
_PARTIAL_SUMMARY_FILENAME = "partial-run-summary.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_document(
    spec_path: str | Path,
    repo_path: str | Path,
    coder_config: CoderConfig,
    output_dir: str | Path | None = None,
) -> DocumentRunResult:
    """Execute a full governed document generation run.

    Args:
        spec_path: Path to the ``doc-spec.yaml`` file.
        repo_path: Repository root (used for source file resolution and
            plan artefact placement).
        coder_config: LLM backend configuration.
        output_dir: Base output directory for all artefacts.  Defaults to
            ``{repo_path}/.saturnday/document-run/``.

    Returns:
        :class:`~saturnday.document._types.DocumentRunResult` with the
        complete run outcome.

    Raises:
        ValueError: If ``spec_path`` does not exist, the spec fails
            mandatory validation, or source files are missing.
    """
    spec_path = Path(spec_path).resolve()
    repo_path = Path(repo_path).resolve()

    # Default output dir — unique per document run to prevent overwrite
    if output_dir is None:
        _ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        _pid = os.getpid()
        _rand = os.urandom(4).hex()
        _doc_run_id = f"docrun_{_ts}_{_pid}_{_rand}"
        _doc_parent = repo_path / ".saturnday" / "document-run"
        effective_output_dir = _doc_parent / _doc_run_id
        effective_output_dir.mkdir(parents=True, exist_ok=True)
        # Write latest pointer for operator convenience
        _doc_parent.mkdir(parents=True, exist_ok=True)
        _pointer = _doc_parent / "latest.json"
        try:
            _pointer.write_text(
                json.dumps({
                    "run_id": _doc_run_id,
                    "path": str(effective_output_dir),
                    "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    else:
        effective_output_dir = Path(output_dir).resolve()

    # -----------------------------------------------------------------------
    # Stage 1: Parse and validate spec
    # -----------------------------------------------------------------------
    try:
        from saturnday.document.spec_parser import parse_doc_spec, validate_doc_spec
    except ImportError as exc:
        raise ValueError(f"document module unavailable: {exc}") from exc

    spec = parse_doc_spec(spec_path)

    validation_errors = validate_doc_spec(spec, repo_path)
    if validation_errors:
        raise ValueError(
            "doc-spec validation failed:\n" + "\n".join(f"  - {e}" for e in validation_errors)
        )

    # -----------------------------------------------------------------------
    # Stage 2: Generate document plan
    # -----------------------------------------------------------------------
    try:
        from saturnday.document.planner import generate_document_plan
    except ImportError as exc:
        raise ValueError(f"document planner unavailable: {exc}") from exc

    plan = generate_document_plan(spec, repo_path, coder_config=coder_config)

    # -----------------------------------------------------------------------
    # Stage 3: Create output directory
    # -----------------------------------------------------------------------
    effective_output_dir.mkdir(parents=True, exist_ok=True)

    _log_document_progress(
        f"Document run started: {plan.document_id}  "
        f"sections={len(plan.sections)}  "
        f"risk={spec.risk_class}",
        effective_output_dir,
    )

    # Initialise the run result we will populate progressively.
    result = DocumentRunResult(
        document_id=plan.document_id,
        type=plan.type,
        total_sections=len(plan.sections),
    )

    # -----------------------------------------------------------------------
    # Stage 4: Execute sections
    # -----------------------------------------------------------------------
    try:
        from saturnday.document.section_retry import run_section_with_retry
    except ImportError as exc:
        raise ValueError(f"section_retry unavailable: {exc}") from exc

    section_results: list[dict] = []
    prior_sections: list[dict] = []

    try:
        for section in plan.sections:
            _log_document_progress(
                f"Section {section.section_id} ({section.name}): starting",
                effective_output_dir,
            )

            try:
                sr = run_section_with_retry(
                    section=section,
                    spec=spec,
                    plan=plan,
                    repo_path=repo_path,
                    coder_config=coder_config,
                    output_dir=effective_output_dir,
                    prior_sections=prior_sections if prior_sections else None,
                    max_retries=spec.max_retry_per_section,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Section %s failed unexpectedly: %s", section.section_id, exc
                )
                # Degrade gracefully: mark section provisional, continue.
                section.status = "PROVISIONAL_UNVERIFIED"
                sr = {
                    "section_id": section.section_id,
                    "status": "PROVISIONAL_UNVERIFIED",
                    "content_path": "",
                    "metadata_path": "",
                    "content": "",
                    "attempt": 1,
                    "retry_count": 0,
                    "findings": [],
                    "error": str(exc),
                    "name": section.name,
                }

            # Attach name to result dict for downstream checks
            sr["name"] = section.name

            section_results.append(sr)

            # Add to prior sections so next sections can reference this one
            if sr.get("content"):
                prior_sections.append({
                    "section_id": sr["section_id"],
                    "content": sr["content"],
                })

            # Update running counts on result object
            result.sections = list(plan.sections)
            result.passed = sum(
                1 for s in plan.sections if s.status in ("PASS", "WARN")
            )
            result.failed = sum(
                1 for s in plan.sections if s.status == "FAIL"
            )
            result.provisional = sum(
                1 for s in plan.sections if s.status == "PROVISIONAL_UNVERIFIED"
            )

            status_str = sr.get("status", "?")
            _log_document_progress(
                f"Section {section.section_id} ({section.name}): {status_str}",
                effective_output_dir,
            )

            # Crash-safe partial summary after each section.
            _write_partial_summary(result, section_results, effective_output_dir)

    except KeyboardInterrupt:
        _log_document_progress(
            "Document run interrupted by user (KeyboardInterrupt). "
            "Partial progress saved.",
            effective_output_dir,
        )
        _write_partial_summary(result, section_results, effective_output_dir)
        raise

    # -----------------------------------------------------------------------
    # Stage 5: Global checks
    # -----------------------------------------------------------------------
    global_findings: list[DocumentFinding] = []
    try:
        from saturnday.document.global_checks import run_global_checks

        global_findings = run_global_checks(section_results, plan)
        result.global_findings = global_findings
        _log_document_progress(
            f"Global checks complete: {len(global_findings)} finding(s)",
            effective_output_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Global checks failed: %s", exc)
        _log_document_progress(
            f"Global checks error (non-fatal): {exc}",
            effective_output_dir,
        )

    # -----------------------------------------------------------------------
    # Stage 6: Claim analysis
    # -----------------------------------------------------------------------
    claims: list[DocumentClaim] = []
    if capability_registry.is_available("doc_post_global"):
        try:
            from saturnday.document.claim_verifier import run_claim_analysis

            sections_content: dict[str, str] = {
                sr["section_id"]: sr.get("content", "")
                for sr in section_results
                if sr.get("content")
            }
            claims = run_claim_analysis(
                sections_content=sections_content,
                spec=spec,
                repo_path=repo_path,
                coder_config=coder_config,
            )
            result.claims = claims

            # Add findings for CONTRADICTED claims (error severity) and
            # UNSUPPORTED claims (warning severity).
            for claim in claims:
                verdict = claim.support_verdict
                if verdict == "CONTRADICTED":
                    global_findings.append(
                        DocumentFinding(
                            finding_id=f"DOC-CLAIM-{claim.claim_id}",
                            section_id=claim.section_id,
                            kind="contradicted_claim",
                            severity="error",
                            detail=(
                                f"Claim contradicted by evidence: {claim.claim_text[:200]}"
                            ),
                            fixable_by_regeneration=True,
                        )
                    )
                elif verdict == "UNSUPPORTED":
                    global_findings.append(
                        DocumentFinding(
                            finding_id=f"DOC-CLAIM-{claim.claim_id}",
                            section_id=claim.section_id,
                            kind="unsupported_claim",
                            severity="warning",
                            detail=(
                                f"Claim unsupported by evidence: {claim.claim_text[:200]}"
                            ),
                            fixable_by_regeneration=True,
                        )
                    )
                elif verdict == "UNCERTAIN_REQUIRES_REVIEW":
                    logger.info(
                        "Claim %s requires review: %s", claim.claim_id, claim.claim_text[:100]
                    )

            result.global_findings = global_findings
            _log_document_progress(
                f"Claim analysis complete: {len(claims)} claim(s) extracted",
                effective_output_dir,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Claim analysis failed (non-fatal): %s", exc)
            _log_document_progress(
                f"Claim analysis error (non-fatal): {exc}",
                effective_output_dir,
            )

    # -----------------------------------------------------------------------
    # Stage 7: Evaluate publishability
    # -----------------------------------------------------------------------
    approvals: list[DocumentApproval] = []
    publishable: bool = True
    if capability_registry.is_available("doc_post_global"):
        try:
            from saturnday.document.signoff import load_approvals_for_doc

            approvals = load_approvals_for_doc(plan.document_id, repo_path)
            result.approvals = approvals
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load approvals (non-fatal): %s", exc)

        try:
            from saturnday.document.provisional import evaluate_publishability

            publishable, pub_reasons = evaluate_publishability(
                section_results, spec, approvals
            )
            if pub_reasons:
                for reason in pub_reasons:
                    logger.info("Publishability: %s", reason)
        except Exception as exc:  # noqa: BLE001
            logger.error("Publishability evaluation failed (non-fatal): %s", exc)

    # -----------------------------------------------------------------------
    # Stage 8: Check sign-off gate
    # -----------------------------------------------------------------------
    signoff_complete: bool = not bool(spec.sign_off_roles)
    if capability_registry.is_available("doc_post_global"):
        try:
            from saturnday.document.signoff import check_signoff_requirements

            signoff_complete, missing_roles = check_signoff_requirements(
                plan.document_id, spec, approvals
            )
            if missing_roles:
                _log_document_progress(
                    f"Sign-off gate: missing roles: {', '.join(missing_roles)}",
                    effective_output_dir,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Sign-off check failed (non-fatal): %s", exc)
            signoff_complete = not bool(spec.sign_off_roles)

    # -----------------------------------------------------------------------
    # Stage 9: Determine final document status
    # -----------------------------------------------------------------------
    document_status = _determine_document_status(
        section_results=section_results,
        global_findings=global_findings,
        publishable=publishable,
        signoff_complete=signoff_complete,
        spec=spec,
    )
    result.document_status = document_status

    # Final counts
    result.sections = list(plan.sections)
    result.passed = sum(1 for s in plan.sections if s.status in ("PASS", "WARN"))
    result.failed = sum(1 for s in plan.sections if s.status == "FAIL")
    result.provisional = sum(
        1 for s in plan.sections if s.status == "PROVISIONAL_UNVERIFIED"
    )

    _log_document_progress(
        f"Document run complete: status={document_status}  "
        f"passed={result.passed}  "
        f"failed={result.failed}  "
        f"provisional={result.provisional}  "
        f"claims={len(claims)}  "
        f"global_findings={len(global_findings)}",
        effective_output_dir,
    )

    # -----------------------------------------------------------------------
    # Stage 10: Write evidence pack
    # -----------------------------------------------------------------------
    try:
        from saturnday.document.evidence_pack import write_document_evidence

        write_document_evidence(
            result=result,
            spec=spec,
            output_dir=effective_output_dir,
            spec_source_path=spec_path,
        )
        _log_document_progress(
            f"Evidence pack written to: {effective_output_dir}",
            effective_output_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Evidence pack write failed (non-fatal): %s", exc)
        _log_document_progress(
            f"Evidence pack write error (non-fatal): {exc}",
            effective_output_dir,
        )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _log_document_progress(msg: str, output_dir: Path) -> None:
    """Write a progress entry to the logger and append to the progress log file.

    The log file is created if absent.  Appends so that prior run history is
    preserved (crash-safe — no truncation at run start).

    Args:
        msg: Progress message.
        output_dir: Directory to write ``document-progress.log`` into.
    """
    logger.info("[document-run] %s", msg)

    try:
        log_path = output_dir / _PROGRESS_LOG_FILENAME
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry = f"[{timestamp}] {msg}\n"
        # Create parent dir if needed (it should already exist, but be safe).
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)
    except Exception as exc:  # noqa: BLE001
        # Progress log failure must never kill the pipeline.
        logger.warning("Could not write document progress log: %s", exc)


def _write_partial_summary(
    result: DocumentRunResult,
    section_results: list[dict],
    output_dir: Path,
) -> None:
    """Write a partial run summary after each section (crash-safe).

    The file is overwritten on each call so only the latest state is kept,
    matching the pattern used in code-mode ticket_runner.py.

    Args:
        result: Current (incomplete) run result.
        section_results: Section result dicts accumulated so far.
        output_dir: Evidence output directory.
    """
    try:
        summary: dict[str, Any] = {
            "document_id": result.document_id,
            "type": result.type,
            "document_status": result.document_status,
            "total_sections": result.total_sections,
            "sections_completed": len(section_results),
            "passed": result.passed,
            "failed": result.failed,
            "provisional": result.provisional,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "section_statuses": [
                {"section_id": sr.get("section_id"), "status": sr.get("status")}
                for sr in section_results
            ],
        }
        path = output_dir / _PARTIAL_SUMMARY_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write partial run summary: %s", exc)


def _determine_document_status(
    section_results: list[dict],
    global_findings: list[DocumentFinding],
    publishable: bool,
    signoff_complete: bool,
    spec: DocumentSpec,
) -> str:
    """Compute the final document status string.

    Precedence (highest to lowest):

    1. ``FAIL`` — any section has status ``"FAIL"`` OR any global finding
       has severity ``"error"``.
    2. ``BLOCKED_FOR_SIGNOFF`` — not publishable due to missing sign-offs
       (provisional sections with missing sign-offs, or all-pass but
       sign-off required and not yet obtained).
    3. ``PROVISIONAL_UNVERIFIED`` — provisional sections remain but document
       is technically publishable at this risk class.
    4. ``WARN`` — sections pass/warn, global warnings only, no sign-off gap.
    5. ``PASS`` — all sections pass, no global findings, sign-off satisfied
       or not required.

    Args:
        section_results: Section result dicts with ``status`` keys.
        global_findings: Cross-section :class:`~saturnday.document._types.DocumentFinding`
            objects.
        publishable: Result from :func:`~saturnday.document.provisional.evaluate_publishability`.
        signoff_complete: Whether all required sign-offs have been obtained.
        spec: Document spec (for risk class and sign-off roles).

    Returns:
        Status string matching :class:`~saturnday.document._types.DocumentStatus` values.
    """
    has_section_fail = any(sr.get("status") == "FAIL" for sr in section_results)
    has_global_error = any(
        (f.severity == "error" if hasattr(f, "severity") else f.get("severity") == "error")
        for f in global_findings
    )
    has_provisional = any(
        sr.get("status") == "PROVISIONAL_UNVERIFIED" for sr in section_results
    )
    has_section_warn = any(sr.get("status") == "WARN" for sr in section_results)
    has_global_warn = any(
        (f.severity == "warning" if hasattr(f, "severity") else f.get("severity") == "warning")
        for f in global_findings
    )
    needs_signoff = bool(spec.sign_off_roles)

    # Rule 1: Hard FAIL
    if has_section_fail or has_global_error:
        return "FAIL"

    # Rule 2: Blocked for sign-off (publishable=False AND missing sign-offs)
    if not publishable:
        return "BLOCKED_FOR_SIGNOFF"

    if needs_signoff and not signoff_complete:
        return "BLOCKED_FOR_SIGNOFF"

    # Rule 3: Provisional (publishable at this risk class but sections unverified)
    if has_provisional:
        risk_class = (spec.risk_class or "high").lower()
        if risk_class == "low":
            return "WARN"
        return "PROVISIONAL_UNVERIFIED"

    # Rule 4: Warn
    if has_section_warn or has_global_warn:
        return "WARN"

    # Rule 5: Pass
    return "PASS"
