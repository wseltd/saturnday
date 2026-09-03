"""Human-readable markdown report generator for release-preflight evidence.

Generates a concise, scannable Markdown report from a
:class:`~saturnday.release.evidence.ReleaseEvidencePack`.  The report is
suitable for inclusion in PR descriptions, audit trails, or CI artefact
summaries.

Typical usage::

    from saturnday.release.evidence import ReleaseEvidencePack
    from saturnday.release.report import generate_release_report

    report_md = generate_release_report(pack)
    (output_dir / "release-report.md").write_text(report_md, encoding="utf-8")
"""
from __future__ import annotations

import logging
from typing import Any

from saturnday.release.evidence import ReleaseEvidencePack, ReleaseCheckResult

logger = logging.getLogger(__name__)

# Maximum number of inventory rows shown before "and N more" footer.
_INVENTORY_TRUNCATE = 50

# Disposition badge labels for clear visual scanning.
_DISPOSITION_BADGE: dict[str, str] = {
    "PASS": "PASS",
    "WARN": "WARN",
    "FAIL": "FAIL",
}


def generate_release_report(pack: ReleaseEvidencePack) -> str:
    """Generate a human-readable Markdown report from a ReleaseEvidencePack.

    The report includes:

    - Header with artefact type, path, and SHA-256.
    - Disposition (PASS/WARN/FAIL) with reasons.
    - Inventory summary (file count, total size).
    - Check results table (name, rule_id, status, finding count).
    - Detailed findings per check (file, kind, detail).
    - Release diff summary if present.
    - Capability state (premium status).
    - Timestamp.

    The report is intentionally concise — it truncates large inventories at
    :data:`_INVENTORY_TRUNCATE` rows and limits individual finding details to
    avoid producing unreadable walls of text for large artefacts.

    Args:
        pack: The completed :class:`~saturnday.release.evidence.ReleaseEvidencePack`
              to render.

    Returns:
        A Markdown string suitable for writing to ``release-report.md``.
    """
    lines: list[str] = []

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------
    disposition_label = _DISPOSITION_BADGE.get(pack.disposition, pack.disposition)
    lines += [
        f"# Release Preflight Report — {disposition_label}",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Artefact type | `{pack.artefact_type}` |",
        f"| Artefact path | `{pack.artefact_path}` |",
        f"| SHA-256 | `{pack.artefact_sha256}` |",
        f"| Disposition | **{disposition_label}** |",
        f"| Run ID | `{pack.run_id}` |",
        f"| Timestamp | {pack.created_utc} |",
        "",
    ]

    # -----------------------------------------------------------------------
    # Disposition reasons
    # -----------------------------------------------------------------------
    if pack.disposition_reasons:
        lines += ["## Disposition Reasons", ""]
        for reason in pack.disposition_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    # -----------------------------------------------------------------------
    # Inventory summary
    # -----------------------------------------------------------------------
    lines += _render_inventory_section(pack.inventory)

    # -----------------------------------------------------------------------
    # Check results table
    # -----------------------------------------------------------------------
    lines += _render_check_results_table(pack.check_results)

    # -----------------------------------------------------------------------
    # Detailed findings per check
    # -----------------------------------------------------------------------
    lines += _render_check_findings(pack.check_results)

    # -----------------------------------------------------------------------
    # Release diff summary (only when a baseline was provided)
    # -----------------------------------------------------------------------
    if pack.baseline_artefact_path or pack.release_diff:
        lines += _render_diff_section(pack.baseline_artefact_path, pack.release_diff)

    # -----------------------------------------------------------------------
    # Capability state
    # -----------------------------------------------------------------------
    lines += _render_capability_state(pack.capability_state)

    # Ensure a trailing newline.
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Private section renderers
# ---------------------------------------------------------------------------


def _render_inventory_section(inventory: dict[str, Any]) -> list[str]:
    """Render the artefact inventory summary section.

    Args:
        inventory: Serialised ``ArtefactInventory`` dict
                   (keys: ``files``, ``artefact_type``, etc.).

    Returns:
        List of Markdown lines for the inventory section.
    """
    lines: list[str] = ["## Artefact Inventory", ""]

    files: list[dict[str, Any]] = inventory.get("files", [])
    file_count = len(files)
    total_size = sum(f.get("size", 0) for f in files)

    lines += [
        "| Metric | Value |",
        "|--------|-------|",
        f"| File count | {file_count} |",
        f"| Total size | {_format_size(total_size)} |",
        "",
    ]

    if files:
        # Show top N files by size, truncate if needed.
        sorted_files = sorted(files, key=lambda f: f.get("size", 0), reverse=True)
        shown = sorted_files[:_INVENTORY_TRUNCATE]
        remaining = file_count - len(shown)

        lines += [
            f"Top files by size (showing {len(shown)} of {file_count}):",
            "",
            "| Path | Size |",
            "|------|------|",
        ]
        for f in shown:
            lines.append(f"| `{f.get('path', '?')}` | {_format_size(f.get('size', 0))} |")

        if remaining > 0:
            lines.append(f"| *... and {remaining} more* | |")

        lines.append("")

    return lines


def _render_check_results_table(check_results: list[ReleaseCheckResult]) -> list[str]:
    """Render the check results summary table.

    Args:
        check_results: Ordered list of :class:`~saturnday.release.evidence.ReleaseCheckResult`.

    Returns:
        List of Markdown lines for the check results table section.
    """
    lines: list[str] = ["## Check Results", ""]

    if not check_results:
        lines += ["*No checks ran.*", ""]
        return lines

    lines += [
        "| Rule ID | Check | Status | Severity | Findings |",
        "|---------|-------|--------|----------|----------|",
    ]
    for r in check_results:
        status_cell = _status_badge(r.status)
        finding_count = len(r.findings)
        lines.append(
            f"| `{r.rule_id}` | {r.name} | {status_cell} | {r.severity} | {finding_count} |"
        )

    lines.append("")
    return lines


def _render_check_findings(check_results: list[ReleaseCheckResult]) -> list[str]:
    """Render detailed findings per check.

    Only checks that have at least one finding are rendered.  Each finding
    is rendered as a bullet with up to three fields: ``path``, ``kind``,
    ``detail``.  Unknown fields fall back to a compact JSON-like representation.

    Args:
        check_results: Ordered list of :class:`~saturnday.release.evidence.ReleaseCheckResult`.

    Returns:
        List of Markdown lines for the findings detail section, or an empty
        list if all checks passed without findings.
    """
    lines: list[str] = []

    checks_with_findings = [r for r in check_results if r.findings]
    if not checks_with_findings:
        return lines

    lines += ["## Findings Detail", ""]

    for r in checks_with_findings:
        lines += [f"### {r.rule_id} — {r.name}", ""]
        for finding in r.findings:
            bullet = _format_finding_bullet(finding)
            lines.append(f"- {bullet}")
        lines.append("")

    return lines


def _render_diff_section(baseline_path: str, release_diff: dict[str, Any]) -> list[str]:
    """Render the release diff summary section.

    Args:
        baseline_path: Path to the baseline artefact (may be empty string).
        release_diff:  Structured diff dict from the diff engine.

    Returns:
        List of Markdown lines for the diff section.
    """
    lines: list[str] = ["## Release Diff", ""]

    if baseline_path:
        lines.append(f"- Baseline: `{baseline_path}`")

    if not release_diff:
        lines += ["*No diff data available.*", ""]
        return lines

    added: list[str] = release_diff.get("added", [])
    removed: list[str] = release_diff.get("removed", [])
    changed: list[str] = release_diff.get("changed", [])

    lines += [
        "",
        "| Change type | Count |",
        "|-------------|-------|",
        f"| Added | {len(added)} |",
        f"| Removed | {len(removed)} |",
        f"| Changed | {len(changed)} |",
        "",
    ]

    if removed:
        lines += ["**Removed files:**", ""]
        for path in removed[:20]:
            lines.append(f"- `{path}`")
        if len(removed) > 20:
            lines.append(f"- *... and {len(removed) - 20} more*")
        lines.append("")

    if added:
        lines += ["**Added files:**", ""]
        for path in added[:20]:
            lines.append(f"- `{path}`")
        if len(added) > 20:
            lines.append(f"- *... and {len(added) - 20} more*")
        lines.append("")

    if changed:
        lines += ["**Changed files:**", ""]
        for path in changed[:20]:
            lines.append(f"- `{path}`")
        if len(changed) > 20:
            lines.append(f"- *... and {len(changed) - 20} more*")
        lines.append("")

    return lines


def _render_capability_state(capability_state: dict[str, Any]) -> list[str]:
    """Render the premium capability state section.

    Args:
        capability_state: Dict from
            :func:`~saturnday.shared.evidence_schema.build_capability_state`.

    Returns:
        List of Markdown lines for the capability state section.
    """
    lines: list[str] = ["## Capability State", ""]

    premium_installed = capability_state.get("premium_package_installed", False)
    premium_enabled = capability_state.get("premium_capabilities_enabled", False)
    entitlement_valid = capability_state.get("entitlement_valid")
    entitlement_org = capability_state.get("entitlement_org")
    available_hooks = capability_state.get("available_premium_hooks", [])

    lines += [
        f"- Premium installed: {premium_installed}",
        f"- Premium enabled: {premium_enabled}",
    ]

    if entitlement_valid is not None:
        lines.append(f"- Entitlement valid: {entitlement_valid}")
    if entitlement_org:
        lines.append(f"- Organisation: {entitlement_org}")
    if available_hooks:
        lines.append(f"- Available hooks: {', '.join(sorted(available_hooks))}")
    else:
        lines.append("- Available hooks: none")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    Args:
        size_bytes: Raw byte count (non-negative integer).

    Returns:
        Human-readable string, e.g. ``"1.2 MB"`` or ``"512 B"``.
    """
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _status_badge(status: str) -> str:
    """Return a Markdown-safe status badge string.

    Args:
        status: One of ``"PASS"``, ``"FAIL"``, ``"WARN"``, ``"SKIPPED"``.

    Returns:
        The status string wrapped in backticks for monospace rendering.
    """
    return f"`{status}`"


def _format_finding_bullet(finding: dict[str, Any]) -> str:
    """Format a single finding dict as a concise Markdown bullet body.

    Tries to extract well-known keys (``path``, ``kind``, ``detail``,
    ``match_preview``, ``pattern``) before falling back to a compact
    key=value representation of all keys.

    Args:
        finding: A finding dict from :attr:`ReleaseCheckResult.findings`.

    Returns:
        A single-line string (no leading ``-``) describing the finding.
    """
    parts: list[str] = []

    path = finding.get("path") or finding.get("file")
    if path:
        parts.append(f"`{path}`")

    kind = finding.get("kind") or finding.get("type")
    if kind:
        parts.append(str(kind))

    detail = finding.get("detail") or finding.get("description") or finding.get("message")
    if detail:
        # Truncate long detail strings to keep the report scannable.
        detail_str = str(detail)
        if len(detail_str) > 120:
            detail_str = detail_str[:117] + "..."
        parts.append(detail_str)

    match_preview = finding.get("match_preview")
    if match_preview and not detail:
        preview_str = str(match_preview)
        if len(preview_str) > 80:
            preview_str = preview_str[:77] + "..."
        parts.append(f"match: `{preview_str}`")

    pattern = finding.get("pattern")
    if pattern and not kind:
        parts.append(f"pattern: {pattern}")

    if parts:
        return " — ".join(parts)

    # Fallback: compact representation of all keys.
    fallback = "; ".join(f"{k}={v!r}" for k, v in finding.items() if v is not None)
    return fallback[:200] if fallback else "(no details)"


__all__ = ["generate_release_report"]
