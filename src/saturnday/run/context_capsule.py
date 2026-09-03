"""Context capsule generator for the Saturnday governed memory system (v1.1.01).

Phase 2 of the memory system. Produces a bounded, structured context block per
ticket that replaces the flat lessons-prefix injection. Also provides outlier
detection (T009), pre-ticket knowledge check generation (T010), and residual
fetch gating (T011).

Design constraints:
- capsule_to_prompt output <= 2000 chars (C-4 from execution plan).
- No new external dependencies — stdlib + lessons.py only.
- Must never raise into the pipeline; all public functions are safe by default.
- All functions are deterministic; no LLM calls in this module.
"""

from __future__ import annotations

import fnmatch
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saturnday._types import TicketSpec

logger = logging.getLogger(__name__)

__all__ = [
    "ContextCapsule",
    "KnowledgeCheck",
    "build_context_capsule",
    "capsule_to_prompt",
    "select_outliers",
    "generate_knowledge_check",
    "format_knowledge_check_for_prompt",
    "requires_residual_fetch",
    "fetch_residual_evidence",
    "check_residual_gates",
]

# ---------------------------------------------------------------------------
# Outlier detection patterns
# ---------------------------------------------------------------------------

_BUILD_FILES = frozenset({
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Makefile",
    "Dockerfile",
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "requirements-dev.txt",
})

_SECURITY_SENSITIVE_GLOBS = (
    "**/auth/**",
    "**/security/**",
    "**/crypto/**",
    "**/*secret*",
    "**/*credential*",
    "**/*token*",
    "**/*.key",
    "**/*.pem",
)

_PUBLIC_API_GLOBS = (
    "**/api/**",
    "**/routes/**",
    "**/__init__.py",
)

_SHELL_GLOBS = (
    "**/*.sh",
)

_CI_GLOB = ".github/**/*.yml"

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ContextCapsule:
    """Bounded per-ticket context built from the memory DB and ticket metadata.

    Attributes:
        ticket_id: Identifier of the ticket this capsule was built for.
        ticket_goal: Natural language goal of the ticket (first 200 chars).
        ticket_class: Classification — 'generation', 'repair', or 'remediation'.
        scope: Dict with 'allowed_globs' and 'out_of_scope_globs' lists.
        repo_invariants: Memory items of type 'invariant' (root_cause strings).
        active_rules: Human-readable rule texts from the rules table.
        recent_relevant_lessons: Up to 5 relevant lesson summaries (strings).
        risk_outliers: Human-readable outlier descriptions from select_outliers().
        residual_refs: Evidence paths that should be fetched before acting.
        playbooks: Matched playbook summaries for prompt injection (EXT4).
    """

    ticket_id: str
    ticket_goal: str
    ticket_class: str
    scope: dict = field(default_factory=dict)
    repo_invariants: list[str] = field(default_factory=list)
    active_rules: list[str] = field(default_factory=list)
    recent_relevant_lessons: list[str] = field(default_factory=list)
    risk_outliers: list[str] = field(default_factory=list)
    residual_refs: list[str] = field(default_factory=list)
    playbooks: list[str] = field(default_factory=list)  # EXT4: matched playbook summaries


@dataclass
class KnowledgeCheck:
    """Structured pre-ticket knowledge check generated from a ContextCapsule.

    Attributes:
        files_in_scope: Glob patterns the coder may touch.
        files_out_of_scope: Glob patterns the coder must not touch.
        symbols_to_touch: Symbol names extracted from the ticket goal.
        active_rules: Rule IDs / human rule texts from the capsule.
        relevant_lessons: Lesson summaries from the capsule.
        what_could_break: Risk area descriptions from outliers.
        applicable_playbooks: Matched playbook summaries (EXT4).
    """

    files_in_scope: list[str] = field(default_factory=list)
    files_out_of_scope: list[str] = field(default_factory=list)
    symbols_to_touch: list[str] = field(default_factory=list)
    active_rules: list[str] = field(default_factory=list)
    relevant_lessons: list[str] = field(default_factory=list)
    what_could_break: list[str] = field(default_factory=list)
    applicable_playbooks: list[str] = field(default_factory=list)  # EXT4


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _matches_any_glob(path: str, globs: tuple[str, ...] | list[str]) -> bool:
    """Return True when *path* matches at least one glob pattern."""
    name = Path(path).name
    for pattern in globs:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def _classify_ticket(goal: str) -> str:
    """Infer a coarse ticket class from the goal text."""
    lower = goal.lower()
    if any(w in lower for w in ("repair", "fix", "remediate", "remed")):
        return "repair"
    if any(w in lower for w in ("refactor", "migrate", "move", "rename")):
        return "remediation"
    return "generation"


def _load_invariants(conn: sqlite3.Connection) -> list[str]:
    """Load active invariant memory items and return their root_cause texts."""
    try:
        rows = conn.execute(
            "SELECT root_cause, files_touched FROM memory_items "
            "WHERE memory_type = 'invariant' AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
    except Exception:
        return []

    results: list[str] = []
    for root_cause, files_raw in rows:
        text = root_cause or ""
        if text:
            results.append(text[:200])
    return results


def _load_rules(conn: sqlite3.Connection) -> list[str]:
    """Load advisory and enforced rule human texts from the rules table."""
    try:
        rows = conn.execute(
            "SELECT rule_id, human_rule FROM rules "
            "WHERE status IN ('active', 'enforced', 'advisory') "
            "ORDER BY trigger_count DESC, created_at DESC LIMIT 10"
        ).fetchall()
    except Exception:
        return []

    return [f"[{rule_id}] {human_rule[:200]}" for rule_id, human_rule in rows if human_rule]


def _load_lessons(
    conn: sqlite3.Connection,
    allowed_globs: tuple[str, ...],
) -> list[str]:
    """Load recent active lessons, filtered to those relevant to allowed_globs."""
    try:
        rows = conn.execute(
            "SELECT item_id, failure_mode, corrective_rule_text, files_touched, risk_tags "
            "FROM memory_items WHERE memory_type = 'lesson' AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    except Exception:
        return []

    import json

    results: list[str] = []
    for item_id, failure_mode, rule_text, files_raw, tags_raw in rows:
        # Keep lessons that overlap file scope or have no file restriction
        files: list[str] = []
        try:
            files = json.loads(files_raw) if files_raw else []
        except Exception:
            files = []

        relevant = not files  # no-file-restriction = always relevant
        if not relevant:
            for f in files:
                if _matches_any_glob(f, list(allowed_globs)):
                    relevant = True
                    break

        if relevant:
            summary = rule_text or failure_mode or item_id
            results.append(summary[:200])
        if len(results) >= 5:
            break

    return results


def _load_residual_refs(conn: sqlite3.Connection) -> list[str]:
    """Load evidence_refs from items where residual_required=1 and status=active."""
    import json as _json

    try:
        rows = conn.execute(
            "SELECT evidence_refs FROM memory_items "
            "WHERE residual_required = 1 AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
    except Exception:
        return []

    refs: list[str] = []
    for (refs_raw,) in rows:
        try:
            batch = _json.loads(refs_raw) if refs_raw else []
            refs.extend(batch[:3])
        except Exception:
            pass
    return refs[:10]


# ---------------------------------------------------------------------------
# T008: build_context_capsule and capsule_to_prompt
# ---------------------------------------------------------------------------


def build_context_capsule(
    ticket: "TicketSpec",
    memory_conn: "sqlite3.Connection | None",
    repo_path: "Path | None" = None,
) -> ContextCapsule:
    """Build a bounded context capsule for a ticket from the memory DB.

    Phase 4 (T014–T016): uses the memory_retrieval pipeline for
    ``recent_relevant_lessons`` — hard filtering → ranking → validation before
    injection.  Falls back to the legacy ``_load_lessons`` helper if the
    retrieval module is unavailable.

    If *memory_conn* is None every list is empty but the capsule is still
    returned (safe for callers to use without a DB).

    Args:
        ticket: The ticket being executed.
        memory_conn: Open connection from lessons.init_memory_db(), or None.
        repo_path: Root of the target repository used for staleness validation.

    Returns:
        Populated ContextCapsule.
    """
    allowed = list(ticket.scope.allowed_globs) if ticket.scope else ["**"]
    forbidden = list(ticket.scope.forbidden_globs) if ticket.scope else []

    capsule = ContextCapsule(
        ticket_id=ticket.ticket_id,
        ticket_goal=ticket.goal[:200],
        ticket_class=_classify_ticket(ticket.goal),
        scope={
            "allowed_globs": allowed,
            "out_of_scope_globs": forbidden,
        },
    )

    if memory_conn is None:
        return capsule

    try:
        capsule.repo_invariants = _load_invariants(memory_conn)
        capsule.active_rules = _load_rules(memory_conn)
        capsule.residual_refs = _load_residual_refs(memory_conn)
    except Exception as exc:
        logger.warning("build_context_capsule: DB query failed — %s", exc)

    # EXT4: match playbooks for this ticket
    try:
        from saturnday.run.lessons import match_playbooks
        matched_pbs = match_playbooks(memory_conn, ticket)
        if matched_pbs:
            capsule.playbooks = [
                f"[{pb.name}] {pb.description[:100]}: "
                + "; ".join(
                    s.get("instruction", str(s))[:60] for s in pb.steps[:5]
                )
                for pb in matched_pbs
            ]
    except Exception as exc:
        logger.debug("build_context_capsule: playbook matching failed — %s", exc)

    # Phase 4: use the retrieval pipeline (filter → rank → validate)
    try:
        from saturnday.run.memory_retrieval import (
            filter_relevant_items,
            rank_and_select,
            validate_before_injection,
            format_lessons_for_injection,
        )
        _raw = filter_relevant_items(memory_conn, ticket)
        _ranked = rank_and_select(_raw, ticket, limit=5)
        _validated = validate_before_injection(
            _ranked,
            repo_path,  # None is acceptable — skips file-existence check
            conn=memory_conn,
        )
        if _validated:
            capsule.recent_relevant_lessons = [
                (item.corrective_rule_text or item.failure_mode or item.item_id)[:200]
                for item in _validated
            ]
        else:
            # Fallback to legacy simple query when retrieval returns nothing
            capsule.recent_relevant_lessons = _load_lessons(
                memory_conn, tuple(allowed)
            )
    except Exception as exc:
        logger.warning(
            "build_context_capsule: retrieval pipeline failed (%s) — "
            "falling back to legacy lesson loading",
            exc,
        )
        try:
            capsule.recent_relevant_lessons = _load_lessons(
                memory_conn, tuple(allowed)
            )
        except Exception:
            pass

    return capsule


def capsule_to_prompt(capsule: ContextCapsule, max_chars: int = 2000) -> str:
    """Render a ContextCapsule as a compact structured prompt block.

    Budget order (fills until max_chars):
    1. Active enforced rules  — up to 500 chars (always included)
    2. Risk outliers          — up to 200 chars (always included)
    3. Playbooks              — up to 400 chars (EXT4, advisory)
    4. Recent lessons         — up to 600 chars (reduced from 800 to accommodate playbooks)
    5. Repo invariants        — up to 300 chars
    6. Residual refs          — up to 200 chars

    Args:
        capsule: The capsule to render.
        max_chars: Hard character limit on the output string.

    Returns:
        Structured text block. Never empty (at minimum returns scope info).
    """
    segments: list[str] = []

    # --- scope header ---
    allowed_str = ", ".join(capsule.scope.get("allowed_globs", ["**"])) or "**"
    forbidden_str = ", ".join(capsule.scope.get("out_of_scope_globs", []))
    header_parts = [f"TICKET: {capsule.ticket_id} | class={capsule.ticket_class}"]
    header_parts.append(f"SCOPE in: {allowed_str}")
    if forbidden_str:
        header_parts.append(f"SCOPE out: {forbidden_str}")
    segments.append("\n".join(header_parts))

    total = sum(len(s) for s in segments)

    def _add_section(label: str, items: list[str], budget: int) -> None:
        nonlocal total
        if not items or total >= max_chars:
            return
        lines = [label]
        used = len(label) + 1
        for i, item in enumerate(items, start=1):
            entry = f"  {i}. {item}"
            if used + len(entry) + 1 > budget:
                break
            lines.append(entry)
            used += len(entry) + 1
        block = "\n".join(lines)
        remaining = max_chars - total
        if remaining > 10:
            segments.append(block[:remaining])
            total += len(segments[-1]) + 1  # +1 for separator newline

    _add_section("ACTIVE RULES:", capsule.active_rules, 500)
    _add_section("RISK OUTLIERS:", capsule.risk_outliers, 200)
    _add_section("PLAYBOOKS:", capsule.playbooks, 400)  # EXT4
    _add_section("RECENT LESSONS:", capsule.recent_relevant_lessons, 600)
    _add_section("REPO INVARIANTS:", capsule.repo_invariants, 300)
    _add_section("RESIDUAL REFS:", capsule.residual_refs, 200)

    result = "\n\n".join(segments)
    return result[:max_chars]


# ---------------------------------------------------------------------------
# T009: Outlier selector
# ---------------------------------------------------------------------------


def select_outliers(
    ticket: "TicketSpec",
    changed_files: list[str],
    findings: list[dict],
) -> list[str]:
    """Detect high-risk outlier conditions in the current ticket context.

    Checks:
    1. Policy exemption files in scope.
    2. Security findings in the findings list.
    3. Public API changes (__init__.py, routes, api directories).
    4. Shell execution paths (subprocess/os.system related findings or .sh files).
    5. Build/deploy file edits (Dockerfile, CI configs, pyproject.toml, etc.).
    6. Contradictory evidence (same file has both PASS and FAIL finding).
    7. Test files in changed_files (possible test deletion risk).

    Args:
        ticket: The ticket being evaluated.
        changed_files: Files changed by the coder (from git status).
        findings: Governance findings list — each a dict with at least
            'path', 'message', 'severity' keys.

    Returns:
        List of human-readable outlier description strings. Empty = no outliers.
    """
    outliers: list[str] = []

    # 1. Policy exemption in scope
    allowed = list(ticket.scope.allowed_globs) if ticket.scope else []
    for glob in allowed:
        if "saturnday-policy" in glob or "policy.yaml" in glob:
            outliers.append("OUTLIER: Policy exemption file is in ticket scope")
            break

    # 2. Security findings
    sec_findings = [
        f for f in findings
        if any(
            kw in (f.get("message", "") + f.get("check_name", "")).lower()
            for kw in ("secret", "credential", "token", "security", "injection", "xss", "sql")
        )
    ]
    if sec_findings:
        outliers.append(
            f"OUTLIER: {len(sec_findings)} security finding(s) present — "
            + "; ".join(
                f.get("check_name", f.get("message", "unknown"))[:60]
                for f in sec_findings[:3]
            )
        )

    # 3. Public API changes
    api_files = [
        f for f in changed_files
        if _matches_any_glob(f, _PUBLIC_API_GLOBS)
    ]
    if api_files:
        outliers.append(
            f"OUTLIER: Public API/interface file(s) changed: "
            + ", ".join(api_files[:3])
        )

    # 4. Shell execution in changed files or findings
    shell_files = [f for f in changed_files if _matches_any_glob(f, _SHELL_GLOBS)]
    shell_findings = [
        f for f in findings
        if any(
            kw in (f.get("message", "") + f.get("check_name", "")).lower()
            for kw in ("subprocess", "os.system", "shell=true", "shell_exec")
        )
    ]
    if shell_files or shell_findings:
        desc_parts: list[str] = []
        if shell_files:
            desc_parts.append(f"shell files: {', '.join(shell_files[:2])}")
        if shell_findings:
            desc_parts.append(f"{len(shell_findings)} shell finding(s)")
        outliers.append("OUTLIER: Shell execution path — " + "; ".join(desc_parts))

    # 5. Build/deploy file edits
    build_files = [
        f for f in changed_files
        if (
            Path(f).name in _BUILD_FILES
            or fnmatch.fnmatch(f, _CI_GLOB)
            or fnmatch.fnmatch(f, ".github/**")
        )
    ]
    if build_files:
        outliers.append(
            f"OUTLIER: Build/deploy/config file(s) changed: "
            + ", ".join(build_files[:3])
        )

    # 6. Contradictory evidence (same path with PASS and FAIL)
    path_pass: set[str] = set()
    path_fail: set[str] = set()
    for f in findings:
        p = f.get("path", "")
        sev = str(f.get("severity", "")).lower()
        disp = str(f.get("disposition", "")).lower()
        if disp == "pass" or sev == "info":
            path_pass.add(p)
        else:
            path_fail.add(p)
    contradictions = path_pass & path_fail
    if contradictions:
        outliers.append(
            "OUTLIER: Contradictory evidence — same file(s) have both PASS and FAIL: "
            + ", ".join(sorted(contradictions)[:3])
        )

    # 7. Test files in changed_files (possible deletion risk)
    test_files = [
        f for f in changed_files
        if (
            Path(f).name.startswith("test_")
            or Path(f).name.endswith("_test.py")
        )
    ]
    if test_files:
        outliers.append(
            f"OUTLIER: Test file(s) in changed set (verify no functions deleted): "
            + ", ".join(test_files[:3])
        )

    return outliers


# ---------------------------------------------------------------------------
# T010: Pre-ticket knowledge check
# ---------------------------------------------------------------------------


def generate_knowledge_check(
    capsule: ContextCapsule,
    repo_path: "Path | None" = None,
) -> KnowledgeCheck:
    """Generate a pre-ticket knowledge check from a ContextCapsule.

    The check is generated deterministically from the capsule data. No LLM
    calls are made.

    Args:
        capsule: Populated ContextCapsule.
        repo_path: Repository root (used to expand globs for files_in_scope).

    Returns:
        KnowledgeCheck with populated fields.
    """
    import re

    files_in_scope = list(capsule.scope.get("allowed_globs", ["**"]))
    files_out_of_scope = list(capsule.scope.get("out_of_scope_globs", []))

    # Expand globs to actual files when repo_path is available
    if repo_path is not None:
        expanded: list[str] = []
        for pattern in files_in_scope:
            try:
                matches = [
                    str(p.relative_to(repo_path))
                    for p in repo_path.glob(pattern)
                    if p.is_file()
                ]
                expanded.extend(matches[:10])
            except Exception:
                expanded.append(pattern)
        if expanded:
            files_in_scope = expanded[:20]

    # Extract probable symbol names from goal (CamelCase or snake_case identifiers)
    symbol_pattern = re.compile(
        r"\b([A-Z][a-zA-Z0-9]+|[a-z_][a-z_0-9]+(?:_[a-z_0-9]+)+)\b"
    )
    raw_symbols = symbol_pattern.findall(capsule.ticket_goal)
    # Filter out common English stop-words
    _stop = {
        "the", "and", "for", "this", "that", "with", "from", "into",
        "a", "in", "is", "to", "of", "at", "by", "an", "be",
    }
    symbols = [
        s for s in dict.fromkeys(raw_symbols)
        if s.lower() not in _stop
    ][:10]

    return KnowledgeCheck(
        files_in_scope=files_in_scope,
        files_out_of_scope=files_out_of_scope,
        symbols_to_touch=symbols,
        active_rules=capsule.active_rules[:5],
        relevant_lessons=capsule.recent_relevant_lessons[:5],
        what_could_break=capsule.risk_outliers[:5],
        applicable_playbooks=capsule.playbooks[:3],  # EXT4
    )


def format_knowledge_check_for_prompt(check: KnowledgeCheck) -> str:
    """Render a KnowledgeCheck as a structured prompt prefix block.

    Format:
        PRE-TICKET CONTEXT:
        Files in scope: ...
        Files out of scope (DO NOT TOUCH): ...
        Active rules: ...
        Recent lessons: ...
        Risk areas: ...

    Args:
        check: Populated KnowledgeCheck.

    Returns:
        Formatted string. Will not exceed ~1000 chars.
    """
    lines: list[str] = ["BEFORE YOU CODE — answer these:"]

    def _fmt_list(items: list[str], label: str, limit: int = 5) -> None:
        if items:
            lines.append(f"{label}: {'; '.join(str(i) for i in items[:limit])}")
        else:
            lines.append(f"{label}: (none)")

    _fmt_list(check.files_in_scope, "1. Files in scope", 10)
    _fmt_list(check.files_out_of_scope, "2. Files out of scope (DO NOT TOUCH)", 10)
    _fmt_list(check.symbols_to_touch, "3. Symbols likely touched")
    _fmt_list(check.active_rules, "4. Active rules that apply")
    _fmt_list(check.relevant_lessons, "5. Relevant prior lessons")
    _fmt_list(check.what_could_break, "6. What could break")

    result = "\n".join(lines)
    return result[:1000]


# ---------------------------------------------------------------------------
# T011: Residual fetch gating
# ---------------------------------------------------------------------------


def requires_residual_fetch(
    action_type: str,
    changed_files: list[str],
) -> bool:
    """Return True when the action requires exact evidence fetching.

    High-risk action types / file patterns that trigger residual fetch:
    - 'test_deletion': any test file in changed_files
    - 'exemption': any finding is being exempted
    - 'security': any file matches security-sensitive patterns
    - 'build': any build/config/deploy file changed
    - 'interface': __init__.py, routes, or API files changed

    Args:
        action_type: A descriptive category string (e.g. 'test_deletion').
        changed_files: Files changed in this operation.

    Returns:
        True when residual fetch is required, False otherwise.
    """
    _HIGH_RISK_ACTIONS = frozenset({
        "test_deletion", "exemption", "security", "build", "interface",
    })
    if action_type in _HIGH_RISK_ACTIONS:
        return True

    for f in changed_files:
        name = Path(f).name
        # Test files
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
        # Build/config files
        if name in _BUILD_FILES:
            return True
        # CI config
        if fnmatch.fnmatch(f, ".github/**"):
            return True
        # Security-sensitive
        if _matches_any_glob(f, _SECURITY_SENSITIVE_GLOBS):
            return True
        # Public interface
        if _matches_any_glob(f, _PUBLIC_API_GLOBS):
            return True

    return False


def fetch_residual_evidence(file_path: str, repo_path: Path) -> str:
    """Read the exact content of a file for prompt injection.

    Provides full-fidelity evidence for high-risk actions instead of relying
    on summaries. Reads up to 3000 characters.

    Args:
        file_path: Relative or absolute path to the file.
        repo_path: Repository root used to resolve relative paths.

    Returns:
        File content (up to 3000 chars), or an error message string if the
        file cannot be read. Never raises.
    """
    try:
        p = Path(file_path)
        if not p.is_absolute():
            p = repo_path / file_path
        if not p.exists():
            return f"[residual evidence: {file_path} does not exist]"
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 3000:
            content = content[:3000] + "\n... [truncated]"
        return content
    except Exception as exc:
        logger.warning("fetch_residual_evidence failed for %s: %s", file_path, exc)
        return f"[residual evidence: could not read {file_path} — {exc}]"


def check_residual_gates(
    capsule: ContextCapsule,
    changed_files: list[str],
    repo_path: Path,
) -> list[str]:
    """Check whether required evidence exists for high-risk actions.

    Advisory only in v1 — returns warning strings, never blocks the pipeline.

    Gate conditions checked:
    - Test file in changed_files: requires prior test evidence in capsule.
    - Security-sensitive file edit: warns to run scan first.
    - Build file change: warns about build evidence.
    - Interface change (__init__.py etc.): warns about contract evidence.

    Args:
        capsule: Populated context capsule for this ticket.
        changed_files: Files changed by the coder so far.
        repo_path: Repository root for existence checks.

    Returns:
        List of warning strings. Empty list = no gates triggered.
    """
    warnings: list[str] = []

    for f in changed_files:
        name = Path(f).name
        full = repo_path / f if not Path(f).is_absolute() else Path(f)

        # Test file gate
        if name.startswith("test_") or name.endswith("_test.py"):
            if not any("test" in ref.lower() for ref in capsule.residual_refs):
                warnings.append(
                    f"GATE: Test file changed ({f}) — verify test functions still "
                    f"exist and no symbols were removed without updating callers."
                )

        # Security file gate
        if _matches_any_glob(f, _SECURITY_SENSITIVE_GLOBS):
            warnings.append(
                f"GATE: Security-sensitive file changed ({f}) — "
                f"confirm a governance scan has been run on this file."
            )

        # Build file gate
        if name in _BUILD_FILES or fnmatch.fnmatch(f, ".github/**"):
            warnings.append(
                f"GATE: Build/config file changed ({f}) — "
                f"confirm build and test pipeline still passes."
            )

        # Interface file gate
        if _matches_any_glob(f, _PUBLIC_API_GLOBS):
            warnings.append(
                f"GATE: Public interface file changed ({f}) — "
                f"confirm all importers of this module are still compatible."
            )

    return warnings
