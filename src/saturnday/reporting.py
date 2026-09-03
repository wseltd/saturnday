"""Human-readable report generation for scan, repair, and run modes.

Provides three top-level generators:

- ``generate_scan_report``  — governance scan results to ``.saturnday/governance-report.md``
- ``generate_repair_report`` — repair run outcomes to ``.saturnday/repair-report.md``
- ``generate_run_report``   — plan execution results to ``{evidence_dir}/run-report.md``

All writers are defensive: external calls are wrapped so a formatting failure
never blocks the caller.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from saturnday.version import __version__

if TYPE_CHECKING:
    from saturnday.evidence import EvidencePack
    from saturnday.repair.repair_runner import RepairRunResult
    from saturnday._types import RunResult

logger = logging.getLogger(__name__)

# Finding kinds that represent environment / installation issues rather than
# code defects.  These are surfaced in a separate "Environment" section.
_ENV_KINDS: frozenset[str] = frozenset(
    {"declared_not_installed", "package_not_importable"}
)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _git_diff_stat(repo_path: Path) -> str:
    """Run ``git diff --stat`` and return the output as a string.

    Returns an empty string when git is not available, the directory is not a
    repository, or the subprocess call raises any exception.

    Args:
        repo_path: Root of the git repository to diff.

    Returns:
        Raw stdout from ``git diff --stat``, or an empty string on failure.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=30,
        )
        return result.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("_git_diff_stat failed: %s", exc)
        return ""


def _diff_analysis(repo_path: Path) -> str:
    """Analyse the git diff and produce a scoring section.

    Runs three git commands to extract:
    - Files touched (``git diff --name-only``)
    - Lines added/removed (``git diff --shortstat``)
    - New imports introduced (``git diff -U0`` line scan)

    All subprocess calls are individually wrapped so a failure in one does
    not prevent the others from running.

    Args:
        repo_path: Root of the git repository to diff.

    Returns:
        Multiline markdown string for the ``## Diff Analysis`` section.
    """
    lines: list[str] = ["## Diff Analysis\n"]

    # Files touched
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = [f for f in r.stdout.strip().splitlines() if f.strip()]
        lines.append(f"- **Files touched:** {len(files)}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("_diff_analysis: --name-only failed: %s", exc)
        files = []
        lines.append("- **Files touched:** unknown")

    # Lines added/removed
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--shortstat"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        stat = r.stdout.strip()
        if stat:
            lines.append(f"- **Changes:** {stat}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("_diff_analysis: --shortstat failed: %s", exc)

    # New dependencies (imports added in the diff)
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "-U0"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        new_imports: set[str] = set()
        for line in r.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                stripped = line[1:].strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    mod = stripped.split()[1].split(".")[0]
                    new_imports.add(mod)
        if new_imports:
            lines.append(f"- **New imports:** {', '.join(sorted(new_imports))}")
        else:
            lines.append("- **New imports:** none")
    except Exception as exc:  # noqa: BLE001
        logger.debug("_diff_analysis: -U0 failed: %s", exc)

    lines.append("")
    return "\n".join(lines)


def _format_check_table(check_results: list) -> str:
    """Format check results as a markdown table.

    Columns: Check | Status | Severity | Findings

    Args:
        check_results: List of ``CheckResult`` objects.

    Returns:
        Multiline markdown string including the header row and one row per check.
    """
    lines: list[str] = [
        "| Check | Status | Severity | Findings |",
        "|-------|--------|----------|----------|",
    ]
    for r in check_results:
        finding_count = len(r.findings) if r.findings else 0
        lines.append(
            f"| {r.name} | {r.status} | {r.severity} | {finding_count} |"
        )
    return "\n".join(lines)


def _read_role_pass(evidence_dir: Path, role_name: str) -> dict | None:
    """Read a ``role-pass-{role_name}.json`` file and return parsed dict.

    Tries multiple filename patterns used by different callers:
    - ``role-pass-{role_name}.json``
    - ``role-pass-{role_name.replace('_', '-')}.json``

    Args:
        evidence_dir: Directory containing role-pass JSON files.
        role_name: Role identifier, e.g. ``"repo_analyst"`` or ``"governance_judge"``.

    Returns:
        Parsed dict, or ``None`` if the file does not exist or cannot be parsed.
    """
    candidates = [
        evidence_dir / f"role-pass-{role_name}.json",
        evidence_dir / f"role-pass-{role_name.replace('_', '-')}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("_read_role_pass: failed to read %s: %s", path, exc)
    return None


def _generate_how_to_run(repo_path: Path) -> list[str]:
    """Generate a 'How to Run' section from project config files.

    Reads pyproject.toml and package.json for install/run commands.
    Returns markdown lines, or empty list if no clear signals found.
    """
    lines: list[str] = []
    repo = Path(repo_path)

    # Python project
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        lines.append("**Python project detected** (`pyproject.toml`)")
        lines.append("")
        lines.append("```bash")
        lines.append("pip install -e .")
        lines.append("```")
        # Check for console_scripts
        try:
            content = pyproject.read_text(encoding="utf-8")
            if "console_scripts" in content or "[project.scripts]" in content:
                lines.append("")
                lines.append("Console entry points are defined in `pyproject.toml`.")
        except Exception:
            pass
        # Check for test config
        if "pytest" in (pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""):
            lines.append("")
            lines.append("```bash")
            lines.append("python -m pytest tests/")
            lines.append("```")
        lines.append("")

    # Node/TS project
    pkg_json = repo / "package.json"
    if pkg_json.is_file():
        try:
            import json as _json
            pkg = _json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            lines.append("**Node/TypeScript project detected** (`package.json`)")
            lines.append("")
            lines.append("```bash")
            lines.append("npm install")
            lines.append("```")
            if "start" in scripts:
                lines.append("")
                lines.append("```bash")
                lines.append("npm start")
                lines.append("```")
            if "test" in scripts:
                lines.append("")
                lines.append("```bash")
                lines.append("npm test")
                lines.append("```")
            lines.append("")
        except Exception:
            pass

    if not lines:
        lines.append("No `pyproject.toml` or `package.json` detected. "
                      "Check the README for install and run instructions.")
        lines.append("")

    return lines


def _format_role_passes(evidence_dir: Path) -> str:
    """Read all role-pass JSON files from *evidence_dir* and format as markdown.

    Discovers files matching ``role-pass-*.json``.  Each file is rendered as a
    subsection showing role name, success flag, and output summary.

    Args:
        evidence_dir: Directory to search for role-pass artefacts.

    Returns:
        Markdown string, or an empty string if no role-pass files are found.
    """
    try:
        paths = sorted(evidence_dir.glob("role-pass-*.json"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("_format_role_passes glob failed: %s", exc)
        return ""

    if not paths:
        return ""

    sections: list[str] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("_format_role_passes: skipping %s: %s", path, exc)
            continue

        role = data.get("role", path.stem)
        success = data.get("success", False)
        output = (data.get("output") or "").strip()
        error = (data.get("error") or "").strip()

        status_badge = "PASS" if success else "FAIL"
        sections.append(f"### {role} — {status_badge}")
        sections.append("")
        if output:
            # Cap at 500 chars to keep the report readable
            truncated = output[:500] + ("..." if len(output) > 500 else "")
            sections.append(truncated)
        if error:
            sections.append(f"**Error:** {error}")
        sections.append("")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# generate_scan_report
# ---------------------------------------------------------------------------


def generate_scan_report(
    pack: "EvidencePack",
    evidence_path: Path,
    repo_path: Path,
) -> Path:
    """Generate ``{repo_path}/.saturnday/governance-report.md``.

    Sections:
    - Header: repo, date, version, disposition
    - Summary: checks run, total findings, pass/warn/fail counts
    - All-checks table
    - Security checks table (SEC-* and SEC-FE-* rule IDs)
    - Detailed findings grouped by check name
    - Environment section (declared_not_installed / package_not_importable)
    - Action items

    Args:
        pack: Completed ``EvidencePack`` from the governance scan.
        evidence_path: Directory where evidence artefacts are stored.
        repo_path: Root of the scanned repository.

    Returns:
        Path to the written report file.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []

    # --- Header --------------------------------------------------------------
    lines.append("# Saturnday Governance Report")
    lines.append("")
    lines.append(f"- **Repo:** {repo_path}")
    lines.append(f"- **Date:** {now}")
    lines.append(f"- **Saturnday version:** {__version__}")
    lines.append(f"- **Disposition:** {pack.disposition}")
    if pack.run_id:
        lines.append(f"- **Run ID:** {pack.run_id}")
    lines.append("")

    # --- Summary -------------------------------------------------------------
    check_results = pack.check_results or []
    total_findings = sum(len(r.findings) for r in check_results)
    pass_count = sum(1 for r in check_results if r.status == "PASS")
    warn_count = sum(1 for r in check_results if r.status in {"WARN", "SKIPPED"})
    fail_count = sum(1 for r in check_results if r.status == "FAIL")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Checks run | {len(check_results)} |")
    lines.append(f"| Total findings | {total_findings} |")
    lines.append(f"| Passed | {pass_count} |")
    lines.append(f"| Warned / Skipped | {warn_count} |")
    lines.append(f"| Failed | {fail_count} |")
    lines.append("")

    # --- All checks table ----------------------------------------------------
    lines.append("## Check Results")
    lines.append("")
    if check_results:
        lines.append(_format_check_table(check_results))
    else:
        lines.append("_No checks recorded._")
    lines.append("")

    # --- Security checks table -----------------------------------------------
    sec_checks = [
        r for r in check_results
        if r.rule_id and (
            r.rule_id.startswith("SEC-FE-")
            or (r.rule_id.startswith("SEC-") and not r.rule_id.startswith("SEC-FE-"))
        )
    ]
    if sec_checks:
        lines.append("## Security Checks")
        lines.append("")
        lines.append(
            "| Check | Rule ID | CWE | OWASP | Status | Severity | Findings |"
        )
        lines.append(
            "|-------|---------|-----|-------|--------|----------|----------|"
        )
        for r in sec_checks:
            cwe = r.cwe or ""
            owasp = r.owasp or ""
            finding_count = len(r.findings) if r.findings else 0
            lines.append(
                f"| {r.name} | {r.rule_id} | {cwe} | {owasp} "
                f"| {r.status} | {r.severity} | {finding_count} |"
            )
        lines.append("")

    # --- Detailed findings ---------------------------------------------------
    findings_with_check: list[tuple[str, dict]] = []
    env_findings: list[tuple[str, dict]] = []

    for r in check_results:
        for f in r.findings:
            kind = f.get("kind", "") if isinstance(f, dict) else getattr(f, "kind", "")
            if kind in _ENV_KINDS:
                env_findings.append((r.name, f))
            else:
                findings_with_check.append((r.name, f))

    if findings_with_check:
        lines.append("## Detailed Findings")
        lines.append("")
        grouped: dict[str, list[dict]] = defaultdict(list)
        for check_name, finding in findings_with_check:
            grouped[check_name].append(finding)

        for check_name, group in grouped.items():
            lines.append(f"### {check_name}")
            lines.append("")
            for f in group:
                if isinstance(f, dict):
                    file_ref = f.get("file", "")
                    line_ref = f.get("line")
                    pattern = f.get("pattern", "")
                    detail = f.get("detail", "") or f.get("message", "")
                else:
                    file_ref = getattr(f, "file", "")
                    line_ref = getattr(f, "line", None)
                    pattern = getattr(f, "pattern", "")
                    detail = getattr(f, "detail", "") or getattr(f, "message", "")

                location = f"{file_ref}:{line_ref}" if line_ref is not None else file_ref
                msg_parts = []
                if pattern:
                    msg_parts.append(f"`{pattern}`")
                if detail:
                    msg_parts.append(detail)
                msg = " — ".join(msg_parts) if msg_parts else "(no detail)"
                lines.append(f"- `{location}` — {msg}")
            lines.append("")

    # --- Environment section -------------------------------------------------
    if env_findings:
        lines.append("## Environment")
        lines.append("")
        lines.append(
            "The following packages are declared in SKILL.md but are not installed "
            "or cannot be imported."
        )
        lines.append("")
        for check_name, f in env_findings:
            if isinstance(f, dict):
                pkg = f.get("pattern", "") or f.get("detail", "") or f.get("file", "")
            else:
                pkg = getattr(f, "pattern", "") or getattr(f, "message", "")
            if pkg:
                lines.append(f"- `pip install {pkg}`")
        lines.append("")

    # --- Action items --------------------------------------------------------
    lines.append("## Action Items")
    lines.append("")

    code_fixes = [r for r in check_results if r.status == "FAIL" and not any(
        (f.get("kind", "") if isinstance(f, dict) else getattr(f, "kind", "")) in _ENV_KINDS
        for f in r.findings
    )]
    env_checks = [r for r in check_results if r.status == "FAIL" and any(
        (f.get("kind", "") if isinstance(f, dict) else getattr(f, "kind", "")) in _ENV_KINDS
        for f in r.findings
    )]

    if code_fixes:
        lines.append("### Code fixes required")
        lines.append("")
        for r in code_fixes:
            lines.append(f"- **{r.name}**: {len(r.findings)} finding(s)")
        lines.append("")

    if env_checks:
        lines.append("### Package installation required")
        lines.append("")
        for r in env_checks:
            lines.append(f"- **{r.name}**: {len(r.findings)} package(s)")
        lines.append("")

    if not code_fixes and not env_checks:
        lines.append("_No action items — all checks passed._")
        lines.append("")

    report = "\n".join(lines)

    # Write report into the evidence directory (alongside evidence files)
    out_dir = evidence_path if isinstance(evidence_path, Path) else Path(evidence_path)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("generate_scan_report: could not create %s: %s", out_dir, exc)

    out_path = out_dir / "governance-report.md"
    try:
        out_path.write_text(report, encoding="utf-8")
        logger.info("Wrote governance report: %s", out_path)
    except Exception as exc:
        logger.warning("generate_scan_report: write failed: %s", exc)

    return out_path


# ---------------------------------------------------------------------------
# generate_repair_report
# ---------------------------------------------------------------------------


def generate_repair_report(
    pre_findings: list,
    post_pack: "EvidencePack | None",
    repair_result: "RepairRunResult",
    tickets: list,
    evidence_dir: Path,
    repo_path: Path,
) -> Path:
    """Generate ``{repo_path}/.saturnday/repair-report.md``.

    Sections:
    - Header with repo, date, version
    - Before state: finding counts by category
    - Repair plan: all tickets with kind, file, severity
    - Per-ticket results table
    - Code changes (git diff --stat)
    - After state: if post_pack provided, disposition and finding counts
    - Before vs after comparison (delta)
    - Role passes from evidence_dir
    - Remaining action items

    Args:
        pre_findings: List of ``Finding`` objects from the pre-repair scan.
        post_pack: ``EvidencePack`` from a post-repair governance scan, or ``None``.
        repair_result: Aggregated ``RepairRunResult`` from the repair batch.
        tickets: List of ``RepairTicket`` objects that were executed.
        evidence_dir: Directory containing role-pass and evidence artefacts.
        repo_path: Root of the repaired repository.

    Returns:
        Path to the written report file.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []

    # --- Header --------------------------------------------------------------
    lines.append("# Saturnday Repair Report")
    lines.append("")
    lines.append(f"- **Repo:** {repo_path}")
    lines.append(f"- **Date:** {now}")
    lines.append(f"- **Saturnday version:** {__version__}")
    lines.append("")

    # --- Before state --------------------------------------------------------
    lines.append("## Before State")
    lines.append("")
    if pre_findings:
        kind_counts: dict[str, int] = defaultdict(int)
        for f in pre_findings:
            kind = getattr(f, "kind", "") or (f.get("kind", "") if isinstance(f, dict) else "")
            kind_counts[kind] += 1

        lines.append(f"Total findings: **{len(pre_findings)}**")
        lines.append("")
        lines.append("| Category | Count |")
        lines.append("|----------|-------|")
        for kind, count in sorted(kind_counts.items()):
            lines.append(f"| {kind} | {count} |")
        lines.append("")
    else:
        lines.append("_No pre-repair findings recorded._")
        lines.append("")

    # --- Repair plan ---------------------------------------------------------
    lines.append("## Repair Plan")
    lines.append("")
    if tickets:
        lines.append("| Ticket | Kind | File | Severity |")
        lines.append("|--------|------|------|----------|")
        for t in tickets:
            ticket_id = getattr(t, "ticket_id", "?")
            kind = getattr(t, "finding_kind", "?")
            file_path = getattr(t, "file_path", "?")
            severity = getattr(t, "severity", "?")
            lines.append(f"| {ticket_id} | {kind} | {file_path} | {severity} |")
        lines.append("")
    else:
        lines.append("_No repair tickets generated._")
        lines.append("")

    # --- Per-ticket results --------------------------------------------------
    lines.append("## Ticket Results")
    lines.append("")
    if repair_result.results:
        lines.append("| Ticket | Status | Before | After | Error |")
        lines.append("|--------|--------|--------|-------|-------|")
        for r in repair_result.results:
            status_map = {"fixed": "FIXED", "partial": "PARTIAL", "failed": "FAILED"}
            status = status_map.get(r.status, r.status.upper())
            error = (r.error or "").replace("|", "\\|")
            lines.append(
                f"| {r.ticket_id} | {status} | {r.findings_before} "
                f"| {r.findings_after} | {error} |"
            )
        lines.append("")
    else:
        lines.append("_No ticket results recorded._")
        lines.append("")

    if repair_result.stopped_early:
        lines.append(
            f"> **Stopped early:** {repair_result.stop_reason or 'Stop condition triggered.'}"
        )
        lines.append("")

    # --- Code changes --------------------------------------------------------
    lines.append("## Code Changes")
    lines.append("")
    diff_stat = _git_diff_stat(repo_path)
    if diff_stat:
        lines.append("```")
        lines.append(diff_stat)
        lines.append("```")
    else:
        lines.append("_No git diff available or no changes detected._")
    lines.append("")

    # --- Diff analysis -------------------------------------------------------
    lines.append(_diff_analysis(repo_path))

    # --- After state ---------------------------------------------------------
    pre_count = len(pre_findings)
    post_count: int | None = None

    if post_pack is not None:
        lines.append("## After State")
        lines.append("")
        lines.append(f"- **Disposition:** {post_pack.disposition}")
        post_check_results = post_pack.check_results or []
        post_total = sum(len(r.findings) for r in post_check_results)
        post_count = post_total
        lines.append(f"- **Total findings:** {post_total}")
        lines.append(f"- **Checks passed:** {sum(1 for r in post_check_results if r.status == 'PASS')}")
        lines.append(f"- **Checks failed:** {sum(1 for r in post_check_results if r.status == 'FAIL')}")
        lines.append("")

        # Before vs after comparison
        lines.append("## Before vs After")
        lines.append("")
        delta = post_total - pre_count
        direction = "reduction" if delta <= 0 else "increase"
        lines.append(f"| Metric | Before | After | Delta |")
        lines.append(f"|--------|--------|-------|-------|")
        lines.append(f"| Total findings | {pre_count} | {post_total} | {delta:+d} |")
        lines.append(f"| Fixed | — | — | {repair_result.fixed} tickets |")
        lines.append(f"| Partial | — | — | {repair_result.partial} tickets |")
        lines.append(f"| Failed | — | — | {repair_result.failed} tickets |")
        lines.append("")
        lines.append(
            f"Net finding {direction}: **{abs(delta)}** "
            f"({pre_count} → {post_total})"
        )
        lines.append("")

    # --- Role passes ---------------------------------------------------------
    role_pass_md = _format_role_passes(evidence_dir)
    if role_pass_md:
        lines.append("## Role Passes")
        lines.append("")
        lines.append(role_pass_md)

    # --- Remaining action items ----------------------------------------------
    lines.append("## Remaining Action Items")
    lines.append("")
    if post_pack is not None:
        remaining_fails = [
            r for r in (post_pack.check_results or []) if r.status == "FAIL"
        ]
        if remaining_fails:
            for r in remaining_fails:
                lines.append(f"- **{r.name}**: {len(r.findings)} finding(s) remaining")
        else:
            lines.append("_All checks passed after repair._")
    elif repair_result.failed > 0:
        lines.append(
            f"- {repair_result.failed} ticket(s) failed — manual review required."
        )
    else:
        lines.append("_No post-repair scan available. Review manually._")
    lines.append("")

    report = "\n".join(lines)

    # Write report into the evidence directory (alongside repair evidence)
    out_dir = evidence_dir if isinstance(evidence_dir, Path) else Path(evidence_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("generate_repair_report: could not create %s: %s", out_dir, exc)

    out_path = out_dir / "repair-report.md"
    try:
        out_path.write_text(report, encoding="utf-8")
        logger.info("Wrote repair report: %s", out_path)
    except Exception as exc:
        logger.warning("generate_repair_report: write failed: %s", exc)

    return out_path


# ---------------------------------------------------------------------------
# generate_run_report helpers
# ---------------------------------------------------------------------------


def _append_recovery_section(
    lines: list[str],
    result: "RunResult",
    evidence_dir: Path,
    repo_path: Path,
) -> None:
    """Append a Recovery section to *lines* when continuation work exists.

    Reads the ledger from *evidence_dir* to classify tickets by disposition.
    Falls back to ``RunResult`` ticket dispositions when the ledger is absent.

    The next-step command uses ``evidence_dir`` and ``repo_path`` as real values
    because both are available at the report-generation call site.  ``--plan``
    and ``--backend`` remain placeholders because they are not available here.

    Args:
        lines: Report lines list to append to (mutated in place).
        result: Completed ``RunResult`` for fallback classification.
        evidence_dir: Directory containing the run's evidence (used to read ledger
            and as the ``--evidence-dir`` value in the next-step command).
        repo_path: Root of the built repository (used as ``--repo`` value).
    """
    from saturnday.run.resume import _select_continuation_command, describe_recovery_state

    state: dict | None = None
    try:
        state = describe_recovery_state(evidence_dir)
    except Exception:
        pass

    if state is not None:
        failed: list[str] = state.get("failed", [])
        coded_ungoverned: list[str] = state.get("coded_ungoverned", [])
        skipped: list[str] = state.get("skipped", [])
        pending: list[str] = state.get("pending", [])
    else:
        failed = [tr.ticket_id for tr in result.ticket_results if tr.disposition == "FAIL"]
        coded_ungoverned = [
            tr.ticket_id for tr in result.ticket_results
            if tr.disposition == "CODED_UNGOVERNED"
        ]
        skipped = [tr.ticket_id for tr in result.ticket_results if tr.disposition == "SKIP"]
        pending = []

    has_actionable = bool(failed or coded_ungoverned or skipped or pending)
    cmd_name = _select_continuation_command(failed, coded_ungoverned, skipped, pending)

    if not has_actionable:
        return

    lines.append("## Recovery")
    lines.append("")

    if coded_ungoverned:
        lines.append(
            f"> **Warning:** {len(coded_ungoverned)} ticket(s) were coded but governance "
            "did not clear them. Code is on the branch. "
            "`saturnday resume` will skip these tickets. "
            "Use `saturnday rerun-failed` to revisit them."
        )
        lines.append("")

    lines.append("| Disposition | Tickets |")
    lines.append("|-------------|---------|")
    if failed:
        lines.append(f"| FAIL | {', '.join(failed)} |")
    if coded_ungoverned:
        lines.append(f"| CODED\\_UNGOVERNED | {', '.join(coded_ungoverned)} |")
    if skipped:
        lines.append(f"| SKIP | {', '.join(skipped)} |")
    if pending:
        lines.append(f"| PENDING | {', '.join(pending)} |")
    lines.append("")

    if cmd_name:
        # evidence_dir and repo_path are real values available at this call site.
        # --plan and --backend are genuine placeholders: plan file path and backend
        # name are not passed to generate_run_report().
        lines.append(
            f"**Next step:** "
            f"`saturnday {cmd_name} --output-dir {evidence_dir}"
            f" --plan <plan> --repo {repo_path} --backend <backend>`"
        )
        lines.append("")


# ---------------------------------------------------------------------------
# generate_run_report
# ---------------------------------------------------------------------------


def generate_run_report(
    result: "RunResult",
    plan_data: dict,
    post_pack: "EvidencePack | None",
    evidence_dir: Path,
    repo_path: Path,
) -> Path:
    """Generate ``{evidence_dir}/run-report.md``.

    Sections:
    - Header with repo, date, version, project_id
    - Brief (from plan_data)
    - Plan summary: ticket count, phases, DoD criteria
    - Per-ticket results table
    - Code changes (git diff --stat)
    - Post-run governance scan results
    - Role passes from evidence_dir
    - DoD evaluation
    - Summary: passed/failed/skipped counts

    Args:
        result: ``RunResult`` from the completed plan execution.
        plan_data: Raw plan dict (as loaded from the plan YAML/JSON).
        post_pack: ``EvidencePack`` from a post-run governance scan, or ``None``.
        evidence_dir: Directory containing role-pass and evidence artefacts.
        repo_path: Root of the built repository.

    Returns:
        Path to the written report file.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []

    # --- Header --------------------------------------------------------------
    lines.append("# Saturnday Run Report")
    lines.append("")
    lines.append(f"- **Repo:** {repo_path}")
    lines.append(f"- **Date:** {now}")
    lines.append(f"- **Saturnday version:** {__version__}")
    lines.append(f"- **Project:** {result.project_id}")
    lines.append("")

    # --- Brief ---------------------------------------------------------------
    brief = plan_data.get("brief") or plan_data.get("notes") or ""
    if brief:
        lines.append("## Brief")
        lines.append("")
        lines.append(brief)
        lines.append("")

    # --- Plan summary --------------------------------------------------------
    lines.append("## Plan Summary")
    lines.append("")

    dod_criteria: list[str] = plan_data.get("definition_of_done", [])
    phases: list[dict] = plan_data.get("phases", [])
    ticket_count = result.total_tickets

    lines.append(f"- **Tickets:** {ticket_count}")
    lines.append(f"- **Phases:** {len(phases)}")
    if dod_criteria:
        lines.append(f"- **Definition of Done:** {', '.join(str(d) for d in dod_criteria)}")
    if result.stop_reason:
        lines.append(f"- **Stop reason:** {result.stop_reason}")
    lines.append("")

    if phases:
        lines.append("| Phase | Tickets |")
        lines.append("|-------|---------|")
        for ph in phases:
            phase_name = ph.get("name") or ph.get("phase_id", "?")
            phase_tickets = ", ".join(str(t) for t in ph.get("ticket_ids", []))
            lines.append(f"| {phase_name} | {phase_tickets} |")
        lines.append("")

    # --- Per-ticket results table --------------------------------------------
    lines.append("## Ticket Results")
    lines.append("")
    if result.ticket_results:
        lines.append(
            "| Ticket | Disposition | Attempts | Governance | Error |"
        )
        lines.append(
            "|--------|-------------|----------|------------|-------|"
        )
        for tr in result.ticket_results:
            error = (tr.error or "").replace("|", "\\|")
            gov = tr.governance_disposition or ""
            lines.append(
                f"| {tr.ticket_id} | {tr.disposition} | {tr.attempts} "
                f"| {gov} | {error} |"
            )
        lines.append("")
    else:
        lines.append("_No ticket results recorded._")
        lines.append("")

    # --- Code changes --------------------------------------------------------
    lines.append("## Code Changes")
    lines.append("")
    diff_stat = _git_diff_stat(repo_path)
    if diff_stat:
        lines.append("```")
        lines.append(diff_stat)
        lines.append("```")
    else:
        lines.append("_No git diff available or no changes detected._")
    lines.append("")

    # --- Diff analysis -------------------------------------------------------
    lines.append(_diff_analysis(repo_path))

    # --- Post-run governance -------------------------------------------------
    if post_pack is not None:
        lines.append("## Post-Run Governance")
        lines.append("")
        lines.append(f"- **Disposition:** {post_pack.disposition}")
        post_check_results = post_pack.check_results or []
        post_total = sum(len(r.findings) for r in post_check_results)
        lines.append(f"- **Total findings:** {post_total}")
        lines.append("")

        if post_check_results:
            lines.append(_format_check_table(post_check_results))
            lines.append("")

        failed_checks = [r for r in post_check_results if r.status == "FAIL"]
        if failed_checks:
            lines.append("### Failed Checks")
            lines.append("")
            for r in failed_checks:
                lines.append(f"- **{r.name}**: {len(r.findings)} finding(s)")
            lines.append("")

    # --- Role passes ---------------------------------------------------------
    role_pass_md = _format_role_passes(evidence_dir)
    if role_pass_md:
        lines.append("## Role Passes")
        lines.append("")
        lines.append(role_pass_md)

    # --- Proof resolution ----------------------------------------------------
    # Phase 7: surface the full (status, source, narrative) triple so the
    # report tells the truth about HOW the product was proved (or why it
    # was not).  Falls back cleanly when the fields are absent on older
    # RunResult instances.
    _pr_status = getattr(result, "proof_resolution_status", "") or ""
    _pr_source = getattr(result, "proof_resolution_source", "") or ""
    _pr_narrative = getattr(result, "proof_resolution_narrative", "") or ""
    if _pr_status or _pr_source or _pr_narrative:
        lines.append("## Proof Resolution")
        lines.append("")
        lines.append(f"- **Status:** {_pr_status or 'not_attempted'}")
        lines.append(f"- **Source:** {_pr_source or 'none'}")
        if _pr_narrative:
            lines.append(f"- **Narrative:** {_pr_narrative}")
        lines.append("")

    # --- DoD evaluation ------------------------------------------------------
    lines.append("## Definition of Done")
    lines.append("")
    dod_status = "MET" if result.definition_of_done_met else "NOT MET"
    lines.append(f"**Status: {dod_status}**")
    lines.append("")
    if dod_criteria:
        for criterion in dod_criteria:
            lines.append(f"- {criterion}")
        lines.append("")

    # --- How to run the finished product -------------------------------------
    if repo_path:
        _how_to_run = _generate_how_to_run(repo_path)
        if _how_to_run:
            lines.append("## How to Run")
            lines.append("")
            lines.extend(_how_to_run)
            lines.append("")

    # --- Summary -------------------------------------------------------------
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Passed | {result.passed} |")
    lines.append(f"| Failed | {result.failed} |")
    lines.append(f"| Skipped | {result.skipped} |")
    lines.append(f"| Total | {result.total_tickets} |")
    lines.append("")

    # --- Recovery ------------------------------------------------------------
    _append_recovery_section(lines, result, evidence_dir, repo_path)

    report = "\n".join(lines)

    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("generate_run_report: could not create %s: %s", evidence_dir, exc)

    # Report goes in the evidence directory — unique per run
    out_path = evidence_dir / "run-report.md"
    try:
        out_path.write_text(report, encoding="utf-8")
        logger.info("Wrote run report: %s", out_path)
    except Exception as exc:
        logger.warning("generate_run_report: write failed: %s", exc)

    return out_path


# ---------------------------------------------------------------------------
# Review report and fix prompts for CODED_UNGOVERNED tickets
# ---------------------------------------------------------------------------


def _remediation_tip(finding: dict) -> str:
    """Extract a short remediation tip from a finding."""
    kind = finding.get("kind", "")
    detail = finding.get("detail", finding.get("message", ""))
    tips = {
        "sql_string_building": "Use parameterized queries instead of string formatting",
        "hardcoded_secret": "Move to environment variable: os.getenv(\"KEY\")",
        "generic_secret": "Move secret to environment variable",
        "test_no_assert": "Add assert statements that verify actual behaviour",
        "tautological_assertion": "Replace with meaningful assertions",
        "missing_type_hint": "Add type annotations to function signature",
        "dead_code": "Remove the function or add a reference to it",
        "missing_license": "Add a LICENSE file to the project root",
        "missing_readme": "Add a README.md with project description",
        "route_no_auth": "Add authentication middleware to this route",
        "csrf_missing": "Add CSRF protection to state-changing endpoint",
        "xss_check": "Sanitize user input before rendering in HTML",
        "idor_check": "Verify object ownership before granting access",
        "hallucinated_import": "Replace with a real package or remove the import",
    }
    if kind in tips:
        return tips[kind]
    if detail and "." in detail:
        return detail.split(".")[0]
    return detail[:100] if detail else "Review and fix this finding"


def generate_review_report(
    ticket_results: "tuple",
    evidence_dir: "Path",
    dod_met: bool = False,
) -> "Path | None":
    """Generate review-required.md and .txt with DoD summary and ungoverned ticket details.

    Returns:
        Path to the .md report, or None if no ungoverned tickets.
    """
    ungoverned = [tr for tr in ticket_results if tr.disposition == "CODED_UNGOVERNED"]
    if not ungoverned:
        return None

    evidence_dir.mkdir(parents=True, exist_ok=True)

    # DoD summary at the top with per-ticket status
    passed = sum(1 for r in ticket_results if r.disposition == "PASS")
    failed = sum(1 for r in ticket_results if r.disposition == "FAIL")
    skipped = sum(1 for r in ticket_results if r.disposition == "SKIP")
    dod_str = "MET" if dod_met else "NOT MET"

    lines = [
        "# Run Report\n",
        f"## Definition of Done: {dod_str}\n",
        f"Passed: {passed}   Coded (review needed): {len(ungoverned)}   Failed: {failed}   Skipped: {skipped}\n",
        "",
    ]
    # Per-ticket status
    icons = {"PASS": "✓", "CODED_UNGOVERNED": "!", "FAIL": "✗", "SKIP": "—"}
    for tr in ticket_results:
        icon = icons.get(tr.disposition, "?")
        line = f"  {icon} {tr.ticket_id}  {tr.disposition}"
        if tr.disposition == "CODED_UNGOVERNED":
            n = len(tr.governance_findings) if tr.governance_findings else 0
            line += f" ({n} finding{'s' if n != 1 else ''})"
        elif tr.disposition == "PASS" and tr.attempts > 1:
            line += f" (attempt {tr.attempts})"
        lines.append(line)
    lines.append("")
    lines.append("---\n")

    # Ungoverned ticket details
    lines.append(f"## Tickets Requiring Review\n")
    lines.append(f"{len(ungoverned)} ticket(s) were coded but did not pass governance.\n")
    lines.append("The code is in the repo and committed. These tickets need manual review")
    lines.append("or AI-assisted fixes before the project should be considered production-ready.\n")
    lines.append("---\n")

    total_findings = 0
    for tr in ungoverned:
        findings = tr.governance_findings or ()
        total_findings += len(findings)
        lines.append(f"## {tr.ticket_id}\n")
        lines.append(f"**Files:** {', '.join(tr.changed_files) if tr.changed_files else 'unknown'}\n")
        lines.append(f"**Attempts:** {tr.attempts}\n")
        lines.append(f"**Findings:** {len(findings)}\n")

        if findings:
            lines.append("")
            for i, f in enumerate(findings, 1):
                kind = f.get("kind", f.get("message", "unknown"))
                file_path = f.get("file", f.get("path", ""))
                line_no = f.get("line", "")
                loc = f"{file_path}:{line_no}" if line_no else file_path
                tip = _remediation_tip(f)
                lines.append(f"{i}. **{kind}** at `{loc}`")
                lines.append(f"   - {tip}\n")

    lines.append("---\n")
    lines.append(f"**Total:** {len(ungoverned)} ungoverned ticket(s), {total_findings} finding(s)\n")
    lines.append("## How to fix\n")
    lines.append("1. Review each finding above")
    lines.append("2. Fix the code manually or use an AI coder with the prompts in `prompts/`")
    lines.append("3. Run governance to verify:\n")
    lines.append("```bash")
    lines.append("saturnday governance --repo . --full")
    lines.append("```\n")

    content = "\n".join(lines)

    md_path = evidence_dir / "review-required.md"
    txt_path = evidence_dir / "review-required.txt"
    try:
        md_path.write_text(content, encoding="utf-8")
        plain = content.replace("```bash", "").replace("```", "").replace("**", "").replace("##", "")
        txt_path.write_text(plain, encoding="utf-8")
        logger.info("Wrote review report: %s", md_path)
    except Exception as exc:
        logger.warning("Failed to write review report: %s", exc)
        return None

    return md_path


def generate_fix_prompts(
    ticket_results: "tuple",
    evidence_dir: "Path",
) -> "list[Path]":
    """Generate copy-paste fix prompts for each CODED_UNGOVERNED ticket.

    Returns:
        List of paths to generated prompt files.
    """
    ungoverned = [tr for tr in ticket_results if tr.disposition == "CODED_UNGOVERNED"]
    if not ungoverned:
        return []

    prompts_dir = evidence_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    paths: list = []

    for tr in ungoverned:
        findings = tr.governance_findings or ()
        files = tr.changed_files or ()

        lines = [
            f"--- Fix prompt for {tr.ticket_id} ---\n",
            "Fix the following governance findings. Do NOT modify any other files.\n",
            "Files to edit:",
        ]
        for f in files:
            lines.append(f"  {f}")

        lines.append("\nFindings to fix:\n")
        for i, finding in enumerate(findings, 1):
            kind = finding.get("kind", finding.get("message", "unknown"))
            file_path = finding.get("file", finding.get("path", ""))
            line_no = finding.get("line", "")
            loc = f"{file_path}:{line_no}" if line_no else file_path
            tip = _remediation_tip(finding)
            lines.append(f"{i}. {kind} at {loc}")
            lines.append(f"   {tip}\n")

        lines.append("Constraints:")
        lines.append("- Only edit the files listed above")
        lines.append("- Do not add new files")
        lines.append("- Do not change function signatures used by other modules")
        lines.append("- Do not remove existing functionality\n")
        lines.append("After fixing, verify with:")
        lines.append("  saturnday governance --repo . --full\n")

        content = "\n".join(lines)
        prompt_path = prompts_dir / f"{tr.ticket_id}-fix-prompt.txt"
        try:
            prompt_path.write_text(content, encoding="utf-8")
            paths.append(prompt_path)
        except Exception as exc:
            logger.warning("Failed to write fix prompt for %s: %s", tr.ticket_id, exc)

    if paths:
        logger.info("Wrote %d fix prompt(s) to %s", len(paths), prompts_dir)

    return paths
