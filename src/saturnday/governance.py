"""Governance mode: run review checks on a diff and produce an evidence pack."""

import logging
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

from .evidence import CheckResult, EvidencePack, SCHEMA_VERSION, compute_disposition, write_evidence_dir
from .policy_manifest import (
    FAMILY_MAP,
    PolicyManifest,
    RULE_CWE,
    RULE_IDS,
    RULE_OWASP,
    default_policy,
    evaluate_scope,
    get_check_severity,
    load_policy,
    should_run_check,
)
from .ratchet import (
    FindingFingerprint,
    compare_findings,
    fingerprint_check_results,
    load_baseline,
)
from .waivers import load_waivers_from_policy, check_waiver
from .review import run_review
from .shell_policy import run_shell
from .version import __version__


def extract_changed_files(
    repo_path: Path, diff_range: str, *, staged: bool = False
) -> tuple[list[str], str]:
    """Run git diff --name-only. Returns (changed_files, raw_diff_text)."""
    if staged:
        name_cmd = ["git", "diff", "--cached", "--name-only"]
        diff_cmd = ["git", "diff", "--cached"]
    else:
        name_cmd = ["git", "diff", "--name-only", diff_range]
        diff_cmd = ["git", "diff", diff_range]

    name_result = subprocess.run(
        name_cmd, cwd=str(repo_path), capture_output=True, text=True, check=False,
    )
    if name_result.returncode != 0:
        raise RuntimeError(
            f"git diff --name-only failed (rc={name_result.returncode}): "
            f"{name_result.stderr.strip()}"
        )

    files = [f.strip() for f in name_result.stdout.strip().splitlines() if f.strip()]

    diff_result = subprocess.run(
        diff_cmd, cwd=str(repo_path), capture_output=True, check=False,
    )
    # Decode with errors="replace" to handle binary files (STEP, images, etc.)
    raw_diff = diff_result.stdout.decode("utf-8", errors="replace") if diff_result.stdout else ""

    return files, raw_diff


def _convert_review_result(
    check_name: str,
    tool_result: dict,
    policy: PolicyManifest,
    elapsed_s: float,
) -> CheckResult:
    """Convert a single review tool result dict to a CheckResult."""
    status = tool_result.get("status", "PASS")
    findings = tool_result.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    severity = get_check_severity(policy, check_name)

    # Map review status to evidence status
    if status == "FAIL":
        evidence_status = "FAIL"
    elif status == "SKIPPED":
        evidence_status = "SKIPPED"
    else:
        evidence_status = "PASS"

    # Normalize findings to dicts
    normalized_findings = []
    for f in findings:
        if isinstance(f, dict):
            normalized_findings.append(f)
        elif isinstance(f, str):
            normalized_findings.append({"message": f})

    # Resolve rule_id through FAMILY_MAP for TS checks (e.g. "hardcoded_jwt_ts" -> "hardcoded_jwt")
    _family = FAMILY_MAP.get(check_name, check_name)

    return CheckResult(
        name=check_name,
        status=evidence_status,
        severity=severity,
        findings=normalized_findings,
        files_checked=[],
        elapsed_s=elapsed_s,
        error=tool_result.get("error"),
        rule_id=RULE_IDS.get(_family),
        cwe=RULE_CWE.get(_family),
        owasp=RULE_OWASP.get(_family),
    )


def _apply_policy_filtering(
    check_results: list[CheckResult],
    disposition: str,
    policy_path: Path | None,
) -> tuple[str, list[str]]:
    """Apply exemptions and expected_findings from policy.

    Fix 65: shared helper used by both run_full_repo_review and
    run_governance_check so policy application is consistent.

    Args:
        check_results: Mutable list — findings may be removed in place.
        disposition: Pre-filter disposition.
        policy_path: Exact path to the policy YAML file, or None.

    Returns:
        Tuple of (disposition, reasons) both computed from post-filter state.
    """
    if policy_path is None:
        return compute_disposition(check_results)

    from .policy_loader import (
        load_policy_file,
        validated_exemptions,
        validated_expected_findings,
        filter_findings_by_exemptions,
    )
    _policy = load_policy_file(Path(policy_path))

    # Exemptions: remove matched findings
    _exemptions = validated_exemptions(_policy)
    if _exemptions:
        _ex_suppressed = 0
        for cr in check_results:
            if not cr.findings:
                continue
            original_count = len(cr.findings)
            cr.findings = filter_findings_by_exemptions(cr.name, cr.findings, _exemptions)
            removed = original_count - len(cr.findings)
            _ex_suppressed += removed
            if not cr.findings and cr.status == "FAIL":
                cr.status = "PASS"
        if _ex_suppressed:
            logger.info("Policy exemptions suppressed %d finding(s)", _ex_suppressed)

    # Recompute disposition AND reasons from post-filter state
    disposition, reasons = compute_disposition(check_results)

    # Expected findings: downgrade FAIL→WARN if all errors are expected
    if disposition == "FAIL":
        _expected = validated_expected_findings(_policy)
        if _expected:
            _unexpected_errors = [
                cr for cr in check_results
                if cr.status == "FAIL" and cr.severity == "error"
                and not all(
                    f.get("kind", "") in _expected
                    or cr.name in _expected  # Fix 66: match check-group name too
                    for f in cr.findings
                )
            ]
            if not _unexpected_errors:
                disposition = "WARN"
                reasons = ["all_findings_expected"]
                logger.info(
                    "All error findings are in expected_findings — downgrading FAIL to WARN"
                )

    return disposition, reasons


def run_full_repo_review(
    repo_path: Path,
    *,
    policy_path: Path | None = None,
    output_dir: Path | None = None,
    target_files: list[str] | None = None,
) -> "tuple[EvidencePack, Path | None]":
    """Run governance checks on all tracked files in the repo.

    Unlike :func:`run_governance_check`, this function does not require a diff
    range.  It discovers all tracked files via ``git ls-files``, filters to
    supported extensions, and feeds them through the standard review pipeline.

    Args:
        repo_path: Path to the repository root (must be a git repo).
        policy_path: Optional path to a policy YAML file.
        output_dir: Directory to write evidence.  If ``None``, evidence is
            written to ``~/.saturnday/evidence/<run_id>``.
        target_files: Optional list of repo-relative file paths to scan.
            When provided, only those files are passed to the review pipeline
            instead of the full ``git ls-files`` output.  Use only for
            file-local finding kinds — repo-level checks must always receive
            the full file list.

    Returns:
        Tuple of ``(EvidencePack, evidence_path)``.  ``evidence_path`` is the
        directory where evidence was written, or ``None`` on early failure.

    Raises:
        RuntimeError: If ``repo_path`` is not a git repository or
            ``git ls-files`` fails.
    """
    import tempfile as _tempfile

    # J.2: Terraform/HCL must reach the review pipeline so ``_check_terraform``
    # actually sees its targets.  The terraform check filters internally to
    # ``.tf``/``.hcl`` and other language tools (ruff/mypy/bandit/eslint)
    # filter to their own extensions, so adding IaC extensions to this set
    # does not affect unrelated checkers.
    # Scope kept narrow: Kubernetes YAML / Dockerfile coverage is a separate
    # evaluation item and NOT widened here.
    supported_extensions = {".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".tf", ".hcl"}

    start_time = time.monotonic()
    created_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = f"review_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # Validate repo
    if not (repo_path / ".git").exists():
        raise RuntimeError(f"Not a git repository: {repo_path}")

    if target_files is not None:
        # Caller has provided a restricted file list — skip git ls-files and
        # use it directly (after filtering to supported extensions).
        changed_files = [
            f for f in target_files
            if Path(f).suffix in supported_extensions
            and not any(part.startswith(".saturnday") for part in Path(f).parts)
        ]
        logger.debug(
            "run_full_repo_review: targeted scan — %d file(s): %s",
            len(changed_files),
            changed_files,
        )
    else:
        # Discover all tracked files
        ls_result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_result.returncode != 0:
            raise RuntimeError(
                f"git ls-files failed (rc={ls_result.returncode}): "
                f"{ls_result.stderr.strip()}"
            )

        all_files = [f.strip() for f in ls_result.stdout.strip().splitlines() if f.strip()]
        changed_files = [
            f for f in all_files
            if Path(f).suffix in supported_extensions
            and not any(part.startswith(".saturnday") for part in Path(f).parts)
        ]

    # Load policy
    if policy_path is not None:
        policy = load_policy(policy_path)
    else:
        policy = default_policy()

    check_results: list[CheckResult] = []

    # Always run review — project-level checks (license, readme, config)
    # must execute even when no supported-extension source files are present.
    with _tempfile.TemporaryDirectory() as tmpdir:
        review_result = run_review(
            repo_path,
            changed_files,
            Path(tmpdir),
            run_shell_func=run_shell,
            timeout_s=30,
            strict=False,
        )

    tool_runs = review_result.get("tool_runs", [])
    tools = review_result.get("tools", {})

    for check_name, tool_result in tools.items():
        if not should_run_check(policy, check_name):
            continue

        elapsed = 0.0
        for tr in tool_runs:
            if tr.get("tool") == check_name:
                elapsed = tr.get("elapsed_s", 0.0)
                break

        check_results.append(
            _convert_review_result(check_name, tool_result, policy, elapsed)
        )

    disposition, reasons = compute_disposition(check_results)

    # Fix 12.A / G12: Run post-checks in --full mode.
    # Post-checks were previously only run inside the per-ticket workflow.
    # For --full governance scans the same senior-judgment surface must fire.
    # Severity is warning — advisory, does not upgrade PASS to FAIL.
    # Findings are not deduplicated against per-ticket runs; --full is
    # a standalone command, not a continuation of a run.
    try:
        from .post_checks import run_post_checks as _run_post_checks
        _post_findings = _run_post_checks(repo_path, changed_files)
        if _post_findings:
            _norm_post = [
                {
                    "file": str(f.get("path", f.get("file", ""))),
                    "line": int(f.get("line", 0)),
                    "detail": str(f.get("message", "")),
                }
                for f in _post_findings
            ]
            check_results.append(CheckResult(
                name="post_checks",
                status="FAIL",
                severity="warning",
                findings=_norm_post,
                files_checked=[],
                elapsed_s=0.0,
                error=None,
                rule_id=None,
            ))
            # Recompute disposition to include post-check findings.
            disposition, reasons = compute_disposition(check_results)
    except Exception:
        logger.warning("Post-checks raised in full-review path — skipping", exc_info=True)

    # Fix 65: apply policy exemptions and expected_findings — same as diff-based path.
    disposition, reasons = _apply_policy_filtering(check_results, disposition, policy_path)

    ended_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pack = EvidencePack(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        mode="review",
        repo_path=str(repo_path),
        diff_range=None,
        saturnday_version=__version__,
        created_utc=created_utc,
        ended_utc=ended_utc,
        check_results=check_results,
        disposition=disposition,
        disposition_reasons=reasons,
        policy_path=str(policy_path) if policy_path else None,
    )

    if output_dir is None:
        # Write evidence inside the repo for sandbox compatibility
        output_dir = repo_path / ".saturnday" / "evidence" / run_id

    evidence_path = write_evidence_dir(pack, output_dir)

    # Record to run_data.db (best effort)
    try:
        from .run_data import get_db, init_db, record_evidence_pack
        conn = get_db()
        try:
            init_db(conn)
            record_evidence_pack(conn, pack)
        finally:
            conn.close()
    except Exception:
        pass

    logger.info("TIMING %-30s %.2fs (%d checks)", "full_repo_review", time.monotonic() - start_time, len(pack.check_results))
    return pack, evidence_path


def run_governance_check(
    repo_path: Path,
    diff_range: str,
    *,
    staged: bool = False,
    policy_path: Path | None = None,
    output_dir: Path | None = None,
    strict: bool = False,
    auto_install_deps: bool = False,
) -> tuple[EvidencePack, Path]:
    """
    Run governance checks on a diff range.

    1. Validate repo is a git repo
    2. Extract changed files from diff
    3. Load policy (or default)
    4. Evaluate scope constraints
    5. Run run_review() against changed files
    6. Convert review results -> CheckResults with policy severity
    7. Compute disposition
    8. Write evidence pack
    9. Record to run_data.db
    10. Return (pack, evidence_dir_path)
    """
    start_time = time.monotonic()
    created_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = f"check_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # 1. Validate repo
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        raise RuntimeError(f"Not a git repository: {repo_path}")

    # 2. Extract changed files
    changed_files, raw_diff = extract_changed_files(repo_path, diff_range, staged=staged)

    # 3. Load policy
    if policy_path is not None:
        policy = load_policy(policy_path)
    else:
        policy = default_policy()

    # 4. Evaluate scope
    scope_ok, scope_reasons = evaluate_scope(policy, changed_files)
    scope_results: list[CheckResult] = []
    if not scope_ok:
        scope_results.append(CheckResult(
            name="scope",
            status="FAIL",
            severity="error",
            findings=[{"message": r} for r in scope_reasons],
            files_checked=changed_files,
            elapsed_s=0.0,
        ))

    # 5. Run review pipeline
    # Respect auto_install_deps from CLI flag OR policy manifest
    effective_auto_install = auto_install_deps or policy.auto_install_deps
    check_results: list[CheckResult] = list(scope_results)

    if changed_files:
        with tempfile.TemporaryDirectory() as tmpdir:
            review_result = run_review(
                repo_path,
                changed_files,
                Path(tmpdir),
                run_shell_func=run_shell,
                timeout_s=30,
                strict=strict,
                auto_install_deps=effective_auto_install,
            )

        # 6. Convert review results
        tool_runs = review_result.get("tool_runs", [])
        tools = review_result.get("tools", {})

        for check_name, tool_result in tools.items():
            if not should_run_check(policy, check_name):
                continue

            # Find elapsed time from tool_runs
            elapsed = 0.0
            for tr in tool_runs:
                if tr.get("tool") == check_name:
                    elapsed = tr.get("elapsed_s", 0.0)
                    break

            check_results.append(
                _convert_review_result(check_name, tool_result, policy, elapsed)
            )

    # 6c/7a. Fix 65: apply policy exemptions + expected_findings via shared helper.
    _pre_disp, _ = compute_disposition(check_results)
    disposition, reasons = _apply_policy_filtering(check_results, _pre_disp, policy_path)

    # 7b. Ratchet comparison (if baseline exists)
    baseline_path = repo_path / ".saturnday-baseline.json"
    ratchet_result = None
    if baseline_path.exists():
        try:
            baseline = load_baseline(baseline_path)
            current_fps = fingerprint_check_results(check_results, repo_path)

            # Compute waived fingerprints from policy waivers
            waived_fps: set[FindingFingerprint] = set()
            if policy_path is not None:
                try:
                    import yaml
                    with open(policy_path) as f:
                        policy_raw = yaml.safe_load(f) or {}
                    waivers = load_waivers_from_policy(policy_raw)
                    if waivers:
                        for fp in current_fps:
                            result = check_waiver(
                                rule_id=fp.rule_id,
                                file_path=fp.path,
                                line=0,
                                waivers=waivers,
                                inline_suppressions=[],
                            )
                            if result.suppressed:
                                waived_fps.add(fp)
                except Exception:
                    pass  # never break governance for waiver errors

            ratchet_result = compare_findings(current_fps, baseline, waived_fingerprints=waived_fps)

            # Ratchet can escalate disposition
            if ratchet_result.disposition == "FAIL":
                disposition = "FAIL"
                for r in ratchet_result.reasons:
                    reasons.append({
                        "check": "ratchet",
                        "status": "FAIL",
                        "severity": "error",
                        "finding_count": len(ratchet_result.new_findings),
                        "message": r,
                    })
            elif (
                ratchet_result.disposition == "PASS"
                and disposition == "FAIL"
                and current_fps
                and not (current_fps - baseline.findings - waived_fps)
            ):
                # Ratchet contract closure (Nick #1): when EVERY current
                # finding is either baselined or waived AND the ratchet
                # itself returned PASS (no new findings, no past
                # enforcement_date), the per-check FAIL disposition
                # represents pre-existing known debt that the operator
                # has already acknowledged via the baseline.  Reduce
                # FAIL → PASS so an unrelated change to a file with
                # legacy findings does not block.  Pre-fix, baselined
                # findings still tripped per-check FAIL because the
                # ratchet only escalated disposition; it never reduced
                # it, so any non-green repo blocked every commit forever.
                disposition = "PASS"
                reasons.append({
                    "check": "ratchet",
                    "status": "PASS",
                    "severity": "info",
                    "finding_count": len(ratchet_result.legacy_findings),
                    "message": (
                        f"{len(ratchet_result.legacy_findings)} legacy "
                        f"finding(s) present but all are baselined or "
                        f"waived; no new findings introduced — ratchet "
                        f"reduces disposition to PASS"
                    ),
                })
        except Exception:
            pass  # never break governance flow for ratchet errors

    ended_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_elapsed = time.monotonic() - start_time
    logger.info("TIMING %-30s %.2fs (%d checks)", "governance_check", total_elapsed, len(check_results))

    # 8. Build evidence pack
    pack = EvidencePack(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        mode="check",
        repo_path=str(repo_path),
        diff_range=diff_range if not staged else "--staged",
        saturnday_version=__version__,
        created_utc=created_utc,
        ended_utc=ended_utc,
        check_results=check_results,
        disposition=disposition,
        disposition_reasons=reasons,
        policy_path=str(policy_path) if policy_path else None,
    )

    # Determine output directory — write inside repo for sandbox compatibility
    if output_dir is None:
        output_dir = repo_path / ".saturnday" / "evidence" / run_id
    evidence_path = write_evidence_dir(pack, output_dir)

    # Write raw diff if available
    if raw_diff:
        (evidence_path / "diff-input.diff").write_text(raw_diff)

    # 9. Record to run_data.db (best effort)
    try:
        from .run_data import get_db, init_db, record_evidence_pack
        conn = get_db()
        try:
            init_db(conn)
            record_evidence_pack(conn, pack)
        finally:
            conn.close()
    except Exception:
        pass  # never break governance flow

    return pack, evidence_path
