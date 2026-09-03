"""Shared policy loading, validation, and exemption matching.

Fix 53.e: consolidates the repeated ad-hoc policy parsing that was
scattered across governance.py, cli.py, ticket_runner.py, and
interactive.py into one canonical implementation.

Supported policy schema
-----------------------

``expected_findings``
    Flat list of finding-kind strings.  Each entry is a plain string
    like ``"dependency_declaration"`` or ``"blast_radius"``.  Dict entries
    are invalid and will be filtered with a warning.

    Effect: when ALL error-severity findings in a governance run match
    an ``expected_findings`` entry, the disposition is downgraded from
    FAIL to WARN.

``exemptions``
    List of dicts with the following fields:

    * ``check`` (required): finding kind / check name to match.
    * ``pattern`` (optional): file-path glob matched via ``fnmatch``.
      ``"*"`` matches all files.  If omitted (or empty), the exemption
      is treated as global for that check.
    * ``path`` (optional): alias for ``pattern`` (backward compatible).
      If both ``pattern`` and ``path`` are present, ``pattern`` wins.
    * ``reason`` (recommended): human-readable explanation.

    Matching is file-path-based only.  Source-text pattern matching
    (e.g. ``pattern: "from rdkit"``) is **not** supported.

``waivers``
    Structured entries handled by ``saturnday.waivers``.  Not covered
    by this module.

``severity_overrides``
    Dict mapping check names to severity strings.  Handled by
    ``saturnday.policy_manifest``.  Not covered by this module.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_policy_file(file_path: Path) -> dict[str, Any]:
    """Load and return parsed policy from an exact file path.

    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    if not file_path.is_file():
        return {}
    try:
        import yaml
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except ImportError:
        logger.warning("pyyaml not installed — cannot load %s", file_path)
        return {}
    except Exception as exc:
        logger.error("Failed to parse %s: %s", file_path, exc)
        return {}


def load_policy(repo_path: Path) -> dict[str, Any]:
    """Load and return parsed policy from ``.saturnday-policy.yaml``.

    Returns an empty dict if the file does not exist or cannot be parsed.
    Logs a clear error on parse failure instead of silently swallowing.
    """
    policy_path = repo_path / ".saturnday-policy.yaml"
    if not policy_path.is_file():
        return {}
    try:
        import yaml
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except ImportError:
        logger.warning("pyyaml not installed — cannot load .saturnday-policy.yaml")
        return {}
    except Exception as exc:
        logger.error(
            "Failed to parse .saturnday-policy.yaml: %s. "
            "Policy will not be applied. Fix the file and retry.",
            exc,
        )
        return {}


def validated_expected_findings(policy: dict[str, Any]) -> set[str]:
    """Extract and validate ``expected_findings`` as a set of strings.

    Filters out non-string entries with a warning instead of crashing.

    Fix I.1: when an entry is a ``bool``, the most likely cause is that
    the YAML source used an unquoted token like ``off``, ``on``, ``yes``,
    ``no``, ``true``, or ``false`` — YAML 1.1 coerces those to booleans.
    This silently dropped the override with a generic warning before;
    we now emit an explicit, operator-usable message naming the value and
    recommending quoting.
    """
    raw = policy.get("expected_findings", [])
    if not raw:
        return set()
    if not isinstance(raw, list):
        logger.error("expected_findings must be a list, got %s", type(raw).__name__)
        return set()

    # Fix I.1: identify YAML-boolean coercion first so the warning names
    # the cause precisely.  ``True``/``False`` in policy data typically
    # come from unquoted YAML values such as ``on``/``off``/``yes``/``no``.
    yaml_bools = [e for e in raw if isinstance(e, bool)]
    if yaml_bools:
        logger.warning(
            "expected_findings contains %d YAML-boolean value(s) (%s). "
            "These were most likely written unquoted (e.g. ``off``, ``on``, "
            "``yes``, ``no``, ``true``, ``false``) — YAML 1.1 silently "
            "coerces those tokens to booleans, dropping the override. "
            "Quote the values in your policy file, e.g. ``- \"off\"``.",
            len(yaml_bools),
            ", ".join(repr(b) for b in yaml_bools[:5]),
        )
        try:
            from saturnday.ticket_runner import _log_gap
            _log_gap("POLICY", "yaml_boolean_coercion",
                     "expected_findings had YAML-coerced boolean entries — quote the values",
                     count=len(yaml_bools))
        except Exception:
            pass

    # Remaining non-string, non-boolean entries still need the generic warning.
    bad = [e for e in raw if not isinstance(e, str) and not isinstance(e, bool)]
    if bad:
        logger.warning(
            "expected_findings contains %d non-string entry(ies) — filtering them out. "
            "Each entry must be a plain string (finding kind), not a dict. "
            "First bad entry: %s",
            len(bad), str(bad[0])[:120],
        )
        try:
            from saturnday.ticket_runner import _log_gap
            _log_gap("POLICY", "malformed_expected_findings",
                     "expected_findings had non-string entries filtered",
                     count=len(bad))
        except Exception:
            pass
    return {e for e in raw if isinstance(e, str)}


def _resolve_pattern(entry: dict) -> str:
    """Resolve the effective file-path pattern from an exemption entry.

    Priority: ``pattern`` > ``path`` > global (``"*"``).
    """
    return entry.get("pattern", "") or entry.get("path", "") or "*"


def is_finding_exempt(
    check_name: str,
    file_path: str,
    exemptions: list[dict],
) -> bool:
    """Return True if a finding is suppressed by any exemption entry.

    Args:
        check_name: The check/finding kind (e.g. ``"dependency_declaration"``).
        file_path: The finding's file path (e.g. ``"src/foo.py"``).
        exemptions: List of exemption dicts from the policy.
    """
    for ex in exemptions:
        if not isinstance(ex, dict):
            continue
        if ex.get("check", "") != check_name:
            continue
        pattern = _resolve_pattern(ex)
        if pattern == "*" or pattern == "" or fnmatch(file_path, pattern):
            return True
    return False


def filter_findings_by_exemptions(
    check_name: str,
    findings: list[dict],
    exemptions: list[dict],
) -> list[dict]:
    """Return findings NOT matched by any exemption."""
    if not exemptions:
        return findings
    return [
        f for f in findings
        if not is_finding_exempt(check_name, f.get("file", ""), exemptions)
    ]


def validated_exemptions(policy: dict[str, Any]) -> list[dict]:
    """Extract and validate ``exemptions`` list from policy.

    Behaviour change (product-contract fix):

    * ``exemptions`` missing or ``[]`` → returns ``[]`` silently.  No
      error, no warnings.  This matches the pre-existing no-op path.
    * ``exemptions`` not a list → returns ``[]`` and logs an error.
      Pre-existing behaviour pinned for backward compatibility.
    * Every entry is a dict with a ``check`` key → returns the list
      unchanged.  Valid current-schema policies are unaffected.
    * Any entry is not a dict, OR any dict entry is missing ``check``
      → **raises** :class:`~saturnday._exceptions.PolicySchemaError`.
      Callers are expected to let the refusal surface, exit non-zero,
      and NOT run governance with a partial exemption set.

    The refusal message names every offending entry's index, its
    present keys, and the accepted current schema, so the operator
    can fix the file without hunting through code.
    """
    from saturnday._exceptions import PolicySchemaError

    raw = policy.get("exemptions", [])
    if not raw:
        return []
    if not isinstance(raw, list):
        logger.error("exemptions must be a list, got %s", type(raw).__name__)
        return []

    invalid: list[dict[str, Any]] = []
    valid: list[dict] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            invalid.append({
                "index": i,
                "present_keys": (),
                "reason": f"entry is not a dict (got {type(entry).__name__})",
            })
            continue
        if "check" not in entry:
            invalid.append({
                "index": i,
                "present_keys": tuple(sorted(entry.keys())),
                "reason": "missing required field 'check'",
            })
            continue
        valid.append(entry)

    if invalid:
        # Structured evidence — one _log_gap per offender — so the run
        # report captures the fact that policy refused, not just that
        # governance didn't execute.
        try:
            from saturnday.ticket_runner import _log_gap
            for inv in invalid:
                _log_gap(
                    "POLICY", "invalid_exemption_entry",
                    "exemption entry rejected — refused to run governance",
                    index=inv["index"],
                    present_keys=list(inv["present_keys"]),
                    reason=inv["reason"],
                )
        except Exception:
            pass

        message = _format_exemption_refusal_message(invalid)
        raise PolicySchemaError(
            message, invalid_entries=tuple(invalid),
        )

    return valid


def _format_exemption_refusal_message(invalid: list[dict[str, Any]]) -> str:
    """Build the operator-facing refusal text for
    :func:`validated_exemptions` when any entry is schema-invalid.

    The message is deliberately specific: lists every offending entry's
    index and present keys, states the required field, shows the
    accepted schema, and names the operator action.  Callers print this
    to stderr on refusal.
    """
    lines: list[str] = []
    lines.append(
        "Policy exemption schema error — .saturnday-policy.yaml contains "
        f"{len(invalid)} entry(ies) that do not match the current schema."
    )
    lines.append("")
    lines.append("Saturnday refuses to run governance with a partially-filtered")
    lines.append("exemption list.  Running under these conditions would produce")
    lines.append("a misleading FAIL disposition.  Fix the policy file and retry.")
    lines.append("")
    lines.append("Offending entries:")
    for inv in invalid:
        present = (
            ", ".join(inv["present_keys"])
            if inv["present_keys"] else "(no keys)"
        )
        lines.append(
            f"  exemptions[{inv['index']}]: {inv['reason']}; "
            f"keys present: [{present}]"
        )
    lines.append("")
    lines.append("Accepted exemption shape (current schema):")
    lines.append("  exemptions:")
    lines.append("    - check: \"<check_name>\"    # REQUIRED")
    lines.append("      pattern: \"<glob>\"         # optional file-path glob")
    lines.append("      path: \"<glob>\"            # optional alias for pattern")
    lines.append("      reason: \"<text>\"          # optional human-readable reason")
    lines.append("")
    lines.append(
        "Action: rename the offending entry's field to 'check' and re-run."
    )
    return "\n".join(lines)
