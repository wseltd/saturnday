"""Load, validate, and dependency-sort project plans.

Accepts the saturnday-v3 full plan format — including ``definition_of_done``,
``stop_conditions``, ``max_project_tickets``, ``phases``, and ``mode`` — and
produces a typed ``ProjectPlan`` with tickets in topological order.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from saturnday._exceptions import PlanValidationError
from saturnday._types import (
    DECLARED_OPERATING_MODES,
    DEPENDENCY_PROFILES,
    OPERATING_MODES,
    PROOF_REALISMS,
    TESTING_STRATEGIES,
    ClarificationEntry,
    PhaseDef,
    ProjectPlan,
    TicketScope,
    TicketSpec,
)

KNOWN_DEFINITION_MARKERS = frozenset({"tickets_applied", "all_tickets_passed"})

logger = logging.getLogger(__name__)


def _parse_string_tuple(val: Any) -> tuple[str, ...]:
    """Safely parse a JSON value into a tuple of strings.

    Args:
        val: A raw JSON value that should be a list of strings.

    Returns:
        A tuple containing only the string elements from ``val``, or an empty
        tuple when ``val`` is not a list or contains no strings.
    """
    if isinstance(val, list):
        return tuple(str(s) for s in val if isinstance(s, str))
    return ()


def load_plan(path: str | Path) -> ProjectPlan:
    """Load a project plan from a JSON file.

    Args:
        path: Filesystem path to the plan JSON.

    Returns:
        A validated ``ProjectPlan`` with tickets in dependency order.

    Raises:
        PlanValidationError: If the plan is invalid.
        FileNotFoundError: If the plan file does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Plan file not found: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanValidationError([f"Invalid JSON: {exc}"]) from exc

    if not isinstance(raw, dict):
        raise PlanValidationError(["Plan must be a JSON object"])

    errors = validate_plan(raw)
    if errors:
        raise PlanValidationError(errors)

    tickets = _parse_tickets(raw.get("tickets", []))
    sorted_tickets = resolve_ticket_order(tickets)

    # Parse v3 plan-level fields
    dod = raw.get("definition_of_done")
    if isinstance(dod, list):
        dod = tuple(str(m) for m in dod if isinstance(m, str))
    else:
        dod = ("all_tickets_passed",)

    stop_conds = raw.get("stop_conditions")
    if isinstance(stop_conds, list):
        stop_conds = tuple(str(s) for s in stop_conds if isinstance(s, str))
    else:
        stop_conds = ()

    max_pt = raw.get("max_project_tickets")
    if not isinstance(max_pt, int) or max_pt < 1:
        max_pt = None

    phases = _parse_phases(raw.get("phases", []))
    mode = raw.get("mode", "generation")
    if mode not in ("generation", "remediation"):
        mode = "generation"

    return ProjectPlan(
        version=raw.get("version", 1),
        project_id=raw.get("project_id", "unknown"),
        notes=raw.get("notes", ""),
        tickets=tuple(sorted_tickets),
        definition_of_done=dod,
        stop_conditions=stop_conds,
        max_project_tickets=max_pt,
        phases=phases,
        mode=mode,
        default_timeout_seconds=raw.get("default_timeout_seconds", 300),
        default_retry_limit=raw.get("default_retry_limit", 2),
        # Plan-governance fields — backward compatible: old plans without these
        # fields load with empty defaults, identical to pre-PG behaviour.
        governing_goal=raw.get("governing_goal", raw.get("notes", "")),
        required_outcomes=_parse_string_tuple(raw.get("required_outcomes")),
        scoped_categories=_parse_string_tuple(raw.get("scoped_categories")),
        exclusions=_parse_string_tuple(raw.get("exclusions")),
        constraints=_parse_string_tuple(raw.get("constraints")),
        proof_expectations=_parse_string_tuple(raw.get("proof_expectations")),
        # Fix 41: plan-level final acceptance command (backward compatible).
        acceptance_cmd=raw.get("acceptance_cmd", ""),
        # Fix 52.a: setup steps for realistic product-mode acceptance.
        acceptance_setup=_parse_string_tuple(raw.get("acceptance_setup")),
        # Fix 56: authoritative runnable-product flag.
        is_runnable_product=bool(raw.get("is_runnable_product", False)),
        # ---- Fix 76 ----
        operating_mode=_parse_operating_mode(raw),
        dependency_profile=_parse_dependency_profile(raw),
        proof_realism=_parse_proof_realism(raw),
        external_dependencies=_parse_string_tuple(raw.get("external_dependencies")),
        local_proof_cmd=str(raw.get("local_proof_cmd", "") or ""),
        live_proof_cmd=str(raw.get("live_proof_cmd", "") or ""),
        demo_seed_source=str(raw.get("demo_seed_source", "") or ""),
        operator_disclaimer=str(raw.get("operator_disclaimer", "") or ""),
        clarification_record=_parse_clarification_record(raw.get("clarification_record")),
        testing_strategy=_parse_testing_strategy(raw),
        proof_resolution_source=_parse_proof_resolution_source(raw),
    )


def _parse_proof_resolution_source(raw: dict[str, Any]) -> str:
    """Phase 7: load the persisted source provenance for the plan's
    ``local_proof_cmd``.  Unknown values fall back to ``none``."""
    from saturnday._types import PROOF_RESOLUTION_SOURCES
    val = raw.get("proof_resolution_source")
    if val is None:
        return "none"
    if not isinstance(val, str) or val not in PROOF_RESOLUTION_SOURCES:
        logger.warning(
            "Plan proof_resolution_source %r is not a known value — "
            "falling back to none", val,
        )
        return "none"
    return val


def _parse_testing_strategy(raw: dict[str, Any]) -> str:
    """Phase 1: parse testing_strategy; default ``unspecified`` and warn on
    unknown values (same pattern as _parse_operating_mode)."""
    val = raw.get("testing_strategy")
    if val is None:
        return "unspecified"
    if not isinstance(val, str) or val not in TESTING_STRATEGIES:
        logger.warning(
            "Plan testing_strategy %r is not a known value — falling back "
            "to unspecified", val,
        )
        return "unspecified"
    return val


def _parse_operating_mode(raw: dict[str, Any]) -> str:
    """Fix 76: parse and validate the persisted operating_mode.

    Plans loaded without this field get ``legacy_unclassified`` (the
    explicit compatibility state); plans that set an unknown value also
    fall back to ``legacy_unclassified`` with a warning.  Validation
    elsewhere will reject *new* plans that emit ``legacy_unclassified``.
    """
    val = raw.get("operating_mode")
    if val is None:
        # Pre-Fix-76 plan: explicit compatibility marker (NOT a guess at
        # ``library`` — that would silently weaken acceptance expectations).
        return "legacy_unclassified"
    if not isinstance(val, str) or val not in OPERATING_MODES:
        logger.warning(
            "Plan operating_mode %r is not a known value — falling back to "
            "legacy_unclassified for compatibility", val,
        )
        return "legacy_unclassified"
    return val


def _parse_dependency_profile(raw: dict[str, Any]) -> str:
    val = raw.get("dependency_profile")
    if val is None:
        return "self_contained"
    if not isinstance(val, str) or val not in DEPENDENCY_PROFILES:
        logger.warning(
            "Plan dependency_profile %r is not a known value — falling back "
            "to self_contained", val,
        )
        return "self_contained"
    return val


def _parse_proof_realism(raw: dict[str, Any]) -> str:
    val = raw.get("proof_realism")
    if val is None:
        return "production_intent"
    if not isinstance(val, str) or val not in PROOF_REALISMS:
        logger.warning(
            "Plan proof_realism %r is not a known value — falling back to "
            "production_intent", val,
        )
        return "production_intent"
    return val


def _parse_clarification_record(raw: Any) -> tuple[ClarificationEntry, ...]:
    """Fix 75: parse the persisted clarification record.

    Each entry is ``{"trigger_kind": str, "question": str, "answer": str,
    "blocking": bool}``.  Malformed entries are skipped silently — the
    record is evidence of dialog, not a strict contract.
    """
    if not isinstance(raw, list):
        return ()
    out: list[ClarificationEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        trigger = str(item.get("trigger_kind", "") or "")
        question = str(item.get("question", "") or "")
        answer = str(item.get("answer", "") or "")
        blocking = bool(item.get("blocking", False))
        if trigger:
            out.append(ClarificationEntry(
                trigger_kind=trigger,
                question=question,
                answer=answer,
                blocking=blocking,
            ))
    return tuple(out)


def validate_plan(raw: dict[str, Any]) -> list[str]:
    """Check required fields, types, budget sanity, and ticket ID uniqueness.

    Returns:
        A list of validation error strings (empty if valid).
    """
    errors: list[str] = []

    if "tickets" not in raw:
        errors.append("Missing required field: 'tickets'")
        return errors

    tickets = raw["tickets"]
    if not isinstance(tickets, list):
        errors.append("'tickets' must be an array")
        return errors

    if not tickets:
        errors.append("Plan has no tickets")
        return errors

    seen_ids: set[str] = set()
    for i, t in enumerate(tickets):
        if not isinstance(t, dict):
            errors.append(f"Ticket at index {i} is not an object")
            continue

        tid = t.get("ticket_id", "")
        if not tid:
            errors.append(f"Ticket at index {i} missing 'ticket_id'")
            continue

        if tid in seen_ids:
            errors.append(f"Duplicate ticket_id: {tid!r}")
        seen_ids.add(tid)

        if not t.get("goal"):
            errors.append(f"Ticket {tid!r} missing 'goal'")

        # Budget sanity
        scope = t.get("scope", {})
        if isinstance(scope, dict):
            budgets = scope.get("budgets", {})
            if isinstance(budgets, dict):
                for key in ("max_files_changed", "max_total_diff_lines", "max_per_file_diff_lines"):
                    val = budgets.get(key)
                    if val is not None and (not isinstance(val, int) or val < 1):
                        errors.append(f"Ticket {tid!r}: budget '{key}' must be a positive integer, got {val!r}")

        # Dependency references
        deps = t.get("dependencies", [])
        if isinstance(deps, list):
            for dep in deps:
                if dep not in seen_ids and dep not in {tt.get("ticket_id") for tt in tickets}:
                    errors.append(f"Ticket {tid!r} depends on unknown ticket {dep!r}")

        # Fix 59: acceptance_criteria is required for governance verification.
        # Without it, the contract sweep has nothing to verify.
        _ac = t.get("acceptance_criteria", [])
        if not _ac or (isinstance(_ac, list) and len(_ac) == 0):
            _mode = raw.get("mode", "generation")
            if _mode == "generation":
                errors.append(
                    f"Ticket {tid!r} has no acceptance_criteria — "
                    f"governance cannot verify ticket outcome without a rubric"
                )

    # Validate definition_of_done markers
    dod = raw.get("definition_of_done")
    if isinstance(dod, list):
        for marker in dod:
            if isinstance(marker, str) and marker not in KNOWN_DEFINITION_MARKERS:
                errors.append(
                    f"Unknown definition_of_done marker: {marker!r}. "
                    f"Known markers: {sorted(KNOWN_DEFINITION_MARKERS)}"
                )

    # Validate max_project_tickets
    max_pt = raw.get("max_project_tickets")
    if max_pt is not None:
        if not isinstance(max_pt, int) or max_pt < 1:
            errors.append(
                f"'max_project_tickets' must be a positive integer, got {max_pt!r}"
            )

    # Validate phases reference real ticket IDs
    phases_raw = raw.get("phases")
    if isinstance(phases_raw, list):
        all_ticket_ids = seen_ids
        for pi, phase in enumerate(phases_raw):
            if not isinstance(phase, dict):
                errors.append(f"Phase at index {pi} is not an object")
                continue
            phase_tids = phase.get("ticket_ids", [])
            if isinstance(phase_tids, list):
                for ptid in phase_tids:
                    if isinstance(ptid, str) and ptid not in all_ticket_ids:
                        errors.append(
                            f"Phase {phase.get('phase_id', pi)!r} references "
                            f"unknown ticket {ptid!r}"
                        )

    # Fix 45 (mode-aware): non-breaking warning when a plan appears to
    # describe a runnable product but has no end-to-end proof field set.
    #
    # Pre-Fix-76 plans use ``acceptance_cmd``.  Fix-76 plans with a
    # declared ``operating_mode`` use ``local_proof_cmd`` instead and
    # are explicitly forbidden from setting ``acceptance_cmd`` (see
    # _validate_mode_and_proof_fields).  The original Fix 45 only
    # checked ``acceptance_cmd``, which fired a false positive on every
    # correctly-shaped Fix 76 plan that had populated
    # ``local_proof_cmd``.  The check is now mode-aware: pick the right
    # field for the plan's operating_mode, and warn only when both
    # *that* field is empty AND the plan looks runnable.
    _acceptance_cmd = raw.get("acceptance_cmd", "") or ""
    _local_proof_cmd = raw.get("local_proof_cmd", "") or ""
    _op_mode = raw.get("operating_mode", "") or ""
    if _op_mode in ("", "legacy_unclassified"):
        _proof_field_value = _acceptance_cmd
        _proof_field_name = "acceptance_cmd"
    else:
        _proof_field_value = _local_proof_cmd
        _proof_field_name = "local_proof_cmd"
    if not _proof_field_value:
        _notes = raw.get("notes", "") + " " + raw.get("governing_goal", "")
        _lower_notes = _notes.lower()
        _runnable_hints = ("cli", "command-line", "tool", "app", "server", "api",
                           "library", "package", "script", "pipeline", "web")
        if any(hint in _lower_notes for hint in _runnable_hints):
            logger.warning(
                "Fix 45: plan describes a runnable product but has no %s. "
                "Consider adding one to verify the assembled product works "
                "end-to-end.",
                _proof_field_name,
            )

    # Fix 76: validate operating_mode / dependency_profile / proof_realism
    # combinations and the proof-field precedence rules.
    errors.extend(_validate_mode_and_proof_fields(raw))

    # Fix 73: validate proof-content meaningfulness for declared-mode plans.
    # Trivial proofs (import-only, pure pytest, --help-only, tautologies,
    # failure-swallowing suffixes) are rejected even when proof-field
    # precedence is satisfied.
    errors.extend(_validate_proof_meaningfulness(raw))

    return errors


# ---------------------------------------------------------------------------
# Fix 73 — proof meaningfulness validator
# ---------------------------------------------------------------------------

import re as _re_pm

# Tautologies: proofs that always succeed regardless of product behaviour.
_TAUTOLOGY_PATTERNS = (
    _re_pm.compile(r"^\s*(true|:)\s*$"),
    _re_pm.compile(r"^\s*echo(\s|$)"),
)
# Failure-swallowing suffixes: the command can fail and still exit 0.
_SWALLOW_PATTERNS = (
    _re_pm.compile(r"\|\|\s*(true|exit\s+0|:)\s*$"),
)
# Pure pytest invocation that proves tests pass, not that the product works.
_PURE_PYTEST_PATTERNS = (
    _re_pm.compile(r"^\s*pytest(\s+tests?/?)?(\s+-[A-Za-z]+)*\s*$"),
    _re_pm.compile(r"^\s*python\s+-m\s+pytest(\s+tests?/?)?(\s+-[A-Za-z]+)*\s*$"),
)
# Import-only python -c proofs.
_IMPORT_ONLY_PATTERNS = (
    _re_pm.compile(r"""^\s*python\s+-c\s+["'](\s*import\s+\w+(\s*\.\s*\w+)*\s*;?\s*)+["']\s*$"""),
)
# Help/version-only invocation (the entire command, no piping or further work).
_HELP_VERSION_ONLY_PATTERNS = (
    _re_pm.compile(r"^\s*\S+(\s+\S+)*\s+(--help|--version|-h|-v)\s*$"),
)
# HTTP-request indicators required for web_service.
_HTTP_REQUEST_TOKENS = (
    "curl ", "curl\t", "wget ",
    "httpx", "requests.", "http.client", "urllib.request", "fetch(",
    "axios", "http_request",
)
# Browser-render / serve indicators required for frontend.
_FRONTEND_RENDER_TOKENS = (
    "playwright", "cypress", "puppeteer", "selenium",
    "headless", "serve dist", "http-server", "vite preview", "next start",
)
# Frontend structural elements — counted to enforce
# multi-structural-assertion when no headless browser tool is in use.
_FRONTEND_STRUCTURAL_TOKENS = (
    "<body", "<main", "<header", "<nav", "<article", "<section", "<div",
    "<form", "<button",
)
# Headless browser tools — when present, the frontend proof is
# considered to have a real rendered-behaviour assertion regardless of
# how many structural greps it has.
_FRONTEND_HEADLESS_TOOLS = (
    "playwright", "cypress", "puppeteer", "selenium",
)
# Web-service readiness signals — patterns that indicate the proof
# polls for readiness instead of relying on a fixed sleep.
_READINESS_LOOP_PATTERNS = (
    _re_pm.compile(r"\bwhile\b[\s\S]*?\b(urlopen|curl|wget|connect)\b", _re_pm.MULTILINE),
    _re_pm.compile(r"\bfor\b[\s\S]*?\b(urlopen|curl|wget)\b", _re_pm.MULTILINE),
    _re_pm.compile(r"\buntil\b[\s\S]*?\b(curl|wget)\b", _re_pm.MULTILINE),
    _re_pm.compile(r"wait[_ ]for[_ ]ready|wait[_ ]until[_ ]ready|healthcheck",
                   _re_pm.IGNORECASE),
)
# Fixed-sleep readiness — rejected when used as the ONLY pre-request wait.
_FIXED_SLEEP_PATTERN = _re_pm.compile(r"\bsleep\s+\d+(\.\d+)?\b")
# Library-proof: must include at least one function call (not just imports).
# Matches ``name(...)`` where name is alphanumeric — rough but sufficient.
_FUNCTION_CALL_PATTERN = _re_pm.compile(r"\b[A-Za-z_]\w*\s*\(")
# CLI-tool meaningful assertion patterns — proves output has substance
# beyond merely being non-empty.
_CLI_MEANINGFUL_ASSERTION_PATTERNS = (
    _re_pm.compile(r"\blen\s*\(\s*[A-Za-z_][\w.]*\s*\)\s*(>=|>|<=)\s*\d+"),
    _re_pm.compile(r"\$\{#\w+\}\s*-(ge|gt|le)\s*\d+"),
    _re_pm.compile(r"\b-(ge|gt|le)\s+\d+"),
    _re_pm.compile(r"\bre\.(search|match|fullmatch)\s*\("),
    _re_pm.compile(r"\bgrep\s+-[^\s]*[Eeq]+[^\s]*\s+"),
    _re_pm.compile(r"\.assertEqual|\.assertIn|assert\s+\w+\s*==\s*"),
)
# Worker-proof side-effect tokens — proves the assertion targets a real
# observable side effect (filesystem, queue, DB) and not just a return value.
_WORKER_SIDE_EFFECT_TOKENS = (
    "os.listdir", "os.path.exists", "os.stat", "Path.glob",
    "Path(", "pathlib", "tempfile", "TemporaryDirectory",
    ".fetchone", ".fetchall", "rowcount", "select ",
    "queue.get", "queue.delivered", "produced", "side effect",
    "WORKER_OUTPUT", "OUTPUT_DIR",
)
# Pattern for "is not None" assertion that, when used ALONE on a function
# return value with no other side-effect check, is too weak for worker mode.
_RETURN_ONLY_PATTERN = _re_pm.compile(
    r"assert\s+\w+\s+is\s+not\s+None\b"
)


def _is_tautology(cmd: str) -> bool:
    return any(p.search(cmd) for p in _TAUTOLOGY_PATTERNS)


def _swallows_failure(cmd: str) -> bool:
    return any(p.search(cmd) for p in _SWALLOW_PATTERNS)


def _is_pure_pytest(cmd: str) -> bool:
    return any(p.match(cmd) for p in _PURE_PYTEST_PATTERNS)


def _is_import_only(cmd: str) -> bool:
    return any(p.match(cmd) for p in _IMPORT_ONLY_PATTERNS)


def _is_help_version_only(cmd: str) -> bool:
    return any(p.match(cmd) for p in _HELP_VERSION_ONLY_PATTERNS)


def _has_http_request(cmd: str) -> bool:
    lower = cmd.lower()
    return any(tok in lower for tok in _HTTP_REQUEST_TOKENS)


def _has_browser_render(cmd: str) -> bool:
    lower = cmd.lower()
    return any(tok in lower for tok in _FRONTEND_RENDER_TOKENS)


def _has_function_call(cmd: str) -> bool:
    """Detect at least one function call in the proof.  Excludes the
    ``import`` keyword (matches names but not statements)."""
    # Strip ``import xxx`` / ``from x import y`` to avoid the ``import``
    # tokens being counted as calls.  This is a rough check, not a full parser.
    stripped = _re_pm.sub(r"\b(import|from)\s+[\w\.]+(\s+as\s+\w+)?", "", cmd)
    return bool(_FUNCTION_CALL_PATTERN.search(stripped))


def _has_assertion(cmd: str) -> bool:
    return bool(_re_pm.search(r"\bassert\b", cmd))


def _has_readiness_loop(cmd: str) -> bool:
    return any(p.search(cmd) for p in _READINESS_LOOP_PATTERNS)


def _has_fixed_sleep(cmd: str) -> bool:
    return bool(_FIXED_SLEEP_PATTERN.search(cmd))


def _has_cli_meaningful_assertion(cmd: str) -> bool:
    """cli_tool proof must assert MORE than ``test -n "$OUT"``.  Acceptable
    forms: explicit length threshold, regex match, structured comparison,
    or grep with a non-trivial pattern flag."""
    return any(p.search(cmd) for p in _CLI_MEANINGFUL_ASSERTION_PATTERNS)


def _frontend_structural_count(cmd: str) -> int:
    return sum(1 for tok in _FRONTEND_STRUCTURAL_TOKENS if tok in cmd)


def _has_frontend_headless_tool(cmd: str) -> bool:
    lower = cmd.lower()
    return any(tok in lower for tok in _FRONTEND_HEADLESS_TOOLS)


def _has_worker_side_effect_assertion(cmd: str) -> bool:
    """Worker proof must reference a real observable side effect.

    Returns True if the proof either (a) uses no ``... is not None``
    return-value-only assertion, or (b) also references at least one
    side-effect indicator (filesystem listing, DB row, queue message)."""
    return any(tok in cmd for tok in _WORKER_SIDE_EFFECT_TOKENS)


# Library introspection-only assertion detection.  ``assert dir(pkg)``,
# ``assert hasattr(pkg, ...)``, ``assert callable(pkg.fn)`` etc. prove the
# module's SHAPE but not that any real product function works.
_INTROSPECTION_ASSERTION_PATTERN = _re_pm.compile(
    r"assert\s+(?:not\s+)?(?:len\s*\(\s*)?(dir|hasattr|vars|callable|getattr|isinstance|type|issubclass)\s*\("
)
_LIBRARY_ASSERT_LINE_PATTERN = _re_pm.compile(r"assert\s+[^\n;]+")


def _is_library_introspection_only(cmd: str) -> bool:
    """Return True iff EVERY ``assert`` in the proof targets only module
    introspection (dir / hasattr / vars / callable / getattr / isinstance
    / type / issubclass) — i.e. the proof checks the module's shape but
    never asserts anything about a real function's behaviour.

    Returns False if the proof has at least one non-introspection
    assertion (e.g. ``assert result == 42`` or ``assert r is not None``).
    Returns False if the proof has no assertions (caught elsewhere).
    """
    asserts = _LIBRARY_ASSERT_LINE_PATTERN.findall(cmd)
    if not asserts:
        return False
    for a in asserts:
        if not _INTROSPECTION_ASSERTION_PATTERN.match(a):
            return False  # at least one non-introspection assertion exists
    return True


def validate_proof_meaningfulness(
    *,
    operating_mode: str,
    proof_realism: str,
    dependency_profile: str,
    local_proof_cmd: str,
) -> list[str]:
    """Fix 73: reject local_proof_cmd values that don't actually exercise the
    product's real operator path for the declared mode.

    The validator is bounded to a small ruleset (universal tautology checks
    plus per-mode shape checks).  It is NOT a full proof linter; it catches
    the always-unacceptable patterns explicitly named in the design and
    leaves richer judgement to the LLM DoD pass.

    Returns a list of human-readable error strings; empty when the proof is
    acceptable for the declared mode.  Legacy plans
    (``operating_mode == "legacy_unclassified"``) bypass these checks (the
    legacy ``acceptance_cmd`` field has its own pre-Fix-73 semantics).
    """
    errs: list[str] = []
    cmd = (local_proof_cmd or "").strip()
    # Legacy plans: skip — Fix 73 is for declared-mode plans only.
    if operating_mode == "legacy_unclassified":
        return errs
    # Empty cmd: handled by the Fix 76 precedence validator (which already
    # rejects empty local_proof_cmd for declared modes); Fix 73 has no
    # additional message to add here.
    if not cmd:
        return errs

    # Fix 73 gap-aware: honest proof-generation gaps are recognized and
    # passed through.  They deliberately fail at runtime — the validator
    # must not reject them as if they were fake proofs.
    from saturnday.run.planner import FIX73_PROOF_GAP_PREFIX
    if FIX73_PROOF_GAP_PREFIX in cmd:
        return errs

    # ---- Universal rejections (any declared mode) ----
    if _is_tautology(cmd):
        errs.append(
            f"local_proof_cmd {cmd!r} is a no-op / tautology — "
            f"proves nothing about the {operating_mode}.  Replace with a "
            f"real exercise of the product's operator path."
        )
    if _swallows_failure(cmd):
        errs.append(
            f"local_proof_cmd swallows failure ('|| true' / '|| exit 0') — "
            f"the proof can fail silently and still exit 0.  Remove the "
            f"failure-swallowing suffix."
        )
    if _is_pure_pytest(cmd):
        errs.append(
            f"local_proof_cmd {cmd!r} is a bare pytest invocation — proves "
            f"tests exist that pass, NOT that the {operating_mode} works.  "
            f"Add a real exercise (HTTP request for web_service, CLI "
            f"invocation with output assertion, etc.)."
        )

    # ---- Cross-mode rejection: import-only is acceptable only for library ----
    if operating_mode != "library" and _is_import_only(cmd):
        errs.append(
            f"local_proof_cmd {cmd!r} is import-only — acceptable for "
            f"library mode only, NOT for {operating_mode}."
        )

    # ---- Per-mode rules ----
    if operating_mode == "library":
        if _is_import_only(cmd):
            errs.append(
                "library mode requires more than 'import': call at least "
                "one public function and assert a non-trivial result "
                "(e.g. python -c \"from x import f; assert f(1) == 1\")."
            )
        else:
            # Fix 73 correction: dir(pkg)-style proofs are NOT acceptable —
            # they prove the module exists, not that any function works.
            # Require an explicit assertion AND at least one function call.
            if not _has_assertion(cmd):
                errs.append(
                    "library mode requires an explicit `assert` against the "
                    "result of a real public function call.  Existence checks "
                    "(dir(...), hasattr) alone do not prove the library works."
                )
            if not _has_function_call(cmd):
                errs.append(
                    "library mode requires at least one function call beyond "
                    "import (e.g. `r = fn(input); assert r is not None`).  "
                    "Proofs that only inspect module surface are too weak."
                )
            # Fix 73 correction v2: even with assert + call, if EVERY
            # assertion is on introspection builtins (dir/hasattr/vars/
            # callable) the proof checks MODULE SHAPE, not product
            # behaviour.  ``assert dir(pkg)`` is not product proof.
            if _is_library_introspection_only(cmd):
                errs.append(
                    "library mode requires an assertion against a real "
                    "function return value.  Proofs that only assert on "
                    "module introspection (dir, hasattr, vars, callable, "
                    "isinstance) prove the module's shape, not that any "
                    "product function works."
                )
    elif operating_mode == "cli_tool":
        if _is_help_version_only(cmd):
            errs.append(
                f"cli_tool mode rejects {cmd!r}: '--help' / '--version' "
                f"alone is not acceptance.  Invoke the CLI with concrete "
                f"arguments AND assert at least one expected output "
                f"substring (pipe to grep -q, capture and test -n, etc.)."
            )
        else:
            # Fix 73 correction: ``test -n "$OUT"`` after ``--version`` is
            # version-check theatre.  Require a meaningful assertion (length
            # threshold, regex match, equality check) so the proof shows the
            # CLI actually does something, not just that it prints SOMETHING.
            if not _has_cli_meaningful_assertion(cmd):
                errs.append(
                    "cli_tool mode requires a meaningful output assertion "
                    "(length threshold like `len(out) >= 20` or `${#OUT} -ge 20`, "
                    "regex match like `re.search(...)` / `grep -E pattern`, or an "
                    "equality check).  Bare non-empty checks (`test -n`) over "
                    "`--version` / `--help` output is version-check theatre."
                )
    elif operating_mode == "web_service":
        if not _has_http_request(cmd):
            errs.append(
                f"web_service mode requires a real HTTP request "
                f"(curl/wget/requests/httpx/fetch) to a documented "
                f"business endpoint over loopback — got {cmd!r}.  "
                f"In-process test client alone is not canonical "
                f"web_service acceptance."
            )
        # Fix 73 correction: a fixed `sleep N` followed by a single curl is
        # NOT readiness — it's race-prone theatre.  Require a polling loop
        # (while/for/until ... curl/urlopen) or an explicit healthcheck token.
        if _has_fixed_sleep(cmd) and not _has_readiness_loop(cmd):
            errs.append(
                "web_service mode requires a real readiness signal — a "
                "polling loop (while/for/until ... curl/urlopen) or an "
                "explicit healthcheck — NOT a fixed `sleep N` before the "
                "first request.  Fixed sleep is race-prone and proves "
                "nothing about service readiness."
            )
    elif operating_mode == "worker":
        # Fix 73 correction: `assert worker.run_one() is not None` is too
        # weak unless the proof also asserts a real observable side effect
        # (filesystem write, DB row inserted, queue message produced).
        if _RETURN_ONLY_PATTERN.search(cmd) and not _has_worker_side_effect_assertion(cmd):
            errs.append(
                "worker mode requires an assertion against a real "
                "observable side effect (filesystem write, DB row inserted, "
                "queue message produced, etc.).  `assert ... is not None` "
                "on a function return value alone is not sufficient — that "
                "value must itself be the documented operator-visible "
                "output AND the proof must check it explicitly."
            )
    elif operating_mode == "frontend":
        # Fix 73 correction: a real frontend proof either uses a headless
        # browser tool (playwright / cypress / puppeteer / selenium) OR
        # asserts MULTIPLE structural elements after serving the build.
        # `curl + grep <html` alone is not acceptance.
        if not _has_browser_render(cmd):
            errs.append(
                f"frontend mode requires a serve/render/assert path: a "
                f"headless browser tool (playwright / cypress / puppeteer "
                f"/ selenium) OR a local server (serve / http-server / "
                f"vite preview / next start) — got {cmd!r}.  Build "
                f"artefact existence alone is NOT acceptance."
            )
        else:
            has_headless = _has_frontend_headless_tool(cmd)
            structural_count = _frontend_structural_count(cmd)
            if not has_headless and structural_count < 2:
                errs.append(
                    "frontend mode requires a real rendered-behaviour "
                    "assertion: either a headless browser tool "
                    "(playwright / cypress / puppeteer / selenium) OR at "
                    "least TWO structural-element checks "
                    f"(<body, <main, <header, <nav, <article, <section, <div) "
                    f"on the served response.  Got {structural_count} "
                    "structural assertion(s).  `grep -q '<html'` alone is "
                    "NOT acceptance — HTML existing proves packaging, not "
                    "rendered behaviour."
                )

    return errs


def _validate_proof_meaningfulness(raw: dict[str, Any]) -> list[str]:
    """Adapter that pulls fields off the raw plan dict and delegates to
    :func:`validate_proof_meaningfulness`."""
    op_mode = raw.get("operating_mode", "legacy_unclassified")
    if not isinstance(op_mode, str):
        op_mode = "legacy_unclassified"
    realism = raw.get("proof_realism", "production_intent")
    if not isinstance(realism, str):
        realism = "production_intent"
    dep_prof = raw.get("dependency_profile", "self_contained")
    if not isinstance(dep_prof, str):
        dep_prof = "self_contained"
    cmd = raw.get("local_proof_cmd", "")
    if not isinstance(cmd, str):
        cmd = ""
    return validate_proof_meaningfulness(
        operating_mode=op_mode,
        proof_realism=realism,
        dependency_profile=dep_prof,
        local_proof_cmd=cmd,
    )


def _validate_mode_and_proof_fields(raw: dict[str, Any]) -> list[str]:
    """Fix 76: enforce mode/profile/realism + proof-field precedence rules.

    Rules (only fail-loud for combinations the planner must never produce; for
    legacy / loaded plans, ``legacy_unclassified`` rides the compatibility
    path with a warning, not a rejection):

    - ``operating_mode``, ``dependency_profile``, ``proof_realism`` must each
      be one of the known values when set.
    - ``legacy_unclassified`` is allowed only for compatibility — emit a WARNING
      so the operator sees the legacy debt; do not reject.
    - For DECLARED-MODE plans (``operating_mode != "legacy_unclassified"``):
        * ``local_proof_cmd`` must be non-empty.
        * ``acceptance_cmd`` must be empty (no silent precedence).
    - For LEGACY-UNCLASSIFIED plans:
        * ``acceptance_cmd`` may be set; ``local_proof_cmd`` must be empty
          (the runner must not dual-read).
    - ``external_dependencies`` must be non-empty iff
      ``dependency_profile == "external_dependencies"``.
    - ``live_proof_cmd`` must be empty unless
      ``dependency_profile == "external_dependencies"``.
    - ``demo_seed_source`` must be non-empty when
      ``proof_realism == "seeded_demo"``.
    - ``operator_disclaimer`` must be non-empty when:
        * ``proof_realism == "seeded_demo"``
        * ``operating_mode == "storage_only"``
        * ``dependency_profile == "external_dependencies"``
    """
    errs: list[str] = []

    # Read raw values (do NOT use _parse_* helpers here — those silently fall
    # back; the validator must surface unknown enum values as errors).
    op_mode = raw.get("operating_mode")
    dep_prof = raw.get("dependency_profile")
    realism = raw.get("proof_realism")

    if op_mode is None:
        # Legacy plan with no field at all — ride the compatibility path.
        # The parser's _parse_operating_mode will set "legacy_unclassified".
        op_mode = "legacy_unclassified"
        logger.warning(
            "Plan has no operating_mode — treating as legacy_unclassified for "
            "compatibility.  Newly generated plans must declare a real mode "
            "(library / cli_tool / web_service / worker / pipeline / frontend "
            "/ storage_only)."
        )
    elif op_mode not in OPERATING_MODES:
        errs.append(
            f"operating_mode {op_mode!r} is not one of {list(OPERATING_MODES)}"
        )
        return errs  # downstream rules depend on a known mode

    if dep_prof is None:
        dep_prof = "self_contained"
    elif dep_prof not in DEPENDENCY_PROFILES:
        errs.append(
            f"dependency_profile {dep_prof!r} is not one of {list(DEPENDENCY_PROFILES)}"
        )
        return errs

    if realism is None:
        realism = "production_intent"
    elif realism not in PROOF_REALISMS:
        errs.append(
            f"proof_realism {realism!r} is not one of {list(PROOF_REALISMS)}"
        )
        return errs

    local_proof = str(raw.get("local_proof_cmd", "") or "")
    accept_cmd = str(raw.get("acceptance_cmd", "") or "")
    live_proof = str(raw.get("live_proof_cmd", "") or "")
    ext_deps = raw.get("external_dependencies") or []
    demo_seed = str(raw.get("demo_seed_source", "") or "")
    disclaimer = str(raw.get("operator_disclaimer", "") or "")
    testing_strategy = raw.get("testing_strategy")
    if testing_strategy is None:
        testing_strategy = "unspecified"
    elif testing_strategy not in TESTING_STRATEGIES:
        errs.append(
            f"testing_strategy {testing_strategy!r} is not one of "
            f"{list(TESTING_STRATEGIES)}"
        )
        return errs

    declared = op_mode in DECLARED_OPERATING_MODES

    # Proof-field precedence (the heart of Fix 76).
    if declared:
        if not local_proof:
            errs.append(
                f"operating_mode={op_mode!r} requires non-empty local_proof_cmd "
                "(declared-mode plans must use the canonical local_proof_cmd "
                "field; acceptance_cmd is honoured only for legacy_unclassified)"
            )
        if accept_cmd:
            errs.append(
                f"operating_mode={op_mode!r} must NOT set acceptance_cmd "
                "(declared-mode plans use local_proof_cmd; setting both would "
                "create silent precedence ambiguity)"
            )
    else:
        # legacy_unclassified
        if local_proof:
            errs.append(
                "operating_mode=legacy_unclassified must NOT set "
                "local_proof_cmd (legacy plans use acceptance_cmd; setting "
                "both would create silent precedence ambiguity)"
            )

    # External dependencies invariants.
    if dep_prof == "external_dependencies":
        if not isinstance(ext_deps, list) or not any(
            isinstance(s, str) and s for s in ext_deps
        ):
            errs.append(
                "dependency_profile=external_dependencies requires non-empty "
                "external_dependencies (list at least one third-party service)"
            )
    else:
        if isinstance(ext_deps, list) and any(
            isinstance(s, str) and s for s in ext_deps
        ):
            errs.append(
                f"external_dependencies must be empty when dependency_profile="
                f"{dep_prof!r} (only external_dependencies profile may declare them)"
            )
        if live_proof:
            errs.append(
                f"live_proof_cmd must be empty when dependency_profile="
                f"{dep_prof!r} (only meaningful for external_dependencies)"
            )

    # Seeded-demo invariants.
    if realism == "seeded_demo":
        if not demo_seed:
            errs.append(
                "proof_realism=seeded_demo requires non-empty demo_seed_source "
                "(name the fixture/script that seeds the demo)"
            )

    # Phase 1: testing_strategy coherence rules.
    live_only_strategies = {
        "live_credentials_gated", "local_emulator",
        "oss_substitute", "generated_fake", "recorded_fixture",
    }
    if testing_strategy in live_only_strategies and dep_prof == "self_contained":
        errs.append(
            f"testing_strategy={testing_strategy!r} implies external or "
            f"local dependencies, but dependency_profile=self_contained"
        )
    if testing_strategy == "seeded_demo" and realism != "seeded_demo":
        errs.append(
            "testing_strategy=seeded_demo requires proof_realism=seeded_demo"
        )

    # Operator-disclaimer requirements.
    needs_disclaimer = (
        realism == "seeded_demo"
        or op_mode == "storage_only"
        or dep_prof == "external_dependencies"
    )
    if needs_disclaimer and not disclaimer:
        reasons = []
        if realism == "seeded_demo":
            reasons.append("proof_realism=seeded_demo")
        if op_mode == "storage_only":
            reasons.append("operating_mode=storage_only")
        if dep_prof == "external_dependencies":
            reasons.append("dependency_profile=external_dependencies")
        errs.append(
            "operator_disclaimer must be non-empty when " + " or ".join(reasons)
            + " (the run summary surfaces this verbatim so operators see "
            "honest framing of what was actually proved)"
        )

    return errs


def resolve_ticket_order(tickets: list[TicketSpec]) -> list[TicketSpec]:
    """Topological sort tickets by their dependency graph.

    Args:
        tickets: Unsorted list of ticket specs.

    Returns:
        Tickets in dependency-first order.

    Raises:
        PlanValidationError: If a dependency cycle is detected.
    """
    by_id: dict[str, TicketSpec] = {t.ticket_id: t for t in tickets}
    visited: set[str] = set()
    in_stack: set[str] = set()
    ordered: list[TicketSpec] = []

    def visit(tid: str) -> None:
        if tid in visited:
            return
        if tid in in_stack:
            raise PlanValidationError([f"Dependency cycle involving ticket {tid!r}"])
        in_stack.add(tid)
        ticket = by_id.get(tid)
        if ticket is None:
            return
        for dep in ticket.dependencies:
            visit(dep)
        in_stack.discard(tid)
        visited.add(tid)
        ordered.append(ticket)

    for tid in by_id:
        visit(tid)

    return ordered


def _parse_phases(raw_phases: list[Any]) -> tuple[PhaseDef, ...]:
    """Parse raw phase dicts into typed PhaseDef objects."""
    if not isinstance(raw_phases, list):
        return ()
    result: list[PhaseDef] = []
    for i, p in enumerate(raw_phases):
        if not isinstance(p, dict):
            continue
        phase_id = str(p.get("phase_id", f"phase-{i + 1}"))
        name = str(p.get("name", phase_id))
        ticket_ids = tuple(
            str(tid) for tid in p.get("ticket_ids", []) if isinstance(tid, str)
        )
        result.append(PhaseDef(phase_id=phase_id, name=name, ticket_ids=ticket_ids))
    return tuple(result)


def _parse_tickets(raw_tickets: list[dict[str, Any]]) -> list[TicketSpec]:
    """Parse raw ticket dicts into typed TicketSpec objects."""
    result: list[TicketSpec] = []
    for t in raw_tickets:
        if not isinstance(t, dict):
            continue

        scope_raw = t.get("scope", {})
        budgets = scope_raw.get("budgets", {}) if isinstance(scope_raw, dict) else {}

        scope = TicketScope(
            allowed_globs=tuple(scope_raw.get("allowed_globs", ["**"])) if isinstance(scope_raw, dict) else TicketScope().allowed_globs,
            forbidden_globs=tuple(scope_raw.get("forbidden_globs", [])) if isinstance(scope_raw, dict) else TicketScope().forbidden_globs,
            max_files_changed=budgets.get("max_files_changed", 20) if isinstance(budgets, dict) else 20,
            max_total_diff_lines=budgets.get("max_total_diff_lines", 2000) if isinstance(budgets, dict) else 2000,
            max_per_file_diff_lines=budgets.get("max_per_file_diff_lines", 500) if isinstance(budgets, dict) else 500,
        )

        deps = t.get("dependencies", [])
        if not isinstance(deps, list):
            deps = []

        criteria = t.get("acceptance_criteria", [])
        if isinstance(criteria, str):
            criteria = [criteria]
        elif not isinstance(criteria, list):
            criteria = []

        out_of_scope_raw = t.get("out_of_scope", [])
        if not isinstance(out_of_scope_raw, list):
            out_of_scope_raw = []

        evidence_required_raw = t.get("evidence_required", [])
        if not isinstance(evidence_required_raw, list):
            evidence_required_raw = []

        failure_mode = t.get("failure_mode", "")
        if not isinstance(failure_mode, str):
            failure_mode = ""

        if_blocked = t.get("if_blocked", "")
        if not isinstance(if_blocked, str):
            if_blocked = ""

        atomic_raw = t.get("atomic", False)
        atomic = bool(atomic_raw) if isinstance(atomic_raw, bool) else False

        tid = t.get("ticket_id", "")

        # Rubric completeness warnings — never reject, only advise.
        if not criteria:
            logger.warning("Ticket %r has no acceptance_criteria — rubric incomplete", tid)
        if not out_of_scope_raw:
            logger.warning("Ticket %r has no out_of_scope — rubric incomplete", tid)
        if not evidence_required_raw:
            logger.debug("Ticket %r has no evidence_required — rubric incomplete", tid)

        result.append(TicketSpec(
            ticket_id=tid,
            goal=t.get("goal", ""),
            scope=scope,
            verify_cmd=t.get("verify_cmd", ""),
            acceptance_criteria=tuple(criteria),
            dependencies=tuple(deps),
            out_of_scope=tuple(str(s) for s in out_of_scope_raw if isinstance(s, str)),
            evidence_required=tuple(str(s) for s in evidence_required_raw if isinstance(s, str)),
            failure_mode=failure_mode,
            if_blocked=if_blocked,
            atomic=atomic,
        ))

    return result
