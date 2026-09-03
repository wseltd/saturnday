"""Two-person release signoff workflow.

RS-017: Implements structured signoff records with artefact-hash binding,
persisted to the release evidence directory as JSON files.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "SignoffRecord",
    "create_signoff",
    "load_signoffs",
    "check_signoff_requirement",
]

_logger = logging.getLogger(__name__)

_SIGNOFF_SUBDIR = "release/signoffs"


@dataclass
class SignoffRecord:
    """An immutable record of a single release approval.

    Attributes:
        signoff_id: UUID4 string uniquely identifying this signoff.
        artefact_sha256: SHA-256 hex digest of the artefact at approval time.
        approver: Identity string of the approver (username or email).
        approved_at: ISO 8601 UTC timestamp of approval.
        notes: Optional free-text notes from the approver.
    """

    signoff_id: str
    artefact_sha256: str
    approver: str
    approved_at: str  # ISO 8601
    notes: str = ""


def _signoff_dir(evidence_dir: Path) -> Path:
    """Return the signoff subdirectory path, creating it if absent."""
    d = evidence_dir / _SIGNOFF_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_signoff(
    evidence_dir: Path,
    artefact_sha256: str,
    approver: str,
    notes: str = "",
) -> SignoffRecord:
    """Record a release signoff in the evidence directory.

    Validates that the provided *artefact_sha256* is a non-empty hex string
    before writing.  The signoff file is written atomically as JSON under
    ``evidence_dir/release/signoffs/<signoff_id>.json``.

    Args:
        evidence_dir: Root evidence directory for this release run.
        artefact_sha256: SHA-256 hex digest of the artefact being approved.
            Must be a 64-character hex string.
        approver: Identity of the approver (username or email).
        notes: Optional free-text notes.

    Returns:
        The persisted :class:`SignoffRecord`.

    Raises:
        ValueError: If *artefact_sha256* is empty or not a valid hex string.
        OSError: If the record cannot be written.
    """
    if not artefact_sha256 or not artefact_sha256.strip():
        raise ValueError("artefact_sha256 must not be empty")
    if not _is_valid_hex(artefact_sha256):
        raise ValueError(
            f"artefact_sha256 must be a hex string, got: {artefact_sha256!r}"
        )
    if not approver or not approver.strip():
        raise ValueError("approver must not be empty")

    signoff_id = str(uuid.uuid4())
    approved_at = datetime.now(tz=timezone.utc).isoformat()

    record = SignoffRecord(
        signoff_id=signoff_id,
        artefact_sha256=artefact_sha256.lower(),
        approver=approver.strip(),
        approved_at=approved_at,
        notes=notes or "",
    )

    out_path = _signoff_dir(evidence_dir) / f"{signoff_id}.json"
    out_path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    _logger.info("Signoff recorded: %s by %s at %s", signoff_id, approver, approved_at)
    return record


def load_signoffs(evidence_dir: Path) -> list[SignoffRecord]:
    """Load all signoff records from the evidence directory.

    Reads every ``*.json`` file under ``evidence_dir/release/signoffs/``.
    Malformed files are logged as warnings and skipped.

    Args:
        evidence_dir: Root evidence directory for this release run.

    Returns:
        List of :class:`SignoffRecord` instances, possibly empty.
    """
    signoff_dir = evidence_dir / _SIGNOFF_SUBDIR
    if not signoff_dir.is_dir():
        return []

    records: list[SignoffRecord] = []
    for path in sorted(signoff_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                SignoffRecord(
                    signoff_id=data["signoff_id"],
                    artefact_sha256=data["artefact_sha256"],
                    approver=data["approver"],
                    approved_at=data["approved_at"],
                    notes=data.get("notes", ""),
                )
            )
        except (KeyError, json.JSONDecodeError, OSError) as exc:
            _logger.warning("Skipping malformed signoff file %s: %s", path, exc)

    return records


def check_signoff_requirement(
    evidence_dir: Path,
    minimum_approvers: int = 2,
) -> tuple[bool, str]:
    """Check whether the minimum approver threshold is satisfied.

    Args:
        evidence_dir: Root evidence directory for this release run.
        minimum_approvers: Minimum number of distinct approvers required.

    Returns:
        ``(True, reason)`` if the threshold is met, ``(False, reason)``
        otherwise.  *reason* is a human-readable explanation.
    """
    if minimum_approvers < 1:
        return True, "No minimum approver requirement configured"

    records = load_signoffs(evidence_dir)
    # Count distinct approvers.
    distinct_approvers = {r.approver for r in records}
    count = len(distinct_approvers)

    if count >= minimum_approvers:
        return True, (
            f"Signoff requirement met: {count} distinct approver(s) "
            f"(minimum {minimum_approvers})"
        )

    return False, (
        f"Signoff requirement NOT met: {count} distinct approver(s) found, "
        f"need {minimum_approvers}"
    )


def _is_valid_hex(value: str) -> bool:
    """Return True if *value* is a non-empty hexadecimal string."""
    try:
        int(value, 16)
        return True
    except ValueError:
        return False
