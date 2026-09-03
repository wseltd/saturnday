"""Waiver and inline suppression system for security governance.

Supports:
- Inline suppressions: # saturnday:ignore SEC-001 reason="..." expires=YYYY-MM-DD owner=name
- Policy-level waivers in .saturnday-policy.yml

Rules:
- Every waiver has an expiry (max 6 months), owner, and reason
- Expired waivers revert to enforcement
- No wildcard waivers (path: "**" rejected)
- Inline suppressions on HARD findings require policy-level approved_by
- Repeated renewals capped at 3
- Suppressions do not inherit to copied/generated files
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .policy_manifest import HARD_CHECKS, TS_HARD_CHECKS

# Max waiver duration: 6 months (183 days)
MAX_WAIVER_DAYS = 183
# Max renewals before the waiver must be resolved
MAX_RENEWALS = 3

_INLINE_PATTERN = re.compile(
    r"#\s*saturnday:ignore\s+"
    r"(?P<rule_id>SEC-[\w-]+)"
    r"(?:\s+reason=['\"](?P<reason>[^'\"]+)['\"])?"
    r"(?:\s+expires=(?P<expires>\d{4}-\d{2}-\d{2}))?"
    r"(?:\s+owner=(?P<owner>\S+))?"
)


@dataclass
class Waiver:
    rule_id: str
    path: str
    reason: str
    owner: str
    expires: date
    approved_by: str | None = None
    renewal_count: int = 0


@dataclass
class InlineSuppression:
    rule_id: str
    file: str
    line: int
    reason: str | None = None
    expires: date | None = None
    owner: str | None = None


@dataclass
class WaiverResult:
    """Result of checking a finding against waivers."""
    suppressed: bool = False
    reason: str | None = None
    errors: list[str] = field(default_factory=list)


def parse_inline_suppressions(file_path: str, content: str) -> list[InlineSuppression]:
    """Parse inline saturnday:ignore comments from file content."""
    suppressions = []
    for lineno, line in enumerate(content.splitlines(), 1):
        m = _INLINE_PATTERN.search(line)
        if m:
            expires = None
            if m.group("expires"):
                try:
                    expires = datetime.strptime(m.group("expires"), "%Y-%m-%d").date()
                except ValueError:
                    pass
            suppressions.append(InlineSuppression(
                rule_id=m.group("rule_id"),
                file=file_path,
                line=lineno,
                reason=m.group("reason"),
                expires=expires,
                owner=m.group("owner"),
            ))
    return suppressions


def load_waivers_from_policy(policy_raw: dict) -> list[Waiver]:
    """Load waivers from parsed policy YAML."""
    waivers = []
    for entry in policy_raw.get("waivers", []):
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", ""))
        # Reject wildcard waivers
        if path in ("**", "*", ""):
            continue
        try:
            expires = datetime.strptime(str(entry.get("expires", "")), "%Y-%m-%d").date()
        except ValueError:
            continue  # Invalid or missing expiry — skip
        waivers.append(Waiver(
            rule_id=str(entry.get("rule_id", "")),
            path=path,
            reason=str(entry.get("reason", "")),
            owner=str(entry.get("owner", "")),
            expires=expires,
            approved_by=entry.get("approved_by"),
            renewal_count=int(entry.get("renewal_count", 0)),
        ))
    return waivers


def _is_hard_check(rule_id: str) -> bool:
    """Check if a rule ID maps to a HARD check."""
    from .policy_manifest import RULE_IDS
    for check_name, rid in RULE_IDS.items():
        if rid == rule_id:
            return check_name in HARD_CHECKS or check_name in TS_HARD_CHECKS
    return False


def check_waiver(
    rule_id: str,
    file_path: str,
    line: int,
    waivers: list[Waiver],
    inline_suppressions: list[InlineSuppression],
    today: date | None = None,
) -> WaiverResult:
    """Check if a finding is suppressed by a waiver or inline suppression.

    Returns WaiverResult with suppressed=True if the finding should be skipped,
    plus any validation errors.
    """
    if today is None:
        today = date.today()

    errors = []
    is_hard = _is_hard_check(rule_id)

    # Check inline suppressions
    for sup in inline_suppressions:
        if sup.rule_id != rule_id or sup.file != file_path:
            continue
        # Check expiry
        if sup.expires and sup.expires < today:
            errors.append(f"Inline suppression for {rule_id} in {file_path}:{sup.line} expired on {sup.expires}")
            continue
        # HARD findings need policy-level approved_by
        if is_hard:
            # Check if there's a matching policy waiver with approved_by
            has_approval = any(
                w.rule_id == rule_id and w.approved_by and w.expires >= today
                for w in waivers
                if _path_matches(file_path, w.path)
            )
            if not has_approval:
                errors.append(
                    f"Inline suppression for HARD rule {rule_id} in {file_path}:{sup.line} "
                    f"requires a policy-level waiver with approved_by"
                )
                continue
        return WaiverResult(suppressed=True, reason=sup.reason, errors=errors)

    # Check policy waivers
    for w in waivers:
        if w.rule_id != rule_id:
            continue
        if not _path_matches(file_path, w.path):
            continue
        # Check expiry
        if w.expires < today:
            errors.append(f"Waiver for {rule_id} on {w.path} expired on {w.expires}")
            continue
        # Check renewal cap
        if w.renewal_count > MAX_RENEWALS:
            errors.append(
                f"Waiver for {rule_id} on {w.path} exceeded max renewals "
                f"({w.renewal_count}/{MAX_RENEWALS}). Finding must be resolved."
            )
            continue
        # Check required fields
        if not w.owner:
            errors.append(f"Waiver for {rule_id} on {w.path} missing owner")
            continue
        if not w.reason:
            errors.append(f"Waiver for {rule_id} on {w.path} missing reason")
            continue
        return WaiverResult(suppressed=True, reason=w.reason, errors=errors)

    return WaiverResult(suppressed=False, errors=errors)


def validate_waivers(waivers: list[Waiver], today: date | None = None) -> list[str]:
    """Validate all waivers and return errors."""
    if today is None:
        today = date.today()
    errors = []
    for w in waivers:
        if w.path in ("**", "*", ""):
            errors.append(f"Wildcard waiver rejected: rule={w.rule_id} path={w.path!r}")
        if not w.owner:
            errors.append(f"Waiver missing owner: rule={w.rule_id} path={w.path}")
        if not w.reason:
            errors.append(f"Waiver missing reason: rule={w.rule_id} path={w.path}")
        if w.expires < today:
            errors.append(f"Expired waiver: rule={w.rule_id} path={w.path} expired={w.expires}")
        days_until_expiry = (w.expires - today).days
        if days_until_expiry > MAX_WAIVER_DAYS:
            errors.append(
                f"Waiver expiry too far: rule={w.rule_id} path={w.path} "
                f"expires in {days_until_expiry} days (max {MAX_WAIVER_DAYS})"
            )
        if w.renewal_count > MAX_RENEWALS:
            errors.append(
                f"Waiver exceeded max renewals: rule={w.rule_id} path={w.path} "
                f"renewals={w.renewal_count} (max {MAX_RENEWALS})"
            )
        if _is_hard_check(w.rule_id) and not w.approved_by:
            errors.append(
                f"HARD rule waiver missing approved_by: rule={w.rule_id} path={w.path}"
            )
    return errors


def _path_matches(file_path: str, waiver_path: str) -> bool:
    """Check if a file path matches a waiver path pattern."""
    from fnmatch import fnmatch
    return fnmatch(file_path, waiver_path) or file_path == waiver_path
