"""Frozen dataclasses used across saturnday.

All types are immutable (``frozen=True``) so they can be safely shared
between modules without risk of accidental mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Coder configuration
# ---------------------------------------------------------------------------

BackendName = Literal["codex-cli", "claude-cli", "openclaude", "openai", "anthropic"]


@dataclass(frozen=True, slots=True)
class CoderConfig:
    """Configuration for the AI coder backend.

    Attributes:
        backend: Which backend to use for code generation.
        base_url: API base URL (only for ``openai`` / ``anthropic`` backends).
        api_key: API key (only for ``openai`` / ``anthropic`` backends).
        model: Model identifier for API backends.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens in the coder response.
        timeout_s: Request timeout in seconds.
    """

    backend: BackendName
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 16384
    timeout_s: int = 1200
    compact_prompts: bool = False
    large_context: bool = False


# ---------------------------------------------------------------------------
# Ticket specification
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TicketScope:
    """File-scope constraints for a single ticket.

    Attributes:
        allowed_globs: Glob patterns the coder may touch.
        forbidden_globs: Glob patterns the coder must not touch.
        max_files_changed: Budget cap on number of files changed.
        max_total_diff_lines: Budget cap on total diff lines.
        max_per_file_diff_lines: Budget cap on diff lines per file.
    """

    allowed_globs: tuple[str, ...] = ("**",)
    forbidden_globs: tuple[str, ...] = ()
    max_files_changed: int = 20
    max_total_diff_lines: int = 2000
    max_per_file_diff_lines: int = 500


@dataclass(frozen=True, slots=True)
class TicketSpec:
    """Specification for a single coding ticket.

    Attributes:
        ticket_id: Unique identifier (e.g. ``T001``).
        goal: Natural-language description of what to build.
        scope: File-scope and budget constraints.
        verify_cmd: Shell command to verify the ticket (e.g. ``pytest tests/test_foo.py``).
        acceptance_criteria: Human-readable acceptance criteria lines.
        dependencies: Ticket IDs that must complete before this one.
        out_of_scope: Explicit list of things the ticket must NOT do.
        evidence_required: Artifacts that must exist after the ticket completes.
        failure_mode: Expected failure type if the ticket fails
            (``coder_error``, ``spec_ambiguity``, ``dependency_missing``,
            ``governance_block``).
        if_blocked: Operator guidance for when the ticket fails all retries.
        allow_last_resort_split: Fix 18 gate.  ``True`` (default) permits the
            last-resort oversize split after retry exhaustion.  Set to
            ``False`` on child tickets created by a last-resort split to
            prevent recursive splitting.
        atomic: Fix 19 gate.  ``False`` (default) allows normal split
            behaviour.  Set to ``True`` to declare that this ticket must not
            be split under any circumstances — neither by the pre-execution
            split path nor by the Fix 18 last-resort split path.  Use for
            schema migrations, coordinated renames, API contracts, thin
            wiring tickets, and any other structurally indivisible work where
            splitting would leave the codebase in an inconsistent state.
    """

    ticket_id: str
    goal: str
    scope: TicketScope = TicketScope()
    verify_cmd: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    evidence_required: tuple[str, ...] = ()
    failure_mode: str = ""
    if_blocked: str = ""
    allow_last_resort_split: bool = True
    atomic: bool = False


# ---------------------------------------------------------------------------
# Phase definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PhaseDef:
    """Definition of a plan execution phase.

    Attributes:
        phase_id: Unique identifier for the phase.
        name: Human-readable phase name.
        ticket_ids: Ticket IDs belonging to this phase.
    """

    phase_id: str
    name: str = ""
    ticket_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Fix 76 — product operating mode, dependency profile, proof realism
# ---------------------------------------------------------------------------

# Concrete runtime shape of the product.  ``legacy_unclassified`` is the
# explicit compatibility state for plans loaded from pre-Fix-76 files; it must
# never be emitted by a newly-generated plan.
OperatingMode = Literal[
    "legacy_unclassified",
    "library",
    "cli_tool",
    "web_service",
    "worker",
    "pipeline",
    "frontend",
    "storage_only",
]
OPERATING_MODES: tuple[str, ...] = (
    "legacy_unclassified",
    "library",
    "cli_tool",
    "web_service",
    "worker",
    "pipeline",
    "frontend",
    "storage_only",
)
DECLARED_OPERATING_MODES: tuple[str, ...] = tuple(
    m for m in OPERATING_MODES if m != "legacy_unclassified"
)

# What external surface the product depends on.  Independent of mode.
DependencyProfile = Literal[
    "self_contained",
    "local_dependencies",
    "external_dependencies",
]
DEPENDENCY_PROFILES: tuple[str, ...] = (
    "self_contained",
    "local_dependencies",
    "external_dependencies",
)

# Whether the local proof exercises real-shaped data or seeded-demo data.
# Orthogonal to operating_mode: a web_service can be production_intent or
# seeded_demo; a cli_tool can be either; etc.
ProofRealism = Literal[
    "production_intent",
    "seeded_demo",
]
PROOF_REALISMS: tuple[str, ...] = (
    "production_intent",
    "seeded_demo",
)

# Phase 1: testing strategy negotiated with the operator for products that
# declare external dependencies.  Shapes local_proof_cmd, live_proof_cmd,
# and acceptance_setup together.
TestingStrategy = Literal[
    "unspecified",
    "live_credentials_gated",
    "local_emulator",
    "oss_substitute",
    "generated_fake",
    "seeded_demo",
    "recorded_fixture",
]
TESTING_STRATEGIES: tuple[str, ...] = (
    "unspecified",
    "live_credentials_gated",
    "local_emulator",
    "oss_substitute",
    "generated_fake",
    "seeded_demo",
    "recorded_fixture",
)

# Phase 1: proof resolution status — what actually happened to the proof
# between plan generation and run completion.  Populated at the point each
# transition occurs; surfaced in run-summary.json so the operator can tell
# "proof was never defined" from "proof was defined and failed".
ProofResolutionStatus = Literal[
    "not_attempted",             # no declared-mode proof expected (legacy / no plan)
    "unresolved_gap",            # plan has FIX73_PROOF_GAP marker, never replaced
    "resolved_from_planning",    # planner emitted a concrete proof from hints
    "resolved_coder_plan_time",  # plan-time coder derivation replaced a gap
    "resolved_coder_post_exec",  # post-execution coder derivation replaced a gap
    "resolved_operator",         # operator supplied or edited the proof
    "passed",                    # resolution succeeded AND the proof ran and passed
    "failed",                    # resolution succeeded AND the proof ran and failed
]
PROOF_RESOLUTION_STATUSES: tuple[str, ...] = (
    "not_attempted",
    "unresolved_gap",
    "resolved_from_planning",
    "resolved_coder_plan_time",
    "resolved_coder_post_exec",
    "resolved_operator",
    "passed",
    "failed",
)

# Phase 1: which code path produced the final local_proof_cmd.
ProofResolutionSource = Literal[
    "none",                     # no local_proof_cmd
    "planner_heuristic",        # planner's concrete template (pipeline, storage_only)
    "planner_gap",              # planner's FIX73_PROOF_GAP marker
    "coder_plan_time",          # derived via coder at plan time
    "coder_post_execution",     # derived via coder after tickets ran
    "operator_supplied",        # operator hand-authored in the plan
    "operator_edited",          # operator edited a prior proof
    # γ — deterministic template applied from a clarification answer
    # (testing_strategy_unspecified / external_dependency_unspecified).
    # Distinguishes "operator answered a bounded clarification" from
    # "operator hand-authored a proof command" so evidence and DoD
    # narratives can report the real origin honestly.
    "clarification_strategy",
    # δ — operator answered a targeted proof question after Phase 4/5
    # derivation failed, supplying a concrete proof input through the
    # registry (TTY prompt or --proof-answer CLI flag).
    "operator_targeted_answer",
]
PROOF_RESOLUTION_SOURCES: tuple[str, ...] = (
    "none",
    "planner_heuristic",
    "planner_gap",
    "coder_plan_time",
    "coder_post_execution",
    "operator_supplied",
    "operator_edited",
    "clarification_strategy",
    "operator_targeted_answer",
)


@dataclass(frozen=True, slots=True)
class ClarificationEntry:
    """Fix 75: one record of a clarification trigger and its resolution.

    Persisted into ``ProjectPlan.clarification_record`` so future runs / audits
    can see what assumptions were made or refused before planning proceeded.

    Attributes:
        trigger_kind: Stable identifier of the ambiguity class
            (e.g. ``mode_ambiguous``, ``data_source_unspecified``).
        question: The question presented to the operator, verbatim.
        answer: The operator's chosen answer (one of the trigger's bounded
            answer categories) or a free-form override.
        blocking: Whether this trigger blocked planning until answered.  When
            False the trigger was advisory and a default was recorded.
    """

    trigger_kind: str
    question: str
    answer: str
    blocking: bool = False


# ---------------------------------------------------------------------------
# Project plan
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProjectPlan:
    """A validated, dependency-sorted project plan.

    Attributes:
        version: Plan schema version.
        project_id: Identifier for the project being built.
        notes: Free-form notes injected into every coder prompt.
        tickets: Ordered sequence of tickets (dependency-sorted).
        definition_of_done: Markers that must be satisfied for the run to succeed.
        stop_conditions: Normalized markers that abort the run when matched.
        max_project_tickets: Maximum tickets to execute (``None`` = no limit).
        phases: Execution phases grouping tickets.
        mode: Plan mode (``generation`` or ``remediation``).
        default_timeout_seconds: Default per-ticket timeout.
        default_retry_limit: Default per-ticket retry limit.
        governing_goal: Original user intent, preserved verbatim for DoD
            evaluation.  Empty string when not set (backward compatible).
        required_outcomes: Structured success criteria the LLM must check at
            plan completion.  Complements ticket-level ``acceptance_criteria``.
        scoped_categories: Categories of work in scope (e.g. ``"security"``).
            Populated from ``parse_repair_categories`` in repair mode or LLM
            extraction during planning.
        exclusions: Files, modules, or capabilities that must NOT be changed.
        constraints: Hard limits on execution (e.g. ``"Python 3.10 only"``).
        proof_expectations: Evidence the user expects (e.g. ``"tests pass"``).
        acceptance_cmd: Shell command that proves the assembled final product works.
            Run once after all tickets complete, in the project venv.  Empty string
            means no final acceptance check is performed (backward compatible).
    """

    version: int
    project_id: str
    notes: str = ""
    tickets: tuple[TicketSpec, ...] = ()
    definition_of_done: tuple[str, ...] = ("all_tickets_passed",)
    stop_conditions: tuple[str, ...] = ()
    max_project_tickets: int | None = None
    phases: tuple[PhaseDef, ...] = ()
    mode: str = "generation"
    default_timeout_seconds: int = 300
    default_retry_limit: int = 2
    # Plan-governance fields (all optional, backward compatible)
    governing_goal: str = ""
    required_outcomes: tuple[str, ...] = ()
    scoped_categories: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    proof_expectations: tuple[str, ...] = ()
    # Fix 41: plan-level final acceptance command (backward compatible, default empty).
    # DEPRECATED for declared-mode plans (Fix 76).  Honoured ONLY when
    # ``operating_mode == "legacy_unclassified"`` (compatibility path for plans
    # loaded from pre-Fix-76 files).  Declared-mode plans must use
    # ``local_proof_cmd`` instead; the validator rejects a declared-mode plan
    # that sets ``acceptance_cmd``.
    acceptance_cmd: str = ""
    # Fix 52.a: setup steps required before realistic product-mode acceptance.
    # Each entry describes one prerequisite (e.g. "download dataset X",
    # "install rdkit-pypi", "start local API server").  Empty for lightweight
    # products that need no special setup.
    acceptance_setup: tuple[str, ...] = ()
    # Fix 56: authoritative runnable-product flag set by planner, consumed by runner.
    is_runnable_product: bool = False
    # ---- Fix 76: persisted product mode / dependency profile / proof realism ----
    # operating_mode: concrete runtime shape of the product.
    # ``legacy_unclassified`` is the compatibility state for pre-Fix-76 plans
    # (loaded with no operating_mode field).  Newly generated plans must
    # declare a real mode; the planner is forbidden from emitting
    # ``legacy_unclassified``.
    operating_mode: str = "legacy_unclassified"
    # dependency_profile: what external surface the product depends on.
    dependency_profile: str = "self_contained"
    # proof_realism: whether the local proof exercises real-shaped data
    # (``production_intent``) or explicitly seeded fixture data (``seeded_demo``).
    proof_realism: str = "production_intent"
    # external_dependencies: names of third-party services the product calls.
    # Required non-empty when ``dependency_profile == "external_dependencies"``;
    # must be empty otherwise.
    external_dependencies: tuple[str, ...] = ()
    # local_proof_cmd: the canonical primary blocking proof for declared-mode
    # plans.  Runs locally without third-party network, live cloud credentials,
    # production databases, or live external services.  Required for any plan
    # whose ``operating_mode`` is not ``legacy_unclassified``.
    local_proof_cmd: str = ""
    # live_proof_cmd: optional, supplementary, non-blocking proof against real
    # external services.  Only meaningful when
    # ``dependency_profile == "external_dependencies"``; must be empty otherwise.
    # Gated on the ``LIVE_PROOF=1`` env var at runtime.  Failure does NOT
    # downgrade DoD.
    live_proof_cmd: str = ""
    # demo_seed_source: names the fixture/script that seeds the demo path.
    # Required non-empty when ``proof_realism == "seeded_demo"``.
    demo_seed_source: str = ""
    # operator_disclaimer: honest text surfaced verbatim in the run summary.
    # Required non-empty when ``proof_realism == "seeded_demo"``,
    # ``operating_mode == "storage_only"``, or
    # ``dependency_profile == "external_dependencies"``.
    operator_disclaimer: str = ""
    # ---- Fix 75: clarification gate record ----
    # Each entry captures one ambiguity trigger and how it was resolved (or
    # refused) before planning proceeded.  Empty when no clarification was
    # required or supplied.
    clarification_record: tuple[ClarificationEntry, ...] = ()
    # ---- Phase 1: testing strategy ----
    # Negotiated with the operator for products that declare external
    # dependencies.  Drives local_proof_cmd / live_proof_cmd / acceptance_setup
    # generation.  Default "unspecified" when no negotiation was performed.
    testing_strategy: str = "unspecified"
    # ---- Phase 7: proof resolution provenance, persisted from plan time ----
    # Records how the current local_proof_cmd came to be set so the runner's
    # narrative reflects the real origin (planner heuristic vs. coder-derived
    # vs. operator-edited vs. honest gap).  Ends up in ProofResolutionSource
    # vocabulary; see _types.PROOF_RESOLUTION_SOURCES.
    proof_resolution_source: str = "none"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TicketResult:
    """Outcome of a single ticket execution.

    Attributes:
        ticket_id: The ticket identifier.
        disposition: Final outcome (``PASS``, ``FAIL``, ``SKIP``, or ``CODED_UNGOVERNED``).
        attempts: Number of attempts made.
        changed_files: Files modified by the ticket.
        governance_disposition: Governance check result (``PASS`` or ``FAIL``).
        governance_evidence_path: Filesystem path to the raw governance
            evidence JSON produced by ``run_governance_check``. Empty string
            when governance did not run or produced no path.
        error: Error message if the ticket failed.
        verify_cmd_specified: Fix 72b — explicit signal that the ticket plan
            declared a ``verify_cmd``.  Combined with ``verify_cmd_passed``
            this disambiguates the four legitimate states:
            specified=False, passed=None  → no verify_cmd in the plan
            specified=True, passed=True   → ran and passed
            specified=True, passed=False  → ran and failed
            specified=True, passed=None   → SPECIFIED BUT SKIPPED (mechanical
                                            proof was lost — the DoD guard
                                            must fail closed on this state).
        verify_cmd_passed: ``True`` if the ticket's ``verify_cmd`` ran and
            passed, ``False`` if it ran and failed, ``None`` if either no
            ``verify_cmd`` was specified or it was specified but did not
            execute (use ``verify_cmd_specified`` to tell those apart).
        verify_cmd_failure: Short failure summary when ``verify_cmd_passed``
            is ``False``.  Empty string otherwise.
    """

    ticket_id: str
    disposition: Literal["PASS", "FAIL", "SKIP", "CODED_UNGOVERNED"]
    attempts: int = 0
    changed_files: tuple[str, ...] = ()
    governance_disposition: str = ""
    governance_evidence_path: str = ""
    error: str = ""
    failure_category: str = ""
    governance_findings: tuple[dict, ...] = ()
    verify_cmd_specified: bool = False
    verify_cmd_passed: bool | None = None
    verify_cmd_failure: str = ""


@dataclass(frozen=True, slots=True)
class RunResult:
    """Summary of an entire plan execution.

    Attributes:
        project_id: The project identifier from the plan.
        total_tickets: Total number of tickets in the plan.
        passed: Number of tickets that passed.
        failed: Number of tickets that failed.
        skipped: Number of tickets that were skipped.
        ticket_results: Per-ticket outcomes.
        plan_governance_met: Whether the plan-level governing intent was
            satisfied.  ``False`` by default; populated after the DoD role
            pass evaluates ``PLAN_GOVERNANCE_MET`` / ``PLAN_GOVERNANCE_NOT_MET``.
        plan_governance_reason: One-line reason string extracted from the DoD
            role pass output, or a sentinel value such as
            ``"PG_NO_GOVERNANCE"`` when the plan carries no governance fields.
    """

    project_id: str
    total_tickets: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    coded_ungoverned: int = 0
    ticket_results: tuple[TicketResult, ...] = ()
    definition_of_done_met: bool = False
    definition_of_done_classification: str = ""
    stop_reason: str = ""
    evidence_dir: str = ""
    plan_governance_met: bool = False
    plan_governance_reason: str = ""
    # Fix 40: hard cross-ticket consistency gate findings.  Each entry is a
    # dict with keys ``name_a``, ``file_a``, ``value_a``, ``name_b``,
    # ``file_b``, ``value_b``.  Non-empty means the gate failed.
    cross_ticket_consistency_failures: tuple[dict, ...] = ()
    # Fix 41: plan-level final acceptance gate (LEGACY path — Fix 76 + 77).
    # ``acceptance_cmd_passed`` is True when the legacy ``acceptance_cmd`` ran
    # and exited 0, False when it ran and failed, None when no acceptance_cmd
    # was declared OR the plan is declared-mode (local_proof_* fields below
    # carry the result instead).
    acceptance_cmd_passed: bool | None = None
    acceptance_cmd_failure: str = ""
    # Fix 77: local proof — the canonical primary blocking proof for
    # declared-mode plans.  ``local_proof_attempted`` is True iff the runner
    # actually ran the command; ``local_proof_passed`` distinguishes
    # passed (True) / failed (False) / not-attempted (None).  These fields
    # are mutually exclusive with ``acceptance_cmd_*`` per
    # ``operating_mode``: legacy plans populate the acceptance_cmd_* pair,
    # declared-mode plans populate the local_proof_* pair.
    local_proof_attempted: bool = False
    local_proof_passed: bool | None = None
    local_proof_failure: str = ""
    # Fix 77: live proof — supplementary, non-blocking, only meaningful for
    # ``dependency_profile == "external_dependencies"`` and only attempted
    # when the operator sets the ``LIVE_PROOF=1`` env signal.
    # A failed live proof is recorded in evidence and surfaced in the run
    # summary but does NOT trigger the DoD mechanical guard's downgrade.
    live_proof_attempted: bool = False
    live_proof_passed: bool | None = None
    live_proof_failure: str = ""
    # ---- Phase 1: honest proof-resolution accounting ----
    # status: what happened to the proof — see ProofResolutionStatus enum.
    # source: which code path produced the final local_proof_cmd — see
    #         ProofResolutionSource enum.
    # narrative: short human-readable description surfaced in run-summary
    #            and in the DoD output.  Populated at each transition so
    #            operator can tell "proof never defined" from "proof failed".
    proof_resolution_status: str = "not_attempted"
    proof_resolution_source: str = "none"
    proof_resolution_narrative: str = ""
    # α (non-interactive acceptance-setup approval).
    # Records which surface approved the acceptance_setup steps so evidence
    # makes "approved by operator in TTY" vs "approved via --approve-acceptance-setup"
    # vs "approved via SATURNDAY_APPROVE_SETUP=1" distinguishable after the fact.
    # Allowed values:
    #   ""                       — no acceptance_setup was declared in the plan
    #   "interactive"            — operator approved via the TTY prompt
    #   "cli_flag"               — non-TTY, approved via --approve-acceptance-setup
    #   "env_var"                — non-TTY, approved via SATURNDAY_APPROVE_SETUP=1
    #   "declined"               — TTY operator declined the prompt
    #   "blocked_noninteractive" — non-TTY, no approval signal — setup skipped
    acceptance_setup_approval_source: str = ""
    # I.4 (opt-in work-branch feature).
    # Populated when the operator passes ``--work-branch`` on run / resume /
    # rerun-failed / rerun-remaining / start.  Empty strings when the flag
    # was not used — existing operators see no change in evidence shape.
    git_parent_branch: str = ""         # branch operator was on before pre-flight
    git_parent_head_sha: str = ""       # SHA the work branch was created from
    git_work_branch: str = ""           # branch saturnday ran on
    git_work_branch_auto_created: bool = False  # True if saturnday created it
    git_final_head_sha: str = ""        # HEAD SHA at end of the run
