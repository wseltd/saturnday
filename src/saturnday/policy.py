import json
from pathlib import Path

from .scope_rules import matches_path_patterns


def _normalize_path(path: str) -> str:
    if path is None:
        return ""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalize_changes(changes: dict) -> dict:
    files = changes.get("files_changed") or []
    normalized_files = []
    seen = set()
    for path in files:
        normalized = _normalize_path(path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_files.append(normalized)
    normalized_files.sort()

    diff_lines_by_file = changes.get("diff_lines_by_file") or {}
    normalized_diff = {}
    for path, count in diff_lines_by_file.items():
        normalized = _normalize_path(path)
        if not normalized:
            continue
        normalized_diff[normalized] = int(count)
    normalized_diff = {key: normalized_diff[key] for key in sorted(normalized_diff)}

    return {
        "files_changed": normalized_files,
        "diff_total_lines": int(changes.get("diff_total_lines", 0)),
        "diff_lines_by_file": normalized_diff,
    }


def build_policy_input(
    ctx,
    *,
    changes: dict,
    knowledge: dict | None = None,
    cloud_preflight: dict | None = None,
    shell_summary: dict | None = None,
) -> dict:
    status_path = Path(ctx.root_dir) / "verification" / "status.json"
    status = {}
    if status_path.exists():
        status = json.loads(status_path.read_text())

    normalized_changes = _normalize_changes(changes)
    verification_status = status.get("status", "SKIPPED")
    exit_code = status.get("exit_code")
    verify_cmd = status.get("verify_cmd", ctx.verify_cmd)
    repo_real = status.get("repo_real", str(ctx.repo_path))

    policy_input = {
        "version": 1,
        "run": {
            "run_id": ctx.run_id,
            "created_utc": ctx.created_utc,
            "repo_path": repo_real,
            "evidence_dir": str(ctx.root_dir),
        },
        "scope": {
            "allowed_globs": [],
            "forbidden_globs": [],
            "budgets": {
                "max_files_changed": None,
                "max_total_diff_lines": None,
                "max_per_file_diff_lines": None,
            },
        },
        "changes": normalized_changes,
        "verification": {
            "status": verification_status,
            "exit_code": exit_code,
            "all_green": verification_status == "RAN" and exit_code == 0,
            "verify_cmd": verify_cmd,
        },
        "waivers": [],
    }
    if knowledge is not None:
        policy_input["knowledge"] = knowledge
    if cloud_preflight is not None:
        policy_input["cloud_preflight"] = cloud_preflight
    if shell_summary is not None:
        policy_input["shell_sandbox"] = shell_summary
    return policy_input


def _matches_any(path: str, patterns: list[str]) -> bool:
    return matches_path_patterns(path, patterns)


def evaluate_policy(policy_input: dict) -> dict:
    scope = policy_input.get("scope", {})
    changes = policy_input.get("changes", {})
    verification = policy_input.get("verification", {})
    waivers = policy_input.get("waivers", [])
    knowledge = policy_input.get("knowledge", {})
    cloud_preflight = policy_input.get("cloud_preflight", {})
    shell_summary = policy_input.get("shell_sandbox", {})

    raw_files = changes.get("files_changed") or []
    files = sorted({p for p in (_normalize_path(path) for path in raw_files) if p})
    diff_total_lines = int(changes.get("diff_total_lines", 0))
    raw_diff_lines = changes.get("diff_lines_by_file") or {}
    diff_lines_by_file = {}
    for path, count in raw_diff_lines.items():
        normalized = _normalize_path(path)
        if not normalized:
            continue
        diff_lines_by_file[normalized] = int(count)

    allowed_globs = scope.get("allowed_globs") or []
    forbidden_globs = scope.get("forbidden_globs") or []
    budgets = scope.get("budgets") or {}

    reasons: list[str] = []

    status = verification.get("status", "SKIPPED")
    exit_code = verification.get("exit_code")
    if status == "SKIPPED":
        reasons.append("verification_skipped")
    elif status == "RAN" and exit_code != 0:
        reasons.append("verification_failed")

    if isinstance(knowledge, dict):
        if knowledge.get("status") == "ERROR":
            reasons.append("knowledge_pack_invalid")
        trust = knowledge.get("trust")
        if isinstance(trust, dict) and trust.get("allow") is False:
            reasons.append("knowledge_trust_denied")

    if isinstance(cloud_preflight, dict):
        if cloud_preflight.get("status") == "FAIL":
            reasons.append("cloud_preflight_failed")

    if isinstance(shell_summary, dict):
        if shell_summary.get("enforced") is True and shell_summary.get("status") == "FAIL":
            reasons.append("shell_sandbox_failed")

    for path in files:
        normalized = _normalize_path(path)
        if forbidden_globs and _matches_any(normalized, forbidden_globs):
            reasons.append(f"forbidden_path:{normalized}")
        if allowed_globs and not _matches_any(normalized, allowed_globs):
            reasons.append(f"outside_allowlist:{normalized}")

    max_files_changed = budgets.get("max_files_changed")
    max_total_diff_lines = budgets.get("max_total_diff_lines")
    max_per_file_diff_lines = budgets.get("max_per_file_diff_lines")

    if max_files_changed is not None and len(files) > max_files_changed:
        reasons.append("max_files_changed_exceeded")
    if max_total_diff_lines is not None and diff_total_lines > max_total_diff_lines:
        reasons.append("max_total_diff_lines_exceeded")
    if max_per_file_diff_lines is not None:
        for path in files:
            if diff_lines_by_file.get(path, 0) > max_per_file_diff_lines:
                reasons.append(f"max_per_file_diff_lines_exceeded:{path}")

    waivers_used = set()
    if waivers and reasons:
        remaining = []
        for reason in reasons:
            waived = False
            for waiver in waivers:
                waiver_id = waiver.get("id")
                if not waiver_id:
                    continue
                if reason == waiver_id or reason.startswith(f"{waiver_id}:"):
                    waivers_used.add(waiver_id)
                    waived = True
                    break
            if not waived:
                remaining.append(reason)
        reasons = remaining

    allow = not reasons
    return {
        "version": 1,
        "allow": allow,
        "deny_reasons": [] if allow else reasons,
        "waivers_used": sorted(waivers_used),
        "stats": {
            "files_changed": len(files),
            "diff_total_lines": diff_total_lines,
        },
    }
