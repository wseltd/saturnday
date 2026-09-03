"""Document plan generator for Saturnday Document Mode.

Takes a ``DocumentSpec`` and produces a ``DocumentPlan`` with one
``DocumentSection`` per required section, pre-populated acceptance criteria
and check lists, and a set of global cross-section checks.

The plan is serialised to ``.saturnday/document/plan.json`` so that a
crashed run can be resumed without re-planning.

Usage::

    from pathlib import Path
    from saturnday.document.planner import generate_document_plan
    from saturnday.document.spec_parser import parse_doc_spec

    spec = parse_doc_spec(Path("doc-spec.yaml"))
    plan = generate_document_plan(spec, repo_path=Path("."))
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from saturnday.document._types import DocumentPlan, DocumentSection, DocumentSpec

if TYPE_CHECKING:
    from saturnday._types import CoderConfig

logger = logging.getLogger(__name__)

from saturnday import capability_registry

# Checks run on every section.
_SECTION_CHECKS = [
    "structure",
    "placeholder_detection",
    "citation_existence",
    "evidence_coverage",
]

# Checks run once across all sections after generation completes.
_GLOBAL_CHECKS = [
    "cross_section_consistency",
    "duplicate_metric_consistency",
    "summary_vs_body_consistency",
]


def _section_id(index: int) -> str:
    """Return a zero-padded section identifier, e.g. ``"S001"``."""
    return f"S{index:03d}"


def _derive_purpose(doc_purpose: str, section_name: str) -> str:
    """Build a section-level purpose statement from the document purpose.

    Args:
        doc_purpose: Top-level document purpose string from the spec.
        section_name: Name of this individual section.

    Returns:
        A short purpose statement for the section.
    """
    return f"{section_name}: {doc_purpose}"


def _acceptance_criteria_from_policy(claim_policy: dict) -> list[str]:
    """Derive acceptance criteria lines from the claim_policy dict.

    Each truthy boolean policy key becomes a criterion.  Non-boolean values
    become a descriptive criterion with the configured value.

    Args:
        claim_policy: The ``claim_policy`` dict from the ``DocumentSpec``.

    Returns:
        List of criterion strings (may be empty if policy is empty).
    """
    criteria: list[str] = []
    for key, value in claim_policy.items():
        if isinstance(value, bool):
            if value:
                criteria.append(key.replace("_", " ").capitalize())
        else:
            criteria.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    return criteria


def _enrich_section_purpose(
    section: DocumentSection,
    spec: DocumentSpec,
    coder_config: "CoderConfig",
    repo_path: "Path | None" = None,
) -> DocumentSection:
    """Optionally enrich a section's purpose via an LLM role call.

    Non-fatal: if invoke_role is unavailable or returns an empty/error
    response, the original purpose is kept.

    Args:
        section: The section whose purpose to enrich.
        spec: The full document spec for context.
        coder_config: Backend configuration for the LLM call.

    Returns:
        The section with an updated purpose (or unchanged on failure).
    """
    _reviewer = capability_registry.get("code_reviewer")
    if _reviewer is None:
        return section

    prompt = (
        f"Document type: {spec.type}\n"
        f"Document purpose: {spec.purpose}\n"
        f"Audience: {spec.audience}\n"
        f"Section name: {section.name}\n\n"
        "Write a single sentence describing the specific purpose of this section "
        "within the document. Be concrete. No preamble, no markdown."
    )

    try:
        result = _reviewer.review(prompt, coder_config, repo_path)
        enriched = (result.get("output") or "").strip()
        if enriched:
            logger.debug("Enriched section purpose for %s: %s", section.section_id, enriched)
            section.purpose = enriched
    except Exception as exc:  # pragma: no cover — LLM failures are non-fatal
        logger.warning(
            "Section purpose enrichment failed for %s: %s", section.section_id, exc
        )

    return section


def generate_document_plan(
    spec: DocumentSpec,
    repo_path: Path,
    coder_config: "CoderConfig | None" = None,
) -> DocumentPlan:
    """Generate a ``DocumentPlan`` from a ``DocumentSpec``.

    Each entry in ``spec.required_sections`` becomes a ``DocumentSection``
    with:
    - An auto-assigned section ID (``S001``, ``S002``, …)
    - A purpose derived from the document purpose and section name
    - All approved sources made available (v1: all sources to all sections)
    - Acceptance criteria derived from the ``claim_policy``
    - A fixed set of per-section check IDs

    Global checks are added as cross-section consistency validators.

    If ``coder_config`` is provided, an LLM call is attempted to enrich
    each section's purpose.  This step is strictly non-fatal: failures are
    logged and the planner continues.

    The resulting plan is written to ``.saturnday/document/plan.json``
    relative to ``repo_path``.

    Args:
        spec: The validated ``DocumentSpec``.
        repo_path: Root of the repository (used to resolve output path).
        coder_config: Optional backend configuration for LLM enrichment.

    Returns:
        The generated ``DocumentPlan``.
    """
    document_id = str(uuid.uuid4())
    acceptance_criteria = _acceptance_criteria_from_policy(spec.claim_policy)

    sections: list[DocumentSection] = []
    for idx, section_name in enumerate(spec.required_sections, start=1):
        section = DocumentSection(
            section_id=_section_id(idx),
            name=section_name,
            purpose=_derive_purpose(spec.purpose, section_name),
            required_sources=list(spec.approved_sources),
            acceptance_criteria=list(acceptance_criteria),
            required_checks=list(_SECTION_CHECKS),
            status="PENDING",
            content_path="",
            retry_count=0,
            findings=[],
        )

        if coder_config is not None:
            section = _enrich_section_purpose(section, spec, coder_config, repo_path=repo_path)

        sections.append(section)

    plan = DocumentPlan(
        document_id=document_id,
        type=spec.type,
        purpose=spec.purpose,
        risk_class=spec.risk_class,
        sections=sections,
        global_checks=list(_GLOBAL_CHECKS),
    )

    _write_plan(plan, repo_path)
    logger.info(
        "Generated document plan: id=%s type=%s sections=%d",
        document_id,
        spec.type,
        len(sections),
    )
    return plan


def _write_plan(plan: DocumentPlan, repo_path: Path) -> Path:
    """Serialise ``plan`` to ``.saturnday/document/plan.json``.

    Creates intermediate directories as needed.

    Args:
        plan: The plan to serialise.
        repo_path: Repository root.

    Returns:
        Path to the written plan file.
    """
    plan_dir = repo_path / ".saturnday" / "document"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "plan.json"

    # Convert dataclasses to plain dicts for JSON serialisation.
    import dataclasses  # noqa: PLC0415 — deferred import avoids top-level cost

    plan_dict = dataclasses.asdict(plan)
    plan_path.write_text(json.dumps(plan_dict, indent=2) + "\n", encoding="utf-8")
    logger.debug("Wrote document plan: %s", plan_path)
    return plan_path
