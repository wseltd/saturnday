"""Evidence output for repair runs."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from saturnday.repair.repair_runner import RepairRunResult


def write_repair_evidence(
    run_result: RepairRunResult,
    output_dir: Path,
    *,
    backend: str = "",
    skill_path: str = "",
) -> dict[str, Path]:
    """Write repair run evidence to *output_dir*.

    Creates two JSON artefacts:

    * ``repair-metadata.json`` — run context (skill path, backend, timestamp,
      ticket count).
    * ``repair-summary.json`` — outcome counters and per-ticket
      ``RepairResult`` records serialised via ``dataclasses.asdict``.

    Args:
        run_result: The ``RepairRunResult`` returned by ``run_repair_batch``.
        output_dir: Directory to write artefacts into (created if absent).
        backend: Coder backend identifier for metadata (e.g. ``"claude-cli"``).
        skill_path: String path to the scanned skill directory for metadata.

    Returns:
        Mapping of ``{"metadata": Path, "summary": Path}`` for callers that
        need to reference or verify the artefact locations.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata: dict = {
        "skill_path": str(skill_path),
        "backend": backend,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ticket_count": run_result.total_tickets,
    }
    meta_path = output_dir / "repair-metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    summary: dict = {
        "fixed": run_result.fixed,
        "partial": run_result.partial,
        "failed": run_result.failed,
        "stopped_early": run_result.stopped_early,
        "stop_reason": run_result.stop_reason,
        "tickets": [asdict(r) for r in run_result.results],
    }
    summary_path = output_dir / "repair-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {"metadata": meta_path, "summary": summary_path}
