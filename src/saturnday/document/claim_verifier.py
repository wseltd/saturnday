"""Claim verifier for Saturnday Document Mode.

Verifies :class:`~saturnday.document._types.DocumentClaim` objects against
**approved evidence only** — no internet lookup, no world-knowledge claims.

Verification strategy by claim type:

* **quantitative** — search source content for the number; numeric match
  yields SUPPORTED, absence yields UNSUPPORTED.
* **cited** — check that each linked source exists in ``source_contents``;
  SUPPORTED if present, UNSUPPORTED if not.
* **all others** — LLM-bounded check using a strictly constrained prompt
  that forbids the model from using any knowledge outside the provided
  evidence.  If no ``coder_config`` is available, verdict is
  UNCERTAIN_REQUIRES_REVIEW (escalates, never silently passes).

LLM responses must parse to one of the five verdict strings.  Any
parse failure → UNCERTAIN_REQUIRES_REVIEW.  LLM timeout or exception →
UNCERTAIN_REQUIRES_REVIEW.

Uncertain claims ALWAYS escalate (``uncertainty=True``).  They are never
silently treated as SUPPORTED.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from saturnday.document._types import ClaimType, DocumentClaim, SupportVerdict

logger = logging.getLogger(__name__)

# call_coder is imported at module level so it can be patched in tests.
# The try/except allows the document module to be imported without
# coder_adapter being available (e.g. in lightweight test environments).
try:
    from saturnday.coder_adapter import call_coder  # noqa: F401
except ImportError:  # pragma: no cover
    call_coder = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------

_VALID_VERDICTS: frozenset[str] = frozenset(v.value for v in SupportVerdict)

# Regex that extracts the first valid verdict token from an LLM response.
# We accept the token even if the model wraps it in a sentence.
_VERDICT_RE = re.compile(
    r"\b(SUPPORTED|WEAKLY_SUPPORTED|UNSUPPORTED|CONTRADICTED|UNCERTAIN_REQUIRES_REVIEW)\b",
    re.IGNORECASE,
)

# Number extraction for quantitative source lookup
_NUMBER_RE = re.compile(
    r"""
    \$?\s*[\d,]+\.?\d*\s*%?   # currency / percentage / plain number with commas
    """,
    re.VERBOSE,
)

# Maximum source content characters sent to LLM per claim (to keep prompts bounded)
_SOURCE_CAP_CHARS = 4_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_claims(
    claims: list[DocumentClaim],
    source_contents: dict[str, str],
    coder_config: object,  # CoderConfig — typed as object to avoid heavy import
    repo_path: Path,
) -> list[DocumentClaim]:
    """Verify claims against approved evidence.

    Each claim is verified independently.  The method modifies ``claims``
    in-place (sets ``support_verdict``, ``uncertainty``, and ``notes``) AND
    returns the same list for convenience.

    Verification is bounded to ``source_contents`` only.  The LLM prompt
    explicitly forbids use of training knowledge or external information.

    Args:
        claims: Extracted :class:`~saturnday.document._types.DocumentClaim`
            objects (typically from :func:`~saturnday.document.claim_extractor.extract_claims`).
        source_contents: Mapping of source path → file text.  Only approved
            sources may be included here.
        coder_config: Backend configuration for LLM calls
            (:class:`~saturnday._types.CoderConfig`).  If ``None``, all
            claims are marked UNCERTAIN_REQUIRES_REVIEW.
        repo_path: Repository root (passed through to ``call_coder``).

    Returns:
        The same ``claims`` list with ``support_verdict`` and
        ``uncertainty`` populated.
    """
    for claim in claims:
        try:
            verdict, notes = _verify_one(claim, source_contents, coder_config, repo_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "claim_verifier: unexpected error verifying %s: %s", claim.claim_id, exc
            )
            verdict = SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value
            notes = f"Verification error: {exc}"

        claim.support_verdict = verdict
        claim.notes = notes
        claim.uncertainty = verdict == SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value

    return claims


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------


def run_claim_analysis(
    sections_content: dict[str, str],
    spec: object,  # DocumentSpec
    repo_path: Path,
    coder_config: object = None,  # CoderConfig | None
) -> list[DocumentClaim]:
    """Extract and (optionally) verify claims across all sections.

    This is the Phase 5 convenience entry point.  It takes a mapping of
    ``{section_id: content_text}``, runs extraction on each section, then
    runs verification on all claims if ``coder_config`` is provided.

    Args:
        sections_content: Mapping of section ID to generated Markdown text.
        spec: :class:`~saturnday.document._types.DocumentSpec` with
            ``approved_sources`` list.
        repo_path: Repository root used for source file reading and LLM calls.
        coder_config: Optional backend config.  If ``None``, claims are
            extracted but not verified (verdicts stay empty).

    Returns:
        Combined list of :class:`~saturnday.document._types.DocumentClaim`
        across all sections, with verdicts populated when ``coder_config``
        is provided.
    """
    from saturnday.document.claim_extractor import extract_claims

    all_claims: list[DocumentClaim] = []
    for section_id, content in sections_content.items():
        section_claims = extract_claims(content, section_id, spec)
        all_claims.extend(section_claims)

    if not all_claims:
        return all_claims

    if coder_config is None:
        logger.debug(
            "claim_verifier: no coder_config — skipping verification for %d claims",
            len(all_claims),
        )
        return all_claims

    # Read approved source contents from disk
    source_contents = _load_source_contents(spec, repo_path)

    return verify_claims(all_claims, source_contents, coder_config, repo_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _verify_one(
    claim: DocumentClaim,
    source_contents: dict[str, str],
    coder_config: object,
    repo_path: Path,
) -> tuple[str, str]:
    """Verify a single claim.  Returns (verdict_value, notes)."""
    # --- No LLM available: escalate ---
    if coder_config is None or call_coder is None:
        return SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value, "No coder_config provided."

    claim_type = claim.claim_type

    # --- Deterministic: cited claim — check source existence ---
    if claim_type == ClaimType.CITED.value:
        return _verify_cited(claim, source_contents)

    # --- Deterministic: quantitative — search for number in sources ---
    if claim_type == ClaimType.QUANTITATIVE.value:
        return _verify_quantitative(claim, source_contents)

    # --- LLM-bounded verification for all other types ---
    return _verify_with_llm(claim, source_contents, coder_config, repo_path)


def _verify_cited(
    claim: DocumentClaim,
    source_contents: dict[str, str],
) -> tuple[str, str]:
    """Verify a cited claim by checking source existence."""
    if not claim.linked_sources:
        # Has citation syntax but no matched approved source
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            "Claim contains citation tokens but no linked approved source was found.",
        )

    missing = [s for s in claim.linked_sources if s not in source_contents]
    if missing:
        return (
            SupportVerdict.UNSUPPORTED.value,
            f"Linked source(s) not found in approved evidence: {missing}",
        )
    return SupportVerdict.SUPPORTED.value, "All linked sources present in approved evidence."


def _verify_quantitative(
    claim: DocumentClaim,
    source_contents: dict[str, str],
) -> tuple[str, str]:
    """Search for claim numbers in source files.

    If any source contains at least one of the numbers mentioned in the
    claim → SUPPORTED.  If no source matches → UNSUPPORTED.
    """
    # Extract normalised number strings from the claim
    raw_numbers = _NUMBER_RE.findall(claim.claim_text)
    # Normalise: strip whitespace, commas, currency symbols, trailing punctuation
    normalised: set[str] = set()
    for n in raw_numbers:
        cleaned = n.replace(",", "").replace(" ", "").strip("%$€£.,;:")
        if cleaned:
            normalised.add(cleaned)

    if not normalised:
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            "Could not extract a numeric value from claim text.",
        )

    # Search relevant sources (linked first, then full set)
    relevant = _relevant_sources(claim, source_contents)

    for number in normalised:
        for _path, content in relevant.items():
            if number in content.replace(",", ""):
                return (
                    SupportVerdict.SUPPORTED.value,
                    f"Number '{number}' found in approved source evidence.",
                )

    return (
        SupportVerdict.UNSUPPORTED.value,
        f"Number(s) {sorted(normalised)} not found in any approved source.",
    )


def _verify_with_llm(
    claim: DocumentClaim,
    source_contents: dict[str, str],
    coder_config: object,
    repo_path: Path,
) -> tuple[str, str]:
    """Bounded LLM verdict for non-deterministic claim types."""
    relevant = _relevant_sources(claim, source_contents)

    if not relevant:
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            "No approved source evidence available for this claim.",
        )

    prompt = _build_verification_prompt(claim, relevant)

    try:
        messages = [{"role": "user", "content": prompt}]
        response = call_coder(coder_config, messages, repo_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "claim_verifier: LLM call failed for %s: %s", claim.claim_id, exc
        )
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            f"LLM verification failed: {exc}",
        )

    return _parse_verdict(response)


def _build_verification_prompt(
    claim: DocumentClaim,
    relevant_sources: dict[str, str],
) -> str:
    """Build the LLM prompt for claim verification.

    The prompt explicitly constrains the LLM to ONLY the provided evidence.
    It must not use any knowledge from training or external sources.

    Args:
        claim: The claim to verify.
        relevant_sources: Mapping of source path → content (already capped).

    Returns:
        Prompt string for the LLM.
    """
    evidence_block = ""
    for path, content in relevant_sources.items():
        capped = content[:_SOURCE_CAP_CHARS]
        if len(content) > _SOURCE_CAP_CHARS:
            capped += "\n[...truncated...]"
        evidence_block += f"\n--- Source: {path} ---\n{capped}\n"

    return (
        "You are a document governance assistant. Your task is to assess whether "
        "a specific claim is supported by the provided evidence.\n\n"
        "CRITICAL CONSTRAINT: You MUST assess the claim ONLY using the evidence "
        "provided below. Do NOT use your training knowledge, world knowledge, or "
        "any external information. If the evidence does not address the claim, "
        "respond with UNCERTAIN_REQUIRES_REVIEW.\n\n"
        f"CLAIM ({claim.claim_type}):\n{claim.claim_text}\n\n"
        f"APPROVED EVIDENCE:{evidence_block}\n\n"
        "Respond with EXACTLY ONE of these verdicts on the first line, followed "
        "by a brief explanation (1-2 sentences):\n"
        "SUPPORTED — the evidence clearly supports the claim\n"
        "WEAKLY_SUPPORTED — the evidence partially supports the claim\n"
        "UNSUPPORTED — the evidence does not support the claim\n"
        "CONTRADICTED — the evidence contradicts the claim\n"
        "UNCERTAIN_REQUIRES_REVIEW — insufficient evidence to assess\n\n"
        "Your response MUST start with one of the five verdict tokens above."
    )


def _parse_verdict(response: str) -> tuple[str, str]:
    """Parse an LLM response into (verdict_value, notes).

    Falls back to UNCERTAIN_REQUIRES_REVIEW on any parse failure so that
    unverifiable claims always escalate rather than silently pass.

    Args:
        response: Raw LLM response text.

    Returns:
        Tuple of (SupportVerdict value string, notes string).
    """
    if not response or not response.strip():
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            "LLM returned empty response.",
        )

    match = _VERDICT_RE.search(response)
    if not match:
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            f"Could not parse verdict from LLM response: {response[:200]!r}",
        )

    raw_verdict = match.group(1).upper()
    if raw_verdict not in _VALID_VERDICTS:
        return (
            SupportVerdict.UNCERTAIN_REQUIRES_REVIEW.value,
            f"Unrecognised verdict token: {raw_verdict!r}",
        )

    # Notes: everything after the verdict token on the same line, or empty
    remainder = response[match.end():].strip()
    # Truncate notes to a reasonable length
    notes = remainder[:500] if remainder else f"Verdict: {raw_verdict}"
    return raw_verdict, notes


def _relevant_sources(
    claim: DocumentClaim,
    source_contents: dict[str, str],
) -> dict[str, str]:
    """Return source contents relevant to this claim.

    Preferred order: linked_sources first (if they exist in source_contents),
    then remaining sources up to a sensible total count.

    Args:
        claim: The claim (provides ``linked_sources``).
        source_contents: Full approved source map.

    Returns:
        Subset of ``source_contents`` relevant to this claim.
    """
    relevant: dict[str, str] = {}

    # Linked sources first
    for src in claim.linked_sources:
        if src in source_contents:
            relevant[src] = source_contents[src]

    # Add remaining sources (up to 3 total to keep prompts bounded)
    for path, content in source_contents.items():
        if path not in relevant:
            relevant[path] = content
        if len(relevant) >= 3:
            break

    return relevant


def _load_source_contents(spec: object, repo_path: Path) -> dict[str, str]:
    """Read approved source files from disk.

    Files that do not exist are silently skipped (they will fail at
    citation verification if referenced).

    Args:
        spec: DocumentSpec with ``approved_sources`` list.
        repo_path: Repository root for resolving relative paths.

    Returns:
        Mapping of source path → file content.
    """
    contents: dict[str, str] = {}
    approved: list[str] = getattr(spec, "approved_sources", []) or []

    for source_path in approved:
        p = Path(source_path)
        if not p.is_absolute():
            p = repo_path / p
        try:
            contents[source_path] = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("claim_verifier: could not read source %s: %s", source_path, exc)

    return contents
