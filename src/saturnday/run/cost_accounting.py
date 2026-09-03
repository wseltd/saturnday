"""Cost observability and usage accounting for Saturnday runs.

Fix 14: Four accounting guarantees
----------------------------------
14.1  Future runs record usable cost/usage telemetry — model identity, auth
      mode, whether API-native usage is available, call counts, retry counts,
      and cumulative accounting state.

14.2  Local-interactive mode declares accounting limitations explicitly and
      structurally.  It does not pretend to know exact spend when it does not.

14.3  When exact accounting is impossible, a certainty classification is
      produced: ``exact``, ``strong_estimate``, ``rough_bracket``, or
      ``not_realistically_possible``.  Classification is conservative —
      it never overstates precision.

14.4  Build cost (Saturnday's own LLM spend) and generated-product runtime
      cost are structurally separate fields.  They are never blended.

Certainty classes
-----------------
``exact``
    API backend returned per-call token usage that Saturnday directly consumed.
    Full token counts and model pricing information are present.

``strong_estimate``
    API backend (api_key / ci) but no per-call usage data was returned.
    Model identity is known; input/output sizes can be reconstructed from
    surviving artefacts.  Estimate within ±20% is realistic.

``rough_bracket``
    CLI backend (local_interactive / local_subscription).  The CLI tool is
    opaque — it does not expose token counts to Saturnday.  Call counts and
    retry counts are known from the run ledger.  Character counts are
    estimable from surviving coder_response artefacts.  No per-token billing
    data exists.

``not_realistically_possible``
    Backend unknown, auth unsupported, or artefacts absent.  No meaningful
    accounting can be produced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from saturnday._types import RunResult

AccountingCertainty = Literal[
    "exact",
    "strong_estimate",
    "rough_bracket",
    "not_realistically_possible",
]

_CLI_BACKENDS = frozenset({"codex-cli", "claude-cli", "openclaude", "cursor-cli"})
_API_BACKENDS = frozenset({"openai", "anthropic"})
_INTERACTIVE_AUTH = frozenset({"local_interactive", "local_subscription"})
_API_AUTH = frozenset({"api_key", "ci"})


@dataclass
class BuildCostAccounting:
    """Accounting for Saturnday's own build-time LLM usage.

    This covers the cost of running Saturnday itself — the LLM calls made to
    plan, execute, and govern the build.  It does NOT include any cost the
    generated product will incur at runtime.

    Attributes:
        backend: Backend identifier (e.g. ``"claude-cli"``, ``"anthropic"``).
        auth_mode: Detected auth mode (``"local_interactive"``, ``"api_key"``, etc.).
        api_usage_available: Whether per-call API usage data was available.
            False for all CLI backends.
        call_count: Total LLM calls made (tickets executed + retries).
        retry_count: Total retry calls (calls beyond the first attempt per
            ticket, summed across all tickets).
        certainty: Certainty classification for this accounting record.
            Never overstates precision.
        limitations: Explicit list of what prevents exact accounting.
            Empty only when ``certainty == "exact"``.
        artefact_estimates: Estimates derivable from surviving artefacts
            (e.g. character counts from coder responses).  May be empty.
    """

    backend: str
    auth_mode: str
    api_usage_available: bool
    call_count: int
    retry_count: int
    certainty: AccountingCertainty
    limitations: list[str]
    artefact_estimates: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedProductRuntimeCost:
    """Structural placeholder for the generated product's runtime LLM cost.

    This section is always present and always distinct from build cost.
    Saturnday does not estimate runtime costs at build time — they depend on
    deployed usage patterns, load, and model pricing at deployment time.

    The presence of this section guarantees that build cost and runtime cost
    are never blended in any accounting output.

    Attributes:
        available: Always False at build time.  Set to True only by a
            post-deployment accounting tool that has runtime data.
        note: Human-readable explanation of why this section is a placeholder.
    """

    available: bool = False
    note: str = (
        "Runtime cost of the generated product is not estimated at build time. "
        "This depends on deployed usage patterns, load, and model pricing at "
        "deployment time. Inspect the generated product's LLM call sites to "
        "estimate its runtime spend separately from Saturnday's build cost."
    )


@dataclass
class UsageAccounting:
    """Complete usage accounting record for a Saturnday run.

    Structurally separates build cost (Saturnday's own LLM usage) from
    generated-product runtime cost (always a placeholder at build time).
    They are never blended.

    Attributes:
        build_cost: Saturnday's own build-time accounting.
        generated_product_runtime_cost: Structural placeholder for the
            generated product's runtime cost.  Never estimated at build time.
    """

    build_cost: BuildCostAccounting
    generated_product_runtime_cost: GeneratedProductRuntimeCost = field(
        default_factory=GeneratedProductRuntimeCost
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON output."""
        return {
            "build_cost": asdict(self.build_cost),
            "generated_product_runtime_cost": asdict(self.generated_product_runtime_cost),
        }


def classify_accounting_certainty(
    auth_mode: str,
    backend: str,
    api_usage_data: dict[str, Any] | None = None,
) -> AccountingCertainty:
    """Classify accounting certainty given what is known about the run.

    Conservative — never overstates precision.

    Args:
        auth_mode: The detected auth mode for this backend.
        backend: The backend identifier.
        api_usage_data: Per-call usage data returned by the API, if any.
            Pass ``None`` or an empty dict when not available.

    Returns:
        One of ``"exact"``, ``"strong_estimate"``, ``"rough_bracket"``, or
        ``"not_realistically_possible"``.
    """
    if not backend or auth_mode == "unsupported":
        return "not_realistically_possible"

    if backend in _CLI_BACKENDS or auth_mode in _INTERACTIVE_AUTH:
        # CLI tools do not expose token counts — rough bracket only
        return "rough_bracket"

    if backend in _API_BACKENDS or auth_mode in _API_AUTH:
        if api_usage_data:
            return "exact"
        return "strong_estimate"

    return "not_realistically_possible"


def _build_limitations(
    auth_mode: str,
    backend: str,
    api_usage_available: bool,
) -> list[str]:
    """Build an explicit list of accounting limitation strings.

    Args:
        auth_mode: The detected auth mode.
        backend: The backend identifier.
        api_usage_available: Whether API usage data was available.

    Returns:
        List of human-readable limitation strings.  Empty only when
        ``api_usage_available`` is True and certainty would be ``"exact"``.
    """
    limitations: list[str] = []

    if not backend:
        limitations.append(
            "Backend identity unknown — no accounting is possible without "
            "knowing which model was used."
        )
        return limitations

    if backend in _CLI_BACKENDS:
        limitations.append(
            f"CLI backend '{backend}' does not expose token usage to callers. "
            f"Saturnday invokes the CLI as a subprocess and cannot intercept "
            f"per-call token counts."
        )

    if auth_mode in _INTERACTIVE_AUTH:
        limitations.append(
            f"Auth mode '{auth_mode}': billing is subscription-based and not "
            f"per-token. There is no per-call billing record accessible to "
            f"Saturnday. Exact spend cannot be determined from this run's "
            f"artefacts."
        )
    elif auth_mode == "unsupported":
        limitations.append(
            "Auth mode 'unsupported': this backend configuration does not "
            "provide a billing surface accessible to Saturnday."
        )
    elif not auth_mode:
        limitations.append(
            "Auth mode not recorded for this run. Accounting certainty is "
            "limited because billing model is unknown."
        )

    if not api_usage_available and backend in _API_BACKENDS:
        limitations.append(
            f"API backend '{backend}' was detected but no per-call usage data "
            f"was returned in this run. Token counts are estimated from "
            f"artefacts rather than from API response metadata."
        )

    return limitations


def build_usage_accounting(
    backend: str,
    auth_mode: str,
    call_count: int,
    retry_count: int,
    api_usage_data: dict[str, Any] | None = None,
    artefact_estimates: dict[str, Any] | None = None,
) -> UsageAccounting:
    """Build a complete UsageAccounting record from run information.

    Args:
        backend: Backend identifier.
        auth_mode: Detected auth mode.
        call_count: Total LLM calls (tickets executed + retries).
        retry_count: Total retry calls across all tickets.
        api_usage_data: Per-call usage data from the API, if available.
        artefact_estimates: Estimates derived from surviving artefacts.

    Returns:
        A UsageAccounting record with separate build_cost and
        generated_product_runtime_cost sections.  Never blends them.
    """
    api_usage_available = bool(api_usage_data)
    certainty = classify_accounting_certainty(auth_mode, backend, api_usage_data)
    limitations = _build_limitations(auth_mode, backend, api_usage_available)

    build_cost = BuildCostAccounting(
        backend=backend,
        auth_mode=auth_mode,
        api_usage_available=api_usage_available,
        call_count=call_count,
        retry_count=retry_count,
        certainty=certainty,
        limitations=limitations,
        artefact_estimates=artefact_estimates or {},
    )

    return UsageAccounting(
        build_cost=build_cost,
        generated_product_runtime_cost=GeneratedProductRuntimeCost(),
    )


def accounting_from_run_result(
    run_result: "RunResult",
    backend: str = "",
    auth_mode: str = "",
    api_usage_data: dict[str, Any] | None = None,
) -> UsageAccounting:
    """Convenience builder that extracts call/retry counts from a RunResult.

    Args:
        run_result: A completed RunResult.
        backend: Backend identifier.
        auth_mode: Detected auth mode.
        api_usage_data: API usage data if available.

    Returns:
        UsageAccounting built from the run's ticket data.
    """
    call_count = sum(tr.attempts for tr in run_result.ticket_results)
    retry_count = sum(
        max(0, tr.attempts - 1) for tr in run_result.ticket_results
    )
    return build_usage_accounting(
        backend=backend,
        auth_mode=auth_mode,
        call_count=call_count,
        retry_count=retry_count,
        api_usage_data=api_usage_data,
    )
