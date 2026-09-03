"""Phase 4 — plan-time coder-assisted proof resolution.

When the planner emits a ``FIX73_PROOF_GAP`` marker for library / cli_tool /
web_service / worker / frontend, this module attempts to *derive* a concrete,
meaningful proof by invoking the coder backend with the plan context:

- operating_mode
- dependency_profile + external_dependencies
- proof_realism + testing_strategy
- enriched_tickets (acceptance_criteria, goals, scope)
- project_id

If the derived command passes ``validate_proof_meaningfulness``, it replaces
the gap marker and the plan is updated with ``proof_resolution_source =
"coder_plan_time"``.  If the derivation fails (LLM error, unparseable
output, validator rejection, retry exhaustion), the gap is preserved
unchanged and ``proof_resolution_status = "unresolved_gap"`` is recorded.

Scope (strict):
- Only library / cli_tool / web_service are attempted here.  ``worker`` and
  ``frontend`` remain gap-by-default because their operator paths are too
  varied to infer safely from plan-time data; post-execution derivation
  (Phase 5) will have a better shot when actual code exists.
- No retry loops over the validator output — one attempt per mode, then
  fall back to the gap.  The coder is not trusted to iterate on its own
  meaningfulness output here.

HONESTY BEATS FAKE PROOF — the same rule as Fix 73 v4.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from saturnday._types import CoderConfig
from saturnday.plan_parser import validate_proof_meaningfulness
from saturnday.run.planner import FIX73_PROOF_GAP_PREFIX

logger = logging.getLogger(__name__)


# Modes we will attempt to resolve at plan time.  worker/frontend are
# excluded because their operator path cannot be inferred safely before
# code exists.
_RESOLVABLE_MODES_PLAN_TIME: tuple[str, ...] = (
    "library", "cli_tool", "web_service",
)


def proof_is_gap(local_proof_cmd: str) -> bool:
    """Return True iff the proof command is an honest FIX73 gap marker."""
    return FIX73_PROOF_GAP_PREFIX in (local_proof_cmd or "")


def resolve_proof_gap(
    plan: dict,
    coder_config: CoderConfig,
    repo_path: str,
    tickets: list[dict],
) -> tuple[str, str]:
    """Attempt plan-time coder-assisted proof derivation.

    Returns ``(status, source)`` where:
    - status ∈ {"resolved_coder_plan_time", "unresolved_gap", "not_attempted"}
    - source ∈ {"coder_plan_time", "planner_gap", "none"}

    Side effect: on success, mutates ``plan["local_proof_cmd"]`` in place
    with the derived proof command.  On failure, the gap marker is left
    unchanged.
    """
    local_proof = str(plan.get("local_proof_cmd", "") or "")
    operating_mode = str(plan.get("operating_mode", "") or "")
    dependency_profile = str(plan.get("dependency_profile", "") or "")
    proof_realism = str(plan.get("proof_realism", "") or "")
    testing_strategy = str(plan.get("testing_strategy", "") or "")

    # Nothing to do when the plan has a real proof already, or when the
    # plan has no proof at all (legacy / not-runnable).
    if not local_proof:
        return ("not_attempted", "none")
    if not proof_is_gap(local_proof):
        # Already concrete (pipeline / storage_only / operator-supplied).
        return ("resolved_from_planning", "planner_heuristic")
    if operating_mode not in _RESOLVABLE_MODES_PLAN_TIME:
        # worker / frontend stay as gaps at plan time.
        return ("unresolved_gap", "planner_gap")

    # Build the derivation prompt.
    prompt = _build_derivation_prompt(
        operating_mode=operating_mode,
        dependency_profile=dependency_profile,
        proof_realism=proof_realism,
        testing_strategy=testing_strategy,
        external_dependencies=list(plan.get("external_dependencies") or []),
        tickets=tickets,
        project_id=str(plan.get("project_id", "") or "project"),
        notes=str(plan.get("notes", "") or ""),
    )

    try:
        from pathlib import Path
        from saturnday.coder_adapter import call_coder
        messages = [
            {"role": "system",
             "content": "You are a senior test engineer.  Write minimal, "
                        "directly-executable proof commands that exercise "
                        "the real product path.  Never write theatre."},
            {"role": "user", "content": prompt},
        ]
        response = call_coder(coder_config, messages, Path(repo_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Phase 4: coder invocation failed for %s proof derivation: %s",
            operating_mode, exc,
        )
        return ("unresolved_gap", "planner_gap")

    derived = _extract_proof_command(response)
    if not derived:
        logger.info(
            "Phase 4: no parseable proof command returned for %s",
            operating_mode,
        )
        return ("unresolved_gap", "planner_gap")

    # Validate against the Fix 73 meaningfulness rules.
    errs = validate_proof_meaningfulness(
        operating_mode=operating_mode,
        proof_realism=proof_realism or "production_intent",
        dependency_profile=dependency_profile or "self_contained",
        local_proof_cmd=derived,
    )
    if errs:
        logger.info(
            "Phase 4: coder proof rejected by validator for %s: %s",
            operating_mode, errs[0][:200],
        )
        return ("unresolved_gap", "planner_gap")

    # Success: replace the gap with the derived command.
    plan["local_proof_cmd"] = derived
    logger.info(
        "Phase 4: plan-time coder-derived proof accepted for %s",
        operating_mode,
    )
    return ("resolved_coder_plan_time", "coder_plan_time")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_derivation_prompt(
    *,
    operating_mode: str,
    dependency_profile: str,
    proof_realism: str,
    testing_strategy: str,
    external_dependencies: list[str],
    tickets: list[dict],
    project_id: str,
    notes: str,
) -> str:
    """Assemble a concrete, bounded prompt asking the coder to produce a
    single executable proof command for the declared mode.  The prompt
    surfaces every piece of context the planner has AND lists the exact
    meaningfulness rules so the coder can self-check before emitting."""

    ticket_summary = _summarise_tickets_for_proof(tickets)
    ext_line = (
        f"External services: {', '.join(external_dependencies)}"
        if external_dependencies else "External services: none"
    )

    rules = {
        "library": (
            "LIBRARY rules:\n"
            "- Import a real public function from the emitted module.\n"
            "- Call it with a concrete non-trivial input.\n"
            "- Assert a non-trivial equality or structural result.\n"
            "- NOT acceptable: import-only, dir()/hasattr() assertions, "
            "bare 'assert result is not None' on unknown function return."
        ),
        "cli_tool": (
            "CLI_TOOL rules:\n"
            "- Invoke the real CLI module via 'python -m <pkg> <real args>'.\n"
            "- Assert the output with a length threshold (>= N) OR regex "
            "match OR equality check.\n"
            "- NOT acceptable: '--help' / '--version' alone, bare "
            "'test -n \"$OUT\"' over --version output, tautologies."
        ),
        "web_service": (
            "WEB_SERVICE rules:\n"
            "- Start the service process.\n"
            "- Poll readiness (while/for loop + curl/urlopen) — NOT fixed "
            "sleep.\n"
            "- Hit a documented business endpoint over loopback HTTP.\n"
            "- Assert response status + at least one meaningful body "
            "property (JSON key, substring, length threshold).\n"
            "- Tear down cleanly.\n"
            "- NOT acceptable: '/' root-only with just '200 OK + body '\n"
            "length', fixed sleep readiness, pure pytest."
        ),
    }.get(operating_mode, "")

    return (
        f"Product context:\n"
        f"- project_id: {project_id}\n"
        f"- operating_mode: {operating_mode}\n"
        f"- dependency_profile: {dependency_profile}\n"
        f"- proof_realism: {proof_realism}\n"
        f"- testing_strategy: {testing_strategy}\n"
        f"- {ext_line}\n"
        f"\n"
        f"Notes:\n{notes or '(none)'}\n"
        f"\n"
        f"Tickets that will produce the code to be proved:\n{ticket_summary}\n"
        f"\n"
        f"Task: emit ONE shell command (single line or Python heredoc) that\n"
        f"exercises the real operator path for this {operating_mode}.  The\n"
        f"command must run locally without third-party network or live\n"
        f"credentials.\n"
        f"\n"
        f"{rules}\n"
        f"\n"
        f"Output format: return ONLY the shell command in a single fenced\n"
        f"code block (```bash ... ```).  No narration, no explanation,\n"
        f"no alternatives.  If you cannot produce a meaningful proof for\n"
        f"this mode given the context, return the literal string:\n"
        f"INSUFFICIENT_CONTEXT"
    )


def _summarise_tickets_for_proof(tickets: list[dict]) -> str:
    """Compact ticket summary that surfaces ticket goals + acceptance_criteria
    — the fields most relevant to proof derivation."""
    if not tickets:
        return "(no tickets)"
    lines: list[str] = []
    for t in tickets[:10]:  # cap at 10 to keep prompt bounded
        tid = t.get("ticket_id", "?")
        goal = (t.get("goal") or "").replace("\n", " ").strip()
        criteria = t.get("acceptance_criteria") or []
        lines.append(f"- {tid}: {goal[:200]}")
        for c in criteria[:3]:
            if isinstance(c, str):
                lines.append(f"    • {c[:200]}")
    if len(tickets) > 10:
        lines.append(f"- (+ {len(tickets) - 10} more tickets)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(
    r"```(?:bash|sh|shell)?\s*\n(.*?)\n\s*```",
    re.DOTALL,
)


def resolve_proof_gap_post_execution(
    plan: dict,
    coder_config: CoderConfig,
    repo_path: str,
    changed_files: list[str],
    tickets: list[dict],
) -> tuple[str, str]:
    """Phase 5: after tickets have run and code exists on disk, attempt
    coder-assisted proof derivation with the ACTUAL generated code as context.

    Stronger than plan-time derivation because the coder can see:
    - real function signatures (for library mode)
    - real CLI subcommands and argument parsing (for cli_tool)
    - real route definitions and handler code (for web_service)
    - real worker entry points and side effects (for worker)
    - real frontend route/component structure (for frontend)

    All five gap-modes are eligible here — the worker / frontend that we
    refused at plan time can now be derived against real code.

    Returns ``(status, source)`` where:
    - status ∈ {"resolved_coder_post_exec", "unresolved_gap", "not_attempted"}
    - source ∈ {"coder_post_execution", "planner_gap", "none"}

    Side effect: on success, mutates ``plan["local_proof_cmd"]`` in place.
    """
    local_proof = str(plan.get("local_proof_cmd", "") or "")
    operating_mode = str(plan.get("operating_mode", "") or "")

    if not local_proof:
        return ("not_attempted", "none")
    if not proof_is_gap(local_proof):
        return ("resolved_from_planning", "planner_heuristic")
    if operating_mode not in (
        "library", "cli_tool", "web_service", "worker", "frontend",
    ):
        return ("unresolved_gap", "planner_gap")

    code_context = _collect_code_context(repo_path, changed_files, operating_mode)
    if not code_context:
        logger.info(
            "Phase 5: no code context available for %s — staying as gap",
            operating_mode,
        )
        return ("unresolved_gap", "planner_gap")

    prompt = _build_post_execution_prompt(
        operating_mode=operating_mode,
        dependency_profile=str(plan.get("dependency_profile", "") or ""),
        proof_realism=str(plan.get("proof_realism", "") or ""),
        testing_strategy=str(plan.get("testing_strategy", "") or ""),
        external_dependencies=list(plan.get("external_dependencies") or []),
        tickets=tickets,
        project_id=str(plan.get("project_id", "") or "project"),
        notes=str(plan.get("notes", "") or ""),
        code_context=code_context,
    )

    try:
        from pathlib import Path
        from saturnday.coder_adapter import call_coder
        messages = [
            {"role": "system",
             "content": "You are a senior test engineer.  Write minimal, "
                        "directly-executable proof commands that exercise "
                        "the real product path using the actual code below.  "
                        "Never write theatre."},
            {"role": "user", "content": prompt},
        ]
        response = call_coder(coder_config, messages, Path(repo_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Phase 5: coder invocation failed for %s proof derivation: %s",
            operating_mode, exc,
        )
        return ("unresolved_gap", "planner_gap")

    derived = _extract_proof_command(response)
    if not derived:
        logger.info(
            "Phase 5: no parseable proof command returned for %s",
            operating_mode,
        )
        return ("unresolved_gap", "planner_gap")

    errs = validate_proof_meaningfulness(
        operating_mode=operating_mode,
        proof_realism=str(plan.get("proof_realism", "") or "production_intent"),
        dependency_profile=str(plan.get("dependency_profile", "") or "self_contained"),
        local_proof_cmd=derived,
    )
    if errs:
        logger.info(
            "Phase 5: coder proof rejected by validator for %s: %s",
            operating_mode, errs[0][:200],
        )
        return ("unresolved_gap", "planner_gap")

    plan["local_proof_cmd"] = derived
    logger.info(
        "Phase 5: post-execution coder-derived proof accepted for %s",
        operating_mode,
    )
    return ("resolved_coder_post_exec", "coder_post_execution")


def _collect_code_context(
    repo_path: str,
    changed_files: list[str],
    operating_mode: str,
) -> str:
    """Collect a bounded slice of actual code for the coder prompt.

    Prefers files that are most likely to carry the operator path:
    - web_service: files with route / endpoint / handler decorators
    - cli_tool: files with argparse / click / main() entry points
    - library: files with public function / class definitions
    - worker: files named worker.py / tasks.py / jobs.py
    - frontend: package.json, main entry files

    Caps total bytes emitted at 12000 to stay inside coder prompt budgets.
    """
    from pathlib import Path
    repo = Path(repo_path)
    if not repo.is_dir():
        return ""

    # Filter changed_files to Python / JS / TS sources.
    source_files = [
        f for f in (changed_files or [])
        if f.endswith((".py", ".js", ".ts", ".jsx", ".tsx"))
    ]
    if not source_files:
        # Fallback: scan top-level src/ for the mode-relevant files.
        for ext in (".py", ".js", ".ts"):
            for p in (repo / "src").rglob(f"*{ext}") if (repo / "src").is_dir() else []:
                try:
                    rel = str(p.relative_to(repo))
                except ValueError:
                    continue
                source_files.append(rel)
        source_files = source_files[:20]

    if not source_files:
        return ""

    # Rank files by mode-relevance keywords.
    _MODE_KEYWORDS = {
        "web_service": ("@app.", "@router.", "FastAPI", "Flask", "Blueprint",
                         "route(", "get(", "post(", "@api"),
        "cli_tool": ("argparse", "click", "typer", "if __name__",
                     "def main(", "sys.argv"),
        "library": ("def ", "class "),
        "worker": ("run_one", "worker", "dequeue", "process_job", "task"),
        "frontend": ("export default", "export function", "export class"),
    }.get(operating_mode, ("def ", "class "))

    ranked: list[tuple[int, str, str]] = []
    total_bytes = 0
    max_bytes = 12000
    max_per_file = 3000
    for rel in source_files:
        full = repo / rel
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        score = sum(text.count(kw) for kw in _MODE_KEYWORDS)
        if score == 0 and operating_mode != "library":
            continue
        snippet = text[:max_per_file]
        ranked.append((score, rel, snippet))
    ranked.sort(key=lambda t: -t[0])

    parts: list[str] = []
    for score, rel, snippet in ranked:
        chunk = f"### {rel}\n```\n{snippet}\n```\n"
        if total_bytes + len(chunk) > max_bytes:
            break
        parts.append(chunk)
        total_bytes += len(chunk)

    return "\n".join(parts)


def _build_post_execution_prompt(
    *,
    operating_mode: str,
    dependency_profile: str,
    proof_realism: str,
    testing_strategy: str,
    external_dependencies: list[str],
    tickets: list[dict],
    project_id: str,
    notes: str,
    code_context: str,
) -> str:
    """Post-execution prompt — identical to plan-time prompt plus the
    ACTUAL CODE that was produced by the tickets."""
    base = _build_derivation_prompt(
        operating_mode=operating_mode,
        dependency_profile=dependency_profile,
        proof_realism=proof_realism,
        testing_strategy=testing_strategy,
        external_dependencies=external_dependencies,
        tickets=tickets,
        project_id=project_id,
        notes=notes,
    )
    return (
        base + "\n\nActual code produced by the tickets "
        "(most relevant files first, each truncated):\n\n" + code_context
    )


def _extract_proof_command(response: str) -> str:
    """Extract the proof command from the coder's response.

    Accepts either a fenced ``bash``/``sh``/``shell`` code block or (as a
    fallback) a single-line plain response.  Returns ``""`` when the
    response is ``INSUFFICIENT_CONTEXT`` or cannot be parsed.
    """
    text = (response or "").strip()
    if not text or "INSUFFICIENT_CONTEXT" in text:
        return ""

    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()

    # Fallback: if the response is a single line that looks like a command
    # (starts with python/curl/npm/etc. OR contains 'subprocess' / '&&'),
    # accept it.  Otherwise reject.
    single_line = text.strip()
    if "\n" in single_line:
        # Multi-line without a fence — unsafe to interpret.
        return ""
    command_starters = (
        "python", "python3", "npx", "npm", "curl", "wget", "bash", "sh",
        "node", "go", "cargo", "pytest", "set -e",
    )
    if any(single_line.startswith(s) for s in command_starters):
        return single_line
    return ""
