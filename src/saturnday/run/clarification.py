"""Fix 75 — clarification gate before planning.

Pure detection + collection logic.  Interactive prompting and CLI flag
parsing live in the call sites (interactive.py / cli.py); this module
returns triggers and collects answers without doing I/O.

Trigger model: ten ambiguity classes derived from the planning-to-proof
analysis.  Each trigger names a bounded set of answer categories so the
operator picks one; free-form answers are also allowed but discouraged
because they cannot be validated by the planner downstream.

Hard contract: blocking triggers MUST be answered before the planner
proceeds.  In TTY mode the operator is prompted; in non-interactive mode
the answers must come from CLI flags (``--clarify kind=answer``).  If a
blocking trigger is left unanswered in non-interactive mode the planner
caller is expected to refuse to continue — no silent assumption.

Boundary (per the implementation brief):
    Fix 75 is a clarification gate only.  It does NOT implement product
    proof logic or mode taxonomy semantics — those live in Fix 76 and
    Fix 73.  The gate ASKS, persists the answer into
    ``ProjectPlan.clarification_record``, and downstream consumers (the
    planner, the LLM enrichment, future tightening passes) read those
    answers as authoritative operator intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from saturnday._types import ClarificationEntry


# Sentinel used in the persisted record when a non-blocking trigger fired
# in non-interactive mode and the operator did NOT supply a CLI answer.
# The plan is still allowed to proceed (non-blocking), but the record makes
# the unanswered question visible.
UNANSWERED_NONINTERACTIVE = "(not answered: non-interactive, no CLI flag)"


# ---------------------------------------------------------------------------
# Trigger model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClarificationTrigger:
    """A single ambiguity instance detected in the brief.

    Attributes:
        kind: Stable identifier (matches the persisted ``trigger_kind``).
        question: Operator-facing prompt.
        answers: Bounded answer categories.  Free-form answers are accepted
            but the planner cannot validate them downstream.
        blocking: When True, planning must refuse to proceed in
            non-interactive mode unless the operator supplies a CLI answer.
        rationale: Short explanation of WHY this fired (which keywords).
    """

    kind: str
    question: str
    answers: tuple[str, ...]
    blocking: bool = False
    rationale: str = ""


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------


# Mode keyword sets — kept in sync with run.planner._MODE_KEYWORDS but
# expressed as the answer-shaped enums for the operator.
_MODE_HINTS: dict[str, tuple[str, ...]] = {
    "web_service": (
        "api", "rest api", "http server", "endpoint", "service",
        "fastapi", "flask", "django", "express",
    ),
    "cli_tool": (
        "cli", "command-line", "command line", "terminal", "executable",
        "script",
    ),
    "library": ("library", "package", "module", "sdk", "client library"),
    "frontend": (
        "frontend", "react", "vue", "svelte", "spa", "single-page",
        "browser app",
    ),
    "worker": (
        "worker", "queue consumer", "scheduled job", "cron", "background job",
    ),
    "pipeline": (
        "pipeline", "etl", "data pipeline", "ingest", "exporter",
        "importer", "converter",
    ),
    "storage_only": ("storage backend", "data store", "persistence layer"),
}

_ALL_MODE_ANSWERS: tuple[str, ...] = (
    "library", "cli_tool", "web_service", "worker", "pipeline",
    "frontend", "storage_only",
)


def _hits(brief_lower: str, keywords: Iterable[str]) -> list[str]:
    return [kw for kw in keywords if kw in brief_lower]


def detect_ambiguity_triggers(brief: str) -> list[ClarificationTrigger]:
    """Return ordered list of ambiguity triggers detected in the brief.

    Empty list means the brief is concrete enough — planning may proceed
    without a clarification dialog.
    """
    if not brief:
        return [
            ClarificationTrigger(
                kind="mode_ambiguous",
                question="Which best describes the product?",
                answers=_ALL_MODE_ANSWERS,
                blocking=True,
                rationale="empty brief — no signal at all",
            )
        ]

    lower = brief.lower()
    triggers: list[ClarificationTrigger] = []

    # 1. mode_ambiguous --------------------------------------------------
    matched_modes: list[str] = []
    for mode, kws in _MODE_HINTS.items():
        if _hits(lower, kws):
            matched_modes.append(mode)
    matched_modes = sorted(set(matched_modes))
    if len(matched_modes) > 1:
        triggers.append(ClarificationTrigger(
            kind="mode_ambiguous",
            question=(
                "Brief mentions multiple product shapes "
                f"({', '.join(matched_modes)}).  Which one best describes the product?"
            ),
            answers=tuple(matched_modes) + ("split_into_separate_plans",),
            blocking=True,
            rationale=f"mode keywords matched: {', '.join(matched_modes)}",
        ))
    elif not matched_modes:
        triggers.append(ClarificationTrigger(
            kind="mode_ambiguous",
            question="Which best describes the product?",
            answers=_ALL_MODE_ANSWERS,
            blocking=True,
            rationale="brief contains no recognisable mode keywords",
        ))

    # 2. data_source_unspecified ----------------------------------------
    data_use_kw = (
        "process data", "ingest", "import from", "consume data",
        "process the input", "process inputs",
    )
    data_source_kw = (
        "csv", "json", "file", "database", "stream", "fixture",
        "stdin", "from disk", "from url",
    )
    if _hits(lower, data_use_kw) and not _hits(lower, data_source_kw):
        triggers.append(ClarificationTrigger(
            kind="data_source_unspecified",
            question="Where does the input data come from?",
            answers=("real_source", "test_fixture", "demo_seed", "streaming",
                     "to_be_wired_later"),
            blocking=True,
            rationale="brief mentions data processing without naming a source",
        ))

    # 3. external_dependency_unspecified --------------------------------
    ext_kw = (
        "slack", "github api", "send email", "smtp",
        "stripe", "openai", "anthropic api", "claude api",
        "twilio", "sendgrid", "huggingface inference",
        "aws", "gcp", "google cloud", "azure cloud",
        "payment", "cloud function",
    )
    matched_ext = _hits(lower, ext_kw)
    integration_clarity_kw = (
        "credential", "api key", "token", "live integration", "real integration",
        "stub", "fake", "mock-only", "no live",
    )
    if matched_ext and not _hits(lower, integration_clarity_kw):
        triggers.append(ClarificationTrigger(
            kind="external_dependency_unspecified",
            question=(
                f"External services implied ({', '.join(matched_ext)}).  "
                "Is this a live integration or a stub?"
            ),
            answers=("live_integration", "local_fake", "mock_only_test"),
            blocking=True,
            rationale=f"external service keywords: {', '.join(matched_ext)}",
        ))

    # 4. acceptance_target_unclear --------------------------------------
    acc_kw = (
        "tests pass", "deploy", "smoke test", "demo runs", "produces output",
        "integration test", "end-to-end", "end to end",
    )
    if not _hits(lower, acc_kw):
        triggers.append(ClarificationTrigger(
            kind="acceptance_target_unclear",
            question="What's the minimum proof you'd expect when this is done?",
            answers=("smoke_test", "integration_test", "end_to_end_demo",
                     "production_deployment"),
            blocking=False,
            rationale="brief did not state acceptance expectations",
        ))

    # 5. ui_surface_unspecified -----------------------------------------
    ui_kw = ("dashboard", "interface", "report", "view")
    surface_kw = (
        "cli", "command-line", "web page", "html report", "csv output",
        "json output", "browser", "terminal",
    )
    if _hits(lower, ui_kw) and not _hits(lower, surface_kw):
        triggers.append(ClarificationTrigger(
            kind="ui_surface_unspecified",
            question="Which surface does the operator interact with?",
            answers=("cli_table", "web_page", "static_html_report",
                     "file_output_csv_or_json", "no_ui"),
            blocking=True,
            rationale="brief mentions UI without specifying surface",
        ))

    # 6. persistence_unspecified ----------------------------------------
    persist_kw = (
        "track", "remember", "save state", "history", "log entries",
        "user data", "stored",
    )
    storage_kw = (
        "in-memory", "in memory", "sqlite", "postgres", "postgresql",
        "mysql", "mariadb", "redis", "kv store", "file on disk",
        "external db", "external database",
    )
    if _hits(lower, persist_kw) and not _hits(lower, storage_kw):
        triggers.append(ClarificationTrigger(
            kind="persistence_unspecified",
            question="How is state stored?",
            answers=("in_memory_only", "sqlite", "postgres", "external_db",
                     "external_kv", "file_on_disk"),
            blocking=True,
            rationale="brief mentions stateful behaviour without specifying storage",
        ))

    # 7. demo_vs_production --------------------------------------------
    ambig_intent_kw = ("simulate", "sample", "example", "playground", "showcase")
    intent_clarity_kw = (
        "demo", "production-intent", "production intent",
        "real production", "for real", "real product",
    )
    if _hits(lower, ambig_intent_kw) and not _hits(lower, intent_clarity_kw):
        triggers.append(ClarificationTrigger(
            kind="demo_vs_production",
            question=(
                "Brief uses simulate / sample / example wording.  "
                "Is this a demo / training fixture, or production-intent?"
            ),
            answers=("demo", "production_intent"),
            blocking=True,
            rationale="brief uses simulate/sample/example without clarifying intent",
        ))

    # 8. setup_assumptions_unstated ------------------------------------
    setup_asset_kw = (
        "model weights", "pretrained", "checkpoint", "huggingface",
        "dataset", "fits files", "netcdf",
    )
    setup_source_kw = (
        "download from", "url:", "path:", "manual", "checkpoint at",
        "fetch from", "available locally",
    )
    if _hits(lower, setup_asset_kw) and not _hits(lower, setup_source_kw):
        triggers.append(ClarificationTrigger(
            kind="setup_assumptions_unstated",
            question="How is the asset (weights/dataset/etc.) provided?",
            answers=("url", "local_path", "manual_setup", "assume_present"),
            blocking=False,
            rationale="brief implies external asset without specifying source",
        ))

    # 9. integration_scope_unclear --------------------------------------
    if "build" in lower and " with " in lower:
        scope_clarity_kw = (
            "install", "integrate with existing", "build it from scratch",
            "use existing", "depends on",
        )
        if not _hits(lower, scope_clarity_kw):
            triggers.append(ClarificationTrigger(
                kind="integration_scope_unclear",
                question=(
                    "Brief uses 'build X with Y'.  Is Y a dependency to "
                    "install, an existing system to integrate with, or a "
                    "thing to build in this plan?"
                ),
                answers=("install_dependency", "integrate_with_existing",
                         "build_it_in_this_plan"),
                blocking=True,
                rationale="brief uses 'X with Y' without clarifying Y's scope",
            ))

    # 10. operator_identity_unclear ------------------------------------
    op_kw = ("user", "operator", "users", "operators")
    op_clarity_kw = (
        "developer", "end-user", "end user", "ci pipeline",
        "ops team", "admin", "internal", "external user",
    )
    if _hits(lower, op_kw) and not _hits(lower, op_clarity_kw):
        triggers.append(ClarificationTrigger(
            kind="operator_identity_unclear",
            question="Who runs this?",
            answers=("developer_local", "end_user_deployed", "ci", "ops"),
            blocking=False,
            rationale="brief uses 'user' / 'operator' without defining who",
        ))

    # 11. testing_strategy_unspecified (Phase 3) -----------------------
    # Fires when the brief implies external dependencies OR when the
    # operator has already clarified external dependencies (via trigger 3
    # above — included in the same pass because matched_ext carries the
    # signal).  Unlike trigger 3, which asks "live vs stub vs mock-only",
    # this asks HOW the local proof should be constructed when some form
    # of local surrogate is wanted.  The two triggers compose: 3 picks
    # the integration intent; 11 picks the local proof shape.
    #
    # A full strategy dialogue would take several turns; here we capture
    # the operator's intent in one bounded answer so downstream planning
    # can populate testing_strategy, local_proof_cmd, live_proof_cmd, and
    # acceptance_setup consistently.
    ext_seen_in_brief = bool(matched_ext)
    # Only ask if we haven't already had strategy-clarifying language.
    strategy_clarity_kw = (
        "local emulator", "oss substitute", "generated fake", "fake client",
        "recorded fixture", "wiremock", "vcr", "localstack", "mailhog",
        "test-mode key", "test mode", "sandbox key", "sandbox credentials",
    )
    if ext_seen_in_brief and not _hits(lower, strategy_clarity_kw):
        triggers.append(ClarificationTrigger(
            kind="testing_strategy_unspecified",
            question=(
                "How should the local proof be constructed for the external "
                "services?  (live_credentials_gated runs against real sandbox "
                "credentials behind LIVE_PROOF=1; local_emulator uses a "
                "process-local emulator like LocalStack / MinIO / MailHog; "
                "oss_substitute uses an equivalent open-source service; "
                "generated_fake uses an in-process Fake* class; seeded_demo "
                "runs against fixture data and flags proof_realism; "
                "recorded_fixture replays checked-in request/response traces.)"
            ),
            answers=(
                "live_credentials_gated",
                "local_emulator",
                "oss_substitute",
                "generated_fake",
                "seeded_demo",
                "recorded_fixture",
            ),
            blocking=True,
            rationale=(
                "external services implied by brief — local proof shape "
                "must be negotiated before the planner emits "
                "local_proof_cmd / acceptance_setup"
            ),
        ))

    return triggers


# ---------------------------------------------------------------------------
# Answer collection
# ---------------------------------------------------------------------------


def parse_clarify_flags(raw_flags: Iterable[str] | None) -> dict[str, str]:
    """Parse ``--clarify kind=answer`` style strings into a dict.

    Malformed entries (no ``=`` separator, empty kind) are silently dropped
    — the planner refuses to proceed if a blocking trigger remains
    unanswered, so dropping bad flags is safe (operator sees the refusal).
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
        if not kind:
            continue
        out[kind] = answer
    return out


def collect_clarifications(
    triggers: list[ClarificationTrigger],
    *,
    cli_answers: dict[str, str] | None = None,
    interactive: bool = True,
    prompt_fn=None,
) -> tuple[list[ClarificationEntry], list[str]]:
    """Collect operator answers for each trigger.

    Returns ``(entries, unanswered_blocking_kinds)``.  The caller is
    responsible for refusing to plan when ``unanswered_blocking_kinds`` is
    non-empty.

    Args:
        triggers: List of detected triggers (from :func:`detect_ambiguity_triggers`).
        cli_answers: Mapping of trigger kind → answer (from
            ``--clarify`` flags).  These take precedence over the
            interactive prompt.
        interactive: When True, missing CLI answers are collected via
            ``prompt_fn`` (defaults to :func:`_default_prompt`, which
            uses :func:`input` and prints to stdout).  When False, the
            blocking trigger is added to ``unanswered_blocking_kinds``
            and a sentinel entry is recorded for non-blocking triggers.
        prompt_fn: Optional callable taking a :class:`ClarificationTrigger`
            and returning the operator's answer string.  Used to make
            tests deterministic without monkeypatching ``input``.
    """
    cli_answers = cli_answers or {}
    entries: list[ClarificationEntry] = []
    unanswered_blocking: list[str] = []

    if prompt_fn is None:
        prompt_fn = _default_prompt

    for trigger in triggers:
        # CLI answer always wins (operator was explicit).
        if trigger.kind in cli_answers:
            entries.append(ClarificationEntry(
                trigger_kind=trigger.kind,
                question=trigger.question,
                answer=cli_answers[trigger.kind],
                blocking=trigger.blocking,
            ))
            continue

        if interactive:
            answer = prompt_fn(trigger)
            entries.append(ClarificationEntry(
                trigger_kind=trigger.kind,
                question=trigger.question,
                answer=answer,
                blocking=trigger.blocking,
            ))
            continue

        # Non-interactive, no CLI answer.
        if trigger.blocking:
            unanswered_blocking.append(trigger.kind)
        # Record the gap honestly even for non-blocking — evidence-of-dialog.
        entries.append(ClarificationEntry(
            trigger_kind=trigger.kind,
            question=trigger.question,
            answer=UNANSWERED_NONINTERACTIVE,
            blocking=trigger.blocking,
        ))

    return entries, unanswered_blocking


def _default_prompt(trigger: ClarificationTrigger) -> str:
    """Default interactive prompt — prints to stdout, reads from stdin."""
    print(f"\n  Clarification needed: {trigger.kind}")
    if trigger.rationale:
        print(f"  ({trigger.rationale})")
    print(f"  {trigger.question}")
    print(f"  Options: {', '.join(trigger.answers)}")
    try:
        return input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def format_refusal_message(unanswered: list[str]) -> str:
    """Build the operator-facing refusal text for non-interactive mode.

    Surfaces every unanswered blocking trigger so the operator knows
    exactly which ``--clarify`` flags to add and rerun.  Phrased as a
    neutral info message — not a scary error — because the main caller
    is a CLI coder subprocess that is expected to read this, pass the
    missing ``--clarify`` flags, and retry transparently.
    """
    if not unanswered:
        return ""
    lines = [
        "Clarifications needed before planning can proceed.  Pass one "
        "--clarify flag per item, or rerun in an interactive terminal:",
    ]
    for kind in unanswered:
        lines.append(f"  --clarify {kind}=<answer>")
    return "\n".join(lines)
