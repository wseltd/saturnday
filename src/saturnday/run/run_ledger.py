"""Run ledger: execution state tracking and stop engine.

Ports proven behaviour from saturnday-v3 autopilot.py:
- ``_normalize_marker()`` (v3 line 976): slug-based marker normalization
- Consecutive failure tracking (v3 stop condition evaluation)
- Definition of done evaluation (v3 line 2496)
- Phase status transitions (v3 lines 1596-1616, 2011-2027)
- Max project tickets enforcement (v3 lines 1633-1695)

The RunLedger is **mutable** — it accumulates state during a run.
RunResult (in _types.py) is **frozen** — it's the final snapshot.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KNOWN_DEFINITION_MARKERS = frozenset({"tickets_applied", "all_tickets_passed"})

DEFAULT_CONSECUTIVE_FAILURE_LIMIT = 3


def _normalize_marker(text: str) -> str:
    """Normalize marker text to a machine-readable slug.

    Ported from v3 autopilot.py line 976 — exact same logic.

    Converts text to lowercase alphanumerics + underscores, removes
    consecutive underscores, and strips leading/trailing underscores.

    Examples:
        >>> _normalize_marker("Tickets Applied")
        'tickets_applied'
        >>> _normalize_marker("All Tickets Passed")
        'all_tickets_passed'
        >>> _normalize_marker("Stop on Compile Error")
        'stop_on_compile_error'
        >>> _normalize_marker("")
        ''
    """
    if not isinstance(text, str):
        return ""
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in text)
    slug = "_".join(part for part in slug.split("_") if part)
    return slug


class PhaseStatus:
    """Mutable status for a single execution phase."""

    __slots__ = (
        "phase_id", "name", "ticket_ids", "status",
        "started_utc", "completed_utc",
    )

    def __init__(
        self,
        phase_id: str,
        name: str = "",
        ticket_ids: tuple[str, ...] = (),
    ) -> None:
        self.phase_id = phase_id
        self.name = name or phase_id
        self.ticket_ids = ticket_ids
        self.status: str = "PENDING"
        self.started_utc: str = ""
        self.completed_utc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "name": self.name,
            "ticket_ids": list(self.ticket_ids),
            "status": self.status,
            "started_utc": self.started_utc,
            "completed_utc": self.completed_utc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhaseStatus:
        ps = cls(
            phase_id=data.get("phase_id", ""),
            name=data.get("name", ""),
            ticket_ids=tuple(data.get("ticket_ids", [])),
        )
        ps.status = data.get("status", "PENDING")
        ps.started_utc = data.get("started_utc", "")
        ps.completed_utc = data.get("completed_utc", "")
        return ps


class TicketStatus:
    """Mutable status for a single ticket within the ledger."""

    __slots__ = ("ticket_id", "disposition", "failure_category", "reasons")

    def __init__(self, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        self.disposition: str = "PENDING"
        self.failure_category: str = ""
        self.reasons: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "disposition": self.disposition,
            "failure_category": self.failure_category,
            "reasons": self.reasons,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TicketStatus:
        ts = cls(data.get("ticket_id", ""))
        ts.disposition = data.get("disposition", "PENDING")
        ts.failure_category = data.get("failure_category", "")
        ts.reasons = data.get("reasons", [])
        return ts


class RunLedger:
    """Mutable execution ledger — tracks run state and enforces stop conditions.

    The ledger accumulates state as tickets execute. It is persisted to
    ``evidence/run/ledger.json`` after each ticket for crash recovery.
    """

    def __init__(
        self,
        definition_of_done: tuple[str, ...] = ("all_tickets_passed",),
        stop_conditions: tuple[str, ...] = (),
        max_project_tickets: int | None = None,
        phases: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
        ticket_ids: tuple[str, ...] = (),
        consecutive_failure_limit: int = DEFAULT_CONSECUTIVE_FAILURE_LIMIT,
    ) -> None:
        self.definition_of_done = definition_of_done
        self.stop_conditions = tuple(
            _normalize_marker(c) for c in stop_conditions if c
        )
        self.max_project_tickets = max_project_tickets
        self.consecutive_failure_limit = consecutive_failure_limit

        # Counters
        self.consecutive_failures: int = 0
        self.total_executed: int = 0
        self.total_passed: int = 0
        self.total_failed: int = 0
        self.total_skipped: int = 0

        # Phase tracking
        self.phase_statuses: list[PhaseStatus] = []
        self._phase_by_ticket: dict[str, int] = {}
        for phase_id, name, tids in phases:
            idx = len(self.phase_statuses)
            self.phase_statuses.append(
                PhaseStatus(phase_id=phase_id, name=name, ticket_ids=tids)
            )
            for tid in tids:
                self._phase_by_ticket[tid] = idx

        # Ticket tracking
        self.ticket_statuses: dict[str, TicketStatus] = {}
        for tid in ticket_ids:
            self.ticket_statuses[tid] = TicketStatus(tid)

        # Stop state
        self.stopped: bool = False
        self.stop_reason: str = ""

        # Parent-level consecutive failure tracking.
        # Sub-tickets from the same parent count as one failure signal.
        self._last_failure_parent: str = ""

    def record_ticket_result(
        self,
        ticket_id: str,
        disposition: str,
        failure_category: str = "",
        reasons: list[str] | None = None,
        parent_ticket_id: str = "",
    ) -> None:
        """Record the outcome of a ticket execution.

        Args:
            ticket_id: The ticket that was executed.
            disposition: ``PASS``, ``FAIL``, ``CODED_UNGOVERNED``, or ``SKIP``.
            failure_category: Category of failure (empty for PASS).
            reasons: Raw failure reason strings for stop condition matching.
            parent_ticket_id: If this ticket is a sub-ticket from a split,
                the parent ticket ID.  Multiple failed sub-tickets from the
                same parent count as **one** consecutive failure signal for
                whole-run stop purposes.
        """
        self.total_executed += 1

        # Determine the effective parent for consecutive failure grouping.
        # If no parent given, the ticket is its own parent.
        effective_parent = parent_ticket_id or ticket_id

        if disposition == "PASS":
            self.total_passed += 1
            self.consecutive_failures = 0
            self._last_failure_parent = ""
        elif disposition == "CODED_UNGOVERNED":
            self.total_failed += 1  # Counts in failed for ledger stats
            self.consecutive_failures = 0  # Code was produced — reset
            self._last_failure_parent = ""
        elif disposition == "FAIL":
            self.total_failed += 1
            # Only increment consecutive_failures if this is a new parent
            # failing.  Multiple sub-ticket failures under the same parent
            # count as one failure signal.
            if effective_parent != self._last_failure_parent:
                self.consecutive_failures += 1
                self._last_failure_parent = effective_parent
        elif disposition == "SKIP":
            self.total_skipped += 1
            # Skips don't reset consecutive failures

        # Update ticket status
        if ticket_id not in self.ticket_statuses:
            self.ticket_statuses[ticket_id] = TicketStatus(ticket_id)
        ts = self.ticket_statuses[ticket_id]
        ts.disposition = disposition
        ts.failure_category = failure_category
        ts.reasons = reasons or []

        logger.debug(
            "Ledger: %s → %s (consecutive_failures=%d, executed=%d)",
            ticket_id, disposition, self.consecutive_failures, self.total_executed,
        )

    def check_stop_conditions(
        self,
        ticket_id: str = "",
        reasons: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Check whether the run should stop after the current ticket.

        Evaluates in order:
        1. Consecutive failure limit
        2. Max project tickets
        3. Stop condition marker matching (v3 line 2029)

        Returns:
            Tuple of (should_stop, reason).
        """
        # 1. Consecutive failure limit
        if self.consecutive_failures >= self.consecutive_failure_limit:
            reason = (
                f"consecutive_failure_limit: {self.consecutive_failures} "
                f"consecutive failures (limit: {self.consecutive_failure_limit})"
            )
            self.stopped = True
            self.stop_reason = reason
            return True, reason

        # 2. Max project tickets
        if (
            self.max_project_tickets is not None
            and self.total_executed >= self.max_project_tickets
        ):
            reason = (
                f"max_project_tickets: executed {self.total_executed} "
                f"(limit: {self.max_project_tickets})"
            )
            self.stopped = True
            self.stop_reason = reason
            return True, reason

        # 3. Stop condition marker matching (ported from v3 line 2029)
        if self.stop_conditions and reasons:
            normalized_reasons = sorted({
                _normalize_marker(r) for r in reasons if r
            })
            matched = sorted(
                {c for c in self.stop_conditions if c and c in normalized_reasons}
            )
            if matched:
                reason = f"stop_condition_met: {', '.join(matched)}"
                self.stopped = True
                self.stop_reason = reason
                return True, reason

        return False, ""

    def evaluate_definition_of_done(self) -> bool:
        """Evaluate whether the definition of done is met.

        Ported from v3 autopilot.py line 2496:
        - If ``definition_of_done`` is empty, returns True (no requirement).
        - Otherwise: all tickets must be executed AND zero failed AND
          at least one known marker is present.

        Returns:
            True if the definition of done is satisfied.
        """
        if not self.definition_of_done:
            return True

        total_tickets = len(self.ticket_statuses)
        if total_tickets == 0:
            return False

        if self.total_executed != total_tickets:
            return False

        # Count only true FAILs (not coded at all), not CODED_UNGOVERNED
        true_fails = sum(
            1 for ts in self.ticket_statuses.values()
            if ts.disposition == "FAIL"
        )
        if true_fails > 0:
            return False

        # At least one known marker must be present
        has_known = any(
            marker in KNOWN_DEFINITION_MARKERS
            for marker in self.definition_of_done
        )
        return has_known

    def update_phase_status(self, ticket_id: str) -> None:
        """Update phase status after a ticket completes.

        Ported from v3 autopilot.py lines 2011-2027:
        - A phase transitions to terminal state only when ALL its tickets
          have execution records.
        - Phase is PASS if zero failed, FAIL otherwise.
        """
        phase_idx = self._phase_by_ticket.get(ticket_id)
        if phase_idx is None:
            return

        phase = self.phase_statuses[phase_idx]

        # Mark phase as started on first ticket execution
        if phase.status == "PENDING":
            phase.status = "IN_PROGRESS"
            phase.started_utc = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        # Check if all phase tickets have execution records
        completed: list[str] = []
        failed: list[str] = []
        for tid in phase.ticket_ids:
            ts = self.ticket_statuses.get(tid)
            if ts is None or ts.disposition == "PENDING":
                return  # Not all tickets done yet
            if ts.disposition == "PASS":
                completed.append(tid)
            else:
                failed.append(tid)

        # All tickets in this phase have terminal disposition
        phase.completed_utc = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        phase.status = "PASS" if not failed else "FAIL"
        logger.info(
            "Phase %s completed: %s (%d passed, %d failed)",
            phase.phase_id, phase.status, len(completed), len(failed),
        )

    def should_skip_ticket(self, ticket_id: str) -> tuple[bool, str]:
        """Check whether a ticket should be skipped.

        Reasons to skip:
        1. Max project tickets already reached
        2. Run was stopped by a prior stop condition

        Returns:
            Tuple of (should_skip, reason).
        """
        if self.stopped:
            return True, f"Run stopped: {self.stop_reason}"

        if (
            self.max_project_tickets is not None
            and self.total_executed >= self.max_project_tickets
        ):
            return True, (
                f"max_project_tickets reached: {self.total_executed}"
                f"/{self.max_project_tickets}"
            )

        return False, ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize ledger state for persistence and resume."""
        return {
            "definition_of_done": list(self.definition_of_done),
            "stop_conditions": list(self.stop_conditions),
            "max_project_tickets": self.max_project_tickets,
            "consecutive_failure_limit": self.consecutive_failure_limit,
            "consecutive_failures": self.consecutive_failures,
            "total_executed": self.total_executed,
            "total_passed": self.total_passed,
            "total_failed": self.total_failed,
            "total_skipped": self.total_skipped,
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
            "_last_failure_parent": self._last_failure_parent,
            "phase_statuses": [ps.to_dict() for ps in self.phase_statuses],
            "ticket_statuses": {
                tid: ts.to_dict()
                for tid, ts in self.ticket_statuses.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunLedger:
        """Restore ledger state from a persisted snapshot."""
        ledger = cls.__new__(cls)
        ledger.definition_of_done = tuple(data.get("definition_of_done", ()))
        ledger.stop_conditions = tuple(data.get("stop_conditions", ()))
        ledger.max_project_tickets = data.get("max_project_tickets")
        ledger.consecutive_failure_limit = data.get(
            "consecutive_failure_limit", DEFAULT_CONSECUTIVE_FAILURE_LIMIT,
        )
        ledger.consecutive_failures = data.get("consecutive_failures", 0)
        ledger.total_executed = data.get("total_executed", 0)
        ledger.total_passed = data.get("total_passed", 0)
        ledger.total_failed = data.get("total_failed", 0)
        ledger.total_skipped = data.get("total_skipped", 0)
        ledger.stopped = data.get("stopped", False)
        ledger.stop_reason = data.get("stop_reason", "")
        ledger._last_failure_parent = data.get("_last_failure_parent", "")

        ledger.phase_statuses = [
            PhaseStatus.from_dict(ps)
            for ps in data.get("phase_statuses", [])
        ]
        ledger._phase_by_ticket = {}
        for idx, ps in enumerate(ledger.phase_statuses):
            for tid in ps.ticket_ids:
                ledger._phase_by_ticket[tid] = idx

        ledger.ticket_statuses = {
            tid: TicketStatus.from_dict(ts_data)
            for tid, ts_data in data.get("ticket_statuses", {}).items()
        }

        return ledger


def create_ledger_from_plan(
    definition_of_done: tuple[str, ...],
    stop_conditions: tuple[str, ...],
    max_project_tickets: int | None,
    phases: tuple[Any, ...],
    ticket_ids: tuple[str, ...],
    consecutive_failure_limit: int | None = None,
) -> RunLedger:
    """Create a RunLedger from plan fields.

    Args:
        definition_of_done: DoD markers from the plan.
        stop_conditions: Stop condition markers from the plan.
        max_project_tickets: Max tickets limit from the plan.
        phases: PhaseDef tuples from the plan.
        ticket_ids: All ticket IDs in execution order.
        consecutive_failure_limit: Optional override for the consecutive
            failure stop threshold.  ``None`` (the default) preserves the
            pre-existing behaviour of using
            :data:`DEFAULT_CONSECUTIVE_FAILURE_LIMIT`.

    Returns:
        A fresh RunLedger ready for execution.
    """
    phase_tuples: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
        (p.phase_id, p.name, p.ticket_ids) for p in phases
    )
    kwargs: dict[str, Any] = dict(
        definition_of_done=definition_of_done,
        stop_conditions=stop_conditions,
        max_project_tickets=max_project_tickets,
        phases=phase_tuples,
        ticket_ids=ticket_ids,
    )
    if consecutive_failure_limit is not None:
        kwargs["consecutive_failure_limit"] = consecutive_failure_limit
    return RunLedger(**kwargs)


def write_ledger_snapshot(ledger: RunLedger, output_dir: Path) -> Path:
    """Write the current ledger state to ``evidence/run/ledger.json``.

    Args:
        ledger: The current run ledger.
        output_dir: Base output directory.

    Returns:
        Path to the written ledger file.
    """
    evidence_dir = output_dir / "evidence" / "run"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "ledger.json"
    path.write_text(json.dumps(ledger.to_dict(), indent=2) + "\n", encoding="utf-8")
    logger.debug("Wrote ledger snapshot: %s", path)
    return path
