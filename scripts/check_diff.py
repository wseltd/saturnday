#!/usr/bin/env python3
"""Standalone governance check entry point.

Usage: python scripts/check_diff.py --repo PATH [--diff RANGE] [--staged] [--policy FILE] [--output DIR] [--strict]

For pre-commit hooks / CI where the saturnday CLI isn't installed directly.
"""

import argparse
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Saturnday governance checks on a diff",
    )
    parser.add_argument("--repo", required=True, help="Path to git repo")
    parser.add_argument("--diff", default="HEAD~1..HEAD", help="Git diff range (default: HEAD~1..HEAD)")
    parser.add_argument("--staged", action="store_true", help="Check staged changes instead of diff range")
    parser.add_argument("--policy", help="Path to saturnday-policy.yaml")
    parser.add_argument("--output", help="Output directory for evidence pack")
    parser.add_argument("--strict", action="store_true", help="Strict mode (all warnings become errors)")
    parser.add_argument("--auto-install-deps", action="store_true",
                        help="Install project deps in temp venv to verify API usage")
    args = parser.parse_args(argv)

    # Ensure saturnday is importable
    try:
        from saturnday.governance import run_governance_check
    except ImportError:
        print(
            "saturnday is not installed. Install with: pip install saturnday",
            file=sys.stderr,
        )
        return 2

    repo_path = Path(args.repo).expanduser().resolve()
    if not repo_path.exists():
        print(f"Repo path does not exist: {repo_path}", file=sys.stderr)
        return 2

    # Resolve policy path — skip if file doesn't exist
    policy_path = None
    if args.policy:
        p = Path(args.policy)
        if p.exists():
            policy_path = p
        else:
            print(f"Policy file not found, using defaults: {args.policy}", file=sys.stderr)

    try:
        pack, evidence_path = run_governance_check(
            repo_path=repo_path,
            diff_range=args.diff,
            staged=args.staged,
            policy_path=policy_path,
            output_dir=Path(args.output) if args.output else None,
            strict=args.strict,
            auto_install_deps=args.auto_install_deps,
        )
    except Exception as exc:
        print(f"Governance check failed: {exc}", file=sys.stderr)
        return 2

    print(f"Evidence dir: {evidence_path}")
    print(f"Disposition: {pack.disposition}")

    if pack.disposition_reasons:
        print("\nIssues:")
        for reason in pack.disposition_reasons:
            print(f"  - {reason['check']}: {reason['status']} (severity={reason['severity']})")

    return 0 if pack.disposition != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
