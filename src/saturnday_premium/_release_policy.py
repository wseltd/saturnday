"""Org-level release policy evaluation for premium release governance.

RS-016: Org policy packs — load ~/.saturnday/release-policy.yaml and evaluate
against existing check results, producing additional policy-specific results.
"""
from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Any

__all__ = ["load_release_policy", "evaluate_release_policy"]

_logger = logging.getLogger(__name__)

_DEFAULT_POLICY_PATH = Path.home() / ".saturnday" / "release-policy.yaml"


def load_release_policy(policy_path: Path | None = None) -> dict:
    """Load release policy from ~/.saturnday/release-policy.yaml or given path.

    Returns an empty dict if no policy file is found (no-op, recorded in
    evidence by the caller).  Never raises on missing file — absence is a
    valid, non-error state meaning "no org policy configured".

    Args:
        policy_path: Optional explicit path to a policy YAML file.  If
            ``None``, the default location
            ``~/.saturnday/release-policy.yaml`` is tried.

    Returns:
        Parsed policy dict, or ``{}`` if no file is found or the file is
        empty.

    Raises:
        ValueError: If the file exists but cannot be parsed as valid YAML.
    """
    try:
        import yaml  # PyYAML — optional dep
    except ImportError:  # pragma: no cover
        _logger.warning("PyYAML not available; org release policy will not be loaded")
        return {}

    path = policy_path if policy_path is not None else _DEFAULT_POLICY_PATH

    if not Path(path).is_file():
        _logger.debug("No release policy file at %s — continuing without org policy", path)
        return {}

    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning("Cannot read release policy at %s: %s", path, exc)
        return {}

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in release policy {path}: {exc}") from exc

    if not isinstance(data, dict):
        _logger.warning("Release policy at %s did not parse to a dict — ignoring", path)
        return {}

    _logger.debug("Loaded release policy from %s: %s", path, list(data.keys()))
    return data


def evaluate_release_policy(check_results: list, policy: dict) -> list:
    """Evaluate org policy against existing check results.

    Appends new policy check results to the list but never removes or
    modifies existing entries.  Each additional result is a plain dict
    compatible with the public ``ReleaseCheckResult`` dataclass fields
    (``check_id``, ``status``, ``message``, ``detail``).

    Policy fields evaluated:

    - ``max_artefact_size_mb`` (int): FAIL if any artefact exceeds this size.
      Size is sourced from an existing check result with
      ``check_id == "RS-INV-artefact-size"``.
    - ``required_checks`` (list[str]): FAIL if any named check ID is SKIPPED
      or absent from check_results.
    - ``blocked_file_patterns`` (list[str]): FAIL if any path in any existing
      check result's ``detail.inventory`` matches a blocked glob.
    - ``require_signoff`` (bool): WARN if ``True`` and no existing result has
      ``check_id == "RS-SIGN-signoff-met"`` with status ``PASS``.
    - ``minimum_approvers`` (int): WARN if fewer than this many approvals are
      recorded (sourced from existing result with
      ``check_id == "RS-SIGN-approver-count"``).

    Args:
        check_results: Existing check results from the public pipeline.
        policy: Org policy dict (may be empty — returns input unchanged).

    Returns:
        New list containing all input results plus any policy-check results.
    """
    if not policy:
        return list(check_results)

    extra: list[dict] = []

    def _status(r: Any) -> str:
        if isinstance(r, dict):
            return r.get("status", "UNKNOWN")
        return str(getattr(r, "status", "UNKNOWN"))

    def _check_id(r: Any) -> str:
        if isinstance(r, dict):
            return r.get("check_id", "")
        return str(getattr(r, "check_id", ""))

    def _detail(r: Any) -> Any:
        if isinstance(r, dict):
            return r.get("detail", {})
        return getattr(r, "detail", {})

    # --- max_artefact_size_mb ---
    max_mb = policy.get("max_artefact_size_mb")
    if max_mb is not None:
        try:
            max_mb = int(max_mb)
        except (TypeError, ValueError):
            _logger.warning("max_artefact_size_mb is not an int: %r — skipping", max_mb)
            max_mb = None

    if max_mb is not None:
        size_results = [r for r in check_results if _check_id(r) == "RS-INV-artefact-size"]
        if size_results:
            for sr in size_results:
                detail = _detail(sr)
                size_mb = detail.get("size_mb") if isinstance(detail, dict) else None
                if size_mb is not None:
                    try:
                        size_mb = float(size_mb)
                    except (TypeError, ValueError):
                        size_mb = None
                if size_mb is not None and size_mb > max_mb:
                    extra.append({
                        "check_id": "RS-POL-max-artefact-size",
                        "status": "FAIL",
                        "message": (
                            f"Artefact size {size_mb:.2f} MB exceeds org policy "
                            f"max_artefact_size_mb={max_mb}"
                        ),
                        "detail": {"size_mb": size_mb, "limit_mb": max_mb},
                    })
                else:
                    extra.append({
                        "check_id": "RS-POL-max-artefact-size",
                        "status": "PASS",
                        "message": f"Artefact size within org policy limit of {max_mb} MB",
                        "detail": {"size_mb": size_mb, "limit_mb": max_mb},
                    })

    # --- required_checks ---
    required_checks: list[str] = policy.get("required_checks", [])
    for req_id in required_checks:
        matched = [r for r in check_results if _check_id(r) == req_id]
        if not matched:
            extra.append({
                "check_id": "RS-POL-required-check",
                "status": "FAIL",
                "message": f"Required check '{req_id}' is absent from check results",
                "detail": {"required_check_id": req_id},
            })
        else:
            skipped = [r for r in matched if _status(r) == "SKIPPED"]
            if skipped:
                extra.append({
                    "check_id": "RS-POL-required-check",
                    "status": "FAIL",
                    "message": f"Required check '{req_id}' was SKIPPED — must not be skipped",
                    "detail": {"required_check_id": req_id},
                })
            else:
                extra.append({
                    "check_id": "RS-POL-required-check",
                    "status": "PASS",
                    "message": f"Required check '{req_id}' is present and not SKIPPED",
                    "detail": {"required_check_id": req_id},
                })

    # --- blocked_file_patterns ---
    blocked_patterns: list[str] = policy.get("blocked_file_patterns", [])
    if blocked_patterns:
        inventory: list[str] = []
        for r in check_results:
            detail = _detail(r)
            if isinstance(detail, dict):
                inv = detail.get("inventory", [])
                if isinstance(inv, list):
                    inventory.extend(str(p) for p in inv)

        for pattern in blocked_patterns:
            matched_paths = [p for p in inventory if fnmatch.fnmatch(p, pattern)]
            if matched_paths:
                extra.append({
                    "check_id": "RS-POL-blocked-file",
                    "status": "FAIL",
                    "message": (
                        f"Blocked file pattern '{pattern}' matched "
                        f"{len(matched_paths)} path(s) in artefact inventory"
                    ),
                    "detail": {"pattern": pattern, "matched": matched_paths},
                })
            else:
                extra.append({
                    "check_id": "RS-POL-blocked-file",
                    "status": "PASS",
                    "message": f"Blocked file pattern '{pattern}' matched no paths",
                    "detail": {"pattern": pattern, "matched": []},
                })

    # --- require_signoff ---
    require_signoff = policy.get("require_signoff", False)
    if require_signoff:
        signoff_results = [r for r in check_results if _check_id(r) == "RS-SIGN-signoff-met"]
        signoff_passed = any(_status(r) == "PASS" for r in signoff_results)
        if not signoff_passed:
            extra.append({
                "check_id": "RS-POL-require-signoff",
                "status": "WARN",
                "message": "Org policy requires release signoff but no passing signoff check found",
                "detail": {"require_signoff": True},
            })
        else:
            extra.append({
                "check_id": "RS-POL-require-signoff",
                "status": "PASS",
                "message": "Release signoff requirement satisfied",
                "detail": {"require_signoff": True},
            })

    # --- minimum_approvers ---
    minimum_approvers = policy.get("minimum_approvers")
    if minimum_approvers is not None:
        try:
            minimum_approvers = int(minimum_approvers)
        except (TypeError, ValueError):
            _logger.warning("minimum_approvers is not an int: %r — skipping", minimum_approvers)
            minimum_approvers = None

    if minimum_approvers is not None:
        count_results = [r for r in check_results if _check_id(r) == "RS-SIGN-approver-count"]
        approver_count: int | None = None
        for cr in count_results:
            d = _detail(cr)
            if isinstance(d, dict) and "approver_count" in d:
                try:
                    approver_count = int(d["approver_count"])
                except (TypeError, ValueError):
                    pass
        if approver_count is None or approver_count < minimum_approvers:
            actual = approver_count if approver_count is not None else 0
            extra.append({
                "check_id": "RS-POL-minimum-approvers",
                "status": "WARN",
                "message": (
                    f"Org policy requires {minimum_approvers} approver(s) "
                    f"but only {actual} found"
                ),
                "detail": {
                    "minimum_approvers": minimum_approvers,
                    "actual_approvers": actual,
                },
            })
        else:
            extra.append({
                "check_id": "RS-POL-minimum-approvers",
                "status": "PASS",
                "message": (
                    f"Approver count {approver_count} meets org minimum of {minimum_approvers}"
                ),
                "detail": {
                    "minimum_approvers": minimum_approvers,
                    "actual_approvers": approver_count,
                },
            })

    _logger.debug(
        "evaluate_release_policy: %d existing results, %d policy results added",
        len(check_results),
        len(extra),
    )
    return list(check_results) + extra
