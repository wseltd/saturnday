"""MemoryProvider protocol for the Saturnday public/premium split.

Defines the exact call signature that a premium memory implementation must
satisfy in order to be registered with the capability registry under the key
``"memory_provider"``.

Premium implementation lives in:
- ``saturnday.run.memory_retrieval`` (retrieve + cleanup)
- ``saturnday.run.memory_enforcement`` (enforce)

Public orchestration code should never import those modules directly.  Instead
it calls ``capability_registry.get("memory_provider")`` and invokes the
protocol methods, falling back gracefully when no handler is registered.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import sqlite3

    from saturnday._types import TicketSpec  # noqa: F401 -- type-check only

logger = logging.getLogger(__name__)

STAGE_NAME: str = "memory_provider"

__all__ = ["MemoryProvider", "STAGE_NAME"]


@runtime_checkable
class MemoryProvider(Protocol):
    """Protocol for the premium memory subsystem.

    A conforming implementation wraps three distinct memory operations:

    - ``retrieve``:  Composite of filter_relevant_items + rank_and_select.
      Returns the top-k memory items relevant to the given ticket and repo
      state.  Premium implementation: ``saturnday.run.memory_retrieval``.

    - ``enforce``:  Checks actively enforced rules against the changed files
      in the current run, returning violations.  Premium implementation:
      ``saturnday.run.memory_enforcement.check_enforced_rules``.

    - ``cleanup``:  Expires stale memory entries and returns the count of
      removed rows.  Premium implementation:
      ``saturnday.run.memory_retrieval.run_staleness_cleanup``.
    """

    def retrieve(
        self,
        conn: sqlite3.Connection,
        ticket: Any,
        repo_path: Path,
        top_k: int = 5,
    ) -> list[dict]:
        """Return top-k memory items relevant to *ticket* in *repo_path*.

        Args:
            conn:       Open SQLite connection to the memory database.
            ticket:     The ticket being executed (``TicketSpec`` at runtime).
            repo_path:  Absolute path to the repository root.
            top_k:      Maximum number of items to return (default 5).

        Returns:
            A list of memory-item dicts, ranked by relevance.  Each dict
            structure is defined by the premium implementation.
        """
        ...  # pragma: no cover

    def enforce(
        self,
        conn: sqlite3.Connection,
        changed_files: list[str],
        repo_path: Path,
    ) -> list[dict]:
        """Check enforced rules against *changed_files*.

        Args:
            conn:           Open SQLite connection to the memory database.
            changed_files:  Repo-relative paths of files modified in this run.
            repo_path:      Absolute path to the repository root.

        Returns:
            A list of violation dicts.  Empty list means no violations.
        """
        ...  # pragma: no cover

    def cleanup(
        self,
        conn: sqlite3.Connection,
        repo_path: Path,
    ) -> int:
        """Expire stale memory entries.

        Args:
            conn:       Open SQLite connection to the memory database.
            repo_path:  Absolute path to the repository root.

        Returns:
            The number of rows removed from the memory store.
        """
        ...  # pragma: no cover
