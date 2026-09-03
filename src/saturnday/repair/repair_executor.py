"""Governed repair execution for a single repair ticket.

Executes the repair-scan-rescan cycle for one ``RepairTicket``:

1. Scan the skill before repair to count findings of the relevant kind.
2. Call the provided ``coder_fn`` to produce a repaired file.
3. Write the repaired content to disk.
4. Re-scan and compare finding counts.
5. Return a ``RepairResult`` with before/after counts and a status.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from saturnday.repair.repair_tickets import RepairTicket

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class RepairResult:
    """Outcome of executing a single repair ticket.

    Attributes:
        ticket_id: The ticket that was executed.
        status: ``"fixed"`` — all findings resolved;
                ``"partial"`` — some findings resolved;
                ``"failed"`` — no findings resolved or an error occurred.
        findings_before: Count of findings of ``ticket.finding_kind`` before repair.
        findings_after: Count of findings of ``ticket.finding_kind`` after repair.
        findings_resolved: Finding kinds that were fully resolved (count → 0).
        error: Error message when ``status == "failed"`` due to an exception.
    """

    ticket_id: str
    status: str
    findings_before: int
    findings_after: int
    findings_resolved: list[str] = field(default_factory=list)
    error: str | None = None
    # Fix 49.b: stable matching key for restart reuse (file:kind composite)
    group_key: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_repair(
    ticket: RepairTicket,
    skill_path: Path,
    coder_fn: Callable[[str, str, Path], str] | None = None,
    *,
    scan_fn: Callable[[Path], list] | None = None,
    cli_mode: bool = False,
) -> RepairResult:
    """Execute a single repair ticket against a skill directory.

    Args:
        ticket: The repair ticket specifying what to fix.
        skill_path: Absolute path to the skill directory root.
        coder_fn: Callable with signature ``(prompt: str, file_path: str,
            repo_path: Path) -> str`` that returns the fully repaired file
            content.  If ``None``, the repair returns ``status="failed"``
            immediately (no coder is available to apply the fix).
        scan_fn: Callable that takes a repo path and returns a list of
            ``Finding`` objects.  Must match the scanner used to generate
            the repair tickets.  If ``None``, falls back to ``scan_skill``.

    Returns:
        ``RepairResult`` with before/after counts and resolution status.

    Note:
        This function does not enforce retry limits or coder timeouts.
        Callers must implement their own retry bounding (e.g., max 3 attempts)
        and timeout logic (e.g., wrapping coder_fn with a timeout decorator).
    """
    # --- pre-scan -----------------------------------------------------------
    _t_pre = time.monotonic()
    if scan_fn is not None:
        before_findings_all = scan_fn(skill_path)
    else:
        from saturnday.guard.cloud_scanner import scan_skill
        before_findings_all = scan_skill(skill_path).findings
    logger.info("TIMING %-30s %.2fs [%s]", "repair_pre_scan", time.monotonic() - _t_pre, ticket.ticket_id)
    # Filter by kind AND file when the ticket targets a specific file,
    # so fixing main.py isn't marked "failed" because bbox_step.py has
    # the same finding kind.
    if ticket.file_path:
        before_findings = [
            f for f in before_findings_all
            if f.kind == ticket.finding_kind and f.file == ticket.file_path
        ]
    else:
        before_findings = [
            f for f in before_findings_all if f.kind == ticket.finding_kind
        ]
    findings_before = len(before_findings)

    if findings_before == 0:
        logger.debug(
            "execute_repair: ticket=%s already clean (0 findings of kind=%s)",
            ticket.ticket_id,
            ticket.finding_kind,
        )
        return RepairResult(
            ticket_id=ticket.ticket_id,
            status="fixed",
            findings_before=0,
            findings_after=0,
            findings_resolved=[ticket.finding_kind],
            group_key=ticket.group_key,
        )

    # --- no coder fast-path -------------------------------------------------
    if coder_fn is None:
        logger.warning(
            "execute_repair: no coder_fn provided for ticket=%s", ticket.ticket_id
        )
        return RepairResult(
            ticket_id=ticket.ticket_id,
            status="failed",
            findings_before=findings_before,
            findings_after=findings_before,
            findings_resolved=[],
            error="No coder function provided",
            group_key=ticket.group_key,
        )

    # --- apply repair -------------------------------------------------------
    # Fix D: snapshot the target file's pre-repair state so a ``status ==
    # "failed"`` outcome can roll back cleanly.  Scope is strictly the
    # ticket's target file — Fix C ensures commits are scoped the same
    # way, so any auxiliary files the coder may have written will not be
    # committed even if they survive on disk.
    _pre_existed = False
    _pre_bytes: bytes | None = None
    try:
        _snapshot_target = skill_path / ticket.file_path
        if _snapshot_target.is_file():
            _pre_existed = True
            _pre_bytes = _snapshot_target.read_bytes()
    except Exception:  # pragma: no cover — snapshot failure must not block repair
        _pre_existed = False
        _pre_bytes = None

    def _rollback_target_on_failure() -> None:
        """Fix D: restore pre-repair state for the ticket's target file.

        API mode: restore exact pre-repair bytes (or delete if the file
        did not exist before).  CLI mode: use ``git checkout HEAD`` if
        the file is tracked; otherwise delete.  Silent on internal error
        — rollback is best-effort, but failure here does not change the
        RepairResult status.
        """
        try:
            tgt = skill_path / ticket.file_path
            if _pre_existed and _pre_bytes is not None:
                tgt.write_bytes(_pre_bytes)
                logger.info(
                    "Fix D: rolled back failed repair write for %s",
                    ticket.file_path,
                )
            else:
                # File didn't exist before the repair attempt — try to remove.
                # For CLI mode we fall back to ``git checkout`` as a safety
                # net in case the file ended up tracked via some other path.
                if tgt.is_file():
                    import subprocess as _sp
                    # Only attempt git checkout for CLI mode where snapshot
                    # may be incomplete; for API mode we trust _pre_existed.
                    if cli_mode:
                        _res = _sp.run(
                            ["git", "-C", str(skill_path),
                             "ls-files", "--error-unmatch", "--", ticket.file_path],
                            capture_output=True, text=True, timeout=10, check=False,
                        )
                        if _res.returncode == 0:
                            _sp.run(
                                ["git", "-C", str(skill_path),
                                 "checkout", "HEAD", "--", ticket.file_path],
                                capture_output=True, text=True, timeout=10, check=False,
                            )
                            logger.info(
                                "Fix D: rolled back failed repair via git checkout for %s",
                                ticket.file_path,
                            )
                            return
                    tgt.unlink()
                    logger.info(
                        "Fix D: removed newly-created file from failed repair: %s",
                        ticket.file_path,
                    )
        except Exception as _exc:  # pragma: no cover
            logger.debug(
                "Fix D: rollback best-effort failed for %s: %s",
                ticket.file_path, _exc,
            )

    try:
        prompt = _build_repair_prompt(ticket)
        target_file = skill_path / ticket.file_path
        # Option-C fix: when the ticket carries a concrete file_path AND
        # that path resolves to a directory, the original is_dir() backstop
        # still refuses — a scanner-emitted directory path was always
        # unrepairable, and pinning that behaviour is required by the
        # test plan.  When ticket.file_path is empty, the ticket is
        # explicitly global-scope (the generation-side normaliser in
        # repair_tickets.py turned a repo-level / malformed finding into
        # file_path=""): let it through to the coder instead of
        # rejecting it as "directory target".
        if ticket.file_path and target_file.is_dir():
            logger.warning(
                "execute_repair: target %s is a directory — governance finding has no specific file, skipping %s",
                target_file, ticket.ticket_id,
            )
            try:
                from saturnday.ticket_runner import _log_gap
                _log_gap("REPAIR", "unrepairable_target",
                         "repair target is directory — structurally unrepairable",
                         ticket_id=ticket.ticket_id, file=ticket.file_path)
            except Exception:
                pass
            return RepairResult(
                ticket_id=ticket.ticket_id,
                status="failed",
                findings_before=findings_before,
                findings_after=findings_before,
                findings_resolved=[],
                error=f"repair target {ticket.file_path!r} is a directory — governance finding has no specific file",
                group_key=ticket.group_key,
            )
        # File may not exist yet (e.g. missing package.json) — coder creates it
        _t_coder = time.monotonic()
        repaired = coder_fn(prompt, str(target_file), skill_path)
        logger.info("TIMING %-30s %.2fs [%s]", "repair_coder_fn", time.monotonic() - _t_coder, ticket.ticket_id)

        # Writes only happen when the ticket has a concrete file_path.
        # Global-scope tickets (file_path=="") never write via the
        # executor — their coder is expected to act agentically in
        # CLI mode, and in API mode there's no single target to
        # write to.  This lets global-scope tickets pass through to
        # the post-scan phase regardless of backend mode.
        if ticket.file_path and (target_file.exists() or cli_mode):
            if cli_mode:
                # CLI backends (claude-cli, codex-cli) edit files directly.
                # The response is conversational text, NOT file content.
                # Do NOT write it to disk — the agent already made changes.
                logger.debug(
                    "execute_repair: CLI mode — coder edited files directly, "
                    "skipping write for %s", target_file
                )
            else:
                # API backends return file content that must be written.
                if not repaired or not repaired.strip():
                    return RepairResult(
                        ticket_id=ticket.ticket_id,
                        status="failed",
                        findings_before=findings_before,
                        findings_after=findings_before,
                        findings_resolved=[],
                        error="Coder returned empty content — refusing to overwrite file",
                        group_key=ticket.group_key,
                    )
                if len(repaired) > 1_000_000:  # 1MB sanity limit
                    return RepairResult(
                        ticket_id=ticket.ticket_id,
                        status="failed",
                        findings_before=findings_before,
                        findings_after=findings_before,
                        findings_resolved=[],
                        error=f"Coder returned {len(repaired)} bytes — exceeds 1MB safety limit",
                        group_key=ticket.group_key,
                    )
                target_file.write_text(repaired, encoding="utf-8")
                logger.debug(
                    "execute_repair: wrote repaired content to %s", target_file
                )
        elif ticket.file_path:
            # For API backends, if file still doesn't exist after coder ran,
            # try writing the coder's output as new file content
            if repaired and repaired.strip() and not cli_mode:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(repaired, encoding="utf-8")
                logger.debug("execute_repair: created new file %s", target_file)
    except Exception as exc:
        logger.exception(
            "execute_repair: coder_fn raised exception for ticket=%s",
            ticket.ticket_id,
        )
        # Fix D: best-effort rollback when the coder raised before post-scan.
        _rollback_target_on_failure()
        return RepairResult(
            ticket_id=ticket.ticket_id,
            status="failed",
            findings_before=findings_before,
            findings_after=findings_before,
            findings_resolved=[],
            error=str(exc),
            group_key=ticket.group_key,
        )

    # --- post-scan ----------------------------------------------------------
    _t_post = time.monotonic()
    if scan_fn is not None:
        after_findings_all = scan_fn(skill_path)
    else:
        from saturnday.guard.cloud_scanner import scan_skill
        after_findings_all = scan_skill(skill_path).findings
    logger.info("TIMING %-30s %.2fs [%s]", "repair_post_scan", time.monotonic() - _t_post, ticket.ticket_id)
    if ticket.file_path:
        after_findings = [
            f for f in after_findings_all
            if f.kind == ticket.finding_kind and f.file == ticket.file_path
        ]
    else:
        after_findings = [
            f for f in after_findings_all if f.kind == ticket.finding_kind
        ]
    findings_after = len(after_findings)

    if findings_after == 0:
        status = "fixed"
    elif findings_after < findings_before:
        status = "partial"
    else:
        status = "failed"

    resolved = [ticket.finding_kind] if findings_after < findings_before else []

    # Fix D: "failed" must mean no changes kept.  Roll the target file
    # back to its pre-repair state so the next successful ticket cannot
    # sweep lingering bad bytes into a commit.
    if status == "failed":
        _rollback_target_on_failure()

    logger.info(
        "execute_repair: ticket=%s status=%s before=%d after=%d",
        ticket.ticket_id,
        status,
        findings_before,
        findings_after,
    )

    return RepairResult(
        ticket_id=ticket.ticket_id,
        status=status,
        findings_before=findings_before,
        findings_after=findings_after,
        findings_resolved=resolved,
        group_key=ticket.group_key,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_repair_prompt(ticket: RepairTicket) -> str:
    """Construct a structured repair prompt from ticket data.

    Args:
        ticket: The repair ticket containing finding evidence and guidance.

    Returns:
        A multi-line prompt string suitable for passing to a coder backend.
    """
    parts = [f"Fix the following {ticket.finding_kind} issue in {ticket.file_path}:"]
    for e in ticket.evidence:
        parts.append(f"  - {e}")
    if ticket.remediation:
        if isinstance(ticket.remediation, str):
            parts.append(f"\nHow to fix: {ticket.remediation}")
        else:
            why = ticket.remediation.get("why", "")
            fix = ticket.remediation.get("fix", "")
            patch = ticket.remediation.get("patch")
            if why:
                parts.append(f"\nWhy it matters: {why}")
            if fix:
                parts.append(f"How to fix: {fix}")
            if patch:
                parts.append(f"Patch template:\n{patch}")
    else:
        try:
            from saturnday.remediation_guidance import get_guidance
            g = get_guidance(ticket.finding_kind)
            if g:
                parts.append(f"\nWhy it matters: {g.why_it_matters}")
                parts.append(f"How to fix: {g.how_to_fix}")
                if g.patch_template:
                    parts.append(f"Patch template:\n{g.patch_template}")
        except ImportError:
            pass
    return "\n".join(parts)
