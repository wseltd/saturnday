"""Registry-specific policy controls for release governance.

Evaluates whether artefact metadata meets registry-specific quality
and security requirements for PyPI (Python) and npm (JavaScript).

RS-018: Registry-specific policy controls.
"""
from __future__ import annotations

import logging
import re
from typing import Any

__all__ = [
    "check_pypi_metadata",
    "check_npm_metadata",
    "evaluate_registry_policy",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Semver pattern (npm) — basic three-component form is sufficient
# ---------------------------------------------------------------------------
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([\w\-]+(?:\.[\w\-]+)*))?(?:\+([\w\-]+(?:\.[\w\-]+)*))?$"
)

# PyPI trusted-publisher: GitHub Actions OIDC workflow pattern
_GH_OIDC_PATTERN = re.compile(
    r"pypa/gh-action-pypi-publish",
    re.IGNORECASE,
)

# Lifecycle scripts that execute on consumer install — security risk
_NPM_DANGEROUS_LIFECYCLE = frozenset({"preinstall", "postinstall", "install"})


# ---------------------------------------------------------------------------
# PyPI checks
# ---------------------------------------------------------------------------


def check_pypi_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Check PyPI-specific metadata quality.

    Evaluates the metadata dict sourced from a wheel ``METADATA`` file or
    sdist ``PKG-INFO`` file.  Keys follow the ``email.policy.EmailMessage``
    header convention used by wheel/sdist inspectors (lowercased, hyphenated,
    e.g. ``"requires-python"``, ``"description-content-type"``).

    Checks performed:

    - ``classifier`` list is non-empty.
    - ``description-content-type`` is set (``text/markdown`` or ``text/x-rst``).
    - ``author`` or ``author-email`` is present.
    - ``license`` is specified.
    - ``requires-python`` is specified.
    - ``project-url`` or ``home-page`` is present.
    - Trusted-publisher readiness: ``_workflow_content`` key contains the
      ``pypa/gh-action-pypi-publish`` OIDC pattern (advisory only).

    Args:
        metadata: Key/value dict from the artefact inspector.  Multi-value
            headers (``classifier``, ``project-url``) may be represented as
            either a list or a newline-joined string — both are handled.

    Returns:
        List of finding dicts.  Each dict contains:
        - ``kind``: short camel-case label for the issue.
        - ``detail``: human-readable explanation.
        - ``severity``: ``"error"`` (blocks release) or ``"warning"`` (advisory).
    """
    findings: list[dict[str, Any]] = []

    def _get(key: str) -> Any:
        """Case-insensitive metadata lookup."""
        # Try exact match first, then lowercase.
        v = metadata.get(key) or metadata.get(key.lower()) or metadata.get(key.upper())
        return v

    def _present(key: str) -> bool:
        v = _get(key)
        if v is None:
            return False
        if isinstance(v, list):
            return len(v) > 0 and any(str(item).strip() for item in v)
        return bool(str(v).strip())

    # --- classifiers ---
    classifiers = _get("classifier") or _get("classifiers") or []
    if isinstance(classifiers, str):
        classifiers = [line.strip() for line in classifiers.splitlines() if line.strip()]
    if not classifiers:
        findings.append({
            "kind": "missingClassifiers",
            "detail": "No PyPI classifiers found; classifiers improve discoverability.",
            "severity": "warning",
        })

    # --- description-content-type ---
    content_type = _get("description-content-type") or ""
    content_type_str = str(content_type).strip().lower()
    if not content_type_str:
        findings.append({
            "kind": "missingDescriptionContentType",
            "detail": (
                "description-content-type is not set.  Set to 'text/markdown' or "
                "'text/x-rst' so PyPI renders the long description correctly."
            ),
            "severity": "warning",
        })
    elif not any(t in content_type_str for t in ("text/markdown", "text/x-rst", "text/plain")):
        findings.append({
            "kind": "unknownDescriptionContentType",
            "detail": (
                f"description-content-type '{content_type_str}' is not a recognised "
                "PyPI content type.  Use 'text/markdown' or 'text/x-rst'."
            ),
            "severity": "warning",
        })

    # --- author or author-email ---
    if not _present("author") and not _present("author-email"):
        findings.append({
            "kind": "missingAuthor",
            "detail": "Neither 'author' nor 'author-email' is specified in package metadata.",
            "severity": "warning",
        })

    # --- license ---
    if not _present("license"):
        findings.append({
            "kind": "missingLicense",
            "detail": "No 'license' field found in package metadata.",
            "severity": "error",
        })

    # --- requires-python ---
    if not _present("requires-python"):
        findings.append({
            "kind": "missingRequiresPython",
            "detail": (
                "'requires-python' is not specified.  PyPI will accept the package "
                "for any Python version, which may cause confusing install failures."
            ),
            "severity": "warning",
        })

    # --- project-url or home-page ---
    if not _present("project-url") and not _present("home-page"):
        findings.append({
            "kind": "missingProjectUrl",
            "detail": "No 'project-url' or 'home-page' metadata field found.",
            "severity": "warning",
        })

    # --- trusted-publisher readiness (advisory) ---
    workflow_content = _get("_workflow_content") or ""
    if workflow_content:
        if not _GH_OIDC_PATTERN.search(str(workflow_content)):
            findings.append({
                "kind": "trustedPublisherNotReady",
                "detail": (
                    "GitHub Actions workflow content was supplied but "
                    "'pypa/gh-action-pypi-publish' OIDC pattern was not found.  "
                    "Configure Trusted Publisher for keyless PyPI uploads."
                ),
                "severity": "warning",
            })
    else:
        _logger.debug(
            "check_pypi_metadata: _workflow_content not present — "
            "trusted-publisher readiness check skipped"
        )

    return findings


# ---------------------------------------------------------------------------
# npm checks
# ---------------------------------------------------------------------------


def check_npm_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Check npm-specific metadata quality.

    Evaluates the ``package.json`` metadata dict from an npm tarball.

    Checks performed:

    - ``name`` field present.
    - ``version`` field present and valid semver.
    - ``files`` field present (explicit inclusion list).
    - ``main`` or ``exports`` field present.
    - ``license`` field present.
    - ``repository`` field present.
    - No dangerous lifecycle scripts (``preinstall``, ``postinstall``,
      ``install``) that execute on consumer install.

    Args:
        metadata: Dict parsed from ``package.json``.

    Returns:
        List of finding dicts with ``kind``, ``detail``, ``severity``.
    """
    findings: list[dict[str, Any]] = []

    # --- name ---
    name = metadata.get("name", "")
    if not name or not str(name).strip():
        findings.append({
            "kind": "missingName",
            "detail": "'name' field is absent from package.json.",
            "severity": "error",
        })

    # --- version ---
    version = metadata.get("version", "")
    if not version or not str(version).strip():
        findings.append({
            "kind": "missingVersion",
            "detail": "'version' field is absent from package.json.",
            "severity": "error",
        })
    elif not _SEMVER_RE.match(str(version).strip()):
        findings.append({
            "kind": "invalidVersion",
            "detail": (
                f"'version' value '{version}' is not valid semver.  "
                "npm requires a valid semver version string."
            ),
            "severity": "error",
        })

    # --- files ---
    files_field = metadata.get("files")
    if files_field is None:
        findings.append({
            "kind": "missingFilesField",
            "detail": (
                "'files' field is absent from package.json.  Without it, npm "
                "publishes everything not in .npmignore — risk of leaking "
                "source, tests, or credentials."
            ),
            "severity": "warning",
        })
    elif not isinstance(files_field, list) or len(files_field) == 0:
        findings.append({
            "kind": "emptyFilesField",
            "detail": (
                "'files' field is empty in package.json.  Specify which "
                "directories/files to include in the published package."
            ),
            "severity": "warning",
        })

    # --- main or exports ---
    if not metadata.get("main") and not metadata.get("exports"):
        findings.append({
            "kind": "missingEntrypoint",
            "detail": (
                "Neither 'main' nor 'exports' is set in package.json.  "
                "Consumers will not have a default module entry point."
            ),
            "severity": "warning",
        })

    # --- license ---
    license_field = metadata.get("license", "")
    if not license_field or not str(license_field).strip():
        findings.append({
            "kind": "missingLicense",
            "detail": "'license' field is absent from package.json.",
            "severity": "error",
        })

    # --- repository ---
    repo_field = metadata.get("repository")
    if not repo_field:
        findings.append({
            "kind": "missingRepository",
            "detail": (
                "'repository' field is absent from package.json.  "
                "npm shows 'repository' on the package page."
            ),
            "severity": "warning",
        })

    # --- dangerous lifecycle scripts ---
    scripts = metadata.get("scripts")
    if isinstance(scripts, dict):
        dangerous = [
            k for k in scripts if k.lower() in _NPM_DANGEROUS_LIFECYCLE
        ]
        if dangerous:
            findings.append({
                "kind": "dangerousLifecycleScripts",
                "detail": (
                    f"Lifecycle script(s) {sorted(dangerous)!r} will execute "
                    "automatically on consumer 'npm install'.  This is a supply "
                    "chain security risk — remove unless strictly necessary and "
                    "clearly documented."
                ),
                "severity": "error",
            })

    return findings


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def evaluate_registry_policy(
    inventory_metadata: dict[str, Any],
    artefact_type: str,
) -> "ReleaseCheckResult":  # type: ignore[name-defined]
    """Run registry-specific checks based on artefact type.

    Dispatches to :func:`check_pypi_metadata` for ``"python"``/``"wheel"``/
    ``"sdist"`` artefacts and to :func:`check_npm_metadata` for ``"npm"``
    artefacts.  Unknown artefact types return an empty PASS result.

    Args:
        inventory_metadata: Metadata dict from the
            :class:`~saturnday.release._types.ArtefactInventory`.
        artefact_type: One of ``"python"``, ``"wheel"``, ``"sdist"``, or
            ``"npm"``.  String comparison is case-insensitive.

    Returns:
        A :class:`~saturnday.release.evidence.ReleaseCheckResult` with
        ``rule_id="REL-REGISTRY"`` and per-finding details embedded in
        ``findings``.  ``status`` is ``"FAIL"`` when any error-severity
        finding is present, ``"WARN"`` when only warning-severity findings
        exist, and ``"PASS"`` when no findings are generated.
    """
    from saturnday.release.evidence import ReleaseCheckResult

    normalised = artefact_type.lower().strip() if artefact_type else ""
    metadata = inventory_metadata or {}

    if normalised in ("python", "wheel", "sdist"):
        findings = check_pypi_metadata(metadata)
        check_name = "pypi_registry_metadata"
    elif normalised == "npm":
        findings = check_npm_metadata(metadata)
        check_name = "npm_registry_metadata"
    else:
        _logger.debug(
            "evaluate_registry_policy: unknown artefact_type %r — returning PASS",
            artefact_type,
        )
        return ReleaseCheckResult(
            name="registry_metadata",
            rule_id="REL-REGISTRY",
            status="PASS",
            severity="info",
            findings=[],
        )

    _logger.debug(
        "evaluate_registry_policy: %s — %d finding(s)",
        check_name,
        len(findings),
    )

    if not findings:
        return ReleaseCheckResult(
            name=check_name,
            rule_id="REL-REGISTRY",
            status="PASS",
            severity="info",
            findings=[],
        )

    # Determine status and severity from finding severity levels.
    has_error = any(f.get("severity") == "error" for f in findings)
    status = "FAIL" if has_error else "WARN"
    severity = "error" if has_error else "warning"

    return ReleaseCheckResult(
        name=check_name,
        rule_id="REL-REGISTRY",
        status=status,
        severity=severity,
        findings=findings,
    )
