"""OpenClaw passive skill scanner.

Walks skill corpora, runs the approved passive check subset directly
(NOT through run_review()), and produces machine-readable + human-readable
output.

Phase 1 non-goals: package installation, dependency resolution, runtime
execution of skills, behaviour verification of skill outputs, full JS
security coverage, exploit validation. This is a passive governance
scanner for repository content.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evidence import CheckResult, compute_disposition
from .version import __version__
from .language_detect import is_ts_js
from .policy_manifest import (
    FAMILY_MAP,
    PolicyManifest,
    default_policy,
    get_check_severity,
    should_run_check,
)
from .review import (
    _check_code_quality,
    _check_stubs,
    _check_syntax,
    _check_test_quality,
    _is_test_path,
    _scan_injection_patterns,
    _scan_placeholders,
    _scan_secrets,
)
from .review_ts import (
    EXCLUDED_DIRS,
    MAX_FILE_SIZE_BYTES,
    check_fake_tests_ts,
    check_hallucinated_imports_ts,
    check_placeholders_ts,
    check_prompt_injection_ts,
    check_secrets_ts,
    check_syntax_ts,
    check_typosquat_ts,
)

# ---------------------------------------------------------------------------
# Finding dataclass — canonical typed finding for all scanner surfaces
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """A single finding from a skill scan.

    Used by the canonical scanner's ``findings`` property and re-exported
    through the cloud_scanner compatibility shim.
    """

    check: str = ""
    severity: str = "medium"
    file: str = ""
    line: int | None = None
    message: str = ""
    kind: str = ""
    remediation: dict | None = None
    suggested_fix: str | None = None
    category: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain dict for JSON output."""
        d: dict[str, Any] = {
            "check": self.check,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }
        if self.kind:
            d["kind"] = self.kind
        if self.remediation:
            d["remediation"] = self.remediation
        if self.suggested_fix:
            d["suggested_fix"] = self.suggested_fix
        return d


# ---------------------------------------------------------------------------
# Regex constants for OpenClaw security checks
# These are used by the ported cloud_scanner checks below.
# ---------------------------------------------------------------------------

# Shell danger patterns
_SHELL_DANGER_RE = re.compile(
    r"""(?x)
    subprocess\.(?:call|run|Popen)\s*\(     |
    os\.(?:system|popen|exec[lv]p?e?)\s*\(  |
    \b(?:eval|exec)\s*\(                    |
    `[^`]*`                                 |
    \$\([^)]+\)
    """,
)

# Same regex WITHOUT backtick pattern — for TS/JS where backticks are
# template literals, not shell execution.
_SHELL_DANGER_RE_NO_BACKTICK = re.compile(
    r"""(?x)
    subprocess\.(?:call|run|Popen)\s*\(     |
    os\.(?:system|popen|exec[lv]p?e?)\s*\(  |
    \b(?:eval|exec)\s*\(                    |
    \$\([^)]+\)
    """,
)

_TS_JS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

# Remote download patterns
_REMOTE_DOWNLOAD_RE = re.compile(
    r"""(?x)
    urllib\.request\.urlretrieve   |
    requests\.get\s*\(            |
    wget\s+                       |
    curl\s+                       |
    fetch\s*\(                    |
    http\.get\s*\(                |
    https\.get\s*\(
    """,
)

# Credential leak patterns
_CREDENTIAL_RE = re.compile(
    r"""(?xi)
    (?:api[_-]?key|secret|password|token|auth|credential)\s*[=:]\s*['"][^'"]{8,}['"]  |
    (?:AKIA[0-9A-Z]{16})                                         |
    (?:ghp_[a-zA-Z0-9]{36})                                      |
    (?:sk-[a-zA-Z0-9]{48})                                       |
    (?:sk-ant-[a-zA-Z0-9-]{90,})
    """,
)

# Command interpolation patterns
_CMD_INTERPOLATION_RE = re.compile(
    r"""(?x)
    subprocess\.\w+\s*\(\s*f['"]         |
    os\.system\s*\(\s*f['"]              |
    subprocess\.\w+\s*\([^)]*\.format\(  |
    os\.system\s*\([^)]*\.format\(
    """,
)

# Broad filesystem access
_BROAD_FS_RE = re.compile(
    r"""(?x)
    shutil\.rmtree\s*\(             |
    os\.(?:remove|unlink)\s*\(      |
    pathlib\.Path\([^)]*\)\.unlink  |
    open\s*\(\s*['"]\/              |
    \.write\s*\(\s*['"]\/
    """,
)

# Missing approval gate — destructive actions without confirmation
_DESTRUCTIVE_NO_CONFIRM_RE = re.compile(
    r"""(?x)
    (?:delete|remove|drop|destroy|purge|wipe)\s*\(
    """,
    re.IGNORECASE,
)

# Scannable text file extensions for cloud checks
_SCANNABLE_EXTS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".sh", ".bash",
    ".yaml", ".yml", ".json", ".toml",
})


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PER_SKILL_TIMEOUT_S = 30
DEFAULT_PROGRESS_INTERVAL = 100
MAX_SUMMARY_FINDINGS_PER_SKILL = 5

# ---------------------------------------------------------------------------
# Python check wrappers — adapt review.py functions to the scanner calling
# convention (check_fn(skill_dir, files) → dict with "name" key).
# The underlying functions already accept (repo_path, changed_files).
# ---------------------------------------------------------------------------

def _py_check_syntax(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "syntax_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_check_syntax(skill_dir, py_files), "name": "syntax_py"}


def _py_check_secrets(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "secrets_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_scan_secrets(skill_dir, py_files), "name": "secrets_py"}


def _py_check_placeholders(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "placeholders_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_scan_placeholders(skill_dir, py_files), "name": "placeholders_py"}


def _py_check_code_quality(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "code_quality_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_check_code_quality(skill_dir, py_files), "name": "code_quality_py"}


def _py_check_stubs(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "stubs_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_check_stubs(skill_dir, py_files), "name": "stubs_py"}


def _py_check_test_quality(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "test_quality_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_check_test_quality(skill_dir, py_files), "name": "test_quality_py"}


def _py_check_injection(skill_dir: Path, files: list[str]) -> dict:
    py_files = [f for f in files if f.endswith(".py")]
    if not py_files:
        return {"name": "injection_py", "status": "PASS", "findings": [], "exit_code": 0, "raw_output": "", "error": None}
    return {**_scan_injection_patterns(skill_dir, py_files), "name": "injection_py"}


# ---------------------------------------------------------------------------
# OpenClaw security check wrappers — ported from cloud_scanner.
# These accept the canonical (skill_dir, files) signature where files is
# list[str] of relative paths, and return check_result dicts compatible with
# the canonical scanner format (kind/detail/confidence).
# Only scannable text file extensions are processed; .md files are skipped
# to avoid false positives from documentation code examples.
# ---------------------------------------------------------------------------

def _read_scannable_files(skill_dir: Path, files: list[str]) -> list[tuple[str, str]]:
    """Read text content of scannable files.

    Returns list of (relative_path, content) for files whose extension is in
    ``_SCANNABLE_EXTS``. Markdown and binary files are excluded to avoid
    false positives from documentation examples.
    """
    result: list[tuple[str, str]] = []
    for rel in files:
        p = Path(rel)
        if p.suffix.lower() not in _SCANNABLE_EXTS:
            continue
        full = skill_dir / rel
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
            result.append((rel, content))
        except OSError:
            continue
    return result


def _oc_check_skill_structure(skill_dir: Path, files: list[str]) -> dict:
    """Check SKILL.md exists and has required structure (heading, min length)."""
    check_findings: list[dict] = []
    has_high = False
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        check_findings.append({
            "kind": "missing_skill_md",
            "detail": "Missing SKILL.md",
            "confidence": "high",
            "file": "SKILL.md",
        })
        has_high = True
    else:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"^#\s+", content, re.MULTILINE):
            check_findings.append({
                "kind": "missing_heading",
                "detail": "SKILL.md has no heading",
                "confidence": "medium",
                "file": "SKILL.md",
            })
        if len(content.strip()) < 50:
            check_findings.append({
                "kind": "thin_skill_md",
                "detail": "SKILL.md is too short (< 50 chars)",
                "confidence": "medium",
                "file": "SKILL.md",
            })
    # high → FAIL; medium → WARN; nothing → PASS
    if has_high:
        status = "FAIL"
    elif check_findings:
        status = "WARN"
    else:
        status = "PASS"
    return {
        "name": "skill_structure",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_shell_danger(skill_dir: Path, files: list[str]) -> dict:
    """Detect dangerous shell execution patterns in scannable source files."""
    check_findings: list[dict] = []
    for rel, content in _read_scannable_files(skill_dir, files):
        # TS/JS files use backticks for template literals, not shell execution
        is_ts_js = Path(rel).suffix.lower() in _TS_JS_EXTENSIONS
        pattern = _SHELL_DANGER_RE_NO_BACKTICK if is_ts_js else _SHELL_DANGER_RE
        for i, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                check_findings.append({
                    "kind": "shell_danger",
                    "detail": f"Potentially dangerous shell execution: {line.strip()[:100]}",
                    "confidence": "high",
                    "file": rel,
                    "line": i,
                })
    status = "FAIL" if check_findings else "PASS"
    return {
        "name": "shell_danger",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_remote_downloads(skill_dir: Path, files: list[str]) -> dict:
    """Detect remote download patterns in scannable source files."""
    check_findings: list[dict] = []
    for rel, content in _read_scannable_files(skill_dir, files):
        for i, line in enumerate(content.splitlines(), 1):
            if _REMOTE_DOWNLOAD_RE.search(line):
                check_findings.append({
                    "kind": "remote_download",
                    "detail": f"Remote download detected: {line.strip()[:100]}",
                    "confidence": "high",
                    "file": rel,
                    "line": i,
                })
    status = "FAIL" if check_findings else "PASS"
    return {
        "name": "remote_download",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_credential_leakage(skill_dir: Path, files: list[str]) -> dict:
    """Detect hardcoded credentials and API keys in scannable source files."""
    check_findings: list[dict] = []
    for rel, content in _read_scannable_files(skill_dir, files):
        for i, line in enumerate(content.splitlines(), 1):
            if _CREDENTIAL_RE.search(line):
                check_findings.append({
                    "kind": "credential_leak",
                    "detail": f"Possible credential leak: {line.strip()[:80]}...",
                    "confidence": "high",
                    "file": rel,
                    "line": i,
                })
    status = "FAIL" if check_findings else "PASS"
    return {
        "name": "credential_leak",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_command_interpolation(skill_dir: Path, files: list[str]) -> dict:
    """Detect command injection via string interpolation in shell calls."""
    check_findings: list[dict] = []
    for rel, content in _read_scannable_files(skill_dir, files):
        for i, line in enumerate(content.splitlines(), 1):
            if _CMD_INTERPOLATION_RE.search(line):
                check_findings.append({
                    "kind": "command_interpolation",
                    "detail": f"Command interpolation risk: {line.strip()[:100]}",
                    "confidence": "high",
                    "file": rel,
                    "line": i,
                })
    status = "FAIL" if check_findings else "PASS"
    return {
        "name": "command_interpolation",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_broad_filesystem(skill_dir: Path, files: list[str]) -> dict:
    """Detect broad filesystem access patterns in scannable source files."""
    check_findings: list[dict] = []
    for rel, content in _read_scannable_files(skill_dir, files):
        for i, line in enumerate(content.splitlines(), 1):
            if _BROAD_FS_RE.search(line):
                check_findings.append({
                    "kind": "broad_filesystem",
                    "detail": f"Broad filesystem access: {line.strip()[:100]}",
                    "confidence": "medium",
                    "file": rel,
                    "line": i,
                })
    # medium confidence → WARN (not FAIL) so it doesn't block disposition by default
    status = "WARN" if check_findings else "PASS"
    return {
        "name": "broad_filesystem",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_missing_approval_gates(skill_dir: Path, files: list[str]) -> dict:
    """Detect destructive actions without approval/confirmation gates."""
    check_findings: list[dict] = []
    for rel, content in _read_scannable_files(skill_dir, files):
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if _DESTRUCTIVE_NO_CONFIRM_RE.search(line):
                window = "\n".join(lines[max(0, i - 5):i + 5])
                if not re.search(r"confirm|approval|prompt|ask|verify", window, re.IGNORECASE):
                    check_findings.append({
                        "kind": "missing_approval_gate",
                        "detail": f"Destructive action without approval gate: {line.strip()[:100]}",
                        "confidence": "medium",
                        "file": rel,
                        "line": i,
                    })
    # medium confidence → WARN
    status = "WARN" if check_findings else "PASS"
    return {
        "name": "missing_approval_gate",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


def _oc_check_publish_hygiene(skill_dir: Path, files: list[str]) -> dict:
    """Check publish readiness: test files and LICENSE presence."""
    check_findings: list[dict] = []
    has_tests = any("test" in rel.lower() and rel.endswith(".py") for rel in files)
    if not has_tests:
        check_findings.append({
            "kind": "no_tests",
            "detail": "No test files found",
            "confidence": "low",
        })
    has_license = (skill_dir / "LICENSE").exists() or (skill_dir / "LICENSE.md").exists()
    if not has_license:
        check_findings.append({
            "kind": "no_license",
            "detail": "No LICENSE file found",
            "confidence": "low",
        })
    # low confidence → WARN only, never FAIL — these are advisory hygiene notes
    status = "WARN" if check_findings else "PASS"
    return {
        "name": "publish_hygiene",
        "status": status,
        "findings": check_findings,
        "exit_code": 0,
        "raw_output": "",
        "error": None,
    }


# OpenClaw security checks — ported from cloud_scanner
OPENCLAW_SECURITY_CHECKS = [
    _oc_check_skill_structure,
    _oc_check_shell_danger,
    _oc_check_remote_downloads,
    _oc_check_credential_leakage,
    _oc_check_command_interpolation,
    _oc_check_broad_filesystem,
    _oc_check_missing_approval_gates,
    _oc_check_publish_hygiene,
]


# Passive checks run by the scanner — explicit approved subset
# TS/JS checks (original)
PASSIVE_CHECKS_TS = [
    check_secrets_ts,
    check_hallucinated_imports_ts,
    check_typosquat_ts,
    check_fake_tests_ts,
    check_prompt_injection_ts,
    check_placeholders_ts,
]

# Python checks (new)
PASSIVE_CHECKS_PY = [
    _py_check_secrets,
    _py_check_placeholders,
    _py_check_code_quality,
    _py_check_stubs,
    _py_check_test_quality,
    _py_check_injection,
]

# Combined list — all passive checks (TS + Python review checks only).
# OpenClaw security checks run as a separate supplementary phase in scan_skill
# so they don't interact with the policy-based compute_disposition logic.
PASSIVE_CHECKS = PASSIVE_CHECKS_TS + PASSIVE_CHECKS_PY

# syntax_ts is runtime-dependent, handled separately
RUNTIME_CHECKS = [
    check_syntax_ts,
]

# Python syntax is passive (pure ast.parse), always runs
PASSIVE_SYNTAX_PY = _py_check_syntax


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SkillScanResult:
    """Result of scanning a single skill directory."""
    relative_path: str
    skill_md_hash: str
    status: str  # "scanned" | "scanned_degraded" | "failed_timeout" | "failed_error" | "skipped"
    disposition: str = "PASS"
    check_results: list[dict] = field(default_factory=list)
    findings_count: int = 0
    elapsed_s: float = 0.0
    error: str | None = None
    skipped_large_files: list[dict] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        """Flat list of all findings across all checks as typed Finding objects.

        Flattens check_results dicts into Finding instances, mapping the
        canonical dict keys (kind/detail/confidence) to Finding fields.
        This property is the compatibility bridge used by the cloud_scanner
        shim and all callers that expect ``result.findings``.
        """
        out: list[Finding] = []
        for cr in self.check_results:
            check_name = cr.get("name", "")
            for f in cr.get("findings", []):
                out.append(Finding(
                    check=check_name,
                    severity=f.get("confidence", f.get("severity", "medium")),
                    file=f.get("file", f.get("path", "")),
                    line=f.get("line"),
                    message=f.get("detail", f.get("message", "")),
                    kind=f.get("kind", check_name),
                    remediation=f.get("remediation"),
                ))
        return out


@dataclass
class ScanSummary:
    """Aggregate statistics for a corpus scan."""
    corpus_root: str
    total_candidates: int = 0
    total_scanned: int = 0
    total_scanned_degraded: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    total_findings: int = 0
    checks_run: list[str] = field(default_factory=list)
    checks_skipped: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

def discover_skills(corpus_root: Path) -> list[Path]:
    """Walk corpus_root and find directories containing SKILL.md.

    Excludes .git/, node_modules/, .venv/, etc.
    Returns list of skill directory paths, sorted.
    """
    skills = []
    for child in sorted(corpus_root.rglob("SKILL.md")):
        # Check exclusion rules
        rel = child.relative_to(corpus_root)
        parts = rel.parts
        if any(p in EXCLUDED_DIRS or p.startswith(".saturnday") for p in parts[:-1]):
            continue
        skills.append(child.parent)
    return skills


def _compute_skill_md_hash(skill_dir: Path) -> str:
    """SHA-256 hash of SKILL.md contents for identity/resumption."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    try:
        return hashlib.sha256(skill_md.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def _collect_skill_files(skill_dir: Path) -> list[str]:
    """Collect all scannable files in a skill directory."""
    files = []
    for child in sorted(skill_dir.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(skill_dir)
        parts = rel.parts
        if any(p in EXCLUDED_DIRS or p.startswith(".saturnday") for p in parts[:-1]):
            continue
        try:
            if child.stat().st_size > MAX_FILE_SIZE_BYTES:
                continue
        except OSError:
            continue
        files.append(str(rel))
    return files


# ---------------------------------------------------------------------------
# Remediation enrichment helper
# ---------------------------------------------------------------------------

def _enrich_check_results_with_guidance(check_results: list[dict]) -> None:
    """Attach remediation guidance to findings in-place where available.

    Iterates all check_result dicts and for each finding dict attempts to
    look up structured guidance keyed by ``kind``.  If
    ``saturnday.remediation_guidance`` is not installed the list is unchanged.

    Args:
        check_results: List of check result dicts (mutated in-place).
    """
    try:
        from saturnday.remediation_guidance import get_guidance  # type: ignore[import]
    except ImportError:
        return
    for cr in check_results:
        enriched: list[dict] = []
        for f in cr.get("findings", []):
            kind = f.get("kind", "")
            g = get_guidance(kind) if kind else None
            if g:
                f = dict(f, remediation={
                    "why": g.why_it_matters,
                    "fix": g.how_to_fix,
                    "patch": g.patch_template,
                })
            enriched.append(f)
        cr["findings"] = enriched


# ---------------------------------------------------------------------------
# Single skill scanning
# ---------------------------------------------------------------------------

def scan_skill(
    skill_dir: Path,
    *,
    policy: PolicyManifest | None = None,
    include_syntax: bool = False,
    timeout_s: int = DEFAULT_PER_SKILL_TIMEOUT_S,
    strict: bool = False,
) -> SkillScanResult:
    """Scan a single skill directory with the passive check subset.

    If strict=True, SKIPPED checks (e.g. missing Node) cause FAIL disposition.
    Returns a SkillScanResult with all check results.
    """
    t0 = time.time()
    rel_path = skill_dir.name  # will be overridden by caller for corpus context
    skill_md_hash = _compute_skill_md_hash(skill_dir)

    if not (skill_dir / "SKILL.md").exists():
        return SkillScanResult(
            relative_path=rel_path,
            skill_md_hash=skill_md_hash,
            status="skipped",
            error="no SKILL.md found",
            elapsed_s=time.time() - t0,
        )

    if policy is None:
        policy = default_policy()

    files = _collect_skill_files(skill_dir)
    check_results = []
    degraded = False
    timed_out = False

    # Build ordered list of checks to run
    checks_to_run: list[tuple[str, object]] = []
    for check_fn in PASSIVE_CHECKS:
        # Derive a short check name from the function name.
        # Patterns handled:
        #   check_secrets_ts      → secrets_ts
        #   _py_check_secrets     → secrets
        #   _oc_check_shell_danger → shell_danger
        fn_name = check_fn.__name__
        if fn_name.startswith("_py_check_"):
            check_name = fn_name[len("_py_check_"):]
        elif fn_name.startswith("_oc_check_"):
            check_name = fn_name[len("_oc_check_"):]
        else:
            check_name = fn_name.replace("check_", "")
        if should_run_check(policy, check_name):
            checks_to_run.append((check_name, check_fn))
    # Python syntax — always runs (pure ast.parse, no runtime dependency)
    checks_to_run.append(("syntax_py", PASSIVE_SYNTAX_PY))
    # TS syntax — only when include_syntax (requires Node runtime)
    if include_syntax and should_run_check(policy, "syntax_ts"):
        checks_to_run.append(("syntax_ts", check_syntax_ts))

    # Checks that accept timeout_s keyword argument
    _TIMEOUT_AWARE_CHECKS = {
        "hallucinated_imports_ts",
        "typosquat_ts",
        "syntax_ts",
    }

    # Run checks with inter-check timeout enforcement
    for idx, (check_name, check_fn) in enumerate(checks_to_run):
        elapsed = time.time() - t0
        remaining = timeout_s - elapsed

        # Timeout enforcement: check elapsed before each check
        if remaining <= 0:
            timed_out = True
            # Record remaining checks as SKIPPED due to timeout
            for remaining_name, _ in checks_to_run[idx:]:
                check_results.append({
                    "name": remaining_name,
                    "status": "SKIPPED",
                    "findings": [{
                        "kind": "timeout_skip",
                        "detail": f"Check {remaining_name} skipped — per-skill budget exhausted after {elapsed:.1f}s",
                    }],
                    "exit_code": 0,
                    "raw_output": "",
                    "error": "timeout",
                })
            break

        try:
            # Pass remaining budget to checks that do network/subprocess I/O
            if check_name in _TIMEOUT_AWARE_CHECKS:
                result = check_fn(skill_dir, files, timeout_s=remaining)
            else:
                result = check_fn(skill_dir, files)
            check_results.append(result)
            if result.get("status") == "SKIPPED":
                degraded = True
        except Exception as exc:
            check_results.append({
                "name": check_name,
                "status": "SKIPPED",
                "findings": [],
                "exit_code": 0,
                "raw_output": "",
                "error": str(exc),
            })
            degraded = True

    # Convert to CheckResults and compute disposition
    evidence_results = []
    for cr in check_results:
        severity = get_check_severity(policy, cr["name"])
        evidence_results.append(CheckResult(
            name=cr["name"],
            status=cr["status"],
            severity=severity,
            findings=cr.get("findings", []),
            elapsed_s=0.0,
            error=cr.get("error"),
        ))

    disposition, _ = compute_disposition(evidence_results)

    # In strict mode, SKIPPED checks (missing runtime) become FAIL
    if strict:
        for cr in check_results:
            if cr["status"] == "SKIPPED":
                disposition = "FAIL"
                break

    # Remediation enrichment — attach structured guidance where available.
    _enrich_check_results_with_guidance(check_results)

    total_findings = sum(len(cr.get("findings", [])) for cr in check_results)
    elapsed = time.time() - t0

    if timed_out:
        status = "failed_timeout"
    elif degraded:
        status = "scanned_degraded"
    else:
        status = "scanned"

    return SkillScanResult(
        relative_path=rel_path,
        skill_md_hash=skill_md_hash,
        status=status,
        disposition=disposition,
        check_results=check_results,
        findings_count=total_findings,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Corpus scanning
# ---------------------------------------------------------------------------

def _load_existing_results(output_dir: Path) -> dict[str, str]:
    """Read already-scanned skill identities from findings.jsonl.

    Returns dict of {relative_path: skill_md_hash} for resume support.
    """
    jsonl_path = output_dir / "findings.jsonl"
    if not jsonl_path.exists():
        return {}
    existing: dict[str, str] = {}
    try:
        for line in jsonl_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                rp = entry.get("relative_path", "")
                h = entry.get("skill_md_hash", "")
                if rp:
                    existing[rp] = h
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return existing


def scan_corpus(
    corpus_root: Path,
    output_dir: Path,
    *,
    policy: PolicyManifest | None = None,
    include_syntax: bool = False,
    strict: bool = False,
    timeout_s: int = DEFAULT_PER_SKILL_TIMEOUT_S,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
    limit: int | None = None,
    top_n: int | None = None,
    fmt: str = "both",
    progress_callback=None,
) -> ScanSummary:
    """Scan an entire skill corpus.

    Output controlled by fmt parameter:
      fmt="both"     — findings.jsonl (always), summary.md, report.json
      fmt="markdown" — findings.jsonl (always), summary.md
      fmt="json"     — findings.jsonl (always), report.json
    findings.jsonl is always written as the incremental raw data store.
    Optional top-n.md appendix written when top_n is set and fmt includes markdown.

    Supports resumption: if output_dir/findings.jsonl exists, already-scanned
    skills (matching path + hash) are skipped.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover skills
    skills = discover_skills(corpus_root)
    if limit is not None:
        skills = skills[:limit]

    # Resume support
    existing = _load_existing_results(output_dir)
    jsonl_path = output_dir / "findings.jsonl"

    summary = ScanSummary(corpus_root=str(corpus_root), total_candidates=len(skills))
    t_start = time.time()

    for i, skill_dir in enumerate(skills):
        rel_path = str(skill_dir.relative_to(corpus_root))
        skill_md_hash = _compute_skill_md_hash(skill_dir)

        # Skip if already scanned with same hash
        if rel_path in existing and existing[rel_path] == skill_md_hash:
            summary.total_skipped += 1
            continue

        # Progress logging
        if progress_callback and (i + 1) % progress_interval == 0:
            progress_callback(
                f"[scan] {i + 1}/{len(skills)} skills processed, "
                f"{summary.total_failed} failed, "
                f"{summary.total_findings} findings"
            )

        # Scan with timeout and error handling
        try:
            result = scan_skill(
                skill_dir,
                policy=policy,
                include_syntax=include_syntax,
                strict=strict,
                timeout_s=timeout_s,
            )
            result.relative_path = rel_path
        except Exception as exc:
            result = SkillScanResult(
                relative_path=rel_path,
                skill_md_hash=skill_md_hash,
                status="failed_error",
                error=str(exc),
            )

        # Update summary stats
        if result.status == "scanned":
            summary.total_scanned += 1
        elif result.status == "scanned_degraded":
            summary.total_scanned_degraded += 1
        elif result.status.startswith("failed"):
            summary.total_failed += 1
        else:
            summary.total_skipped += 1

        summary.total_findings += result.findings_count

        # Write to JSONL incrementally
        entry = {
            "relative_path": result.relative_path,
            "skill_md_hash": result.skill_md_hash,
            "status": result.status,
            "disposition": result.disposition,
            "findings_count": result.findings_count,
            "elapsed_s": round(result.elapsed_s, 3),
            "error": result.error,
            "checks": result.check_results,
        }
        with open(jsonl_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    summary.elapsed_s = time.time() - t_start

    # Determine which checks ran/skipped
    check_names = set()
    skipped_checks = set()
    for line in jsonl_path.read_text().splitlines() if jsonl_path.exists() else []:
        try:
            entry = json.loads(line)
            for cr in entry.get("checks", []):
                name = cr.get("name", "")
                if cr.get("status") == "SKIPPED":
                    skipped_checks.add(name)
                else:
                    check_names.add(name)
        except Exception:
            continue
    summary.checks_run = sorted(check_names)
    summary.checks_skipped = sorted(skipped_checks - check_names)

    # Write outputs based on format
    if fmt in ("both", "markdown"):
        _write_summary_md(output_dir, summary, jsonl_path, top_n)
    if fmt in ("both", "json"):
        _write_report_json(output_dir, summary, jsonl_path)

    return summary


def _write_summary_md(
    output_dir: Path,
    summary: ScanSummary,
    jsonl_path: Path,
    top_n: int | None,
) -> None:
    """Write compact human-readable summary (~200 lines max)."""
    lines = [
        "# Saturnday OpenClaw Scan Summary",
        "",
        f"**Corpus:** `{summary.corpus_root}`",
        f"**Elapsed:** {summary.elapsed_s:.1f}s",
        "",
        "## Statistics",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total candidates | {summary.total_candidates} |",
        f"| Scanned successfully | {summary.total_scanned} |",
        f"| Scanned (degraded) | {summary.total_scanned_degraded} |",
        f"| Failed | {summary.total_failed} |",
        f"| Skipped | {summary.total_skipped} |",
        f"| Total findings | {summary.total_findings} |",
        "",
    ]

    if summary.checks_run:
        lines.append("## Checks Run")
        lines.append("")
        for c in summary.checks_run:
            lines.append(f"- {c}")
        lines.append("")

    if summary.checks_skipped:
        lines.append("## Checks Skipped")
        lines.append("")
        for c in summary.checks_skipped:
            lines.append(f"- {c}")
        lines.append("")

    # Aggregate findings by check
    check_counts: dict[str, int] = {}
    skill_findings: list[tuple[int, str, list[dict]]] = []
    if jsonl_path.exists():
        for line_text in jsonl_path.read_text().splitlines():
            try:
                entry = json.loads(line_text)
                all_findings = []
                for cr in entry.get("checks", []):
                    findings = cr.get("findings", [])
                    name = cr.get("name", "unknown")
                    check_counts[name] = check_counts.get(name, 0) + len(findings)
                    all_findings.extend(findings)
                if all_findings:
                    skill_findings.append((
                        len(all_findings),
                        entry.get("relative_path", "?"),
                        all_findings,
                    ))
            except Exception:
                continue

    if check_counts:
        lines.append("## Findings by Check")
        lines.append("")
        lines.append("| Check | Findings |")
        lines.append("|-------|----------|")
        for name in sorted(check_counts, key=lambda k: -check_counts[k]):
            lines.append(f"| {name} | {check_counts[name]} |")
        lines.append("")

    # Top skills by finding count
    skill_findings.sort(reverse=True)
    top_skills = skill_findings[:20]
    if top_skills:
        lines.append("## Top Skills by Finding Count")
        lines.append("")
        lines.append("| Skill | Findings |")
        lines.append("|-------|----------|")
        for count, path, _ in top_skills:
            lines.append(f"| `{path}` | {count} |")
        lines.append("")

    (output_dir / "summary.md").write_text("\n".join(lines) + "\n")

    # Optional top-n detailed appendix
    if top_n and skill_findings:
        _write_top_n_md(output_dir, skill_findings[:top_n])


def _write_top_n_md(
    output_dir: Path,
    top_skills: list[tuple[int, str, list[dict]]],
) -> None:
    """Write detailed findings for top-N worst skills."""
    lines = ["# Detailed Findings — Top Skills", ""]
    for count, path, findings in top_skills:
        lines.append(f"## `{path}` ({count} findings)")
        lines.append("")
        # Cap at MAX_SUMMARY_FINDINGS_PER_SKILL
        shown = findings[:MAX_SUMMARY_FINDINGS_PER_SKILL]
        for f in shown:
            kind = f.get("kind", "unknown")
            detail = f.get("detail", "")
            conf = f.get("confidence", "")
            conf_str = f" [{conf}]" if conf else ""
            lines.append(f"- **{kind}**{conf_str}: {detail}")
        if len(findings) > MAX_SUMMARY_FINDINGS_PER_SKILL:
            omitted = len(findings) - MAX_SUMMARY_FINDINGS_PER_SKILL
            lines.append(f"- *{omitted} additional findings omitted, see JSONL for full detail*")
        lines.append("")

    (output_dir / "top-n.md").write_text("\n".join(lines) + "\n")


def _write_report_json(
    output_dir: Path,
    summary: ScanSummary,
    jsonl_path: Path,
) -> None:
    """Write stable aggregate JSON contract for pipeline consumers.

    Fields may be added but existing fields must not be removed or renamed.
    """
    # Aggregate findings from JSONL
    findings_by_severity: dict[str, int] = {}
    findings_by_category: dict[str, int] = {}
    skills_data: list[dict] = []

    if jsonl_path.exists():
        for line_text in jsonl_path.read_text().splitlines():
            if not line_text.strip():
                continue
            try:
                entry = json.loads(line_text)
            except json.JSONDecodeError:
                continue
            skill_findings: list[dict] = []
            for cr in entry.get("checks", []):
                check_name = cr.get("name", "unknown")
                for f in cr.get("findings", []):
                    kind = f.get("kind", check_name)
                    findings_by_category[kind] = findings_by_category.get(kind, 0) + 1
                    sev = f.get("confidence", "medium")
                    findings_by_severity[sev] = findings_by_severity.get(sev, 0) + 1
                    skill_findings.append({
                        "check": check_name,
                        "severity": sev,
                        "file": f.get("file", ""),
                        "line": f.get("line"),
                        "message": f.get("detail", ""),
                        "suggested_fix": None,
                    })
            skills_data.append({
                "skill_path": entry.get("relative_path", ""),
                "disposition": entry.get("disposition", "PASS"),
                "findings": skill_findings,
            })

    # Compute overall disposition
    dispositions = [s["disposition"] for s in skills_data]
    if "FAIL" in dispositions:
        overall = "FAIL"
    elif "WARN" in dispositions:
        overall = "WARN"
    else:
        overall = "PASS"

    report = {
        "saturnday_version": __version__,
        "scan_timestamp": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "summary": {
            "total_skills": summary.total_candidates,
            "total_findings": summary.total_findings,
            "disposition": overall,
            "findings_by_severity": findings_by_severity,
            "findings_by_category": findings_by_category,
        },
        "skills": skills_data,
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def write_single_skill_output(
    output_dir: Path,
    result: SkillScanResult,
    *,
    fmt: str = "both",
) -> None:
    """Write output for a single-skill scan, respecting format parameter.

    findings.jsonl is always written as the raw data store.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "relative_path": result.relative_path,
        "skill_md_hash": result.skill_md_hash,
        "status": result.status,
        "disposition": result.disposition,
        "findings_count": result.findings_count,
        "elapsed_s": round(result.elapsed_s, 3),
        "error": result.error,
        "checks": result.check_results,
    }
    # JSONL is always written
    (output_dir / "findings.jsonl").write_text(json.dumps(entry) + "\n")

    if fmt in ("both", "json"):
        # Stable JSON contract
        skill_findings: list[dict] = []
        for cr in result.check_results:
            for f in cr.get("findings", []):
                skill_findings.append({
                    "check": cr.get("name", "unknown"),
                    "severity": f.get("confidence", "medium"),
                    "file": f.get("file", ""),
                    "line": f.get("line"),
                    "message": f.get("detail", ""),
                    "suggested_fix": None,
                })
        report = {
            "saturnday_version": __version__,
            "scan_timestamp": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
            "summary": {
                "total_skills": 1,
                "total_findings": result.findings_count,
                "disposition": result.disposition,
                "findings_by_severity": {},
                "findings_by_category": {},
            },
            "skills": [{
                "skill_path": result.relative_path,
                "disposition": result.disposition,
                "findings": skill_findings,
            }],
        }
        (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    if fmt in ("both", "markdown"):
        lines = [
            "# Saturnday Skill Scan",
            "",
            f"**Skill:** `{result.relative_path}`",
            f"**Disposition:** {result.disposition}",
            f"**Findings:** {result.findings_count}",
            "",
        ]
        for cr in result.check_results:
            findings = cr.get("findings", [])
            status = cr.get("status", "PASS")
            lines.append(f"## {cr.get('name', 'unknown')} — {status}")
            if findings:
                for f in findings[:MAX_SUMMARY_FINDINGS_PER_SKILL]:
                    lines.append(f"- {f.get('detail', '')}")
                if len(findings) > MAX_SUMMARY_FINDINGS_PER_SKILL:
                    lines.append(f"- *{len(findings) - MAX_SUMMARY_FINDINGS_PER_SKILL} more omitted*")
            lines.append("")
        (output_dir / "summary.md").write_text("\n".join(lines) + "\n")
