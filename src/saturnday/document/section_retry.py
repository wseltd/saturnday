"""Section retry for Saturnday Document Mode.

Provides ``run_section_with_retry``: generates a section, verifies it, and
retries on failure by injecting the prior-attempt findings into a corrective
prompt.  Mirrors the ticket_runner.py retry pattern.

All prior attempts are preserved as
``output_dir/sections/{section_id}/attempt_{n}/`` subdirectories so the full
generation history is auditable.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from saturnday._types import CoderConfig
from saturnday.document._types import DocumentPlan, DocumentSection, DocumentSpec
from saturnday.document.section_runner import run_section

logger = logging.getLogger(__name__)


def run_section_with_retry(
    section: DocumentSection,
    spec: DocumentSpec,
    plan: DocumentPlan,
    repo_path: Path,
    coder_config: CoderConfig,
    output_dir: Path,
    prior_sections: list[dict] | None = None,
    max_retries: int = 2,
    verify_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Generate a section, verify it, and retry on failure.

    Flow:

    1. Call :func:`~saturnday.document.section_runner.run_section`.
    2. Call ``verify_fn`` on the generated content.
    3. If the section passes (``PASS`` or ``WARN``): return immediately.
    4. If the section fails (``FAIL``) and budget remains: retry with a prompt
       that explicitly lists the prior failures.
    5. Before each retry, archive the previous attempt into
       ``output_dir/sections/{section_id}/attempt_{n}/``.
    6. On budget exhaustion: return status ``PROVISIONAL_UNVERIFIED``.

    Args:
        section: The section to generate.
        spec: The document spec governing this run.
        plan: The full document plan (for prior section context).
        repo_path: Repository root for source file resolution.
        coder_config: LLM backend configuration.
        output_dir: Base directory for section artefacts.
        prior_sections: Optional previously generated section result dicts.
        max_retries: Maximum additional attempts after the first (default 2,
            matching the code-mode ticket default).
        verify_fn: Callable that accepts ``(content, section, spec, plan,
            source_contents)`` and returns ``(status_str, findings_list)``.
            Defaults to the canonical verifier.

    Returns:
        Dict with keys:

        - ``section_id`` (str)
        - ``status`` (str — ``"PASS"``, ``"WARN"``, ``"FAIL"``,
          ``"PROVISIONAL_UNVERIFIED"``, or ``"ERROR"``)
        - ``content_path`` (str)
        - ``metadata_path`` (str)
        - ``content`` (str)
        - ``attempt`` (int — final attempt number)
        - ``retry_count`` (int — number of retries performed)
        - ``findings`` (list[dict])
        - ``error`` (str)
    """
    effective_verify = verify_fn if verify_fn is not None else _default_verify_fn

    total_attempts = 1 + max_retries
    prior_findings: list[dict] = []
    last_result: dict[str, Any] = {}
    last_status: str = "PENDING"
    last_findings: list[dict] = []

    for attempt_n in range(1, total_attempts + 1):
        # Build an enriched prior_sections list that injects retry context.
        enriched_prior = _build_enriched_prior(prior_sections, prior_findings, attempt_n)

        result = run_section(
            section=section,
            spec=spec,
            plan=plan,
            repo_path=repo_path,
            coder_config=coder_config,
            output_dir=output_dir,
            prior_sections=enriched_prior,
            attempt=attempt_n,
        )

        if result["status"] == "ERROR":
            # LLM/IO error — not verifiable; treat as exhaustion.
            logger.warning(
                "Section %s attempt %d returned ERROR: %s",
                section.section_id,
                attempt_n,
                result.get("error", ""),
            )
            last_result = result
            last_result["status"] = "PROVISIONAL_UNVERIFIED"
            last_result["retry_count"] = attempt_n - 1
            last_result["findings"] = prior_findings
            _persist_attempt(output_dir, section.section_id, attempt_n, result)
            return last_result

        content = result["content"]
        status_str, findings = effective_verify(content, section, spec, plan)

        logger.info(
            "Section %s attempt %d status: %s findings: %d",
            section.section_id,
            attempt_n,
            status_str,
            len(findings),
        )

        _persist_attempt(output_dir, section.section_id, attempt_n, result)

        last_result = result
        last_status = status_str
        last_findings = findings

        if status_str in ("PASS", "WARN"):
            # Update section object to reflect outcome.
            section.status = status_str
            section.retry_count = attempt_n - 1
            section.findings = [_finding_to_dict(f) for f in findings]
            last_result["status"] = status_str
            last_result["retry_count"] = attempt_n - 1
            last_result["findings"] = section.findings
            return last_result

        # FAIL — prepare for retry if budget allows.
        prior_findings = [_finding_to_dict(f) for f in findings]

    # Budget exhausted — mark as provisional.
    section.status = "PROVISIONAL_UNVERIFIED"
    section.retry_count = total_attempts - 1
    section.findings = [_finding_to_dict(f) for f in last_findings]

    last_result["status"] = "PROVISIONAL_UNVERIFIED"
    last_result["retry_count"] = total_attempts - 1
    last_result["findings"] = section.findings
    logger.warning(
        "Section %s exhausted %d attempts; marked PROVISIONAL_UNVERIFIED",
        section.section_id,
        total_attempts,
    )
    return last_result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_verify_fn(
    content: str,
    section: DocumentSection,
    spec: DocumentSpec,
    plan: DocumentPlan,
    source_contents: dict[str, str] | None = None,
) -> tuple[str, list[Any]]:
    """Default verifier that calls canonical section_verifier.

    Imports lazily to allow the caller to patch ``section_verifier`` in tests.
    """
    from saturnday.document.section_verifier import compute_section_status, verify_section

    if source_contents is None:
        source_contents = {}
    findings = verify_section(content, section, spec, plan, source_contents)
    status = compute_section_status(findings)
    return status, findings


def _build_enriched_prior(
    prior_sections: list[dict] | None,
    prior_findings: list[dict],
    attempt_n: int,
) -> list[dict] | None:
    """Prepend a retry-context entry to the prior_sections list.

    On attempt 1 (no failures yet) returns ``prior_sections`` unchanged.
    On later attempts, injects a synthetic entry describing what failed.

    Args:
        prior_sections: Original prior-sections list (may be None).
        prior_findings: Findings from the most recent failed attempt.
        attempt_n: Current attempt number (1-based).

    Returns:
        Enriched list or the original list if no retry context needed.
    """
    if attempt_n == 1 or not prior_findings:
        return prior_sections

    failure_lines: list[str] = [
        f"RETRY ATTEMPT {attempt_n}: The previous attempt failed verification.",
        "You MUST fix ALL of the following specific issues before writing the section:",
        "",
    ]
    for idx, f in enumerate(prior_findings, start=1):
        severity = f.get("severity", "error")
        kind = f.get("kind", "unknown")
        detail = f.get("detail", "")
        failure_lines.append(f"  Issue {idx} [{severity.upper()}] ({kind}): {detail}")

    failure_lines.append(
        "\nDo NOT repeat the same mistakes. Address each issue listed above explicitly."
    )

    retry_entry = {
        "section_id": "RETRY_CONTEXT",
        "content": "\n".join(failure_lines),
    }

    if prior_sections:
        return [retry_entry] + list(prior_sections)
    return [retry_entry]


def _persist_attempt(
    output_dir: Path,
    section_id: str,
    attempt_n: int,
    result: dict[str, Any],
) -> None:
    """Copy generated artefacts into an attempt-numbered subdirectory.

    The canonical section dir (``output_dir/sections/{section_id}/``) always
    contains the latest attempt.  Each prior attempt is archived as
    ``output_dir/sections/{section_id}/attempt_{n}/``.

    Args:
        output_dir: Base output directory.
        section_id: Section identifier.
        attempt_n: Attempt number (1-based).
        result: Result dict from :func:`run_section`.
    """
    section_dir = output_dir / "sections" / section_id
    attempt_dir = section_dir / f"attempt_{attempt_n}"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    for key in ("content_path", "metadata_path"):
        src_str = result.get(key, "")
        if not src_str:
            continue
        src = Path(src_str)
        if src.exists():
            dst = attempt_dir / src.name
            shutil.copy2(src, dst)

    # Write a compact attempt summary.
    summary = {
        "attempt": attempt_n,
        "status": result.get("status", ""),
        "error": result.get("error", ""),
    }
    (attempt_dir / "attempt_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )


def _finding_to_dict(finding: Any) -> dict[str, Any]:
    """Convert a DocumentFinding (or plain dict) to a serialisable dict.

    Args:
        finding: A :class:`~saturnday.document._types.DocumentFinding` or dict.

    Returns:
        Dict with keys ``finding_id``, ``section_id``, ``kind``,
        ``severity``, ``detail``, ``fixable_by_regeneration``.
    """
    if isinstance(finding, dict):
        return finding
    return {
        "finding_id": getattr(finding, "finding_id", ""),
        "section_id": getattr(finding, "section_id", ""),
        "kind": getattr(finding, "kind", ""),
        "severity": getattr(finding, "severity", "error"),
        "detail": getattr(finding, "detail", ""),
        "fixable_by_regeneration": getattr(finding, "fixable_by_regeneration", False),
    }
