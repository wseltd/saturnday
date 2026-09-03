"""Memory enforcement — check enforced rules against changed files.

Phase 5 (T018).  Enforced rules from the memory DB are checked after
post-checks pass but before the code_reviewer step.  Violations produce
findings in the standard governance format.

All functions are non-fatal — errors are logged and swallowed so this
module never blocks the pipeline.

Usage::

    from saturnday.run.memory_enforcement import check_enforced_rules
    findings = check_enforced_rules(conn, changed_files, repo_path)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

from saturnday.run.lessons import increment_rule_trigger, load_active_rules

logger = logging.getLogger(__name__)

__all__ = ["check_enforced_rules"]


def check_enforced_rules(
    conn: "sqlite3.Connection",
    changed_files: list[str],
    repo_path: Path,
) -> list[dict]:
    """Check enforced rules from the memory DB against the changed files.

    For each rule with ``status = 'enforced'``:

    1. Evaluate trigger_conditions (file_patterns glob match against
       changed_files, risk_tags are advisory only in this version).
    2. If triggered, scan the file contents for forbidden_patterns (regex).
    3. If a forbidden_pattern is found AND no required_pattern is present
       in that file, emit a finding.
    4. Increment trigger_count on any rule that fires.

    Args:
        conn: Open connection from :func:`~saturnday.run.lessons.init_memory_db`.
        changed_files: File paths (relative to repo_path) to inspect.
        repo_path: Root of the target repository.

    Returns:
        List of finding dicts compatible with the governance finding format::

            {
                "file": relative_path,
                "line": line_number,
                "kind": "enforced_rule_<rule_id>",
                "detail": human_rule_text,
                "severity": rule_severity,
                "rule_id": rule_id,
            }
    """
    if not changed_files:
        return []

    enforced_rules = load_active_rules(conn, status="enforced")
    if not enforced_rules:
        return []

    findings: list[dict] = []

    for rule in enforced_rules:
        triggered_files = _get_triggered_files(rule, changed_files, conn=conn)
        if not triggered_files:
            continue

        rule_fired = False
        for rel_path in triggered_files:
            file_path = repo_path / rel_path
            if not file_path.is_file():
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("memory_enforcement: cannot read %s: %s", rel_path, exc)
                continue

            file_findings = _check_file_against_rule(rule, content, rel_path)
            if file_findings:
                findings.extend(file_findings)
                rule_fired = True

        if rule_fired:
            try:
                increment_rule_trigger(conn, rule.rule_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "memory_enforcement: failed to increment trigger for %s: %s",
                    rule.rule_id,
                    exc,
                )

    return findings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_triggered_files(
    rule: object,
    changed_files: list[str],
    conn: "sqlite3.Connection | None" = None,
) -> list[str]:
    """Return changed files that match a rule's trigger_conditions.

    Matches file_patterns from trigger_conditions against changed_files using
    simple substring containment (no glob engine required — patterns are
    fragments like '.py', 'security', etc.).  If no file_patterns are set,
    the rule applies to all changed files.

    EXT5: When ``conn`` is supplied and file_patterns is empty, also checks
    the ``memory_scopes`` table for path scopes on this rule and uses those
    as additional file patterns.

    Args:
        rule: A Rule dataclass instance.
        changed_files: Relative file paths.
        conn: Optional open DB connection for EXT5 scope-aware expansion.

    Returns:
        Subset of changed_files that match the trigger conditions.
    """
    trigger_conditions: dict = getattr(rule, "trigger_conditions", {}) or {}
    file_patterns: list[str] = trigger_conditions.get("file_patterns", [])

    matched: list[str] = []

    if file_patterns:
        for rel_path in changed_files:
            for pattern in file_patterns:
                if _path_matches_pattern(rel_path, pattern):
                    matched.append(rel_path)
                    break
    else:
        # No explicit file_patterns — start with all changed files
        matched = list(changed_files)

    # EXT5: scope-aware trigger expansion using memory_scopes path entries
    if conn is not None and not file_patterns:
        try:
            from saturnday.run.lessons import load_scopes
            rule_id = getattr(rule, "rule_id", None)
            if rule_id:
                rule_scopes = load_scopes(conn, rule_id, "rule")
                scope_patterns = [
                    s["scope_value"]
                    for s in rule_scopes
                    if s["scope_type"] == "path"
                ]
                if scope_patterns:
                    # Scope patterns specified — restrict to those matches
                    scope_matched: list[str] = []
                    for rel_path in changed_files:
                        if rel_path not in scope_matched:
                            for pattern in scope_patterns:
                                if _path_matches_pattern(rel_path, pattern):
                                    scope_matched.append(rel_path)
                                    break
                    matched = scope_matched
        except Exception:
            pass  # scopes table may not exist on older DBs

    return matched


def _path_matches_pattern(path: str, pattern: str) -> bool:
    """Return True if path matches the pattern.

    Supports ``*`` as a wildcard (converted to regex ``.*``).

    Args:
        path: Relative file path string.
        pattern: Pattern string, may contain ``*``.

    Returns:
        True if the pattern matches anywhere in the path.
    """
    if "*" not in pattern:
        return pattern in path
    regex = re.escape(pattern).replace(r"\*", ".*")
    return bool(re.search(regex, path))


def _check_file_against_rule(
    rule: object,
    content: str,
    rel_path: str,
) -> list[dict]:
    """Produce findings for a single file against a single rule.

    Logic:
    - If no forbidden_patterns: skip (no pattern to enforce).
    - For each forbidden_pattern found in content:
        - If required_patterns is non-empty and at least one required_pattern
          is also present in the file, suppress the finding (the correct
          safeguard is already in place).
        - Otherwise emit a finding.

    Args:
        rule: Rule dataclass instance.
        content: Full file text.
        rel_path: Relative path (for the finding dict).

    Returns:
        List of finding dicts (may be empty).
    """
    forbidden_patterns: list[str] = getattr(rule, "forbidden_patterns", []) or []
    required_patterns: list[str] = getattr(rule, "required_patterns", []) or []
    rule_id: str = getattr(rule, "rule_id", "unknown")
    human_rule: str = getattr(rule, "human_rule", "")
    severity: str = getattr(rule, "severity", "medium")

    if not forbidden_patterns:
        return []

    # Check required patterns once per file (not per forbidden match).
    required_satisfied = _any_pattern_present(required_patterns, content)

    findings: list[dict] = []
    lines = content.splitlines()

    for fp in forbidden_patterns:
        try:
            compiled = re.compile(fp)
        except re.error as exc:
            logger.debug(
                "memory_enforcement: bad regex in rule %s: %s (%s)",
                rule_id,
                fp,
                exc,
            )
            continue

        for line_num, line in enumerate(lines, start=1):
            if compiled.search(line):
                if required_satisfied:
                    # Required safeguard present — suppress finding.
                    break
                findings.append(
                    {
                        "file": rel_path,
                        "line": line_num,
                        "kind": f"enforced_rule_{rule_id}",
                        "detail": human_rule,
                        "severity": severity,
                        "rule_id": rule_id,
                    }
                )
                break  # one finding per forbidden_pattern per file

    return findings


def _any_pattern_present(patterns: list[str], content: str) -> bool:
    """Return True if any of the patterns matches anywhere in content.

    Args:
        patterns: List of regex pattern strings.
        content: Full file text to search.

    Returns:
        True if at least one pattern matches.
    """
    for p in patterns:
        try:
            if re.search(p, content):
                return True
        except re.error:
            continue
    return False
