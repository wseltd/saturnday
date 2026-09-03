"""Read-only evidence listing for ``saturnday evidence list``.

Lists all governance, run, and release evidence bundles in a repo
without modifying state or dumping raw JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from saturnday.evidence_type import detect_evidence_type

logger = logging.getLogger(__name__)


def gather_evidence(repo_path: Path) -> list[dict[str, Any]]:
    """Gather all evidence bundles from a repo.  Read-only.

    Returns a list of dicts sorted by timestamp (newest first),
    each with: category, run_id, path, disposition (if available).
    """
    repo_path = repo_path.resolve()
    bundles: list[dict[str, Any]] = []

    # Governance and release evidence (.saturnday/evidence/*)
    evidence_dir = repo_path / ".saturnday" / "evidence"
    if evidence_dir.is_dir():
        for d in evidence_dir.iterdir():
            if not d.is_dir():
                continue
            etype = detect_evidence_type(d)
            if etype == "governance":
                entry: dict[str, Any] = {
                    "category": "governance",
                    "run_id": d.name,
                    "path": str(d),
                    "disposition": None,
                }
                disp_file = d / "final-disposition.json"
                if disp_file.is_file():
                    try:
                        data = json.loads(disp_file.read_text(encoding="utf-8"))
                        entry["disposition"] = data.get("disposition")
                    except Exception:
                        pass
                bundles.append(entry)
            elif d.name.startswith("release"):
                # Release evidence is nested: .saturnday/evidence/release/release_*/
                for rd in d.iterdir():
                    if rd.is_dir() and detect_evidence_type(rd) == "release":
                        rentry: dict[str, Any] = {
                            "category": "release",
                            "run_id": rd.name,
                            "path": str(rd),
                            "disposition": None,
                            "exception_count": None,
                        }
                        ev_file = rd / "evidence.json"
                        if ev_file.is_file():
                            try:
                                data = json.loads(ev_file.read_text(encoding="utf-8"))
                                rentry["disposition"] = data.get("disposition")
                            except Exception:
                                pass
                        # Count exceptions if present
                        from saturnday.release_exception_list import count_exceptions
                        exc_counts = count_exceptions(rd)
                        if exc_counts["total"] > 0:
                            rentry["exception_count"] = exc_counts
                        # Count signoffs if present
                        from saturnday.release_signoff_list import count_signoffs
                        sig_counts = count_signoffs(rd)
                        if sig_counts["total"] > 0:
                            rentry["signoff_count"] = sig_counts
                        bundles.append(rentry)

    # Run evidence (.saturnday/run/run_*)
    run_dir = repo_path / ".saturnday" / "run"
    if run_dir.is_dir():
        for d in run_dir.iterdir():
            if d.is_dir() and d.name.startswith("run_"):
                bundles.append({
                    "category": "run",
                    "run_id": d.name,
                    "path": str(d),
                    "disposition": None,
                })

    # Sort newest first by run_id (which contains timestamps)
    bundles.sort(key=lambda b: b["run_id"], reverse=True)
    return bundles


def format_evidence_list(bundles: list[dict[str, Any]]) -> str:
    """Format evidence bundles as human-readable output."""
    if not bundles:
        return "  No evidence bundles found.\n  Run saturnday governance --repo . --full to create the first one."

    lines: list[str] = []
    lines.append(f"  Evidence bundles: {len(bundles)}")
    lines.append("")

    for b in bundles:
        disp = b.get("disposition")
        disp_str = f"  [{disp}]" if disp else ""
        exc = b.get("exception_count")
        sig = b.get("signoff_count")
        extras = []
        if sig:
            extras.append(f"{sig['distinct_approvers']} approver(s)")
        if exc:
            extras.append(f"{exc['active']} active, {exc['expired']} expired exceptions")
        extra_str = f"  ({'; '.join(extras)})" if extras else ""
        lines.append(f"    {b['category']:12s} {b['run_id']}{disp_str}{extra_str}")
        lines.append(f"                 {b['path']}")

    return "\n".join(lines)
