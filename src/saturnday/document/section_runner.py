"""Section runner for Saturnday Document Mode.

Generates content for a single document section: reads approved source files,
builds a bounded LLM prompt, calls the coder, and writes the output to disk.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saturnday._types import CoderConfig
from saturnday.coder_adapter import call_coder
from saturnday.document._types import DocumentPlan, DocumentSection, DocumentSpec

logger = logging.getLogger(__name__)

# Source caps to prevent prompt explosion.
_SOURCE_CAP_BYTES: int = 8192
_TOTAL_SOURCE_CAP_BYTES: int = 32768


def run_section(
    section: DocumentSection,
    spec: DocumentSpec,
    plan: DocumentPlan,
    repo_path: Path,
    coder_config: CoderConfig,
    output_dir: Path,
    prior_sections: list[dict] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Generate content for a single document section.

    Steps:

    1. Read source files listed in ``section.required_sources``.
    2. Build a bounded generation prompt from section spec, sources,
       prior section summaries, and claim policy.
    3. Call the LLM via ``call_coder``.
    4. Write content to ``output_dir/sections/{section_id}/section.md``.
    5. Write metadata JSON to ``output_dir/sections/{section_id}/metadata.json``.
    6. Return a result dict.

    Args:
        section: The section to generate.
        spec: The document spec governing this run.
        plan: The full document plan (for prior section context).
        repo_path: Repository root used as base for source file resolution.
        coder_config: LLM backend configuration.
        output_dir: Base directory for section output artefacts.
        prior_sections: Optional list of previously generated section result dicts
            (keys: section_id, content).  Used for cross-reference consistency.
        attempt: Attempt number (1-based).  Used in metadata and retry subdirs.

    Returns:
        Dict with keys:

        - ``section_id`` (str)
        - ``status`` (str — ``"GENERATED"`` on success, ``"ERROR"`` on failure)
        - ``content_path`` (str — absolute path to section.md, empty on failure)
        - ``metadata_path`` (str — absolute path to metadata.json, empty on failure)
        - ``content`` (str — raw markdown, empty on failure)
        - ``attempt`` (int)
        - ``error`` (str — error message, empty on success)
    """
    section_dir = output_dir / "sections" / section.section_id
    section_dir.mkdir(parents=True, exist_ok=True)

    try:
        source_contents = _read_all_sources(section.required_sources, repo_path)

        prompt = _build_section_prompt(section, spec, source_contents, prior_sections)

        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are a technical writer producing a governed document section. "
                    "Write only factual content grounded in the provided source material. "
                    "Do not fabricate citations, statistics, or claims not present in the "
                    "sources. Do not retrieve information from the open web."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        raw_content = call_coder(config=coder_config, messages=messages, repo_path=repo_path)

        content_path = section_dir / "section.md"
        content_path.write_text(raw_content, encoding="utf-8")

        source_refs = list(source_contents.keys())
        metadata: dict[str, Any] = {
            "section_id": section.section_id,
            "section_name": section.name,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "source_refs": source_refs,
            "attempt_number": attempt,
            "status": "GENERATED",
        }
        metadata_path = section_dir / "metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        logger.info(
            "Section %s generated: %d chars, attempt %d",
            section.section_id,
            len(raw_content),
            attempt,
        )

        return {
            "section_id": section.section_id,
            "status": "GENERATED",
            "content_path": str(content_path),
            "metadata_path": str(metadata_path),
            "content": raw_content,
            "attempt": attempt,
            "error": "",
        }

    except Exception as exc:
        logger.error(
            "Section %s generation failed (attempt %d): %s",
            section.section_id,
            attempt,
            exc,
        )
        return {
            "section_id": section.section_id,
            "status": "ERROR",
            "content_path": "",
            "metadata_path": "",
            "content": "",
            "attempt": attempt,
            "error": str(exc),
        }


def _build_section_prompt(
    section: DocumentSection,
    spec: DocumentSpec,
    source_contents: dict[str, str],
    prior_sections: list[dict] | None,
) -> str:
    """Build the LLM prompt for section generation.

    The prompt is structured in order of priority:

    1. Document purpose and audience (bounding context).
    2. Section identity and purpose.
    3. Claim policy constraints (hard rules).
    4. Approved source content (capped at 8K each, 32K total).
    5. Prior section summaries (for consistency).
    6. Acceptance criteria.
    7. Output instruction.

    Args:
        section: Section spec.
        spec: Document spec.
        source_contents: Mapping of source path → content (already capped).
        prior_sections: Previously generated section result dicts.

    Returns:
        Prompt string for the LLM.
    """
    parts: list[str] = []

    # --- Document context ---
    parts.append(
        f"DOCUMENT PURPOSE: {spec.purpose}\n"
        f"DOCUMENT TYPE: {spec.type}\n"
        f"AUDIENCE: {spec.audience}"
    )
    if spec.jurisdiction:
        parts.append(f"JURISDICTION: {spec.jurisdiction}")

    # --- Section identity ---
    parts.append(
        f"\nSECTION TO GENERATE: {section.name}\n"
        f"SECTION ID: {section.section_id}\n"
        f"SECTION PURPOSE: {section.purpose}"
    )

    # --- Claim policy constraints ---
    policy_lines: list[str] = []
    if spec.claim_policy.get("quantitative_claims_require_source"):
        policy_lines.append(
            "- Every quantitative claim (numbers, percentages, currency) MUST cite "
            "the source file or section it comes from."
        )
    if spec.claim_policy.get("recommendation_claims_require_support"):
        policy_lines.append(
            "- Every recommendation ('should', 'must', 'recommend') MUST be "
            "supported by evidence from the approved sources."
        )
    if not spec.claim_policy.get("uncited_narrative_allowed", True):
        policy_lines.append(
            "- All narrative paragraphs MUST include at least one source citation."
        )
    policy_lines.append(
        "- Do NOT invent citations, statistics, or claims not found in the sources below."
    )
    policy_lines.append(
        "- Do NOT retrieve information from the open web or external systems."
    )
    if policy_lines:
        parts.append("\nCLAIM POLICY:\n" + "\n".join(policy_lines))

    # --- Approved sources ---
    if source_contents:
        source_parts: list[str] = ["\nAPPROVED SOURCE CONTENT (cite by filename):"]
        for source_path, content in source_contents.items():
            source_parts.append(f"\n--- Source: {source_path} ---\n{content}")
        parts.append("\n".join(source_parts))
    else:
        parts.append(
            "\nAPPROVED SOURCE CONTENT: No source files specified for this section."
        )

    # --- Prior section summaries ---
    if prior_sections:
        summary_lines: list[str] = ["\nPRIOR SECTIONS GENERATED (for consistency):"]
        for ps in prior_sections:
            ps_id = ps.get("section_id", "?")
            ps_content = ps.get("content", "")
            # Provide a short summary: first 400 chars of content
            excerpt = ps_content[:400].strip()
            if len(ps_content) > 400:
                excerpt += "..."
            summary_lines.append(f"\n[{ps_id}] {excerpt}")
        parts.append("\n".join(summary_lines))

    # --- Acceptance criteria ---
    if section.acceptance_criteria:
        criteria_lines = "\n".join(
            f"- {c}" for c in section.acceptance_criteria
        )
        parts.append(f"\nACCEPTANCE CRITERIA FOR THIS SECTION:\n{criteria_lines}")

    # --- Output instruction ---
    parts.append(
        f"\nWrite the complete content for the '{section.name}' section in Markdown. "
        f"Begin with a heading '# {section.name}'. "
        "Use only information from the approved sources above. "
        "Do not add a closing meta-comment or preamble about what you are doing."
    )

    return "\n".join(parts)


def _read_source_content(source_path: str, repo_path: Path, cap: int = _SOURCE_CAP_BYTES) -> str:
    """Read source file content, capped at ``cap`` bytes.

    Resolves relative paths against ``repo_path``.  Returns empty string on any
    read error (missing file, permission denied, decode error).

    Args:
        source_path: Path string (may be relative to repo_path or absolute).
        repo_path: Repository root for relative path resolution.
        cap: Maximum bytes to read.  Default 8192.

    Returns:
        File content string, truncated to ``cap`` bytes if necessary.
        Empty string on error.
    """
    try:
        p = Path(source_path)
        if not p.is_absolute():
            p = repo_path / p
        if not p.exists():
            logger.warning("Source file not found: %s", p)
            return ""
        raw = p.read_bytes()[:cap]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")
    except Exception as exc:
        logger.warning("Failed to read source %s: %s", source_path, exc)
        return ""


def _read_all_sources(
    required_sources: list[str],
    repo_path: Path,
) -> dict[str, str]:
    """Read all required sources, applying per-source and total caps.

    Reads up to ``_SOURCE_CAP_BYTES`` per file.  Stops adding sources once
    the total accumulated content exceeds ``_TOTAL_SOURCE_CAP_BYTES``.

    Args:
        required_sources: List of source path strings.
        repo_path: Repository root for relative path resolution.

    Returns:
        Mapping of source path → content.  Excluded sources are omitted.
    """
    result: dict[str, str] = {}
    total = 0
    for source_path in required_sources:
        if total >= _TOTAL_SOURCE_CAP_BYTES:
            logger.info(
                "Total source cap reached (%d bytes); skipping remaining sources",
                _TOTAL_SOURCE_CAP_BYTES,
            )
            break
        remaining_budget = _TOTAL_SOURCE_CAP_BYTES - total
        cap = min(_SOURCE_CAP_BYTES, remaining_budget)
        content = _read_source_content(source_path, repo_path, cap=cap)
        result[source_path] = content
        total += len(content)
    return result
