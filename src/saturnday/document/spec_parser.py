"""Parser and validator for doc-spec.yaml files.

A doc-spec.yaml is the document-mode equivalent of a plan.json.  It defines
structure, sources, risk class, claim policy, and required sign-off roles for
a single governed document.

Usage::

    from pathlib import Path
    from saturnday.document.spec_parser import parse_doc_spec, validate_doc_spec

    spec = parse_doc_spec(Path("doc-spec.yaml"))
    errors = validate_doc_spec(spec, repo_path=Path("."))
    if errors:
        raise ValueError(errors)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from saturnday.document._types import DocumentSpec

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = (
    "type",
    "purpose",
    "audience",
    "risk_class",
    "required_sections",
    "claim_policy",
    "approved_sources",
    "sign_off_roles",
)

_VALID_RISK_CLASSES = {"low", "medium", "high"}


def parse_doc_spec(spec_path: Path) -> DocumentSpec:
    """Parse and validate a doc-spec.yaml file into a DocumentSpec.

    Args:
        spec_path: Absolute or relative path to the doc-spec.yaml file.

    Returns:
        A populated ``DocumentSpec`` instance.

    Raises:
        FileNotFoundError: If ``spec_path`` does not exist.
        ValueError: If the YAML is missing required keys or has invalid types.
    """
    if not spec_path.exists():
        raise FileNotFoundError(f"doc-spec.yaml not found: {spec_path}")

    logger.debug("Parsing doc-spec: %s", spec_path)
    raw_text = spec_path.read_text(encoding="utf-8")

    try:
        data: dict[str, Any] = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML parse error in {spec_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"doc-spec.yaml must be a YAML mapping, got {type(data).__name__}")

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(f"doc-spec.yaml missing required keys: {missing}")

    spec = DocumentSpec(
        type=str(data["type"]),
        purpose=str(data["purpose"]),
        audience=str(data["audience"]),
        risk_class=str(data["risk_class"]),
        required_sections=list(data["required_sections"]),
        claim_policy=dict(data["claim_policy"]) if data["claim_policy"] else {},
        approved_sources=list(data["approved_sources"]),
        sign_off_roles=list(data["sign_off_roles"]),
        jurisdiction=str(data.get("jurisdiction", "")),
        template=str(data.get("template", "")),
        required_terminology=list(data.get("required_terminology", [])),
        banned_terminology=list(data.get("banned_terminology", [])),
        numeric_tolerance=dict(data.get("numeric_tolerance", {})),
        citation_style=str(data.get("citation_style", "internal_reference")),
        max_retry_per_section=int(data.get("max_retry_per_section", 2)),
    )

    logger.info(
        "Parsed doc-spec: type=%s risk_class=%s sections=%d",
        spec.type,
        spec.risk_class,
        len(spec.required_sections),
    )
    return spec


def validate_doc_spec(spec: DocumentSpec, repo_path: Path) -> list[str]:
    """Validate a DocumentSpec against structural and filesystem constraints.

    Checks are purely local and deterministic — no LLM calls.

    Validation rules:
    - ``type`` must be a non-empty string.
    - ``purpose`` must be non-empty.
    - ``audience`` must be non-empty.
    - ``risk_class`` must be ``"low"``, ``"medium"``, or ``"high"``.
    - ``required_sections`` must be a non-empty list of unique strings.
    - ``claim_policy`` must be a dict with at least one key.
    - ``approved_sources`` must be non-empty; each path must exist relative
      to ``repo_path``.
    - ``sign_off_roles`` must be non-empty for ``medium`` and ``high``
      risk classes; may be empty for ``low``.

    Args:
        spec: The ``DocumentSpec`` to validate.
        repo_path: Root of the repository (used to resolve ``approved_sources``).

    Returns:
        List of error strings.  Empty list means the spec is valid.
    """
    errors: list[str] = []

    # --- Scalar fields -------------------------------------------------------
    if not spec.type or not spec.type.strip():
        errors.append("type must be a non-empty string")

    if not spec.purpose or not spec.purpose.strip():
        errors.append("purpose must be non-empty")

    if not spec.audience or not spec.audience.strip():
        errors.append("audience must be non-empty")

    if spec.risk_class not in _VALID_RISK_CLASSES:
        errors.append(
            f"risk_class must be one of {sorted(_VALID_RISK_CLASSES)}, got {spec.risk_class!r}"
        )

    # --- required_sections ---------------------------------------------------
    if not spec.required_sections:
        errors.append("required_sections must be a non-empty list")
    else:
        seen: set[str] = set()
        duplicates: list[str] = []
        for section in spec.required_sections:
            if section in seen:
                duplicates.append(section)
            seen.add(section)
        if duplicates:
            errors.append(f"required_sections contains duplicates: {duplicates}")

    # --- claim_policy --------------------------------------------------------
    if not isinstance(spec.claim_policy, dict) or len(spec.claim_policy) == 0:
        errors.append("claim_policy must be a dict with at least one key")

    # --- approved_sources ----------------------------------------------------
    if not spec.approved_sources:
        errors.append("approved_sources must be non-empty")
    else:
        for src in spec.approved_sources:
            resolved = repo_path / src
            if not resolved.exists():
                errors.append(f"approved_source does not exist: {src} (resolved: {resolved})")

    # --- sign_off_roles ------------------------------------------------------
    if spec.risk_class in ("medium", "high"):
        if not spec.sign_off_roles:
            errors.append(
                f"sign_off_roles must be non-empty for risk_class={spec.risk_class!r}"
            )

    if errors:
        logger.warning("doc-spec validation failed with %d error(s)", len(errors))
    else:
        logger.debug("doc-spec validation passed")

    return errors
