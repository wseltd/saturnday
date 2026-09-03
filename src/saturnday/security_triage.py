"""LLM-based security finding triage — data flow false positive filter.

After pattern-matching governance checks produce security findings, this module
groups them by file, reads code context, and asks an LLM to classify each as
TRUE_POSITIVE, FALSE_POSITIVE, or UNCERTAIN.

FALSE_POSITIVE findings are removed from the findings list (still recorded in
the original evidence pack for audit).  UNCERTAIN and TRUE_POSITIVE findings
are kept at their original severity.

This is a precision filter, not a replacement for the scanner.  The pattern
matcher has high recall (catches everything suspicious).  This module improves
precision by removing findings the LLM can prove are safe.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from saturnday._types import CoderConfig

logger = logging.getLogger(__name__)

# Security check names whose findings are candidates for triage.
# Non-security checks (quality, AI-specific, DevOps) are already accurate
# and don't need LLM triage.
_SECURITY_CHECK_NAMES = frozenset({
    "sql_injection", "hardcoded_jwt", "auth_bypass", "websocket_auth",
    "oauth_flow_integrity", "cookie_security_hard", "cookie_security",
    "user_enumeration", "jwt_verification_policy", "csrf_state_change",
    "rate_limit_wiring", "token_expiry", "token_revocation",
    "weak_randomness", "xss_check",
    "client_trusted_logic", "idor_check", "rate_limit_backend_quality",
    # TS equivalents
    "hardcoded_jwt_ts", "auth_bypass_ts", "websocket_auth_ts",
    "csrf_state_change_ts", "jwt_verification_ts", "token_expiry_ts",
    "rate_limit_wiring_ts", "rate_limit_backend_quality_ts",
    "xss_check_ts", "ssrf_check_ts", "user_enum_ts",
    "client_trusted_logic_ts",
    "oauth_flow_ts", "cookie_security_ts", "idor_ts",
    "weak_randomness_ts", "token_revocation_ts",
})

# Checks that detect the ABSENCE of something (missing logging, missing
# headers, etc.) should NEVER be triaged.  The LLM cannot meaningfully
# say "missing audit logging is a false positive" because the issue is
# that the code doesn't exist, not a data-flow question.
_NEVER_TRIAGE_KINDS = frozenset({
    "missing_security_logging", "bare_logging",
    "missing_security_logging_ts", "bare_logging_ts",
    "missing_rate_limit", "missing_auth",
})

# How many lines of context to show around each finding
_CONTEXT_LINES_BEFORE = 10
_CONTEXT_LINES_AFTER = 5


def triage_security_findings(
    findings: list[dict],
    repo_path: Path,
    coder_config: CoderConfig,
) -> list[dict]:
    """Filter false positive security findings via LLM data flow analysis.

    Non-security findings are passed through unchanged.  Security findings
    are grouped by file, sent to the LLM for triage, and false positives
    are removed.

    Args:
        findings: Flat list of finding dicts from governance.
        repo_path: Repository root for reading source files.
        coder_config: Backend config for LLM calls.

    Returns:
        Filtered findings list with false positives removed.
    """
    # Separate security findings from others
    security_findings: list[tuple[int, dict]] = []
    other_findings: list[dict] = []

    for i, f in enumerate(findings):
        if _is_security_finding(f):
            security_findings.append((i, f))
        else:
            other_findings.append(f)

    if not security_findings:
        return findings  # nothing to triage

    logger.info(
        "Security triage: %d security finding(s) to analyze",
        len(security_findings),
    )

    # Group by file
    by_file: dict[str, list[tuple[int, dict]]] = {}
    for idx, f in security_findings:
        file_path = f.get("file", "")
        by_file.setdefault(file_path, []).append((idx, f))

    # Triage each file group
    false_positive_indices: set[int] = set()
    for file_path, file_findings in by_file.items():
        try:
            fp_indices = _triage_file(
                file_path, file_findings, repo_path, coder_config,
            )
            false_positive_indices.update(fp_indices)
        except Exception as exc:
            # Triage failure is non-fatal — keep all findings
            logger.debug("Triage failed for %s: %s", file_path, exc)

    if false_positive_indices:
        logger.info(
            "Security triage: %d finding(s) identified as false positives",
            len(false_positive_indices),
        )

    # Rebuild findings list: keep non-security + security that aren't FP
    result = list(other_findings)
    for idx, f in security_findings:
        if idx not in false_positive_indices:
            result.append(f)
        else:
            logger.info(
                "Triage: filtered false positive — %s in %s:%s",
                f.get("kind", "?"), f.get("file", "?"), f.get("line", "?"),
            )

    return result


def _is_security_finding(finding: dict) -> bool:
    """Check if a finding is from a security check AND eligible for triage.

    Findings about the ABSENCE of code (missing logging, missing rate limits)
    are excluded — the LLM cannot meaningfully triage "this doesn't exist."
    """
    kind = finding.get("kind", "")

    # Never triage absence-based findings
    if kind in _NEVER_TRIAGE_KINDS:
        return False

    rule_id = finding.get("rule_id", "")
    if rule_id and rule_id.startswith("SEC-"):
        return True

    # Check against known security finding kinds
    security_kinds = {
        "sql_injection", "csrf_missing", "jwt_literal_secret",
        "missing_token_expiry", "route_no_auth",
        "ws_no_auth", "ws_no_origin_check", "missing_oauth_state",
        "session_cookie_no_httponly", "session_cookie_no_samesite",
        "missing_jwt_policy", "alg_none_allowed", "unpinned_algorithm",
        "rate_limit_memory_store",
        "xss_reflected", "xss_stored", "ssrf_urlopen",
        "user_enumeration", "idor_pattern", "weak_randomness",
        "client_trusted_logic", "token_no_revoke",
        "redirect_from_input", "missing_pkce",
        "env_fallback_secret",
    }
    return kind in security_kinds


def _triage_file(
    file_path: str,
    file_findings: list[tuple[int, dict]],
    repo_path: Path,
    coder_config: CoderConfig,
) -> set[int]:
    """Triage all security findings in a single file via one LLM call.

    Returns set of finding indices classified as FALSE_POSITIVE.
    """
    from saturnday.coder_adapter import call_coder
    from saturnday.role_modes import load_role_prompt

    # Read the source file
    full_path = repo_path / file_path
    if not full_path.is_file():
        return set()
    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return set()

    lines = source.splitlines()

    # Build findings description with code context
    findings_block = []
    for local_idx, (global_idx, f) in enumerate(file_findings):
        line_num = f.get("line", 0)
        kind = f.get("kind", "unknown")
        detail = f.get("detail", "")
        rule_id = f.get("rule_id", "")

        # Extract code context around the finding
        start = max(0, line_num - 1 - _CONTEXT_LINES_BEFORE)
        end = min(len(lines), line_num + _CONTEXT_LINES_AFTER)
        context_lines = []
        for i in range(start, end):
            marker = " >>>" if i == line_num - 1 else "    "
            context_lines.append(f"{marker} {i + 1:4d} | {lines[i]}")

        findings_block.append(
            f"### Finding {local_idx} (global index {global_idx})\n"
            f"- Rule: {rule_id}\n"
            f"- Kind: {kind}\n"
            f"- Detail: {detail}\n"
            f"- File: {file_path}, line {line_num}\n"
            f"```\n"
            + "\n".join(context_lines)
            + "\n```"
        )

    # Build the user prompt
    user_prompt = (
        f"## File: {file_path}\n\n"
        f"## Findings to triage ({len(file_findings)} total)\n\n"
        + "\n\n".join(findings_block)
        + "\n\n## Your task\n"
        f"For each finding (0 to {len(file_findings) - 1}), output a JSON array "
        f"with your verdict. Use the finding_index from 0 to {len(file_findings) - 1} "
        f"(the local index shown above, NOT the global index)."
    )

    # Call LLM
    system_prompt = load_role_prompt("security_triage")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response = call_coder(coder_config, messages, repo_path)
    except Exception as exc:
        logger.debug("Triage LLM call failed for %s: %s", file_path, exc)
        return set()

    # Parse response
    return _parse_triage_response(response, file_findings)


def _parse_triage_response(
    response: str,
    file_findings: list[tuple[int, dict]],
) -> set[int]:
    """Parse the LLM triage response and return global indices of false positives."""
    false_positives: set[int] = set()

    # Try to extract JSON from response
    text = response.strip()

    # Handle markdown code blocks
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    # Find JSON array
    bracket_start = text.find("[")
    bracket_end = text.rfind("]")
    if bracket_start == -1 or bracket_end == -1:
        logger.debug("Triage response has no JSON array")
        return false_positives

    try:
        verdicts = json.loads(text[bracket_start:bracket_end + 1])
    except json.JSONDecodeError as exc:
        logger.debug("Triage response JSON parse failed: %s", exc)
        return false_positives

    if not isinstance(verdicts, list):
        return false_positives

    for v in verdicts:
        if not isinstance(v, dict):
            continue
        local_idx = v.get("finding_index")
        verdict = v.get("verdict", "").upper()
        reason = v.get("reason", "")

        if local_idx is None or not isinstance(local_idx, int):
            continue
        if local_idx < 0 or local_idx >= len(file_findings):
            continue

        global_idx = file_findings[local_idx][0]

        if verdict == "FALSE_POSITIVE":
            false_positives.add(global_idx)
            logger.info(
                "Triage: FALSE_POSITIVE [%d] %s — %s",
                global_idx,
                file_findings[local_idx][1].get("kind", "?"),
                reason[:100],
            )
        elif verdict == "TRUE_POSITIVE":
            logger.debug(
                "Triage: TRUE_POSITIVE [%d] %s — %s",
                global_idx,
                file_findings[local_idx][1].get("kind", "?"),
                reason[:100],
            )
        # UNCERTAIN = do nothing, keep the finding

    return false_positives
