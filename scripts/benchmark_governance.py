#!/usr/bin/env python3
"""Performance benchmark for governance checks.

Runs governance checks against pinned fixture sizes and reports pass/warn/fail
against defined thresholds.

Usage:
    python scripts/benchmark_governance.py --mode delta|medium|full
    python scripts/benchmark_governance.py --mode all

Thresholds (median of 3 runs):
  Delta (1 file):     warn >5s,  fail >10s
  Medium (~20 files): warn >15s, fail >30s
  Full (200+ files):  warn >3min, fail >5min

Benchmark fixtures:
  Delta:  tests/fixtures/integration_app.py
  Medium: pallets/flask at SHA 4cae5d8e411b1e69949d8fae669afeacbd3e5908
  Full:   saleor/saleor at SHA 22a30bf2e4ccda91e1e43673d77e25d439f61746
"""

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

REPO_ROOT = Path(__file__).resolve().parent.parent

# Thresholds in seconds
THRESHOLDS = {
    "delta": {"warn": 5.0, "fail": 10.0},
    "medium": {"warn": 15.0, "fail": 30.0},
    "full": {"warn": 180.0, "fail": 300.0},
}

# Pinned repos for medium/full
MEDIUM_REPO = {
    "url": "https://github.com/pallets/flask.git",
    "sha": "4cae5d8e411b1e69949d8fae669afeacbd3e5908",
    "name": "flask",
}
FULL_REPO = {
    "url": "https://github.com/saleor/saleor.git",
    "sha": "22a30bf2e4ccda91e1e43673d77e25d439f61746",
    "name": "saleor",
}

# File inclusion/exclusion
INCLUDE_PATTERNS = ["**/*.py"]
EXCLUDE_DIRS = {"tests", "docs", "examples", "migrations", "__pycache__"}

RUNS = 3


def get_python_files(repo_path: Path) -> list[str]:
    """Get Python files matching inclusion/exclusion rules."""
    files = []
    for p in sorted(repo_path.rglob("*.py")):
        rel = p.relative_to(repo_path)
        parts = rel.parts
        if any(d in EXCLUDE_DIRS for d in parts):
            continue
        files.append(str(rel))
    return files


def clone_repo(url: str, sha: str, dest: Path) -> None:
    """Clone a repo at a pinned SHA."""
    subprocess.run(
        ["git", "clone", "--no-checkout", url, str(dest)],
        capture_output=True, timeout=120, check=True,
    )
    subprocess.run(
        ["git", "checkout", sha],
        cwd=str(dest), capture_output=True, timeout=30, check=True,
    )


def run_checks_on_files(repo_path: Path, files: list[str]) -> float:
    """Run security checks on files, return elapsed seconds."""
    from saturnday.review import run_review
    from saturnday.shell_policy import run_shell

    start = time.monotonic()
    with tempfile.TemporaryDirectory() as tmpdir:
        run_review(
            repo_path,
            files,
            Path(tmpdir),
            run_shell_func=run_shell,
            timeout_s=30,
            strict=False,
        )
    return time.monotonic() - start


def benchmark_delta() -> dict:
    """Benchmark: single file (~200 lines)."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "integration_app.py"
    if not fixture.exists():
        return {"error": f"Fixture not found: {fixture}"}

    # Use the fixtures dir as a pseudo-repo
    repo_path = fixture.parent
    files = ["integration_app.py"]
    timings = []
    for i in range(RUNS):
        elapsed = run_checks_on_files(repo_path, files)
        timings.append(elapsed)
        print(f"  Delta run {i+1}: {elapsed:.2f}s")

    median = statistics.median(timings)
    return _evaluate("delta", median, timings, files_count=1)


def benchmark_medium() -> dict:
    """Benchmark: medium repo (~20 files)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / MEDIUM_REPO["name"]
        print(f"  Cloning {MEDIUM_REPO['name']} at {MEDIUM_REPO['sha'][:8]}...")
        try:
            clone_repo(MEDIUM_REPO["url"], MEDIUM_REPO["sha"], dest)
        except Exception as e:
            return {"error": f"Clone failed: {e}", "repo_sha": MEDIUM_REPO["sha"]}

        files = get_python_files(dest)
        print(f"  Found {len(files)} Python files")
        timings = []
        for i in range(RUNS):
            elapsed = run_checks_on_files(dest, files)
            timings.append(elapsed)
            print(f"  Medium run {i+1}: {elapsed:.2f}s")

        median = statistics.median(timings)
        result = _evaluate("medium", median, timings, files_count=len(files))
        result["repo_sha"] = MEDIUM_REPO["sha"]
        return result


def benchmark_full() -> dict:
    """Benchmark: full repo (200+ files)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / FULL_REPO["name"]
        print(f"  Cloning {FULL_REPO['name']} at {FULL_REPO['sha'][:8]}...")
        try:
            clone_repo(FULL_REPO["url"], FULL_REPO["sha"], dest)
        except Exception as e:
            return {"error": f"Clone failed: {e}", "repo_sha": FULL_REPO["sha"]}

        files = get_python_files(dest)
        print(f"  Found {len(files)} Python files")
        timings = []
        for i in range(RUNS):
            elapsed = run_checks_on_files(dest, files)
            timings.append(elapsed)
            print(f"  Full run {i+1}: {elapsed:.2f}s")

        median = statistics.median(timings)
        result = _evaluate("full", median, timings, files_count=len(files))
        result["repo_sha"] = FULL_REPO["sha"]
        return result


def _evaluate(mode: str, median: float, timings: list[float], files_count: int) -> dict:
    """Evaluate timing against thresholds."""
    thresholds = THRESHOLDS[mode]
    if median > thresholds["fail"]:
        status = "FAIL"
    elif median > thresholds["warn"]:
        status = "WARN"
    else:
        status = "PASS"

    return {
        "mode": mode,
        "status": status,
        "median_s": round(median, 2),
        "timings_s": [round(t, 2) for t in timings],
        "files_count": files_count,
        "threshold_warn_s": thresholds["warn"],
        "threshold_fail_s": thresholds["fail"],
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark governance checks")
    parser.add_argument(
        "--mode", required=True, choices=["delta", "medium", "full", "all"],
        help="Benchmark mode",
    )
    parser.add_argument("--output", help="Output JSON file")
    args = parser.parse_args()

    modes = ["delta", "medium", "full"] if args.mode == "all" else [args.mode]
    results = {}

    for mode in modes:
        print(f"\n{'='*50}")
        print(f"Benchmark: {mode}")
        print(f"{'='*50}")

        if mode == "delta":
            results[mode] = benchmark_delta()
        elif mode == "medium":
            results[mode] = benchmark_medium()
        elif mode == "full":
            results[mode] = benchmark_full()

    # Summary
    print(f"\n{'='*50}")
    print("BENCHMARK SUMMARY")
    print(f"{'='*50}")
    any_fail = False
    for mode, result in results.items():
        if "error" in result:
            print(f"  {mode}: ERROR — {result['error']}")
            continue
        status = result["status"]
        median = result["median_s"]
        files = result["files_count"]
        print(f"  {mode}: {status} (median={median}s, files={files})")
        if status == "FAIL":
            any_fail = True

    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2) + "\n")
        print(f"\nResults written to: {args.output}")

    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
