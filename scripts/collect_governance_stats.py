#!/usr/bin/env python3
"""Collect canonical governance stats from runtime registries.

Produces governance_status.json — the single source of truth for all
governance counts. The closure report is generated FROM this file,
never the other way around.

Usage:
    python scripts/collect_governance_stats.py [--output governance_status.json]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def count_python_checks() -> dict:
    """Count Python checks from the canonical RULE_IDS registry."""
    from saturnday.policy_manifest import RULE_IDS
    return {
        "count": len(RULE_IDS),
        "rule_ids": dict(sorted(RULE_IDS.items(), key=lambda x: x[1])),
    }


def count_ts_checks() -> dict:
    """Count TS checks from the canonical ALL_TS_CHECKS list in review_ts.py."""
    from saturnday.review_ts import ALL_TS_CHECKS
    from saturnday.policy_manifest import FAMILY_MAP, RULE_IDS

    # ALL_TS_CHECKS is a list of functions — extract names
    all_names = []
    for fn in ALL_TS_CHECKS:
        name = fn.__name__
        # remove 'check_' prefix to get the emitted name
        if name.startswith("check_"):
            name = name[len("check_"):]
        all_names.append(name)

    # Security governance TS checks: those that map via FAMILY_MAP to a RULE_IDS family
    security_ts = [
        name for name in all_names
        if name in FAMILY_MAP and FAMILY_MAP[name] in RULE_IDS
    ]
    return {
        "total_ts_checks": len(ALL_TS_CHECKS),
        "security_ts_checks": len(security_ts),
        "security_check_names": sorted(security_ts),
    }


def verify_wired_checks() -> dict:
    """Verify all declared checks are callable in the loaded modules."""
    from saturnday.policy_manifest import HARD_CHECKS, SOFT_CHECKS, RULE_IDS
    import saturnday.review as review_mod

    security_checks = set(RULE_IDS.keys())

    # Some checks have aliases: xss_check -> _check_xss, idor_check -> _check_idor
    _FUNC_ALIASES = {
        "xss_check": "_check_xss",
        "idor_check": "_check_idor",
    }

    wired = []
    missing = []

    for check_name in sorted(security_checks):
        func_name = _FUNC_ALIASES.get(check_name, f"_check_{check_name}")
        if hasattr(review_mod, func_name) and callable(getattr(review_mod, func_name)):
            wired.append(check_name)
        else:
            missing.append(check_name)

    return {
        "total_security_checks": len(security_checks),
        "wired": len(wired),
        "missing": missing,
        "all_wired": len(missing) == 0,
    }


def count_tests_by_file() -> dict:
    """Count tests per file using pytest --co -q."""
    repo_root = Path(__file__).resolve().parent.parent
    test_dir = repo_root / "tests"
    results = {}

    test_files = sorted(test_dir.glob("test_*.py"))
    for tf in test_files:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "--co", "-q", str(tf)],
                capture_output=True, text=True, cwd=str(repo_root), timeout=30,
            )
            lines = proc.stdout.strip().splitlines()
            count = 0
            for line in lines:
                # Format: "tests/test_file.py: N" or "N tests collected"
                if "::" in line:
                    count += 1
                elif line.strip().endswith(("collected", "selected")):
                    parts = line.split()
                    for p in parts:
                        if p.isdigit():
                            count = int(p)
                            break
                elif ":" in line and not line.startswith("ERRORS"):
                    # "tests/test_ratchet.py: 35" format
                    parts = line.rsplit(":", 1)
                    if len(parts) == 2 and parts[1].strip().isdigit():
                        count = int(parts[1].strip())
            results[tf.name] = count
        except Exception as e:
            results[tf.name] = f"ERROR: {e}"

    total = sum(v for v in results.values() if isinstance(v, int))
    return {"by_file": results, "total": total}


def count_fixtures() -> dict:
    """Count fixture files by category using pathlib.glob()."""
    repo_root = Path(__file__).resolve().parent.parent
    fixtures_dir = repo_root / "tests" / "fixtures"

    categories = {}
    for subdir in sorted(fixtures_dir.iterdir()):
        if subdir.is_dir():
            py_files = list(subdir.glob("*.py"))
            ts_files = list(subdir.glob("*.ts"))
            categories[subdir.name] = {
                "python": len(py_files),
                "typescript": len(ts_files),
                "total": len(py_files) + len(ts_files),
            }

    # Integration app (top-level fixture)
    integration = fixtures_dir / "integration_app.py"
    if integration.exists():
        categories["integration"] = {"python": 1, "typescript": 0, "total": 1}

    total = sum(c["total"] for c in categories.values())
    return {"by_category": categories, "total": total}


def count_structural_tests() -> int:
    """Count structural integration tests via pytest --co -q."""
    repo_root = Path(__file__).resolve().parent.parent
    test_file = repo_root / "tests" / "test_security_integration.py"
    if not test_file.exists():
        return 0
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--co", "-q", str(test_file)],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        lines = proc.stdout.strip().splitlines()
        count = sum(1 for l in lines if "::" in l)
        if count == 0:
            # Fallback: parse "filename: N" format
            for line in lines:
                if ":" in line:
                    parts = line.rsplit(":", 1)
                    if len(parts) == 2 and parts[1].strip().isdigit():
                        count = int(parts[1].strip())
        return count
    except Exception:
        return 0


def check_false_positive_audit() -> dict:
    """Parse false_positive_audit_results.json for framework coverage."""
    repo_root = Path(__file__).resolve().parent.parent
    results_path = repo_root / "tests" / "false_positive_audit_results.json"
    if not results_path.exists():
        return {"exists": False, "frameworks": []}

    data = json.loads(results_path.read_text())
    frameworks = {}
    for fw_name, checks in data.items():
        total_findings = sum(c.get("finding_count", 0) for c in checks.values())
        checks_with_findings = sum(1 for c in checks.values() if c.get("finding_count", 0) > 0)
        frameworks[fw_name] = {
            "total_findings": total_findings,
            "checks_with_findings": checks_with_findings,
            "total_checks_run": len(checks),
        }

    return {"exists": True, "frameworks": frameworks}


def check_agent_fixtures() -> bool:
    """Check whether agent-generated fixtures exist."""
    repo_root = Path(__file__).resolve().parent.parent
    agent_dir = repo_root / "tests" / "fixtures" / "agent_generated"
    if not agent_dir.exists():
        return False
    # Check for actual fixture files
    py_files = list(agent_dir.glob("*.py"))
    ts_files = list(agent_dir.glob("*.ts"))
    return len(py_files) + len(ts_files) > 0


def check_baseline_exists() -> bool:
    """Check whether .saturnday-baseline.json exists."""
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / ".saturnday-baseline.json").exists()


def check_governance_workflow() -> bool:
    """Check whether .github/workflows/governance.yml exists."""
    repo_root = Path(__file__).resolve().parent.parent
    return (repo_root / ".github" / "workflows" / "governance.yml").exists()


def check_jwt_scope_usage() -> dict:
    """Check for jwt_scope: internal_single_issuer usage in policy manifests."""
    repo_root = Path(__file__).resolve().parent.parent
    from saturnday.policy_manifest import PolicyManifest

    # Check for .saturnday-policy.yml files
    policy_files = list(repo_root.rglob(".saturnday-policy.yml"))
    escape_hatch_users = []

    for pf in policy_files:
        try:
            import yaml
            data = yaml.safe_load(pf.read_text())
            if isinstance(data, dict) and data.get("jwt_scope") == "internal_single_issuer":
                escape_hatch_users.append(str(pf.relative_to(repo_root)))
        except Exception:
            pass

    return {
        "escape_hatch_used": len(escape_hatch_users) > 0,
        "files": escape_hatch_users,
    }


def collect_all() -> dict:
    """Collect all governance stats."""
    python_checks = count_python_checks()
    ts_checks = count_ts_checks()
    wired = verify_wired_checks()
    tests = count_tests_by_file()
    fixtures = count_fixtures()
    structural = count_structural_tests()
    fp_audit = check_false_positive_audit()
    agent_fixtures = check_agent_fixtures()
    baseline_exists = check_baseline_exists()
    governance_workflow = check_governance_workflow()
    jwt_scope = check_jwt_scope_usage()

    return {
        "python_checks": python_checks,
        "ts_checks": ts_checks,
        "wired_verification": wired,
        "tests": tests,
        "fixtures": fixtures,
        "structural_integration_tests": structural,
        "false_positive_audit": fp_audit,
        "agent_generated_fixtures_exist": agent_fixtures,
        "baseline_exists": baseline_exists,
        "governance_workflow_exists": governance_workflow,
        "jwt_scope_escape_hatch": jwt_scope,
    }


def main():
    parser = argparse.ArgumentParser(description="Collect governance stats")
    parser.add_argument(
        "--output", default="governance_status.json",
        help="Output path for governance_status.json",
    )
    args = parser.parse_args()

    stats = collect_all()

    output_path = Path(args.output)
    output_path.write_text(json.dumps(stats, indent=2) + "\n")
    print(f"Governance stats written to: {output_path}")

    # Print summary
    print(f"\nPython security checks: {stats['python_checks']['count']}")
    print(f"TS security checks: {stats['ts_checks']['security_ts_checks']}")
    print(f"All checks wired: {stats['wired_verification']['all_wired']}")
    print(f"Total tests: {stats['tests']['total']}")
    print(f"Total fixtures: {stats['fixtures']['total']}")
    print(f"Structural integration tests: {stats['structural_integration_tests']}")
    print(f"FP audit frameworks: {list(stats['false_positive_audit'].get('frameworks', {}).keys())}")
    print(f"Agent fixtures exist: {stats['agent_generated_fixtures_exist']}")
    print(f"Baseline exists: {stats['baseline_exists']}")
    print(f"Governance workflow exists: {stats['governance_workflow_exists']}")


if __name__ == "__main__":
    main()
