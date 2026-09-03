"""Document evidence pack writer.

Assembles and writes the complete evidence artefact for a governed document
run.  Reuses the existing evidence directory pattern established by
``saturnday.run.evidence`` — one canonical ``.saturnday/document-run/``
directory per run with deterministic sub-paths.

Directory layout produced::

    {output_dir}/
      doc-spec.yaml           # verbatim copy of the input spec file (if path known)
      plan.json               # verbatim copy of the document plan JSON
      run-summary.json        # top-level run outcome
      final-document.md       # assembled full document from all section drafts
      sections/
        {section_id}/
          section.md          # generated section content
          metadata.json       # section metadata (attempt, sources, status, …)
      findings/
        local.json            # per-section findings list
        global.json           # cross-section findings list
      claims/
        claims.json           # all extracted and verified claims
      approvals.json          # sign-off records collected
      ARTEFACT_HASH.txt       # SHA-256 of final-document.md

The caller is responsible for populating ``DocumentRunResult`` before calling
``write_document_evidence``.  This module only writes; it does not run checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saturnday.capability_registry import is_available
from saturnday.document._types import DocumentRunResult, DocumentSpec
from saturnday.shared.evidence_schema import (
    EVIDENCE_DIR_DOCUMENT,  # noqa: F401
    build_capability_state,
    build_skipped_stages,
)

logger = logging.getLogger(__name__)

# Canonical sub-directory name for document evidence
_DOCUMENT_RUN_DIR = "document-run"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """Recursively convert dataclass instances to dicts for JSON serialisation."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _write_json(path: Path, data: Any) -> None:
    """Write *data* as indented JSON to *path*, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    logger.debug("Wrote evidence file: %s", path)


def _assemble_final_document(result: DocumentRunResult, output_dir: Path) -> str:
    """Assemble all section drafts into a single Markdown document.

    Reads generated ``section.md`` files from ``output_dir/sections/`` in
    section order.  Falls back to the section name as a heading when no draft
    file exists for that section.

    Args:
        result: The completed document run result.
        output_dir: Base evidence output directory.

    Returns:
        Assembled full-document Markdown string.
    """
    parts: list[str] = []
    for section in result.sections:
        section_md_path = output_dir / "sections" / section.section_id / "section.md"
        if section_md_path.is_file():
            content = section_md_path.read_text(encoding="utf-8", errors="replace")
            parts.append(content.strip())
        else:
            # Placeholder when section was never generated
            parts.append(f"## {section.name}\n\n*(Section not generated)*")
    return "\n\n---\n\n".join(parts)


def _sha256_of(text: str) -> str:
    """Return the hex SHA-256 digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_document_evidence(
    result: DocumentRunResult,
    spec: DocumentSpec,
    output_dir: Path,
    spec_source_path: Path | None = None,
    plan_source_path: Path | None = None,
) -> Path:
    """Write the complete evidence pack for a governed document run.

    Assembles all artefacts into ``output_dir`` (creating it if necessary).
    This function is idempotent — re-running it overwrites existing files.

    Args:
        result: The completed DocumentRunResult.
        spec: The DocumentSpec used for this run.
        output_dir: Base output directory.  Typically
            ``{repo}/.saturnday/document-run/{doc_id}/``.
        spec_source_path: Optional path to the original ``doc-spec.yaml``.
            When provided, a verbatim copy is written to the evidence dir.
        plan_source_path: Optional path to the ``plan.json`` that was loaded.
            When provided, a verbatim copy is written to the evidence dir.

    Returns:
        Path to the evidence directory (same as ``output_dir``).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "Writing document evidence pack for %s to %s",
        result.document_id, output_dir,
    )

    # ------------------------------------------------------------------
    # 1. Copy spec and plan files verbatim
    # ------------------------------------------------------------------
    if spec_source_path is not None and spec_source_path.is_file():
        dest = output_dir / "doc-spec.yaml"
        shutil.copy2(str(spec_source_path), str(dest))
        logger.debug("Copied spec: %s", dest)
    else:
        # Write a serialised representation so the pack is self-contained
        _write_json(output_dir / "doc-spec.json", _serialize(spec))

    if plan_source_path is not None and plan_source_path.is_file():
        dest = output_dir / "plan.json"
        shutil.copy2(str(plan_source_path), str(dest))
        logger.debug("Copied plan: %s", dest)

    # ------------------------------------------------------------------
    # 2. Section drafts and metadata
    # ------------------------------------------------------------------
    # Section content files (section.md / metadata.json) are already written
    # by the section_runner during execution.  We verify they exist and log
    # any gaps — we do NOT regenerate content here.
    for section in result.sections:
        section_dir = output_dir / "sections" / section.section_id
        section_dir.mkdir(parents=True, exist_ok=True)
        md_path = section_dir / "section.md"
        meta_path = section_dir / "metadata.json"
        if not md_path.is_file():
            logger.warning(
                "Section draft missing: %s (status=%s)", md_path, section.status
            )
        if not meta_path.is_file():
            # Write a minimal metadata stub so the pack is complete
            stub: dict[str, Any] = {
                "section_id": section.section_id,
                "name": section.name,
                "status": section.status,
                "retry_count": section.retry_count,
                "findings_count": len(section.findings),
            }
            _write_json(meta_path, stub)

    # ------------------------------------------------------------------
    # 3. Findings
    # ------------------------------------------------------------------
    findings_dir = output_dir / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    # Local findings — aggregated from all section findings lists
    local_findings: list[dict[str, Any]] = []
    for section in result.sections:
        for f in section.findings:
            entry = dict(f) if isinstance(f, dict) else _serialize(f)
            entry.setdefault("section_id", section.section_id)
            local_findings.append(entry)

    _write_json(findings_dir / "local.json", local_findings)

    # Global findings — from cross-section checks
    global_findings = [_serialize(f) for f in result.global_findings]
    _write_json(findings_dir / "global.json", global_findings)

    # ------------------------------------------------------------------
    # 4. Claims
    # ------------------------------------------------------------------
    if result.claims:
        claims_dir = output_dir / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        claims_data = [_serialize(c) for c in result.claims]
        _write_json(claims_dir / "claims.json", claims_data)

    # ------------------------------------------------------------------
    # 5. Approvals
    # ------------------------------------------------------------------
    approvals_data = [_serialize(a) for a in result.approvals]
    _write_json(output_dir / "approvals.json", approvals_data)

    # ------------------------------------------------------------------
    # 6. Assemble final document and compute artefact hash
    # ------------------------------------------------------------------
    final_text = _assemble_final_document(result, output_dir)
    final_doc_path = output_dir / "final-document.md"
    final_doc_path.write_text(final_text + "\n", encoding="utf-8")
    logger.info("Wrote final document: %s (%d chars)", final_doc_path, len(final_text))

    artefact_hash = _sha256_of(final_text)
    hash_path = output_dir / "ARTEFACT_HASH.txt"
    hash_path.write_text(f"sha256:{artefact_hash}\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # 7. Run summary
    # ------------------------------------------------------------------
    summary: dict[str, Any] = {
        "document_id": result.document_id,
        "type": result.type,
        "document_status": result.document_status,
        "total_sections": result.total_sections,
        "passed": result.passed,
        "failed": result.failed,
        "provisional": result.provisional,
        "local_findings_count": len(local_findings),
        "global_findings_count": len(global_findings),
        "claims_count": len(result.claims),
        "approvals_count": len(result.approvals),
        "artefact_hash": f"sha256:{artefact_hash}",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spec_risk_class": spec.risk_class,
        "spec_sign_off_roles": spec.sign_off_roles,
    }

    # Capability state — additive fields; existing fields are not modified.
    _cap_state = build_capability_state()
    summary["premium_capabilities_enabled"] = _cap_state["premium_capabilities_enabled"]
    summary["available_premium_hooks"] = _cap_state["available_premium_hooks"]
    summary["skipped_premium_stages"] = build_skipped_stages()

    # Document-mode per-hook availability
    summary["claim_verification_available"] = is_available("doc_post_global")
    summary["publishability_evaluated"] = is_available("doc_post_global")
    summary["premium_evidence"] = is_available("evidence_appender")

    _write_json(output_dir / "run-summary.json", summary)
    logger.info(
        "Document evidence pack complete: %s (status=%s)",
        output_dir, result.document_status,
    )

    return output_dir
