"""Deterministic and LLM-assisted impact analysis for Saturnday (v1.1.01).

Phase 3 of the governed memory system (T012 + T013).

T012: compute_impact — determines blast radius from changed files using import
graph scanning, symbol reference scanning, test linkage, config detection, and
architecture boundary detection.

T013: explain_impact_with_llm — uses the code_reviewer role to explain what the
deterministic report might have missed. WARNING-only; never blocks the pipeline.

Design constraints (from execution plan):
- No imports from saturnday.governance — this is a separate concern.
- Uses project_state.py AST data + subprocess grep for import scanning.
- Cap at 50 dependents to avoid explosion in large repos.
- All public functions are guarded; failures return empty/safe values.
- No new external dependencies.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saturnday._types import CoderConfig, TicketSpec
    from saturnday.project_state import ProjectState

# Imported at module level so tests can patch saturnday.run.impact_analysis.invoke_role.
# The try/except handles environments where role_modes is unavailable.
try:
    from saturnday.role_modes import invoke_role
except ImportError:
    invoke_role = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "ImpactCandidate",
    "ImpactReport",
    "compute_impact",
    "format_impact_for_prompt",
    "explain_impact_with_llm",
    "select_verification_layers",
]

# Config/build/deploy file names and patterns
_CONFIG_NAMES: frozenset[str] = frozenset({
    "pyproject.toml",
    "package.json",
    "Dockerfile",
    "Makefile",
    "tsconfig.json",
    "setup.py",
    "setup.cfg",
})
_CONFIG_PATTERNS: tuple[str, ...] = (
    "docker-compose",
    "requirements",
    ".env",
)
_CONFIG_PARENT_DIRS: frozenset[str] = frozenset({".github/workflows"})

# Maximum import dependents to avoid explosion in large repos (plan constraint S-5)
_MAX_DEPENDENTS = 50


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ImpactCandidate:
    """A single file that may be affected by the current change.

    Attributes:
        file: Repository-relative path of the potentially affected file.
        impact_type: Category of impact — one of ``import_dependency``,
            ``symbol_reference``, ``test_coverage``, ``config``, ``build``,
            ``deploy``, ``policy``.
        detail: Human-readable reason this file is included.
        risk_level: Estimated risk — ``high``, ``medium``, or ``low``.
    """

    file: str
    impact_type: str  # import_dependency | symbol_reference | test_coverage | config | build | deploy | policy
    detail: str
    risk_level: str   # high | medium | low


@dataclass
class ImpactReport:
    """Blast-radius report produced by :func:`compute_impact`.

    Attributes:
        touched_files: Files explicitly changed by the current ticket.
        import_dependents: Other files that import from touched files.
        symbol_references: Files that reference exported symbols from touched files.
        linked_tests: Test files linked to changed source modules.
        config_impacts: Config, build, or deploy files in the change set.
        architecture_crossings: Top-level package boundaries crossed.
        total_blast_radius: Count of all potentially affected files.
    """

    touched_files: list[str] = field(default_factory=list)
    import_dependents: list[ImpactCandidate] = field(default_factory=list)
    symbol_references: list[ImpactCandidate] = field(default_factory=list)
    linked_tests: list[ImpactCandidate] = field(default_factory=list)
    config_impacts: list[ImpactCandidate] = field(default_factory=list)
    architecture_crossings: list[str] = field(default_factory=list)
    total_blast_radius: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _module_import_name(rel_path: str) -> str:
    """Convert a repository-relative path to its dotted import name.

    Examples::

        src/saturnday/run/foo.py -> saturnday.run.foo
        saturnday/bar.py         -> saturnday.bar
        foo.py                   -> foo

    Strips a leading ``src/`` segment (common layout convention) so that
    ``src/saturnday/foo.py`` resolves as ``saturnday.foo``.
    """
    p = Path(rel_path)
    parts = list(p.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _grep_import_references(
    repo_path: Path,
    module_name: str,
    changed_files_set: frozenset[str],
) -> list[str]:
    """Return relative paths of repo files that import ``module_name``.

    Uses subprocess grep with ``-rl`` for speed.  Caps at
    ``_MAX_DEPENDENTS`` results.  Never raises.
    """
    if not module_name:
        return []
    # Build patterns for both import styles
    patterns = [
        f"from {module_name} import",
        f"import {module_name}",
    ]
    # Also match partial module paths (e.g. "from saturnday.run.foo import" matches "saturnday.run.foo")
    results: set[str] = set()
    try:
        for pattern in patterns:
            proc = subprocess.run(
                ["grep", "-rl", "--include=*.py", pattern, str(repo_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            for line in proc.stdout.splitlines():
                try:
                    rel = str(Path(line).relative_to(repo_path))
                except ValueError:
                    rel = line.strip()
                if rel not in changed_files_set and rel.endswith(".py"):
                    results.add(rel)
                    if len(results) >= _MAX_DEPENDENTS:
                        return sorted(results)
    except Exception as exc:
        logger.debug("grep import scan failed for %s: %s", module_name, exc)
    return sorted(results)


def _extract_exported_symbols(repo_path: Path, rel_path: str) -> list[str]:
    """Extract top-level public function and class names from a Python file.

    Falls back to empty list on any parse error.
    """
    symbols: list[str] = []
    if not rel_path.endswith(".py"):
        return symbols
    full_path = repo_path / rel_path
    if not full_path.exists():
        return symbols
    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=rel_path)
    except Exception:
        return symbols

    # Check for __all__ first
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                symbols.append(elt.value)
                    return symbols  # __all__ is definitive

    # No __all__ — collect top-level public defs
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                symbols.append(node.name)
    return symbols


def _grep_symbol_references(
    repo_path: Path,
    symbol: str,
    changed_files_set: frozenset[str],
) -> list[str]:
    """Return relative paths of repo files that reference ``symbol``.

    Uses a whole-word grep pattern to avoid partial matches.  Caps at 20
    results per symbol.
    """
    if not symbol or len(symbol) < 3:  # noqa: PLR2004 — skip very short names
        return []
    results: set[str] = set()
    try:
        proc = subprocess.run(
            ["grep", "-rl", "--include=*.py", "-w", symbol, str(repo_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        for line in proc.stdout.splitlines():
            try:
                rel = str(Path(line).relative_to(repo_path))
            except ValueError:
                rel = line.strip()
            if rel not in changed_files_set and rel.endswith(".py"):
                results.add(rel)
                if len(results) >= 20:  # noqa: PLR2004
                    break
    except Exception as exc:
        logger.debug("grep symbol scan failed for %s: %s", symbol, exc)
    return sorted(results)


def _is_config_file(rel_path: str) -> tuple[bool, str]:
    """Check if a path is a config/build/deploy file.

    Returns ``(is_config, impact_type)`` where ``impact_type`` is one of
    ``config``, ``build``, ``deploy``, or ``policy``.
    """
    name = Path(rel_path).name
    parts = Path(rel_path).parts

    if name == ".saturnday-policy.yaml" or name.startswith(".saturnday-policy"):
        return True, "policy"

    if name in _CONFIG_NAMES:
        if "Dockerfile" in name or "docker-compose" in name:
            return True, "deploy"
        return True, "build"

    for pattern in _CONFIG_PATTERNS:
        if pattern in name:
            if "env" in pattern:
                return True, "config"
            return True, "build"

    # .github/workflows/ counts as deploy
    path_str = rel_path.replace("\\", "/")
    for parent_dir in _CONFIG_PARENT_DIRS:
        if path_str.startswith(parent_dir):
            return True, "deploy"

    # *.yml / *.yaml in root or .github
    if name.endswith((".yml", ".yaml")) and (len(parts) <= 2 or ".github" in parts):  # noqa: PLR2004
        return True, "deploy"

    return False, ""


def _top_level_package(rel_path: str) -> str:
    """Return the top-level directory component of a path, or the filename."""
    parts = Path(rel_path).parts
    return parts[0] if len(parts) > 1 else rel_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_impact(
    changed_files: list[str],
    repo_path: Path,
    state: "ProjectState | None",
) -> ImpactReport:
    """Compute blast radius from a list of changed files.

    Steps:
    1. Import dependency scan — other files that import changed modules.
    2. Symbol reference scan — files referencing exported symbols.
    3. Test linkage — test files for changed source modules.
    4. Config/build/deploy detection — policy and infra files in change set.
    5. Architecture boundary crossings — top-level directory boundaries.

    Args:
        changed_files: Repository-relative paths of files that were changed.
        repo_path: Root of the repository.
        state: Optional :class:`~saturnday.project_state.ProjectState` with
            pre-computed AST data.  ``None`` is safe — all analysis falls back
            to direct file scanning.

    Returns:
        :class:`ImpactReport` with all detected impact candidates.
    """
    if not changed_files:
        return ImpactReport(total_blast_radius=0)

    changed_files_set = frozenset(changed_files)
    report = ImpactReport(touched_files=list(changed_files))
    affected_files: set[str] = set()

    # -- Step 4 first: config/policy detection (cheap, no I/O) ---------------
    for rel_path in changed_files:
        is_cfg, cfg_type = _is_config_file(rel_path)
        if is_cfg:
            risk = "high" if cfg_type in ("deploy", "policy") else "medium"
            report.config_impacts.append(ImpactCandidate(
                file=rel_path,
                impact_type=cfg_type,
                detail=f"{Path(rel_path).name} is a {cfg_type} file",
                risk_level=risk,
            ))

    # -- Step 1: import dependency scan ---------------------------------------
    python_changed = [f for f in changed_files if f.endswith(".py")]
    for rel_path in python_changed:
        module_name = _module_import_name(rel_path)
        dependents = _grep_import_references(repo_path, module_name, changed_files_set)
        for dep in dependents:
            is_test = Path(dep).name.startswith("test_") or dep.endswith("_test.py")
            risk = "high" if is_test else "medium"
            candidate = ImpactCandidate(
                file=dep,
                impact_type="import_dependency",
                detail=f"imports {module_name}",
                risk_level=risk,
            )
            report.import_dependents.append(candidate)
            affected_files.add(dep)

    # -- Step 2: symbol reference scan ----------------------------------------
    for rel_path in python_changed:
        symbols = _extract_exported_symbols(repo_path, rel_path)
        # Use state.modules if available for richer symbol list
        if state is not None and rel_path in state.modules:
            ms = state.modules[rel_path]
            symbols = list(set(symbols) | set(ms.functions.keys()) | set(ms.classes.keys()))
        for symbol in symbols:
            refs = _grep_symbol_references(repo_path, symbol, changed_files_set)
            for ref in refs:
                if ref not in affected_files:
                    report.symbol_references.append(ImpactCandidate(
                        file=ref,
                        impact_type="symbol_reference",
                        detail=f"references symbol '{symbol}' from {Path(rel_path).name}",
                        risk_level="low",
                    ))
                    affected_files.add(ref)

    # -- Step 3: test linkage -------------------------------------------------
    for rel_path in python_changed:
        stem = Path(rel_path).stem
        # Convention: src/foo/bar.py -> tests/test_bar.py or tests/foo/test_bar.py
        candidate_test_paths = [
            f"tests/test_{stem}.py",
            f"test_{stem}.py",
        ]
        for test_path in candidate_test_paths:
            full_test = repo_path / test_path
            if full_test.exists():
                if test_path not in changed_files_set:
                    # Test file exists but was NOT in the change set
                    report.linked_tests.append(ImpactCandidate(
                        file=test_path,
                        impact_type="test_coverage",
                        detail=f"test file for {Path(rel_path).name} was NOT updated",
                        risk_level="medium",
                    ))
                    affected_files.add(test_path)
                break  # Only add once per source file

    # -- Step 5: architecture boundary crossings ------------------------------
    top_level_dirs: set[str] = {_top_level_package(f) for f in changed_files}
    if len(top_level_dirs) > 1:
        sorted_dirs = sorted(top_level_dirs)
        report.architecture_crossings = [
            f"Change spans {len(sorted_dirs)} top-level boundaries: {', '.join(sorted_dirs)}"
        ]

    # -- Compute total blast radius -------------------------------------------
    all_affected: set[str] = (
        {c.file for c in report.import_dependents}
        | {c.file for c in report.symbol_references}
        | {c.file for c in report.linked_tests}
        | {c.file for c in report.config_impacts}
    )
    report.total_blast_radius = len(all_affected)

    return report


def format_impact_for_prompt(report: ImpactReport) -> str:
    """Render an :class:`ImpactReport` as compact structured text for prompt injection.

    Output is bounded to ~500–1000 chars.  High-risk candidates appear first.
    An empty report returns an empty string.

    Args:
        report: The deterministic impact report to format.

    Returns:
        Structured text block suitable for injection into a coder prompt, or
        empty string if there is nothing noteworthy.
    """
    if not report.touched_files and report.total_blast_radius == 0:
        return ""

    lines: list[str] = [
        f"IMPACT CONTEXT: {report.total_blast_radius} file(s) potentially affected",
    ]

    if report.architecture_crossings:
        for crossing in report.architecture_crossings:
            lines.append(f"  [WARNING] {crossing}")

    # High-risk first
    all_candidates: list[ImpactCandidate] = (
        [c for c in report.config_impacts if c.risk_level == "high"]
        + [c for c in report.import_dependents if c.risk_level == "high"]
        + [c for c in report.linked_tests if c.risk_level in ("high", "medium")]
        + [c for c in report.config_impacts if c.risk_level == "medium"]
        + [c for c in report.import_dependents if c.risk_level == "medium"]
        + [c for c in report.symbol_references if c.risk_level in ("high", "medium")]
        + [c for c in report.import_dependents if c.risk_level == "low"]
        + [c for c in report.symbol_references if c.risk_level == "low"]
    )

    shown = 0
    for candidate in all_candidates:
        if shown >= 10:  # noqa: PLR2004 — cap to keep prompt size bounded
            remaining = len(all_candidates) - shown
            if remaining > 0:
                lines.append(f"  ... and {remaining} more")
            break
        risk_tag = f"[{candidate.risk_level.upper()}]"
        lines.append(f"  {risk_tag} {candidate.file}: {candidate.detail}")
        shown += 1

    return "\n".join(lines)


def explain_impact_with_llm(
    report: ImpactReport,
    ticket: "TicketSpec",
    coder_config: "CoderConfig",
    repo_path: Path,
) -> str:
    """Use the code_reviewer role to explain the deterministic impact report.

    WARNING-only; never blocks the pipeline.  Only runs when blast_radius > 3.
    Returns an explanation string, or empty string on any error.

    Args:
        report: Deterministic blast-radius report from :func:`compute_impact`.
        ticket: The ticket being executed.
        coder_config: Coder backend configuration.
        repo_path: Repository root.

    Returns:
        LLM explanation (~200–500 chars), or empty string on failure/skip.
    """
    if report.total_blast_radius <= 3:  # noqa: PLR2004 — trivial change, skip
        return ""

    try:
        if invoke_role is None:
            return ""
        impact_text = format_impact_for_prompt(report)
        task = (
            f"## Ticket Goal\n{ticket.goal}\n\n"
            f"## Deterministic Impact Report\n{impact_text}\n\n"
            "## Question\n"
            "Given these deterministic impact candidates, explain briefly:\n"
            "1. What could actually break that the scan may have missed?\n"
            "2. Are there affected tests not found by import scanning?\n"
            "3. Are there architecture boundary concerns?\n"
            "4. Is any active rule implicated?\n"
            "Keep the response under 500 chars. This is advisory only."
        )
        result = invoke_role(
            "code_reviewer",
            task,
            coder_config=coder_config,
            repo_path=repo_path,
        )
        if result.success and result.output:
            return result.output.strip()[:500]
    except Exception as exc:
        logger.debug("LLM impact explanation failed: %s", exc)

    return ""


# ---------------------------------------------------------------------------
# T024: Execution selector — decides which verification layers should run
# ---------------------------------------------------------------------------

_TEST_PREFIXES: frozenset[str] = frozenset({"tests/", "test/", "spec/"})


def _selector_is_test_file(path: str) -> bool:
    """Return True when *path* lives under a test directory."""
    return any(path.startswith(pfx) for pfx in _TEST_PREFIXES) or "/test" in path


def select_verification_layers(
    report: "ImpactReport",
    ticket: "TicketSpec",
) -> dict:
    """Decide which verification layers should run for this ticket.

    Returns a dict keyed by layer name, value is True (run) or False (skip).
    All layers default to True; expensive layers are skipped for trivial or
    non-code changes to keep pipeline overhead bounded.

    Reuses the existing :func:`_is_config_file` helper (which returns a
    ``(bool, str)`` tuple) to classify touched files.

    Args:
        report: Impact report produced by :func:`compute_impact`.
        ticket: The ticket being executed.

    Returns:
        Dict with keys: ``spec_assertions``, ``property_tests``,
        ``dataflow_check``, ``llm_spec_inference``, ``llm_review``.
    """
    changed = list(report.touched_files)
    blast = report.total_blast_radius

    # _is_config_file returns (bool, impact_type) — extract the boolean
    all_config = bool(changed) and all(_is_config_file(f)[0] for f in changed)
    all_tests = bool(changed) and all(_selector_is_test_file(f) for f in changed)
    any_config = any(_is_config_file(f)[0] for f in changed)
    high_risk = any(
        c.risk_level == "high"
        for c in (
            list(report.import_dependents)
            + list(report.symbol_references)
            + list(report.config_impacts)
        )
    )
    explicit_assertions = bool(ticket.acceptance_criteria)

    return {
        # spec_assertions: always run unless the change is config/deploy only
        "spec_assertions": not all_config,
        # property_tests: skip for trivial (blast=0) or config-only changes
        "property_tests": blast > 2 or any_config,  # noqa: PLR2004
        # dataflow_check: skip for trivial changes and pure test changes
        "dataflow_check": blast > 3 and not all_tests,  # noqa: PLR2004
        # llm_spec_inference: only run for high blast with no explicit assertions
        "llm_spec_inference": blast > 5 and not explicit_assertions,  # noqa: PLR2004
        # llm_review: run when blast is meaningful or any high-risk impact
        "llm_review": blast > 3 or high_risk,  # noqa: PLR2004
    }
