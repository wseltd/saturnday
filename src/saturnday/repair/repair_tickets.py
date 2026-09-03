"""Generate bounded repair tickets from scanner findings.

Each ``RepairTicket`` groups findings of the same kind within the same file
(or across the repo when ``group_by="check"``), providing a bounded unit of
work for the repair executor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from saturnday.guard.cloud_scanner import Finding
from saturnday.repair.finding_locality import is_file_local

logger = logging.getLogger(__name__)


@dataclass
class RepairTicket:
    """A single bounded repair unit derived from scanner findings.

    Attributes:
        ticket_id: Sequential identifier, e.g. ``REPAIR-001``.
        title: Human-readable summary of the repair.
        severity: Severity of the first finding in the group.
        file_path: Primary file targeted by this ticket.
        line: Line number of the first finding (``None`` for whole-file checks).
        finding_kind: The ``kind`` field shared by all findings in this ticket.
        evidence: Formatted ``"file:line: message"`` strings for every finding.
        remediation: Structured guidance dict (``why``, ``fix``, ``patch``),
            copied from the first finding if guidance was attached by the scanner.
        group_key: The grouping key used to create this ticket.
    """

    ticket_id: str
    title: str
    severity: str
    file_path: str
    line: int | None
    finding_kind: str
    evidence: list[str] = field(default_factory=list)
    remediation: dict | None = None
    group_key: str = ""

    def to_dict(self) -> dict:
        """Serialise ticket to a plain dict for JSON persistence.

        Returns:
            Dictionary containing all ticket fields, suitable for
            ``json.dumps`` and round-tripping via ``from_dict``.
        """
        return {
            "ticket_id": self.ticket_id,
            "title": self.title,
            "severity": self.severity,
            "file_path": self.file_path,
            "line": self.line,
            "finding_kind": self.finding_kind,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "group_key": self.group_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RepairTicket":
        """Deserialise a ticket from a plain dict.

        Args:
            data: Dictionary as produced by ``to_dict`` or a repair-plan JSON.

        Returns:
            A ``RepairTicket`` instance with all fields populated.

        Raises:
            ValueError: If any required key (``ticket_id``, ``finding_kind``,
                ``file_path``) is missing from *data*.
        """
        for key in ("ticket_id", "finding_kind", "file_path"):
            if key not in data:
                raise ValueError(f"Missing required key: {key}")
        return cls(
            ticket_id=data["ticket_id"],
            title=data.get("title", ""),
            severity=data.get("severity", "medium"),
            file_path=data["file_path"],
            line=data.get("line"),
            finding_kind=data["finding_kind"],
            evidence=data.get("evidence", []),
            remediation=data.get("remediation"),
            group_key=data.get("group_key", ""),
        )


def _normalise_target_path(
    finding_kind: str,
    finding_file: str,
    repo_path: Path | None,
) -> str:
    """Decide the ticket's ``file_path`` for a finding.

    Implements the Option-C hybrid gate (memo §4):

    1. If the finding kind is NOT file-local (per the
       ``finding_locality`` registry), the ticket must not carry a
       file path — the repair would need to address a repo-level
       concern.  Return ``""`` so the executor's global-scope path
       (see ``repair_executor.py`` around line 305) takes over.
    2. If the scanner emitted an empty / whitespace-only path,
       return ``""`` for the same reason.
    3. If ``repo_path`` is provided AND ``finding_file`` resolves
       to a directory under it, return ``""``.  This catches the
       concrete defect where scanners emit directory-shaped paths
       (e.g. ``tests_pass`` with ``file="tests"``) that would
       otherwise reach the executor's ``is_dir()`` backstop.
    4. Otherwise, return the concrete path unchanged — the happy
       path for correctly-localised file-local findings.

    Unknown finding kinds fall through case 1 (``is_file_local``
    returns ``False`` by design for unregistered kinds), which
    means they are safely normalised to global scope.  This
    behaviour is required by the memo.

    The executor's own ``is_dir()`` guard at
    ``repair_executor.py:212`` remains as a last-line backstop and
    is NOT removed.
    """
    # Case 1: repo-level or unknown kind.
    if not is_file_local(finding_kind):
        logger.info(
            "repair-ticket: normalising file_path to global scope — "
            "finding kind %r is repo-level or unknown in finding_locality registry",
            finding_kind,
        )
        return ""

    # Case 2: empty / missing path.
    stripped = (finding_file or "").strip()
    if not stripped:
        logger.info(
            "repair-ticket: normalising file_path to global scope — "
            "scanner emitted empty file for kind %r",
            finding_kind,
        )
        return ""

    # Case 3: directory path under repo (only when repo_path is supplied).
    if repo_path is not None:
        try:
            resolved = (repo_path / stripped)
            if resolved.is_dir():
                logger.info(
                    "repair-ticket: normalising file_path to global scope — "
                    "scanner emitted directory path %r for kind %r",
                    stripped, finding_kind,
                )
                return ""
        except (OSError, ValueError):
            # Path resolution failure — fall through to case 4 and let
            # the executor's backstop handle anything truly malformed.
            pass

    # Case 4: concrete file-local path survives unchanged.
    return stripped


def generate_repair_tickets(
    findings: list[Finding],
    *,
    group_by: str = "file",
    max_per_ticket: int = 10,
    repo_path: Path | None = None,
) -> list[RepairTicket]:
    """Convert scanner findings into bounded repair tickets.

    Groups findings by ``file+kind`` (default) or ``kind`` alone, then splits
    groups larger than ``max_per_ticket`` into sequential tickets.

    Args:
        findings: Findings from ``scan_skill()`` or ``scan_corpus()``.
        group_by: ``"file"`` groups by ``file + kind``; ``"check"`` groups by
            ``kind`` only (useful for cross-repo remediations).
        max_per_ticket: Maximum findings per ticket.  Groups larger than this
            are split into sequential tickets.
        repo_path: Repository root.  When provided, enables the
            directory-path check in :func:`_normalise_target_path`.  When
            ``None``, the locality + empty-path checks still apply; the
            executor's existing ``is_dir()`` backstop covers any
            directory-shaped path that survives to the executor.

    Returns:
        List of ``RepairTicket`` objects, numbered REPAIR-001, REPAIR-002, …
    """
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        if group_by == "file":
            key = f"{f.file}:{f.kind}"
        else:
            key = f.kind
        groups.setdefault(key, []).append(f)

    tickets: list[RepairTicket] = []
    counter = 1

    for group_key, group_findings in groups.items():
        # Split large groups so each ticket stays bounded
        for i in range(0, len(group_findings), max_per_ticket):
            chunk = group_findings[i : i + max_per_ticket]
            first = chunk[0]

            # Option-C normalisation — see _normalise_target_path.
            # Repo-level / unknown-kind / empty / directory-shaped
            # targets become "" so the executor uses its global-scope
            # path instead of the ``is_dir()`` failure backstop.
            normalised_file_path = _normalise_target_path(
                first.kind, first.file, repo_path,
            )

            if group_by == "file":
                title = f"Fix {first.kind} in {first.file}"
            else:
                title = f"Fix {first.kind} across repo"

            evidence = [
                f"{f.file}:{f.line}: {f.message}" if f.line is not None
                else f"{f.file}: {f.message}"
                for f in chunk
            ]

            ticket = RepairTicket(
                ticket_id=f"REPAIR-{counter:03d}",
                title=title,
                severity=first.severity,
                file_path=normalised_file_path,
                line=first.line,
                finding_kind=first.kind,
                evidence=evidence,
                remediation=first.remediation,
                group_key=group_key,
            )
            tickets.append(ticket)
            counter += 1

    return tickets
