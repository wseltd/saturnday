"""Release exception workflow with audit trail.

RS-027: Structured exception records with approver, expiry, and artefact-hash
binding.  Exceptions downgrade matched FAIL findings to WARN with an
``excepted_by`` annotation; they never suppress findings entirely.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "ExceptionRecord",
    "record_exception",
    "load_exceptions",
    "apply_exceptions",
]

_logger = logging.getLogger(__name__)

_EXCEPTION_SUBDIR = "release/exceptions"


@dataclass
class ExceptionRecord:
    """An immutable record of a release exception.

    Attributes:
        exception_id: UUID4 string uniquely identifying this exception.
        rule_ids: List of governance rule IDs covered by this exception.
        file_patterns: Glob patterns matching files the exception applies to.
        approver: Identity of the person granting the exception.
        reason: Justification for the exception.
        expiry: ISO 8601 UTC timestamp after which the exception expires.
            Empty string means no expiry.
        artefact_sha256: SHA-256 hex digest of the artefact at exception time.
        created_at: ISO 8601 UTC timestamp when the exception was recorded.
    """

    exception_id: str
    rule_ids: list[str]
    file_patterns: list[str]
    approver: str
    reason: str
    expiry: str  # ISO 8601 or ""
    artefact_sha256: str
    created_at: str


def _exception_dir(evidence_dir: Path) -> Path:
    """Return the exceptions subdirectory, creating it if absent."""
    d = evidence_dir / _EXCEPTION_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_exception(
    evidence_dir: Path,
    rule_ids: list[str],
    file_patterns: list[str],
    approver: str,
    reason: str,
    expiry: str | None,
    artefact_sha256: str,
) -> ExceptionRecord:
    """Write a signed exception record to the evidence directory.

    The record is persisted as JSON under
    ``evidence_dir/release/exceptions/<exception_id>.json``.

    Args:
        evidence_dir: Root evidence directory for this release run.
        rule_ids: Governance rule IDs covered by this exception.  At least
            one is required.
        file_patterns: Glob patterns matching files the exception applies to.
            May be empty to apply to all files for the given rule IDs.
        approver: Identity of the approver granting the exception.
        reason: Justification text.  Must not be empty.
        expiry: ISO 8601 timestamp after which the exception expires, or
            ``None`` / empty string for no expiry.
        artefact_sha256: SHA-256 hex digest of the artefact at exception time.

    Returns:
        The persisted :class:`ExceptionRecord`.

    Raises:
        ValueError: If *rule_ids* is empty, *reason* is empty, or
            *approver* is empty.
        OSError: If the record cannot be written.
    """
    if not rule_ids:
        raise ValueError("rule_ids must not be empty")
    if not approver or not approver.strip():
        raise ValueError("approver must not be empty")
    if not reason or not reason.strip():
        raise ValueError("reason must not be empty")

    exception_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc).isoformat()
    expiry_str = expiry.strip() if expiry else ""

    record = ExceptionRecord(
        exception_id=exception_id,
        rule_ids=[r for r in rule_ids if r],
        file_patterns=[p for p in (file_patterns or []) if p],
        approver=approver.strip(),
        reason=reason.strip(),
        expiry=expiry_str,
        artefact_sha256=artefact_sha256 or "",
        created_at=created_at,
    )

    out_path = _exception_dir(evidence_dir) / f"{exception_id}.json"
    out_path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    _logger.info(
        "Exception recorded: %s — rules=%s approver=%s expiry=%s",
        exception_id,
        rule_ids,
        approver,
        expiry_str or "none",
    )
    return record


def load_exceptions(evidence_dir: Path) -> list[ExceptionRecord]:
    """Load all active (non-expired) exceptions from the evidence directory.

    Reads every ``*.json`` file under ``evidence_dir/release/exceptions/``.
    Expired records (where ``expiry`` is a past ISO 8601 timestamp) are
    filtered out.  Malformed files are logged as warnings and skipped.

    Args:
        evidence_dir: Root evidence directory for this release run.

    Returns:
        List of non-expired :class:`ExceptionRecord` instances, possibly empty.
    """
    exc_dir = evidence_dir / _EXCEPTION_SUBDIR
    if not exc_dir.is_dir():
        return []

    now = datetime.now(tz=timezone.utc)
    records: list[ExceptionRecord] = []

    for path in sorted(exc_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expiry_str = data.get("expiry", "")
            if expiry_str:
                try:
                    expiry_dt = datetime.fromisoformat(expiry_str)
                    # Make timezone-aware if naive (assume UTC).
                    if expiry_dt.tzinfo is None:
                        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                    if expiry_dt <= now:
                        _logger.debug("Exception %s expired at %s — skipping", path.stem, expiry_str)
                        continue
                except ValueError:
                    _logger.warning(
                        "Unrecognised expiry format in %s: %r — treating as active", path, expiry_str
                    )

            records.append(
                ExceptionRecord(
                    exception_id=data["exception_id"],
                    rule_ids=data.get("rule_ids", []),
                    file_patterns=data.get("file_patterns", []),
                    approver=data["approver"],
                    reason=data["reason"],
                    expiry=expiry_str,
                    artefact_sha256=data.get("artefact_sha256", ""),
                    created_at=data["created_at"],
                )
            )
        except (KeyError, json.JSONDecodeError, OSError) as exc:
            _logger.warning("Skipping malformed exception file %s: %s", path, exc)

    return records


def apply_exceptions(
    findings: list[dict],
    exceptions: list[ExceptionRecord],
) -> list[dict]:
    """Downgrade matched FAIL findings to WARN with an ``excepted_by`` annotation.

    A finding is matched if:

    1. Its ``rule_id`` (or ``check_id``) is in ``exception.rule_ids``, AND
    2. Either the exception has no ``file_patterns``, OR at least one of the
       finding's ``file`` / ``path`` / ``files`` values matches a pattern.

    Matched FAIL findings are downgraded to ``WARN`` and annotated with an
    ``excepted_by`` field containing the matching exception IDs and their
    reasons.  Non-FAIL findings and unmatched findings are returned unchanged.

    Args:
        findings: List of finding dicts (each must have at least a ``status``
            key and a ``rule_id`` or ``check_id`` key).
        exceptions: List of active :class:`ExceptionRecord` instances.

    Returns:
        New list of finding dicts.  The input list is not mutated.
    """
    if not exceptions:
        return list(findings)

    result: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            result.append(finding)
            continue

        if finding.get("status") != "FAIL":
            result.append(finding)
            continue

        rule_id = finding.get("rule_id") or finding.get("check_id") or ""
        finding_files: list[str] = _extract_files(finding)

        matched_exceptions: list[ExceptionRecord] = []
        for exc in exceptions:
            if rule_id not in exc.rule_ids:
                continue
            if not exc.file_patterns:
                # Exception applies to all files for this rule.
                matched_exceptions.append(exc)
                continue
            # Match if any finding file matches any exception pattern.
            if any(
                fnmatch.fnmatch(f, pat)
                for f in finding_files
                for pat in exc.file_patterns
            ):
                matched_exceptions.append(exc)

        if matched_exceptions:
            updated = dict(finding)
            updated["status"] = "WARN"
            updated["excepted_by"] = [
                {"exception_id": e.exception_id, "reason": e.reason}
                for e in matched_exceptions
            ]
            result.append(updated)
        else:
            result.append(finding)

    return result


def _extract_files(finding: dict) -> list[str]:
    """Extract file/path references from a finding dict."""
    files: list[str] = []
    for key in ("file", "path", "files", "paths"):
        val = finding.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            files.append(val)
        elif isinstance(val, list):
            files.extend(str(v) for v in val)
    return files
