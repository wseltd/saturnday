"""Fingerprint-based ratchet enforcement for security governance.

Compares current scan findings against a baseline snapshot to detect:
- New findings (not in baseline) — blocked in block_new mode
- Resolved findings (in baseline but not current) — tracked
- Legacy findings (in baseline and still present) — escalated after enforcement date

Fingerprints are line-number-independent: a finding moved by unrelated edits
is still recognized as the same finding.

Schema version: 1.0 — fingerprinting approach may be revised in later versions.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class FindingFingerprint:
    """Immutable identity of a single finding, independent of line numbers."""
    rule_id: str
    path: str  # normalised: relative, forward slashes
    symbol: str  # enclosing function/class, or "" for module-level
    snippet_hash: str  # SHA-256 of normalised diagnostic snippet
    severity: str  # error | warning

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> FindingFingerprint:
        return cls(
            rule_id=d["rule_id"],
            path=d["path"],
            symbol=d.get("symbol", ""),
            snippet_hash=d["snippet_hash"],
            severity=d["severity"],
        )


@dataclass
class RatchetResult:
    """Outcome of comparing current findings against baseline."""
    new_findings: list[FindingFingerprint] = field(default_factory=list)
    legacy_findings: list[FindingFingerprint] = field(default_factory=list)
    resolved_findings: list[FindingFingerprint] = field(default_factory=list)
    disposition: str = "PASS"  # PASS | WARN | FAIL
    reasons: list[str] = field(default_factory=list)


def _normalise_path(path: str) -> str:
    """Normalise a file path: relative, forward slashes, no leading ./"""
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _normalise_snippet(text: str) -> str:
    """Normalise a diagnostic snippet for hashing.

    Strips whitespace, removes literal string values and numeric literals
    to reduce fragility under harmless edits.
    """
    text = text.strip()
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Remove string literal contents (keep quotes)
    text = re.sub(r'"[^"]*"', '""', text)
    text = re.sub(r"'[^']*'", "''", text)
    # Normalise numeric literals
    text = re.sub(r"\b\d+\b", "0", text)
    return text


def _hash_snippet(snippet: str) -> str:
    """SHA-256 of normalised snippet."""
    normalised = _normalise_snippet(snippet)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _get_symbol_at_line(source: str, line: int) -> str:
    """Get enclosing function or class name at a given line using AST.

    Returns empty string for module-level code or if parsing fails.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""

    enclosing = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
                if node.lineno <= line <= (node.end_lineno or node.lineno):
                    # Prefer the deepest enclosing scope
                    enclosing = node.name
    return enclosing


def compute_fingerprint(
    rule_id: str,
    path: str,
    severity: str,
    snippet: str,
    line: int = 0,
    source: str = "",
) -> FindingFingerprint:
    """Compute a stable fingerprint for a single finding.

    Args:
        rule_id: e.g. "SEC-013"
        path: file path (will be normalised)
        severity: "error" or "warning"
        snippet: diagnostic text or matched code fragment
        line: line number (used only for AST symbol lookup, not in fingerprint)
        source: full file source (for AST-based symbol resolution)
    """
    norm_path = _normalise_path(path)
    symbol = _get_symbol_at_line(source, line) if source and line > 0 else ""
    snippet_hash = _hash_snippet(snippet)

    return FindingFingerprint(
        rule_id=rule_id,
        path=norm_path,
        symbol=symbol,
        snippet_hash=snippet_hash,
        severity=severity,
    )


def fingerprint_check_results(
    check_results: list,
    repo_path: Path | None = None,
) -> set[FindingFingerprint]:
    """Extract fingerprints from a list of CheckResult objects.

    Each finding dict in a CheckResult is expected to have:
      - file/filename: relative path
      - line/line_number: line number
      - message/detail/issue_text: diagnostic text
    Plus the CheckResult itself has: name (check_name), severity, rule_id.
    """
    from .policy_manifest import RULE_IDS

    fingerprints: set[FindingFingerprint] = set()

    for cr in check_results:
        if cr.status not in ("FAIL", "WARN"):
            continue

        rule_id = cr.rule_id or RULE_IDS.get(cr.name, cr.name)

        for finding in cr.findings:
            # Some checks (notably ``_scan_secrets`` in review.py) emit
            # the location under the key ``path`` rather than ``file``.
            # Prior to accepting it as a fallback, every secret finding
            # ended up with file_path == "" and therefore an identical
            # fingerprint — which made baselined secrets indistinguishable
            # from new secrets in a different file (Nick #1 secondary).
            file_path = (
                finding.get("file")
                or finding.get("filename")
                or finding.get("path")
                or ""
            )
            line = finding.get("line") or finding.get("line_number") or 0
            if isinstance(line, str):
                try:
                    line = int(line)
                except ValueError:
                    line = 0

            snippet = (
                finding.get("detail")
                or finding.get("issue_text")
                or finding.get("message")
                or finding.get("pattern", "")
            )

            # Try to read source for AST symbol resolution
            source = ""
            if repo_path and file_path:
                source_path = repo_path / file_path
                if source_path.exists():
                    try:
                        source = source_path.read_text()
                    except Exception:
                        pass

            fp = compute_fingerprint(
                rule_id=rule_id,
                path=file_path,
                severity=cr.severity,
                snippet=snippet,
                line=line,
                source=source,
            )
            fingerprints.add(fp)

    return fingerprints


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------

BASELINE_SCHEMA_VERSION = "1.0"


@dataclass
class Baseline:
    """Snapshot of findings at a point in time.

    Semantic contract (I.3):

    * ``ratchet_mode == "block_new"`` (default):
      only **new** non-waived findings block.  Pre-existing (legacy) debt
      captured in the baseline is allowed — this is the "treat known debt
      as frozen" mode.

    * ``ratchet_mode == "ratchet_down"``:
      new findings block AND the total non-waived count must not exceed
      the baseline count.  Resolving an old finding and introducing a new
      one is NOT a wash — the new one still blocks via the new-findings
      check, and inserting two when one was resolved still fails the
      count check.  Continuous downward pressure.

    * ``enforcement_date`` is a **deadline to clear legacy debt**,
      independent of mode name.  Before the date, both modes allow the
      legacy findings captured in the baseline.  Once ``today >=
      enforcement_date``, BOTH modes start failing on any remaining
      legacy finding in addition to their mode-specific checks.  This
      means:

        - ``block_new`` with a past enforcement_date no longer "only
          blocks new findings" — the deadline you set has passed and
          legacy debt is now blocking as well.  The mode name describes
          the *pre-deadline* behaviour; the ``enforcement_date`` field
          is the knob that switches on post-deadline strictness.

    Baselines without ``enforcement_date`` never switch on legacy
    blocking — legacy debt is allowed forever until the operator either
    resolves it, waives it, regenerates the baseline, or sets a
    deadline.
    """
    schema_version: str = BASELINE_SCHEMA_VERSION
    created_utc: str = ""
    # I.3: when set, this is a HARD DEADLINE for clearing legacy debt.
    # Once today >= enforcement_date, any remaining legacy (pre-existing)
    # finding starts failing the ratchet in both ``block_new`` and
    # ``ratchet_down`` modes.  Leave empty ("") to allow legacy forever.
    enforcement_date: str = ""  # ISO date: YYYY-MM-DD
    ratchet_mode: str = "block_new"  # block_new | ratchet_down
    repo_sha: str = ""
    findings: set[FindingFingerprint] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "created_utc": self.created_utc,
            "enforcement_date": self.enforcement_date,
            "ratchet_mode": self.ratchet_mode,
            "repo_sha": self.repo_sha,
            "findings": [fp.as_dict() for fp in sorted(
                self.findings, key=lambda f: (f.rule_id, f.path, f.symbol)
            )],
        }


def save_baseline(path: Path, baseline: Baseline) -> None:
    """Write baseline to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline.as_dict(), indent=2) + "\n")


def load_baseline(path: Path) -> Baseline:
    """Load baseline from JSON file."""
    data = json.loads(path.read_text())
    findings = set()
    for f in data.get("findings", []):
        findings.add(FindingFingerprint.from_dict(f))

    return Baseline(
        schema_version=data.get("schema_version", BASELINE_SCHEMA_VERSION),
        created_utc=data.get("created_utc", ""),
        enforcement_date=data.get("enforcement_date", ""),
        ratchet_mode=data.get("ratchet_mode", "block_new"),
        repo_sha=data.get("repo_sha", ""),
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_findings(
    current: set[FindingFingerprint],
    baseline: Baseline,
    waived_fingerprints: set[FindingFingerprint] | None = None,
    today: date | None = None,
) -> RatchetResult:
    """Compare current findings against baseline.

    See :class:`Baseline` for the full semantic contract.  Short summary:

    * Both modes always fail on new non-waived findings.
    * ``ratchet_down`` additionally fails on total-count increase.
    * ``enforcement_date``, when set and in the past, makes BOTH modes
      additionally fail on remaining legacy (pre-existing) findings —
      this is the deadline-to-clear-debt feature.  Before the date,
      legacy debt is allowed in both modes.

    I.3: ``block_new`` is NOT a synonym for ``ratchet_down`` after the
    enforcement date passes — the two modes have distinct
    pre-deadline behaviour.  The deadline is what turns legacy debt
    into a blocking surface, not the mode name.

    Args:
        current: fingerprints from current scan
        baseline: loaded baseline snapshot
        waived_fingerprints: findings that have active waivers (excluded from blocking)
        today: override for testing
    """
    if today is None:
        today = date.today()
    if waived_fingerprints is None:
        waived_fingerprints = set()

    baseline_fps = baseline.findings
    mode = baseline.ratchet_mode

    new_findings = current - baseline_fps
    resolved_findings = baseline_fps - current
    legacy_findings = current & baseline_fps

    # Remove waived findings from new (they don't block)
    blocking_new = new_findings - waived_fingerprints

    result = RatchetResult(
        new_findings=sorted(new_findings, key=lambda f: (f.rule_id, f.path)),
        legacy_findings=sorted(legacy_findings, key=lambda f: (f.rule_id, f.path)),
        resolved_findings=sorted(resolved_findings, key=lambda f: (f.rule_id, f.path)),
    )

    # Check enforcement date
    enforcement_date = None
    if baseline.enforcement_date:
        try:
            enforcement_date = date.fromisoformat(baseline.enforcement_date)
        except ValueError:
            pass

    past_enforcement = enforcement_date is not None and today >= enforcement_date

    def _deadline_reason(non_waived_legacy: set) -> str:
        """I.3: compose a FAIL reason that frames the block as the
        deadline the operator configured, not as a surprise change of
        mode semantics.  Includes days-since-deadline so the evidence
        is self-explanatory."""
        days_since = (today - enforcement_date).days if enforcement_date else 0
        return (
            f"Enforcement date {baseline.enforcement_date} passed "
            f"{days_since} day(s) ago — "
            f"{len(non_waived_legacy)} legacy finding(s) still unresolved "
            f"and now blocking per the deadline recorded in this baseline "
            f"(ratchet_mode={mode!r})."
        )

    if mode == "block_new":
        if blocking_new:
            result.disposition = "FAIL"
            result.reasons.append(
                f"{len(blocking_new)} new non-waived finding(s) introduced"
            )
        elif past_enforcement:
            # I.3: after the deadline the operator set, legacy findings
            # also start blocking — this is the baseline's deadline
            # feature, not a silent switch to ratchet_down semantics.
            non_waived_legacy = set(legacy_findings) - waived_fingerprints
            if non_waived_legacy:
                result.disposition = "FAIL"
                result.reasons.append(_deadline_reason(non_waived_legacy))
            else:
                result.disposition = "PASS"
        else:
            result.disposition = "PASS"

    elif mode == "ratchet_down":
        if blocking_new:
            result.disposition = "FAIL"
            result.reasons.append(
                f"{len(blocking_new)} new non-waived finding(s) introduced"
            )
        elif len(current - waived_fingerprints) > len(baseline_fps - waived_fingerprints):
            result.disposition = "FAIL"
            result.reasons.append(
                "Total non-waived findings increased vs baseline"
            )
        elif past_enforcement:
            non_waived_legacy = set(legacy_findings) - waived_fingerprints
            if non_waived_legacy:
                result.disposition = "FAIL"
                result.reasons.append(_deadline_reason(non_waived_legacy))
            else:
                result.disposition = "PASS"
        else:
            result.disposition = "PASS"

    return result


# ---------------------------------------------------------------------------
# Baseline generation helpers
# ---------------------------------------------------------------------------

def generate_baseline(
    fingerprints: set[FindingFingerprint],
    repo_sha: str = "",
    enforcement_date: str = "",
    ratchet_mode: str = "block_new",
) -> Baseline:
    """Create a new baseline from current scan fingerprints."""
    return Baseline(
        schema_version=BASELINE_SCHEMA_VERSION,
        created_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        enforcement_date=enforcement_date,
        ratchet_mode=ratchet_mode,
        repo_sha=repo_sha,
        findings=fingerprints,
    )
