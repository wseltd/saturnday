#!/usr/bin/env python3
"""False positive audit — run security checks against mature repos.

Usage:
    python scripts/false_positive_audit.py [--repos flask,fastapi,django]

Clones repos to temp dirs, runs all security checks, reports findings per check.
"""

import json
import subprocess
import sys
import tempfile
import shutil
from collections import defaultdict
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from saturnday.review import (
    _check_hardcoded_jwt,
    _check_weak_randomness,
    _check_jwt_verification_policy,
    _check_cookie_security_hard,
    _check_cookie_security_soft,
    _check_token_revocation,
    _check_token_expiry,
    _check_csrf_state_change,
    _check_oauth_flow_integrity,
    _check_auth_bypass,
    _check_websocket_auth,
    _check_rate_limit_wiring,
    _check_rate_limit_backend_quality,
    _check_xss,
    _check_sql_injection,
    _check_user_enumeration,
    _check_client_trusted_logic,
    _check_idor,
    _check_security_event_logging,
)

REPOS = {
    "flask": "https://github.com/pallets/flask.git",
    "fastapi": "https://github.com/fastapi/fastapi.git",
    "django": "https://github.com/django/django.git",
}

CHECKS = [
    ("hardcoded_jwt", _check_hardcoded_jwt),
    ("weak_randomness", _check_weak_randomness),
    ("jwt_verification_policy", _check_jwt_verification_policy),
    ("cookie_security_hard", _check_cookie_security_hard),
    ("cookie_security_soft", _check_cookie_security_soft),
    ("token_revocation", _check_token_revocation),
    ("token_expiry", _check_token_expiry),
    ("csrf_state_change", _check_csrf_state_change),
    ("oauth_flow_integrity", _check_oauth_flow_integrity),
    ("auth_bypass", _check_auth_bypass),
    ("websocket_auth", _check_websocket_auth),
    ("rate_limit_wiring", _check_rate_limit_wiring),
    ("rate_limit_backend_quality", _check_rate_limit_backend_quality),
    ("xss_check", _check_xss),
    ("sql_injection", _check_sql_injection),
    ("user_enumeration", _check_user_enumeration),
    ("client_trusted_logic", _check_client_trusted_logic),
    ("idor_check", _check_idor),
    ("security_event_logging", _check_security_event_logging),
]


def get_python_files(repo_path: Path, max_files: int = 200) -> list[str]:
    """Get Python files from repo, limited to avoid huge repos."""
    files = []
    for p in sorted(repo_path.rglob("*.py")):
        rel = str(p.relative_to(repo_path))
        if any(skip in rel for skip in ["test", "docs", "examples", "migrations", "__pycache__"]):
            continue
        files.append(rel)
        if len(files) >= max_files:
            break
    return files


def audit_repo(name: str, url: str) -> dict:
    """Clone a repo and run all checks."""
    tmpdir = Path(tempfile.mkdtemp())
    repo_path = tmpdir / name
    print(f"\n{'='*60}")
    print(f"Auditing: {name}")
    print(f"{'='*60}")

    try:
        print(f"  Cloning {url}...")
        subprocess.run(
            ["git", "clone", "--depth", "1", url, str(repo_path)],
            capture_output=True, timeout=120,
        )

        py_files = get_python_files(repo_path)
        print(f"  Found {len(py_files)} Python files (excluding tests/docs)")

        results = {}
        for check_name, check_fn in CHECKS:
            try:
                result = check_fn(repo_path, py_files)
                count = len(result.get("findings", []))
                status = result["status"]
                results[check_name] = {
                    "status": status,
                    "finding_count": count,
                    "findings": result.get("findings", [])[:5],  # Keep max 5 for report
                }
                marker = "⚠️ " if count > 0 else "  "
                print(f"  {marker}{check_name}: {status} ({count} findings)")
            except Exception as e:
                results[check_name] = {"status": "ERROR", "error": str(e), "finding_count": 0}
                print(f"  ❌ {check_name}: ERROR — {e}")

        return results
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    repos_to_audit = sys.argv[1].split(",") if len(sys.argv) > 1 else list(REPOS.keys())

    all_results = {}
    for name in repos_to_audit:
        if name not in REPOS:
            print(f"Unknown repo: {name}. Available: {list(REPOS.keys())}")
            continue
        all_results[name] = audit_repo(name, REPOS[name])

    # Summary
    print(f"\n{'='*60}")
    print("FALSE POSITIVE AUDIT SUMMARY")
    print(f"{'='*60}")
    print(f"\n{'Check':<30} ", end="")
    for name in all_results:
        print(f"{name:>10}", end="")
    print()
    print("-" * (30 + 10 * len(all_results)))

    for check_name, _ in CHECKS:
        print(f"{check_name:<30} ", end="")
        for repo_name in all_results:
            count = all_results[repo_name].get(check_name, {}).get("finding_count", 0)
            print(f"{count:>10}", end="")
        print()

    # Save results
    output_path = Path(__file__).resolve().parent.parent / "tests" / "false_positive_audit_results.json"
    # Strip findings details for compact output
    compact = {}
    for repo_name, checks in all_results.items():
        compact[repo_name] = {}
        for check_name, data in checks.items():
            compact[repo_name][check_name] = {
                "status": data["status"],
                "finding_count": data["finding_count"],
            }
    output_path.write_text(json.dumps(compact, indent=2) + "\n")
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
