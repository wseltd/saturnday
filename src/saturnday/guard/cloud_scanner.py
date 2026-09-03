"""Cloud scanner compatibility shim.

All security check logic has been migrated to the canonical scanner at
``saturnday.openclaw_scanner`` (``OPENCLAW_SECURITY_CHECKS`` registry and
helper functions).  This module re-exports ``Finding`` from the canonical
scanner and provides a backward-compatible ``scan_skill`` that runs only the
OpenClaw security checks (not the full policy-governed canonical check set).

The canonical scanner's ``scan_skill`` runs TS and Python code-quality checks
in addition to security checks.  The shim's ``scan_skill`` runs only the
eight security checks, matching the original cloud_scanner behavior expected
by publish_preflight, sarif, repair_executor, and related callers.

Callers that import ``Finding``, ``SkillScanResult``, ``discover_skills``,
``scan_skill``, ``scan_corpus``, or ``scan_repo`` from this module will
continue to work without changes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Re-export Finding and helpers from the canonical scanner
# ---------------------------------------------------------------------------

from saturnday.openclaw_scanner import (
    Finding,
    ScanSummary,
    DEFAULT_PER_SKILL_TIMEOUT_S,
    OPENCLAW_SECURITY_CHECKS,
    _compute_skill_md_hash,
    _collect_skill_files,
    _enrich_check_results_with_guidance,
    discover_skills,
)
import saturnday.openclaw_scanner as _canonical


# ---------------------------------------------------------------------------
# Compatibility SkillScanResult
# ---------------------------------------------------------------------------

@dataclass
class SkillScanResult:
    """Backward-compatible scan result for cloud_scanner callers.

    Exposes a ``findings`` attribute as a flat list of ``Finding`` objects,
    matching the original cloud_scanner API used by publish_preflight, sarif,
    repair_executor, and related tests.

    Attributes:
        relative_path: Relative path of the scanned skill directory.
        skill_md_hash: SHA-256 hash of SKILL.md (first 16 hex chars).
        status: ``"scanned"`` | ``"failed_timeout"`` | ``"failed_error"`` |
                ``"skipped"``.
        disposition: ``"PASS"`` | ``"WARN"`` | ``"FAIL"``.
        findings: Flat list of ``Finding`` objects from all checks.
        elapsed_s: Wall-clock seconds for this scan.
        error: Error message if status is ``"failed_*"`` or ``"skipped"``.
    """

    relative_path: str
    skill_md_hash: str
    status: str
    disposition: str = "PASS"
    findings: list[Finding] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str | None = None

    @property
    def findings_count(self) -> int:
        """Number of findings."""
        return len(self.findings)


# ---------------------------------------------------------------------------
# scan_skill — runs OpenClaw security checks only
# ---------------------------------------------------------------------------

def scan_skill(
    skill_dir: Path,
    *,
    timeout_s: int = DEFAULT_PER_SKILL_TIMEOUT_S,
) -> SkillScanResult:
    """Scan a single skill directory for OpenClaw security and hygiene issues.

    Runs the eight OpenClaw security checks from the canonical scanner
    registry (``OPENCLAW_SECURITY_CHECKS``):

    - skill_structure — SKILL.md heading and length
    - shell_danger — subprocess / os.system / eval patterns
    - remote_download — urllib / requests / wget / curl
    - credential_leak — hardcoded secrets
    - command_interpolation — f-string in subprocess calls
    - broad_filesystem — shutil.rmtree / os.unlink
    - missing_approval_gate — destructive without confirmation
    - publish_hygiene — test files + LICENSE

    This does NOT run the canonical scanner's full policy-governed TS/Python
    code-quality check set.  High-severity findings produce ``disposition=FAIL``;
    medium/low produce ``WARN``; nothing produces ``PASS``.

    Args:
        skill_dir: Path to the skill directory.
        timeout_s: Per-skill scan timeout in seconds.

    Returns:
        ``SkillScanResult`` with ``findings`` populated as ``Finding`` objects.
    """
    t0 = time.time()
    rel_path = skill_dir.name
    skill_md_hash = _compute_skill_md_hash(skill_dir)

    if not (skill_dir / "SKILL.md").exists():
        return SkillScanResult(
            relative_path=rel_path,
            skill_md_hash=skill_md_hash,
            status="skipped",
            error="no SKILL.md found",
            elapsed_s=time.time() - t0,
        )

    # Collect all file paths (content is read lazily by each check)
    files = _collect_skill_files(skill_dir)
    check_results: list[dict] = []
    timed_out = False

    for sec_fn in OPENCLAW_SECURITY_CHECKS:
        if time.time() - t0 >= timeout_s:
            timed_out = True
            break
        try:
            check_results.append(sec_fn(skill_dir, files))
        except Exception as exc:
            check_results.append({
                "name": getattr(sec_fn, "__name__", "unknown"),
                "status": "SKIPPED",
                "findings": [],
                "exit_code": 0,
                "raw_output": "",
                "error": str(exc),
            })

    # Apply remediation enrichment
    _enrich_check_results_with_guidance(check_results)

    # Build flat findings list
    all_findings: list[Finding] = []
    for cr in check_results:
        check_name = cr.get("name", "")
        for f in cr.get("findings", []):
            all_findings.append(Finding(
                check=check_name,
                severity=f.get("confidence", f.get("severity", "medium")),
                file=f.get("file", f.get("path", "")),
                line=f.get("line"),
                message=f.get("detail", f.get("message", "")),
                kind=f.get("kind", check_name),
                remediation=f.get("remediation"),
            ))

    # Compute disposition: FAIL on any high finding, WARN on medium/low, else PASS
    has_high = any(f.severity == "high" for f in all_findings)
    non_timeout = [f for f in all_findings if f.kind != "timeout_skip"]
    disposition = "FAIL" if has_high else ("WARN" if non_timeout else "PASS")
    status = "failed_timeout" if timed_out else "scanned"

    return SkillScanResult(
        relative_path=rel_path,
        skill_md_hash=skill_md_hash,
        status=status,
        disposition=disposition,
        findings=all_findings,
        elapsed_s=time.time() - t0,
        error=None,
    )


# ---------------------------------------------------------------------------
# scan_corpus — simple corpus scan writing findings.jsonl
# ---------------------------------------------------------------------------

def scan_corpus(
    corpus_root: Path,
    output_dir: Path,
    *,
    timeout_s: int = DEFAULT_PER_SKILL_TIMEOUT_S,
    limit: int | None = None,
) -> ScanSummary:
    """Scan an entire skill corpus using the OpenClaw security checks.

    Writes ``findings.jsonl`` incrementally for each scanned skill.

    Args:
        corpus_root: Root directory containing skill directories.
        output_dir: Directory for output files.
        timeout_s: Per-skill timeout.
        limit: Max number of skills to scan.

    Returns:
        ``ScanSummary`` with aggregate statistics.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    skills = discover_skills(corpus_root)
    if limit is not None:
        skills = skills[:limit]

    jsonl_path = output_dir / "findings.jsonl"
    summary = ScanSummary(corpus_root=str(corpus_root), total_candidates=len(skills))
    t_start = time.time()

    for skill_dir in skills:
        rel_path = str(skill_dir.relative_to(corpus_root))
        try:
            result = scan_skill(skill_dir, timeout_s=timeout_s)
            result.relative_path = rel_path
        except Exception as exc:
            result = SkillScanResult(
                relative_path=rel_path,
                skill_md_hash=_compute_skill_md_hash(skill_dir),
                status="failed_error",
                error=str(exc),
            )

        if result.status == "scanned":
            summary.total_scanned += 1
        elif result.status.startswith("failed"):
            summary.total_failed += 1
        else:
            summary.total_skipped += 1

        summary.total_findings += result.findings_count

        entry = {
            "relative_path": result.relative_path,
            "skill_md_hash": result.skill_md_hash,
            "status": result.status,
            "disposition": result.disposition,
            "findings_count": result.findings_count,
            "elapsed_s": round(result.elapsed_s, 3),
            "error": result.error,
            "findings": [f.to_dict() for f in result.findings],
        }
        with open(jsonl_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    summary.elapsed_s = time.time() - t_start
    return summary


# Compat alias — single-repo convenience wrapper
scan_repo = scan_corpus
