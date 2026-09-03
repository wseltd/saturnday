"""Premium registration entry point for the Saturnday public/premium split.

This is the ONLY file that bridges premium module internals with the protocol
interfaces defined in ``saturnday.interfaces``.  It acts as the adapter layer
between the concrete premium implementations and the registry-compatible
protocol wrappers.

Usage (premium ``__init__.py``)::

    from saturnday.premium_registration import register_premium_stages
    register_premium_stages()

Each import is individually try/except guarded so that partial premium installs
register whatever is available.  The public package never calls this module;
premium calls it at import time.

Do NOT auto-call at module load.

Adapter classes were moved to ``saturnday_premium._adapters`` in SPLIT-037.
``register_premium_stages()`` imports them from there.
"""
from __future__ import annotations

import logging

from saturnday import capability_registry

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registration entry points
# ---------------------------------------------------------------------------


def register_premium_stages() -> None:
    """Register all available premium stage adapters with the capability registry.

    Called by the premium package ``__init__.py`` at import time.  Each
    import is individually guarded so that a partial premium install registers
    whatever is available.

    Logs INFO on success, DEBUG on ImportError for each stage.
    """
    # 1. security_triage
    try:
        from saturnday.interfaces.triage import STAGE_NAME as _ST_NAME

        from saturnday.security_triage import triage_security_findings as _  # noqa: F401
        from saturnday_premium._adapters import _TriageAdapter

        capability_registry.register(_ST_NAME, _TriageAdapter())
        _logger.info("premium_registration: registered stage %r", _ST_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping security_triage — %s", exc)

    # 2. memory_provider (memory_retrieval + memory_enforcement)
    try:
        from saturnday.interfaces.memory import STAGE_NAME as _MEM_NAME
        from saturnday.run.memory_enforcement import check_enforced_rules as _  # noqa: F401
        from saturnday.run.memory_retrieval import run_staleness_cleanup as _  # noqa: F401
        from saturnday_premium._adapters import _MemoryAdapter

        capability_registry.register(_MEM_NAME, _MemoryAdapter())
        _logger.info("premium_registration: registered stage %r", _MEM_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping memory_provider — %s", exc)

    # 3. spec_verifier (spec_verifier + property_tests + dataflow_checker)
    try:
        from saturnday.interfaces.spec import STAGE_NAME as _SV_NAME
        from saturnday.run.dataflow_checker import check_cross_function_flow as _  # noqa: F401
        from saturnday.run.property_tests import run_property_tests as _  # noqa: F401
        from saturnday.run.spec_verifier import run_spec_assertions as _  # noqa: F401
        from saturnday_premium._adapters import _SpecVerifierAdapter

        capability_registry.register(_SV_NAME, _SpecVerifierAdapter())
        _logger.info("premium_registration: registered stage %r", _SV_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping spec_verifier — %s", exc)

    # 4. impact_analysis
    try:
        from saturnday.interfaces.impact import STAGE_NAME as _IA_NAME
        from saturnday.run.impact_analysis import compute_impact as _  # noqa: F401
        from saturnday_premium._adapters import _ImpactAdapter

        capability_registry.register(_IA_NAME, _ImpactAdapter())
        _logger.info("premium_registration: registered stage %r", _IA_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping impact_analysis — %s", exc)

    # 5. code_reviewer (invoke_role with "code_reviewer" role — premium use)
    try:
        from saturnday.interfaces.reviewer import STAGE_NAME as _CR_NAME
        from saturnday.role_modes import invoke_role as _  # noqa: F401
        from saturnday_premium._adapters import _ReviewerAdapter

        capability_registry.register(_CR_NAME, _ReviewerAdapter())
        _logger.info("premium_registration: registered stage %r", _CR_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping code_reviewer — %s", exc)

    # 6. doc_post_global (claim_verifier + signoff + provisional)
    try:
        from saturnday.document.claim_verifier import run_claim_analysis as _  # noqa: F401
        from saturnday.document.provisional import evaluate_publishability as _  # noqa: F401
        from saturnday.document.signoff import check_signoff_requirements as _  # noqa: F401
        from saturnday.interfaces.doc_stages import STAGE_NAME as _DPG_NAME
        from saturnday_premium._adapters import _DocPostGlobalAdapter

        capability_registry.register(_DPG_NAME, _DocPostGlobalAdapter())
        _logger.info("premium_registration: registered stage %r", _DPG_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping doc_post_global — %s", exc)

    # 7. run_metrics (not a protocol stage — registered as direct callable reference)
    try:
        from saturnday.run.metrics import compute_run_metrics

        capability_registry.register("run_metrics", compute_run_metrics)
        _logger.info("premium_registration: registered stage 'run_metrics'")
    except ImportError as exc:
        _logger.debug("premium_registration: skipping run_metrics — %s", exc)

    # 8. evidence_appender (EvidenceAppender protocol — additive baseline)
    try:
        from saturnday.interfaces.evidence import STAGE_NAME as _EA_NAME
        from saturnday_premium._adapters import _EvidenceAppenderAdapter

        capability_registry.register(_EA_NAME, _EvidenceAppenderAdapter())
        _logger.info("premium_registration: registered stage %r", _EA_NAME)
    except ImportError as exc:
        _logger.debug("premium_registration: skipping evidence_appender — %s", exc)

    # 9. Release governance
    try:
        from saturnday.interfaces.release import STAGE_NAME as _release_stage
        from saturnday_premium._adapters import _ReleaseGovernanceAdapter

        capability_registry.register(_release_stage, _ReleaseGovernanceAdapter())
        _logger.debug("Registered premium stage: %s", _release_stage)
    except ImportError as exc:
        _logger.debug("Skipping release_governance registration: %s", exc)
    except Exception as exc:
        _logger.warning("Failed to register release_governance: %s", exc)


def try_register_premium() -> bool:
    """Attempt premium registration by importing saturnday_premium.

    On first import the premium package bootstraps automatically (checks
    entitlement and calls :func:`register_premium_stages`).  On subsequent
    calls (when the package is already in ``sys.modules``), this function
    calls :func:`register_premium_stages` directly so that the registry is
    re-populated after a ``capability_registry.clear()`` — e.g. in tests.

    Returns:
        ``True`` if at least one premium stage is now registered in the
        capability registry after the attempt; ``False`` if the package is
        not installed or no new stages were registered (e.g. not entitled).
    """
    before = set(capability_registry.registered_stages())
    try:
        import saturnday_premium  # noqa: F401 -- triggers bootstrap on first import
        _logger.info("premium_registration: saturnday-premium package found and imported")
    except ImportError:
        _logger.debug("premium_registration: saturnday-premium not installed")
        return False

    after = set(capability_registry.registered_stages())
    new_stages = after - before

    if not new_stages:
        # Package was already imported (bootstrap already ran once).  If the
        # caller cleared the registry, we must re-register explicitly.
        entitled = getattr(saturnday_premium, "is_entitled", lambda: False)()
        if entitled:
            register_premium_stages()
            after = set(capability_registry.registered_stages())
            new_stages = after - before

    if new_stages:
        _logger.info(
            "premium_registration: registered %d stage(s): %s",
            len(new_stages),
            sorted(new_stages),
        )
        return True
    _logger.debug(
        "premium_registration: no new stages registered (entitled=%s)",
        getattr(saturnday_premium, "is_entitled", lambda: "unknown")(),
    )
    return False
