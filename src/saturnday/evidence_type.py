"""Shared evidence-type detection for operator-surface commands.

Single source of truth for classifying an evidence directory as
run, governance, release, or unknown based on its file contents.
"""

from __future__ import annotations

from pathlib import Path


def detect_evidence_type(evidence_dir: Path) -> str:
    """Classify an evidence directory by its contents.

    Args:
        evidence_dir: Path to an evidence bundle directory.

    Returns:
        One of ``"run"``, ``"governance"``, ``"release"``, or ``"unknown"``.
    """
    if not evidence_dir.is_dir():
        return "unknown"

    # Run evidence: has evidence/run/ledger.json
    if (evidence_dir / "evidence" / "run" / "ledger.json").exists():
        return "run"

    # Governance evidence: has governance-report.md or final-disposition.json
    if (evidence_dir / "governance-report.md").exists():
        return "governance"
    if (evidence_dir / "final-disposition.json").exists():
        return "governance"

    # Release evidence: has evidence.json
    if (evidence_dir / "evidence.json").exists():
        return "release"

    return "unknown"
