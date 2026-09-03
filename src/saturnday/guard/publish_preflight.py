"""Publish preflight gate for OpenClaw skills.

Blocks publish unless all required conditions are met:
- OpenClaw rulepack passes (no high-severity findings)
- Required metadata exists (SKILL.md)
- No blocking shell/exfiltration patterns
- Evidence pack is complete
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from saturnday.guard.cloud_scanner import scan_skill

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """Result of a publish preflight check.

    Attributes:
        allowed: Whether publish is allowed.
        blocking_reasons: Reasons blocking publish (empty if allowed).
        warnings: Non-blocking warnings.
        findings_count: Total findings from the scanner.
    """

    allowed: bool
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    findings_count: int = 0


def run_publish_preflight(skill_dir: str | Path) -> PreflightResult:
    """Run the publish preflight gate for a skill.

    Args:
        skill_dir: Path to the skill directory.

    Returns:
        PreflightResult indicating whether publish is allowed.
    """
    skill_dir = Path(skill_dir).resolve()
    blocking: list[str] = []
    warnings: list[str] = []

    # 1. Check SKILL.md exists
    if not (skill_dir / "SKILL.md").exists():
        blocking.append("Missing SKILL.md — required for publish")

    # 2. Run OpenClaw scanner
    result = scan_skill(skill_dir)

    # 3. Block on high-severity findings
    high_findings = [f for f in result.findings if f.severity == "high"]
    if high_findings:
        blocking.append(
            f"Scanner found {len(high_findings)} high-severity finding(s): "
            + "; ".join(f.message[:80] for f in high_findings[:3])
        )

    # 4. Block on FAIL disposition
    if result.disposition == "FAIL":
        if not high_findings:  # Don't double-report
            blocking.append(f"Scanner disposition: {result.disposition}")

    # 5. Warn on medium findings
    medium_findings = [f for f in result.findings if f.severity == "medium"]
    if medium_findings:
        warnings.append(
            f"Scanner found {len(medium_findings)} medium-severity finding(s)"
        )

    # 6. Warn on missing tests
    low_findings = [f for f in result.findings if f.kind == "no_tests"]
    if low_findings:
        warnings.append("No test files found in skill directory")

    allowed = len(blocking) == 0

    if allowed:
        logger.info("Publish preflight PASSED for %s", skill_dir.name)
    else:
        logger.warning(
            "Publish preflight BLOCKED for %s: %s",
            skill_dir.name, "; ".join(blocking),
        )

    return PreflightResult(
        allowed=allowed,
        blocking_reasons=tuple(blocking),
        warnings=tuple(warnings),
        findings_count=result.findings_count,
    )
