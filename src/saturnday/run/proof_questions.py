"""δ — targeted proof-question registry.

When Saturnday's proof-resolution pipeline (planner heuristic → Phase 4
coder derivation → Phase 5 post-execution coder derivation) cannot close
an operating_mode's proof gap, this module provides a *small*
deterministic registry of targeted operator questions that can close it.

Design constraints:

* **Bounded.**  A fixed dict of question specs keyed by
  ``(operating_mode, gap_reason)``.  No general interviewing engine, no
  free-form chains.
* **Deterministic.**  Each spec names one question, a single answer
  format, and one apply function that transforms the answer into a
  concrete ``local_proof_cmd``.  No branching logic beyond the registry
  key.
* **Non-interactive-safe.**  Every spec declares a CLI flag key
  (``--proof-answer <key>=<value>``) so non-TTY operators can pre-answer.
* **Honest.**  When the operator supplies an empty / malformed answer,
  the resolver leaves the gap in place; when proof can only be closed
  by an answer that wasn't supplied, the caller refuses rather than
  silently downgrading.

Hook point: called from ``ticket_runner.run_plan`` after Phase 5
post-execution derivation fails.  The returned ``(status, source,
narrative)`` is threaded into ``RunResult.proof_resolution_*`` just like
Phase 4/5 results.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProofQuestionSpec:
    """One targeted question for a specific proof-gap shape.

    Attributes:
        kind: Stable identifier used as the ``--proof-answer`` CLI key
            and as the persisted trigger kind in evidence.
        operating_mode: The plan's operating_mode this question applies
            to, or the literal string ``"any"`` when the question is
            mode-agnostic.
        question: Operator-facing prompt.
        prompt_hint: One-line example of the answer format.
        apply_fn: Callable mapping the operator's answer to a concrete
            ``local_proof_cmd`` string.  Returns ``""`` when the answer
            is unusable (empty, malformed, clearly tautological).
    """

    kind: str
    operating_mode: str
    question: str
    prompt_hint: str
    apply_fn: Callable[[str, dict], str]


# ---------------------------------------------------------------------------
# Apply functions — each turns a single operator answer into a real command
# ---------------------------------------------------------------------------


def _apply_cli_entrypoint(answer: str, plan: dict) -> str:
    """Operator gave the concrete CLI invocation.  We wrap it in a proof
    template that captures output and requires non-empty stdout — a pass
    means the exact operator-declared command ran successfully and
    produced real output."""
    cmd = (answer or "").strip()
    if not cmd:
        return ""
    # Safety: reject obviously tautological answers.  "python --version"
    # alone should not pass as proof of a real CLI product.
    if cmd in ("--help", "--version", "-h", "-v"):
        return ""
    # Shell-safe: wrap in /bin/sh -c invocation that redirects stdout to
    # a temp file the python check reads.  Any exit code ≠ 0 fails.
    try:
        shlex.split(cmd)  # syntactic check
    except ValueError:
        return ""
    return (
        f"set -e\n"
        f"OUT=$(mktemp)\n"
        f"trap 'rm -f \"$OUT\"' EXIT\n"
        f"{cmd} > \"$OUT\" 2>&1\n"
        f"test -s \"$OUT\" || (echo 'cli_entrypoint proof: operator-declared "
        f"command produced no output' && exit 1)\n"
        f"echo \"cli_entrypoint proof: $(wc -c < \"$OUT\") bytes of output from: {cmd}\""
    )


def _apply_ws_endpoint(answer: str, plan: dict) -> str:
    """Operator named a concrete business endpoint.

    Expected answer formats (any of):
      * ``/path``
      * ``METHOD /path``
      * ``METHOD /path {json body}``
      * ``METHOD /path expected_key``
    """
    text = (answer or "").strip()
    if not text:
        return ""

    method = "GET"
    path = ""
    body = ""
    expected_key = ""

    parts = text.split(None, 1)
    first = parts[0]
    if first.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        method = first.upper()
        if len(parts) < 2:
            return ""
        rest = parts[1].strip()
    else:
        rest = text

    if rest.startswith("/"):
        path_and_rest = rest.split(None, 1)
        path = path_and_rest[0]
        if len(path_and_rest) == 2:
            after = path_and_rest[1].strip()
            if after.startswith("{"):
                body = after
            else:
                expected_key = after.split()[0] if after else ""
    else:
        return ""

    if not path:
        return ""

    project_id = (plan.get("project_id") or "project").replace("-", "_")

    body_clause = f"-d {shlex.quote(body)}" if body else ""
    assert_clause = (
        f"python -c \"import json,sys; d=json.load(sys.stdin); "
        f"assert {shlex.quote(expected_key)!r}.strip(\\\"'\\\") in d, "
        f"'ws_endpoint proof: expected key {shlex.quote(expected_key)} missing'; "
        f"print('ws_endpoint proof:', list(d.keys())[:5])\""
        if expected_key
        else "python -c \"import sys; body=sys.stdin.read(); "
             "assert len(body) > 0, 'ws_endpoint proof: empty body'; "
             "print('ws_endpoint proof: {} bytes'.format(len(body)))\""
    )

    return (
        f"set -e\n"
        f"python -m {project_id} &\n"
        f"SVC_PID=$!\n"
        f"trap 'kill -TERM $SVC_PID 2>/dev/null; wait $SVC_PID 2>/dev/null; exit' EXIT\n"
        f"for _ in $(seq 1 60); do\n"
        f"  curl -fsS --connect-timeout 1 http://127.0.0.1:8000{path} "
        f"-X {method} {body_clause} > /dev/null 2>&1 && break\n"
        f"  sleep 0.5\n"
        f"done\n"
        f"curl -fsS -X {method} {body_clause} http://127.0.0.1:8000{path} | "
        f"{assert_clause}"
    )


def _apply_library_call(answer: str, plan: dict) -> str:
    """Operator supplied a concrete Python expression that exercises the
    library.  Expected format: ``module.func(args) == expected`` OR a
    full multi-statement snippet.  We run it under python -c and require
    exit 0."""
    text = (answer or "").strip()
    if not text:
        return ""
    # Must contain either a function call or an assert — otherwise
    # trivially true.
    if "(" not in text and "assert" not in text:
        return ""
    snippet = text + "; print('library_call proof: pass')"
    return "python -c " + shlex.quote(snippet)


def _apply_worker_cycle(answer: str, plan: dict) -> str:
    """Operator supplied a concrete worker invocation.  Expected format:
    a python expression that enqueues work, runs one cycle, and asserts
    the side effect.  E.g. ``from pkg import enqueue, run_one; enqueue({'k':'v'}); run_one(); assert get_result('k') == 'v'``."""
    text = (answer or "").strip()
    if not text:
        return ""
    if "assert" not in text:
        return ""
    snippet = text + "; print('worker_cycle proof: pass')"
    return "python -c " + shlex.quote(snippet)


def _apply_frontend_path(answer: str, plan: dict) -> str:
    """Operator named a concrete end-to-end path for the frontend.
    Expected format: the name of an existing playwright / cypress spec
    file, or a URL/selector pair.  We delegate to playwright by default
    but honour the raw answer if it looks like a shell command."""
    text = (answer or "").strip()
    if not text:
        return ""
    if text.endswith((".spec.ts", ".spec.js", ".spec.tsx")):
        return f"npx playwright test {shlex.quote(text)}"
    # Shell command path.
    return text


def _apply_custom_proof(answer: str, plan: dict) -> str:
    """Operator hand-authored a proof command.  Trust it literally
    after a shlex sanity check — this is the escape hatch."""
    text = (answer or "").strip()
    if not text:
        return ""
    try:
        shlex.split(text)
    except ValueError:
        return ""
    return text


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


REGISTRY: tuple[ProofQuestionSpec, ...] = (
    ProofQuestionSpec(
        kind="cli_entrypoint",
        operating_mode="cli_tool",
        question=(
            "Proof automation could not infer a concrete CLI invocation "
            "from your tickets.  What's the exact command that exercises "
            "your product end-to-end?"
        ),
        prompt_hint="e.g. python -m mypkg run --input data.csv --output /tmp/out.json",
        apply_fn=_apply_cli_entrypoint,
    ),
    ProofQuestionSpec(
        kind="ws_endpoint",
        operating_mode="web_service",
        question=(
            "Proof automation could not infer a concrete business endpoint "
            "from your tickets.  Name a documented endpoint (and optionally "
            "a method, request body, or expected response key)."
        ),
        prompt_hint="e.g. GET /users count  OR  POST /ingest {\"id\":1} id",
        apply_fn=_apply_ws_endpoint,
    ),
    ProofQuestionSpec(
        kind="library_call",
        operating_mode="library",
        question=(
            "Proof automation could not infer a concrete library call from "
            "your tickets.  Supply a Python expression that imports the "
            "library and asserts a non-trivial result."
        ),
        prompt_hint="e.g. from mylib import square; assert square(3) == 9",
        apply_fn=_apply_library_call,
    ),
    ProofQuestionSpec(
        kind="worker_cycle",
        operating_mode="worker",
        question=(
            "Proof automation could not infer a worker cycle from your "
            "tickets.  Supply a Python snippet that enqueues a job, runs "
            "one cycle, and asserts the side effect."
        ),
        prompt_hint=(
            "e.g. from pkg import enqueue, run_one; enqueue({'k':'v'}); "
            "run_one(); assert get_result('k') == 'v'"
        ),
        apply_fn=_apply_worker_cycle,
    ),
    ProofQuestionSpec(
        kind="frontend_path",
        operating_mode="frontend",
        question=(
            "Proof automation could not infer an operator path from your "
            "frontend tickets.  Name a playwright / cypress spec file, or "
            "supply a full shell command that exercises one documented "
            "operator path."
        ),
        prompt_hint="e.g. e2e/smoke.spec.ts  OR  npm run test:e2e:smoke",
        apply_fn=_apply_frontend_path,
    ),
    # Mode-agnostic escape hatch — the operator simply declares the proof
    # command directly.  Accepted for any operating_mode.
    ProofQuestionSpec(
        kind="custom_proof",
        operating_mode="any",
        question=(
            "Proof automation could not infer a concrete proof.  Supply "
            "the exact shell command to run as local_proof_cmd."
        ),
        prompt_hint="e.g. pytest tests/e2e -x  OR  bash scripts/smoke.sh",
        apply_fn=_apply_custom_proof,
    ),
)


def specs_for_mode(operating_mode: str) -> list[ProofQuestionSpec]:
    """Return the registry subset applicable to ``operating_mode``,
    mode-specific specs first, ``any`` specs last."""
    mode_specific = [s for s in REGISTRY if s.operating_mode == operating_mode]
    mode_any = [s for s in REGISTRY if s.operating_mode == "any"]
    return mode_specific + mode_any


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def parse_proof_answer_flags(raw_flags: Iterable[str] | None) -> dict[str, str]:
    """Parse ``--proof-answer kind=answer`` strings into a dict.

    Mirrors :func:`saturnday.run.clarification.parse_clarify_flags`.
    Malformed entries are dropped silently — the resolver refuses to
    accept empty answers, so a dropped malformed flag does not produce
    a false pass.
    """
    out: dict[str, str] = {}
    if not raw_flags:
        return out
    for entry in raw_flags:
        if "=" not in entry:
            continue
        kind, _, answer = entry.partition("=")
        kind = kind.strip()
        answer = answer.strip()
        if not kind or not answer:
            continue
        out[kind] = answer
    return out


def resolve_via_questions(
    plan: dict,
    *,
    cli_answers: dict[str, str] | None = None,
    interactive: bool,
    prompt_fn: Callable[[ProofQuestionSpec], str] | None = None,
) -> tuple[str, str, str]:
    """Attempt δ — targeted-question resolution of an unresolved proof.

    Args:
        plan: Mutable plan dict.  On success this function writes the
            derived ``local_proof_cmd`` and sets
            ``proof_resolution_source`` to ``"operator_targeted_answer"``.
        cli_answers: Mapping of spec kind → operator-supplied answer, as
            parsed from ``--proof-answer kind=value`` flags.
        interactive: When True and the CLI answer for the relevant spec
            is absent, ``prompt_fn`` is invoked to collect one at run
            time.  When False the resolver refuses to prompt and returns
            without modifying the plan.
        prompt_fn: Optional prompt callable used in TTY mode.  Defaults
            to a stdin-based prompt.  Injectable for tests.

    Returns:
        ``(status, source, narrative)``.

        * ``status`` ∈ ``{"resolved_operator", "unresolved_gap", "not_attempted"}``
        * ``source`` ∈ ``{"operator_targeted_answer", "planner_gap", "none"}``
        * ``narrative`` is an operator-visible sentence explaining the
          outcome — surfaced in ``RunResult.proof_resolution_narrative``.
    """
    cli_answers = cli_answers or {}
    operating_mode = str(plan.get("operating_mode") or "")
    specs = specs_for_mode(operating_mode)
    if not specs:
        return (
            "not_attempted",
            "none",
            f"δ: no targeted-question registry entries for operating_mode={operating_mode!r}",
        )

    if prompt_fn is None:
        prompt_fn = _default_prompt

    # CLI pre-answers win before any interactive prompt.  Try every spec
    # eligible for this mode in order, mode-specific first.
    for spec in specs:
        raw = cli_answers.get(spec.kind, "")
        if not raw:
            continue
        cmd = spec.apply_fn(raw, plan)
        if cmd:
            plan["local_proof_cmd"] = cmd
            plan["proof_resolution_source"] = "operator_targeted_answer"
            logger.info(
                "δ: CLI pre-answer resolved proof via spec=%s", spec.kind,
            )
            return (
                "resolved_operator",
                "operator_targeted_answer",
                f"δ: operator-supplied --proof-answer {spec.kind}=<answer> "
                f"generated local_proof_cmd.",
            )
        logger.info(
            "δ: CLI pre-answer for %s was rejected as unusable "
            "(empty / tautological / malformed) — continuing",
            spec.kind,
        )

    if not interactive:
        # Non-interactive + no usable CLI answer → refuse.  Caller
        # (ticket_runner) is responsible for surfacing the refusal via
        # the DoD narrative and stop_reason.
        return (
            "unresolved_gap",
            "planner_gap",
            (
                "δ: proof remained unresolved and no usable --proof-answer "
                f"was supplied for operating_mode={operating_mode!r}.  "
                f"Expected one of: "
                + ", ".join(f"--proof-answer {s.kind}=<value>" for s in specs)
            ),
        )

    # Interactive TTY — ask ONE targeted question (the mode-specific
    # spec, or the mode-agnostic custom_proof escape hatch when no
    # mode-specific spec exists).  If the operator declines or supplies
    # an unusable answer, the gap stays and the caller surfaces the
    # refusal narrative — no chained interrogation.
    spec = specs[0]
    raw = prompt_fn(spec)
    if raw:
        cmd = spec.apply_fn(raw, plan)
        if cmd:
            plan["local_proof_cmd"] = cmd
            plan["proof_resolution_source"] = "operator_targeted_answer"
            logger.info(
                "δ: interactive answer resolved proof via spec=%s", spec.kind,
            )
            return (
                "resolved_operator",
                "operator_targeted_answer",
                f"δ: operator answered the {spec.kind} question interactively; "
                f"local_proof_cmd regenerated.",
            )
        logger.info(
            "δ: interactive answer for %s was rejected as unusable", spec.kind,
        )

    return (
        "unresolved_gap",
        "planner_gap",
        (
            "δ: proof remained unresolved after the targeted question.  "
            "Answer was empty, tautological, or malformed."
        ),
    )


def _default_prompt(spec: ProofQuestionSpec) -> str:
    """TTY prompt — prints the question + hint, reads one line."""
    print(f"\n  Proof automation gap: {spec.kind} (operating_mode={spec.operating_mode})")
    print(f"  {spec.question}")
    print(f"  Example: {spec.prompt_hint}")
    print("  (Press Enter to leave the proof unresolved)")
    try:
        return input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def format_refusal_message(
    operating_mode: str, cli_answers: dict[str, str] | None = None,
) -> str:
    """Build the operator-facing refusal text for the non-interactive +
    unresolved-proof path.

    Surfaced on stderr and in the DoD narrative when a run ends with an
    unresolved proof gap and the operator did not supply a usable
    ``--proof-answer``.
    """
    specs = specs_for_mode(operating_mode)
    supplied = list((cli_answers or {}).keys())
    lines = [
        "Saturnday refuses to pass the run: local_proof_cmd remained "
        "unresolved after planner, Phase 4 coder-derivation, and Phase 5 "
        "post-execution derivation.",
        "",
        f"operating_mode: {operating_mode or '(unknown)'}",
    ]
    if supplied:
        lines.append(f"--proof-answer supplied: {', '.join(supplied)} (none were usable)")
    lines.append("")
    lines.append(
        "To close the gap without re-running in an interactive terminal, "
        "supply one of:"
    )
    for spec in specs:
        lines.append(f"  --proof-answer {spec.kind}=<answer>")
        lines.append(f"      {spec.question}")
        lines.append(f"      Example: {spec.prompt_hint}")
    lines.append("")
    lines.append(
        "Alternatively, rerun in an interactive terminal to be prompted, "
        "or hand-author local_proof_cmd in the plan file and set "
        "proof_resolution_source=operator_supplied."
    )
    return "\n".join(lines)


__all__ = [
    "ProofQuestionSpec",
    "REGISTRY",
    "specs_for_mode",
    "parse_proof_answer_flags",
    "resolve_via_questions",
    "format_refusal_message",
]
