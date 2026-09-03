"""Standalone CLI entry point for Devstral-backed Saturnday runs.

Usage::

    saturnday-devstral run --plan .saturnday/plan.json --repo . \\
        --base-url http://127.0.0.1:8000/v1 --model devstral-24b \\
        --api-key EMPTY --max-tokens 4096

This entry point is completely separate from ``saturnday`` CLI.
It imports ``run_plan`` from ``ticket_runner_devstral`` which applies
Phase 2 (temperature escalation) and Phase 3 (import validation) patches
without modifying the original ``ticket_runner.py``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> None:
    """CLI entry point for ``saturnday-devstral``."""
    parser = argparse.ArgumentParser(
        prog="saturnday-devstral",
        description="Devstral-specific Saturnday runner with temperature escalation and import validation",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- run subcommand ---
    run_parser = subparsers.add_parser("run", help="Execute a project plan with Devstral overrides")
    run_parser.add_argument("--plan", required=True, help="Path to the project plan JSON file")
    run_parser.add_argument("--repo", required=True, help="Path to the target repository")
    run_parser.add_argument(
        "--standards-dir", default="./senior_engineering_standards",
        help="Path to engineering standards directory (default: ./senior_engineering_standards)",
    )
    run_parser.add_argument("--output-dir", default=None, help="Evidence output directory")
    run_parser.add_argument("--api-key", default=None, help="API key for the vLLM server")
    run_parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1", help="vLLM API base URL")
    run_parser.add_argument("--model", default="devstral-24b", help="Model name (default: devstral-24b)")
    run_parser.add_argument("--temperature", type=float, default=0.0, help="Base temperature for attempt 1 (default: 0.0)")
    run_parser.add_argument("--timeout", type=int, default=1200, help="Request timeout in seconds (default: 1200)")
    run_parser.add_argument("--max-tokens", type=int, default=4096, help="Max tokens in coder response (default: 4096)")
    run_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose (DEBUG) logging")
    run_parser.add_argument(
        "--auto-repair", action="store_true", default=False,
        help="Attempt automatic repair after ticket retries are exhausted",
    )
    run_parser.add_argument(
        "--no-role-passes", action="store_true", default=False,
        help="Disable post-run role passes",
    )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        sys.exit(_cmd_run(args))


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute the ``run`` subcommand with Devstral overrides."""
    # Logging
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    api_key = args.api_key or os.environ.get("SATURDAY_VLLM_API_KEY", "localtoken-change-me")

    plan_path = Path(args.plan)
    if not plan_path.exists():
        print(f"Error: Plan file not found: {plan_path}", file=sys.stderr)
        return 1

    repo_path = Path(args.repo)
    if not repo_path.is_dir():
        print(f"Error: Repository not found: {repo_path}", file=sys.stderr)
        return 1

    # Verify git repo
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(repo_path), capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"Error: {repo_path} is not inside a git repository", file=sys.stderr)
        return 1

    # Find standards directory
    standards_dir = Path(args.standards_dir)
    if not standards_dir.is_dir():
        from saturnday.interactive import _find_standards_dir
        standards_dir = _find_standards_dir(repo_path)
        if not standards_dir.is_dir():
            print(f"Error: Standards directory not found: {args.standards_dir}", file=sys.stderr)
            return 1

    # Build CoderConfig — always openai backend, always compact_prompts + large_context
    from saturnday._types import CoderConfig
    config = CoderConfig(
        backend="openai",
        base_url=args.base_url,
        api_key=api_key,
        model=args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        timeout_s=args.timeout,
        compact_prompts=True,
        large_context=True,
    )

    # Run with Devstral overrides (Phase 2 + Phase 3)
    from saturnday.ticket_runner_devstral import run_plan_devstral
    try:
        run_result = run_plan_devstral(
            plan_path=plan_path,
            repo_path=repo_path,
            coder_config=config,
            standards_dir=standards_dir,
            output_dir=args.output_dir,
            auto_repair=getattr(args, "auto_repair", False),
            role_passes=not getattr(args, "no_role_passes", False),
        )
    except Exception as exc:
        logger.error("Plan execution failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Print summary using the standard formatter
    from saturnday.cli import _print_run_summary
    _print_run_summary(
        run_result,
        output_dir=args.output_dir or "",
        plan_path=str(plan_path),
        repo_path=str(repo_path),
        backend="openai",
    )

    if run_result.plan_governance_reason and run_result.plan_governance_reason not in (
        "PLAN_GOVERNANCE_NO_GOVERNANCE",
    ) and not run_result.plan_governance_met:
        print(
            "Error: plan governance was NOT met — see 'Plan governance: NOT MET' above.",
            file=sys.stderr,
        )
        return 1

    return 0 if run_result.failed == 0 else 1


if __name__ == "__main__":
    main()
