"""Sign-off model for governed document runs.

Manages document approval records: recording approvals/rejections and
verifying that all required sign-off roles have been satisfied before
a high-risk document can reach APPROVED status.

Approvals are persisted as JSON at ``.saturnday/document/approvals.json``
inside the repo directory.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saturnday.document._types import DocumentApproval, DocumentSpec

logger = logging.getLogger(__name__)

_APPROVALS_REL_PATH = ".saturnday/document/approvals.json"


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _approvals_path(repo_path: Path) -> Path:
    """Return the canonical approvals file path for a repo."""
    return repo_path / _APPROVALS_REL_PATH


def _load_approvals(repo_path: Path) -> list[dict[str, Any]]:
    """Load raw approval records from disk.

    Returns an empty list when the file does not exist or is unreadable.

    Args:
        repo_path: Root of the repository.

    Returns:
        List of raw approval dicts.
    """
    path = _approvals_path(repo_path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        logger.warning("Approvals file is not a list: %s", path)
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not load approvals from %s: %s", path, exc)
        return []


def _save_approvals(repo_path: Path, records: list[dict[str, Any]]) -> None:
    """Write approval records to disk.

    Creates intermediate directories as needed.

    Args:
        repo_path: Root of the repository.
        records: List of raw approval dicts to persist.
    """
    path = _approvals_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
        logger.info("Saved %d approval record(s) to %s", len(records), path)
    except OSError as exc:
        logger.error("Failed to save approvals to %s: %s", path, exc)
        raise


def _record_to_approval(raw: dict[str, Any]) -> DocumentApproval:
    """Convert a raw dict to a DocumentApproval dataclass instance.

    Unknown keys are silently ignored.

    Args:
        raw: Dict read from the approvals JSON file.

    Returns:
        Populated DocumentApproval instance.
    """
    return DocumentApproval(
        document_id=str(raw.get("document_id", "")),
        role=str(raw.get("role", "")),
        actor=str(raw.get("actor", "")),
        status=str(raw.get("status", "")),
        timestamp=str(raw.get("timestamp", "")),
        notes=str(raw.get("notes", "")),
        overridden_findings=list(raw.get("overridden_findings") or []),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_approval(
    doc_id: str,
    role: str,
    actor: str,
    status: str,
    notes: str = "",
    overridden_findings: list[str] | None = None,
    repo_path: Path | None = None,
) -> DocumentApproval:
    """Record an approval action for a governed document.

    Creates a new DocumentApproval, persists it to
    ``.saturnday/document/approvals.json`` inside ``repo_path``, and returns
    the approval object.

    When ``repo_path`` is ``None`` the approval is returned but not persisted.
    This supports unit-test usage.

    Args:
        doc_id: Document run identifier (matches DocumentPlan.document_id).
        role: The sign-off role this approval satisfies (e.g. ``"cfo"``).
        actor: Human-readable name of the approver (e.g. ``"Jane Doe"``).
        status: One of ``"approved"``, ``"rejected"``, or ``"pending"``.
        notes: Optional reviewer comment or rationale.
        overridden_findings: Finding IDs this approver explicitly accepted.
        repo_path: Repo root used for locating the approvals file.  When
            ``None`` the record is returned but not written to disk.

    Returns:
        The new DocumentApproval record.

    Raises:
        ValueError: If ``status`` is not one of the accepted values.
    """
    valid_statuses = {"approved", "rejected", "pending"}
    if status not in valid_statuses:
        raise ValueError(
            f"Invalid approval status '{status}'; "
            f"must be one of {sorted(valid_statuses)}"
        )

    approval = DocumentApproval(
        document_id=doc_id,
        role=role,
        actor=actor,
        status=status,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        notes=notes,
        overridden_findings=list(overridden_findings or []),
    )

    if repo_path is not None:
        existing = _load_approvals(repo_path)
        # Replace any existing record for the same doc_id + role combination
        # so re-approvals overwrite rather than accumulate stale entries.
        updated = [
            r for r in existing
            if not (r.get("document_id") == doc_id and r.get("role") == role)
        ]
        updated.append({
            "document_id": approval.document_id,
            "role": approval.role,
            "actor": approval.actor,
            "status": approval.status,
            "timestamp": approval.timestamp,
            "notes": approval.notes,
            "overridden_findings": approval.overridden_findings,
        })
        _save_approvals(repo_path, updated)
        logger.info(
            "Recorded %s by %s (%s) for document %s",
            status, actor, role, doc_id,
        )

    return approval


def check_signoff_requirements(
    doc_id: str,
    spec: DocumentSpec,
    approvals: list[DocumentApproval],
) -> tuple[bool, list[str]]:
    """Verify that all required sign-off roles have approved the document.

    A document is considered fully signed-off when every role listed in
    ``spec.sign_off_roles`` has at least one corresponding approval record
    with ``status == "approved"`` for this ``doc_id``.

    High-risk documents (``risk_class == "high"``) that have outstanding
    rejections cannot reach APPROVED even if all roles have approved.

    Args:
        doc_id: The document run identifier to check approvals for.
        spec: The DocumentSpec defining required sign-off roles and risk class.
        approvals: All known approval records (may include other doc IDs).

    Returns:
        Tuple of ``(all_satisfied, missing_roles)`` where:

        - ``all_satisfied`` is ``True`` only when every required role has
          an approved record and there are no rejections for high-risk docs.
        - ``missing_roles`` is a list of role strings that still need
          approval (empty when all_satisfied is True).
    """
    # Filter to approvals for this document only
    doc_approvals = [a for a in approvals if a.document_id == doc_id]

    # Build a map: role -> latest status
    role_status: dict[str, str] = {}
    for a in doc_approvals:
        # Keep the most recent approval per role (list is append-ordered)
        role_status[a.role] = a.status

    required_roles: list[str] = list(spec.sign_off_roles)

    # Check for any rejection — blocks approval for all risk classes
    rejected_roles = [r for r, s in role_status.items() if s == "rejected"]
    if rejected_roles:
        missing = [r for r in required_roles if role_status.get(r) != "approved"]
        # Also add rejectors as missing if they haven't subsequently approved
        return (False, missing or rejected_roles)

    # Find roles that haven't been approved yet
    missing_roles = [r for r in required_roles if role_status.get(r) != "approved"]

    all_satisfied = len(missing_roles) == 0
    return (all_satisfied, missing_roles)


def load_approvals_for_doc(doc_id: str, repo_path: Path) -> list[DocumentApproval]:
    """Load all approval records for a specific document from disk.

    Args:
        doc_id: The document run identifier.
        repo_path: Repo root where ``.saturnday/document/approvals.json`` lives.

    Returns:
        List of DocumentApproval objects for the given document.
    """
    raw_records = _load_approvals(repo_path)
    return [
        _record_to_approval(r)
        for r in raw_records
        if r.get("document_id") == doc_id
    ]
