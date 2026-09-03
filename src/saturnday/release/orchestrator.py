"""Release preflight orchestrator for ``saturnday release-preflight``.

Coordinates the full release-preflight pipeline:

1. Build artefact from source (if no pre-built path provided).
2. Unpack and inventory the artefact.
3. Run all registered release checks.
4. Query the premium capability registry for additional governance.
5. Compute the overall disposition.
6. Write evidence.
7. Return the :class:`ReleaseEvidencePack`.

Check modules are imported dynamically from ``saturnday.release.checks.<name>``.
If a check module cannot be imported (not yet implemented), the check is
recorded as SKIPPED with a note — no exception is raised.  This keeps the
orchestrator forward-compatible as individual check modules are added one by one
across RS-007 through RS-012.

Usage::

    from pathlib import Path
    from saturnday.release.orchestrator import run_release_preflight

    pack = run_release_preflight(
        repo_path=Path("."),
        artefact_type="python",
    )
    print(pack.disposition)       # PASS / WARN / FAIL
    print(pack.evidence_path)     # .saturnday/evidence/release/{run_id}/
"""
from __future__ import annotations

import importlib
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default check registry
# Each entry is (check_module_name, rule_id).
# Module is loaded from ``saturnday.release.checks.<check_module_name>``.
# If the import fails the check is recorded as SKIPPED.
# ---------------------------------------------------------------------------

DEFAULT_CHECKS: list[tuple[str, str]] = [
    ("source_map_blocker", "REL-001"),
    ("secrets_in_artefact", "REL-002"),
    ("internal_file_blocker", "REL-003"),
    ("allowlist_manifest", "REL-004"),
    ("release_diff", "REL-005"),
]


# ---------------------------------------------------------------------------
# Public result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ReleasePreflightResult:
    """Aggregated result from the full release-preflight pipeline.

    Attributes:
        disposition:    Overall verdict — ``"PASS"``, ``"WARN"``, or ``"FAIL"``.
        evidence_pack:  Complete :class:`~saturnday.release.evidence.ReleaseEvidencePack`
                        with all check results, inventory, and capability state.
        evidence_path:  Directory on disk where evidence was written.  ``None``
                        if writing was skipped or failed.
        error:          Non-empty string when the pipeline itself failed
                        (e.g. build failed, invalid artefact).  When set, the
                        other fields may be incomplete.
    """

    disposition: str
    evidence_pack: Any  # ReleaseEvidencePack — avoids circular type annotation
    evidence_path: Optional[Path] = None
    error: str = ""


# ---------------------------------------------------------------------------
# Orchestrator entry point
# ---------------------------------------------------------------------------


def run_release_preflight(
    repo_path: Path,
    artefact_type: str = "python",
    wheel_path: Optional[Path] = None,
    sdist_path: Optional[Path] = None,
    tarball_path: Optional[Path] = None,
    image_ref: Optional[str] = None,
    image_archive_path: Optional[Path] = None,
    baseline_path: Optional[Path] = None,
    manifest_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> ReleasePreflightResult:
    """Run the full release-preflight pipeline and return evidence.

    The function is intentionally non-fatal: every error path returns a
    :class:`ReleasePreflightResult` with a non-empty ``error`` field rather
    than raising.  The caller is responsible for interpreting the disposition
    and exiting with the appropriate code.

    Args:
        repo_path:           Path to the project repository.  Used as the
                             build root when no pre-built artefact paths are
                             supplied.
        artefact_type:       One of ``"python"``, ``"npm"``, or ``"oci"``.
                             Determines which builder and inspector modules
                             are used.
        wheel_path:          Path to a pre-built wheel.  Python only.
        sdist_path:          Path to a pre-built sdist.  Python only.
        tarball_path:        Path to a pre-built npm ``.tgz`` tarball.
                             npm only.
        image_ref:           OCI image reference (e.g. ``"myapp:latest"``).
                             OCI only.  Requires Docker or Skopeo.
        image_archive_path:  Path to a pre-saved OCI archive (``.tar`` from
                             ``docker save``).  OCI only.  Takes precedence
                             over *image_ref* when both are supplied.
        baseline_path:       Path to a baseline artefact for diff comparison.
                             Optional.  When absent, release-diff check is
                             skipped.
        manifest_path:       Path to ``.saturnday-release-manifest.yaml``.
                             Optional.  When absent, allowlist checks use
                             deny-by-default rules.
        output_dir:          Directory to write evidence into.  When ``None``,
                             defaults to ``repo_path / ".saturnday" /
                             "evidence" / "release" / {run_id}``.

    Returns:
        :class:`ReleasePreflightResult` with disposition, evidence pack,
        evidence path, and any error string.
    """
    from saturnday.release.evidence import (
        ReleaseEvidencePack,
        ReleaseCheckResult,
        compute_release_disposition,
        generate_release_run_id,
        write_release_evidence,
        RELEASE_SCHEMA_VERSION,
    )
    from saturnday.shared.evidence_schema import build_capability_state

    repo_path = Path(repo_path).resolve()
    run_id = generate_release_run_id()

    logger.info(
        "release-preflight started: run_id=%s type=%s repo=%s",
        run_id, artefact_type, repo_path,
    )

    # ------------------------------------------------------------------
    # Step 1: Resolve or build the artefact
    # ------------------------------------------------------------------
    inventory: Any = None
    unpack_dir: Optional[Path] = None
    actual_artefact_path: Optional[Path] = None
    build_temp_dir = None  # TemporaryDirectory to keep alive through checks

    try:
        if artefact_type == "python":
            inventory, unpack_dir, actual_artefact_path, build_temp_dir = (
                _prepare_python_artefact(repo_path, wheel_path, sdist_path)
            )
        elif artefact_type == "npm":
            inventory, unpack_dir, actual_artefact_path, build_temp_dir = (
                _prepare_npm_artefact(repo_path, tarball_path)
            )
        elif artefact_type == "oci":
            inventory, unpack_dir, actual_artefact_path, build_temp_dir = (
                _prepare_oci_artefact(image_ref, image_archive_path)
            )
        else:
            return _error_result(
                f"Unsupported artefact_type: {artefact_type!r}. "
                f"Expected 'python', 'npm', or 'oci'.",
                run_id=run_id,
                artefact_type=artefact_type,
            )
    except Exception as exc:
        logger.error("Artefact preparation failed: %s", exc)
        return _error_result(
            f"Artefact preparation failed: {exc}",
            run_id=run_id,
            artefact_type=artefact_type,
        )

    # ------------------------------------------------------------------
    # Step 2: Load manifest (if provided)
    # ------------------------------------------------------------------
    manifest: Optional[dict[str, Any]] = None
    if manifest_path is not None:
        manifest = _load_manifest(manifest_path)

    # ------------------------------------------------------------------
    # Step 3: Run all registered checks
    # ------------------------------------------------------------------
    check_results: list[ReleaseCheckResult] = []

    for check_module_name, rule_id in DEFAULT_CHECKS:
        result = _run_check(
            check_module_name=check_module_name,
            rule_id=rule_id,
            inventory=inventory,
            unpack_dir=unpack_dir,
            manifest=manifest,
            baseline_path=baseline_path,
        )
        check_results.append(result)
        logger.debug(
            "check %s (%s): %s (%d finding(s))",
            rule_id, check_module_name, result.status, len(result.findings),
        )

    # ------------------------------------------------------------------
    # Step 4: Premium governance hook (release_governance)
    # ------------------------------------------------------------------
    try:
        from saturnday import capability_registry as _cr
        premium_handler = _cr.get("release_governance")
    except Exception:
        premium_handler = None

    if premium_handler is not None:
        logger.info("Running premium release_governance handler")
        try:
            # RS-018: pass inventory metadata through policy dict so that
            # registry-specific checks can inspect artefact metadata without
            # changing the apply_policy protocol signature.
            policy_with_context: dict = dict(manifest or {})
            policy_with_context["_inventory_metadata"] = (
                inventory.metadata if hasattr(inventory, "metadata") else {}
            )
            policy_with_context["_artefact_type"] = (
                inventory.artefact_type if hasattr(inventory, "artefact_type") else artefact_type
            )
            extra_results = premium_handler.apply_policy(
                check_results=check_results,
                policy=policy_with_context,
            )
            if extra_results:
                check_results = extra_results
                logger.debug(
                    "Premium release_governance returned %d result(s)",
                    len(extra_results),
                )
        except Exception as exc:
            logger.warning("Premium release_governance hook failed: %s", exc)

    # ------------------------------------------------------------------
    # Step 5: Compute disposition (after optional premium extension)
    # ------------------------------------------------------------------
    disposition, disposition_reasons = compute_release_disposition(check_results)

    # ------------------------------------------------------------------
    # Step 6: Compute evidence output directory
    # ------------------------------------------------------------------
    if output_dir is None:
        evidence_dir = repo_path / ".saturnday" / "evidence" / "release" / run_id
    else:
        evidence_dir = Path(output_dir).resolve()

    # ------------------------------------------------------------------
    # Step 7: Build and write evidence pack
    # ------------------------------------------------------------------
    from dataclasses import asdict
    inventory_dict: dict[str, Any] = {}
    if inventory is not None:
        try:
            inventory_dict = asdict(inventory)
        except Exception:
            inventory_dict = {"error": "could not serialise inventory"}

    capability_state = build_capability_state()

    pack = ReleaseEvidencePack(
        schema_version=RELEASE_SCHEMA_VERSION,
        run_id=run_id,
        artefact_type=artefact_type,
        artefact_path=str(actual_artefact_path) if actual_artefact_path else "",
        artefact_sha256=inventory.artefact_sha256 if inventory else "",
        inventory=inventory_dict,
        check_results=check_results,
        disposition=disposition,
        disposition_reasons=disposition_reasons,
        baseline_artefact_path=str(baseline_path) if baseline_path else "",
        release_diff={},
        capability_state=capability_state,
    )

    written_path: Optional[Path] = None
    try:
        written_path = write_release_evidence(pack, evidence_dir)
        logger.info("Release evidence written to %s", written_path)
    except OSError as exc:
        logger.error("Failed to write release evidence: %s", exc)

    # Cleanup build temp dir (if we created one)
    if build_temp_dir is not None:
        try:
            build_temp_dir.cleanup()
        except Exception:
            pass

    logger.info(
        "release-preflight complete: run_id=%s disposition=%s",
        run_id, disposition,
    )

    return ReleasePreflightResult(
        disposition=disposition,
        evidence_pack=pack,
        evidence_path=written_path,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _prepare_python_artefact(
    repo_path: Path,
    wheel_path: Optional[Path],
    sdist_path: Optional[Path],
) -> tuple[Any, Path, Path, Any]:
    """Resolve or build a Python artefact and return its inventory.

    Returns:
        Tuple of ``(inventory, unpack_dir, artefact_path, build_temp_dir)``.
        ``build_temp_dir`` is a :class:`tempfile.TemporaryDirectory` instance
        (or ``None``) that the caller must call ``.cleanup()`` on when done.

    Raises:
        ValueError: When neither wheel nor sdist is supplied and the build
                    fails or produces no output artefacts.
    """
    from saturnday.release.wheel_inspector import inspect_wheel
    from saturnday.release.sdist_inspector import inspect_sdist

    build_temp = None

    # Prefer wheel if provided; fall back to sdist; otherwise build.
    if wheel_path is not None:
        actual = Path(wheel_path).resolve()
        inventory, unpack_dir = inspect_wheel(actual)
        return inventory, unpack_dir, actual, build_temp

    if sdist_path is not None:
        actual = Path(sdist_path).resolve()
        inventory, unpack_dir = inspect_sdist(actual)
        return inventory, unpack_dir, actual, build_temp

    # Neither supplied — build from source.
    from saturnday.release.python_builder import build_python_artefacts

    build_temp = tempfile.TemporaryDirectory(prefix="saturnday_build_")
    logger.info("Building Python artefacts from %s ...", repo_path)
    build_result = build_python_artefacts(
        repo_path=repo_path,
        output_dir=Path(build_temp.name),
    )
    if build_result.exit_code != 0:
        build_temp.cleanup()
        raise ValueError(
            f"python -m build failed (exit {build_result.exit_code}):\n"
            f"{build_result.stderr}"
        )

    # Prefer wheel over sdist when both were produced.
    if build_result.wheel_path is not None:
        actual = build_result.wheel_path
        inventory, unpack_dir = inspect_wheel(actual)
    elif build_result.sdist_path is not None:
        actual = build_result.sdist_path
        inventory, unpack_dir = inspect_sdist(actual)
    else:
        build_temp.cleanup()
        raise ValueError(
            "Build succeeded but no wheel or sdist was found in the output directory."
        )

    return inventory, unpack_dir, actual, build_temp


def _prepare_npm_artefact(
    repo_path: Path,
    tarball_path: Optional[Path],
) -> tuple[Any, Path, Path, Any]:
    """Resolve or build an npm artefact and return its inventory.

    Returns:
        Tuple of ``(inventory, unpack_dir, artefact_path, build_temp_dir)``.

    Raises:
        ValueError: When no tarball is supplied and the npm build fails.
    """
    from saturnday.release.npm_inspector import inspect_npm_tarball

    build_temp = None

    if tarball_path is not None:
        actual = Path(tarball_path).resolve()
        inventory, unpack_dir = inspect_npm_tarball(actual)
        return inventory, unpack_dir, actual, build_temp

    # Build from source.
    from saturnday.release.npm_builder import build_npm_artefact

    build_temp = tempfile.TemporaryDirectory(prefix="saturnday_npm_build_")
    logger.info("Building npm artefact from %s ...", repo_path)
    build_result = build_npm_artefact(
        repo_path=repo_path,
        output_dir=Path(build_temp.name),
    )
    if build_result.exit_code != 0:
        build_temp.cleanup()
        raise ValueError(
            f"npm pack failed (exit {build_result.exit_code}):\n"
            f"{build_result.stderr}"
        )

    if build_result.tarball_path is None:
        build_temp.cleanup()
        raise ValueError(
            "npm pack succeeded but no tarball was found in the output directory."
        )

    actual = build_result.tarball_path
    inventory, unpack_dir = inspect_npm_tarball(
        actual,
        npm_file_list=build_result.npm_file_list or None,
    )
    return inventory, unpack_dir, actual, build_temp


def _prepare_oci_artefact(
    image_ref: Optional[str],
    image_archive_path: Optional[Path],
) -> tuple[Any, Path, Optional[Path], None]:
    """Resolve or extract an OCI container image artefact and return its inventory.

    Prefers *image_archive_path* (pre-saved archive) over *image_ref* (live
    Docker/Skopeo pull).  At least one must be non-``None``.

    Returns:
        Tuple of ``(inventory, unpack_dir, artefact_path, None)``.
        The fourth element is always ``None`` — OCI inspection does not create
        a managed :class:`tempfile.TemporaryDirectory` that needs cleanup;
        the temporary directory is owned by the inspector.

    Raises:
        ValueError: When neither *image_ref* nor *image_archive_path* is given,
                    or when the archive path does not exist.
    """
    from saturnday.release.oci_inspector import inspect_oci_archive, inspect_oci_image

    if image_archive_path is not None:
        actual = Path(image_archive_path).resolve()
        logger.info("Inspecting OCI archive: %s", actual)
        inventory, unpack_dir = inspect_oci_archive(actual)
        return inventory, unpack_dir, actual, None

    if image_ref is not None:
        logger.info("Inspecting OCI image: %s", image_ref)
        inventory, unpack_dir = inspect_oci_image(image_ref)
        return inventory, unpack_dir, None, None

    raise ValueError(
        "OCI artefact preparation requires either image_ref or image_archive_path."
    )


def _load_manifest(manifest_path: Path) -> Optional[dict[str, Any]]:
    """Load the release manifest YAML file.

    Returns the parsed dict on success or ``None`` if the file cannot be read
    or parsed.  Non-fatal: warnings are logged.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "PyYAML not installed — manifest %s will be ignored.  "
            "Install pyyaml to enable allowlist checks.",
            manifest_path,
        )
        return None

    try:
        text = Path(manifest_path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            logger.warning("Manifest %s did not parse to a dict — ignored.", manifest_path)
            return None
        logger.debug("Loaded release manifest from %s", manifest_path)
        return data
    except OSError as exc:
        logger.warning("Could not read manifest %s: %s", manifest_path, exc)
        return None
    except Exception as exc:
        logger.warning("Could not parse manifest %s: %s", manifest_path, exc)
        return None


def _run_check(
    check_module_name: str,
    rule_id: str,
    inventory: Any,
    unpack_dir: Optional[Path],
    manifest: Optional[dict[str, Any]],
    baseline_path: Optional[Path],
) -> "ReleaseCheckResult":  # type: ignore[name-defined]  # forward ref OK
    """Dynamically import and execute a single check module.

    The check interface contract::

        def run_check(
            inventory: ArtefactInventory,
            unpack_dir: Path,
            manifest: dict | None,
        ) -> ReleaseCheckResult

    If the module cannot be imported, returns a SKIPPED result.
    If the check raises, returns a WARN result with the exception message.
    """
    from saturnday.release.evidence import ReleaseCheckResult

    module_fqn = f"saturnday.release.checks.{check_module_name}"
    t0 = time.monotonic()

    try:
        mod = importlib.import_module(module_fqn)
    except ImportError:
        logger.debug("Check module %s not yet available — SKIPPED", module_fqn)
        return ReleaseCheckResult(
            name=check_module_name,
            rule_id=rule_id,
            status="SKIPPED",
            severity="info",
            findings=[],
            files_checked=0,
            elapsed_s=0.0,
        )

    try:
        check_fn = getattr(mod, "run_check")
    except AttributeError:
        logger.warning(
            "Check module %s has no run_check() function — SKIPPED", module_fqn
        )
        return ReleaseCheckResult(
            name=check_module_name,
            rule_id=rule_id,
            status="SKIPPED",
            severity="info",
            findings=[{"message": f"{module_fqn} missing run_check() function"}],
            files_checked=0,
            elapsed_s=0.0,
        )

    try:
        # The release_diff check accepts an extra baseline keyword argument.
        # All other checks use the standard three-parameter signature.
        if check_module_name == "release_diff" and baseline_path is not None:
            from saturnday.release.wheel_inspector import inspect_wheel
            from saturnday.release.sdist_inspector import inspect_sdist
            from saturnday.release.npm_inspector import inspect_npm_tarball

            _baseline_ext = str(baseline_path).lower()
            _baseline_tmp = __import__("tempfile").TemporaryDirectory(
                prefix="saturnday_baseline_"
            )
            try:
                if _baseline_ext.endswith(".whl"):
                    baseline_inv, _ = inspect_wheel(baseline_path, Path(_baseline_tmp.name))
                elif _baseline_ext.endswith(".tar.gz"):
                    # Could be sdist or npm tarball; use artefact_type hint from inventory.
                    if inventory is not None and getattr(inventory, "artefact_type", "") == "npm":
                        baseline_inv, _ = inspect_npm_tarball(baseline_path, Path(_baseline_tmp.name))
                    else:
                        baseline_inv, _ = inspect_sdist(baseline_path, Path(_baseline_tmp.name))
                elif _baseline_ext.endswith(".tgz"):
                    baseline_inv, _ = inspect_npm_tarball(baseline_path, Path(_baseline_tmp.name))
                else:
                    baseline_inv = None
                    logger.warning(
                        "Cannot determine inspector for baseline artefact %s — "
                        "release_diff will receive baseline=None",
                        baseline_path,
                    )

                result = check_fn(
                    inventory=inventory,
                    unpack_dir=unpack_dir,
                    manifest=manifest,
                    baseline=baseline_inv,
                )
            finally:
                _baseline_tmp.cleanup()
        else:
            result = check_fn(
                inventory=inventory,
                unpack_dir=unpack_dir,
                manifest=manifest,
            )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.warning("Check %s raised an exception: %s", check_module_name, exc)
        return ReleaseCheckResult(
            name=check_module_name,
            rule_id=rule_id,
            status="WARN",
            severity="warning",
            findings=[{"message": f"check raised exception: {exc}"}],
            files_checked=0,
            elapsed_s=elapsed,
        )

    return result


def _error_result(
    error_msg: str,
    *,
    run_id: str,
    artefact_type: str,
) -> ReleasePreflightResult:
    """Build a ReleasePreflightResult representing a fatal pipeline error."""
    from saturnday.release.evidence import (
        ReleaseEvidencePack,
        RELEASE_SCHEMA_VERSION,
    )
    from saturnday.shared.evidence_schema import build_capability_state

    pack = ReleaseEvidencePack(
        schema_version=RELEASE_SCHEMA_VERSION,
        run_id=run_id,
        artefact_type=artefact_type,
        artefact_path="",
        artefact_sha256="",
        inventory={},
        check_results=[],
        disposition="FAIL",
        disposition_reasons=[error_msg],
        capability_state=build_capability_state(),
    )
    return ReleasePreflightResult(
        disposition="FAIL",
        evidence_pack=pack,
        evidence_path=None,
        error=error_msg,
    )


__all__ = [
    "DEFAULT_CHECKS",
    "ReleasePreflightResult",
    "run_release_preflight",
]
