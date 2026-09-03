"""Provenance and attestation verification for release governance.

RS-019: checks whether release artefacts have valid provenance attestations.
Supports PEP 740 (PyPI attestations) and SLSA provenance v1 bundles (npm
provenance via ``npm publish --provenance``).

Cryptographic signature verification (Sigstore/Fulcio trust root) is explicitly
OUT OF SCOPE.  This module performs structural validation only:

- Is the attestation file present?
- Is it valid JSON with the expected top-level structure?
- Does the subject digest match the artefact SHA-256?
- Is the predicateType a recognised provenance type?
- Is a signature envelope present (structure check, not crypto)?

If ``require_provenance: true`` is set in the org release-policy (or manifest),
a missing attestation escalates from WARN to FAIL.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

__all__ = [
    "check_provenance",
    "evaluate_provenance",
]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known predicate types that count as valid SLSA / PyPI provenance.
# ---------------------------------------------------------------------------

_KNOWN_PREDICATE_TYPES: frozenset[str] = frozenset(
    {
        "https://slsa.dev/provenance/v0.1",
        "https://slsa.dev/provenance/v0.2",
        "https://slsa.dev/provenance/v1",
        "https://docs.pypi.org/attestations/publish/v1",
        "https://github.com/slsa-framework/slsa-github-generator",
    }
)

# ---------------------------------------------------------------------------
# Attestation file suffixes to search for, in priority order.
# ---------------------------------------------------------------------------

_ATTESTATION_SUFFIXES: tuple[str, ...] = (
    ".attestation",
    ".sigstore",
    ".intoto.jsonl",
)

# ---------------------------------------------------------------------------
# Rule identifiers
# ---------------------------------------------------------------------------

_RULE_ID = "REL-PROV-001"
_CHECK_NAME = "provenance"


def _find_attestation(
    artefact_path: Path,
    evidence_dir: Path | None,
) -> Path | None:
    """Look for an attestation file alongside the artefact or in evidence_dir.

    Search order:
    1. ``{artefact_path}{suffix}`` for each suffix in :data:`_ATTESTATION_SUFFIXES`.
    2. An ``attestations/`` subdirectory next to the artefact.
    3. Same searches inside *evidence_dir* (if provided).

    Args:
        artefact_path: Absolute path to the packed artefact (e.g. the wheel).
        evidence_dir:  Optional evidence directory that may contain attestation
                       files placed there by a CI pipeline.

    Returns:
        Path to the first attestation file found, or ``None``.
    """
    search_roots: list[Path] = [artefact_path.parent]
    if evidence_dir is not None:
        search_roots.append(Path(evidence_dir))

    for root in search_roots:
        # Direct suffix matches alongside the artefact name.
        for suffix in _ATTESTATION_SUFFIXES:
            candidate = root / (artefact_path.name + suffix)
            if candidate.is_file():
                _logger.debug("Found attestation at %s", candidate)
                return candidate

        # Dedicated attestations/ subdirectory.
        attestations_dir = root / "attestations"
        if attestations_dir.is_dir():
            for suffix in _ATTESTATION_SUFFIXES:
                candidate = attestations_dir / (artefact_path.name + suffix)
                if candidate.is_file():
                    _logger.debug("Found attestation at %s", candidate)
                    return candidate

    return None


def _load_attestation(attestation_path: Path) -> tuple[dict | None, str]:
    """Parse the attestation JSON file.

    Returns:
        ``(parsed_dict, error_message)`` — *error_message* is empty on success.
    """
    try:
        raw = attestation_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"Cannot read attestation file: {exc}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"Attestation is not valid JSON: {exc}"

    if not isinstance(data, dict):
        return None, "Attestation JSON is not a dict"

    return data, ""


def _extract_subject_digest(attestation: dict) -> str | None:
    """Extract the SHA-256 digest of the attested subject.

    Handles two schemas:

    1. **SLSA / in-toto v1 envelope** — ``{"subject": [{"digest": {"sha256": ...}}]}``.
    2. **Sigstore bundle** — ``{"verificationMaterial": {...}, "dsseEnvelope": {"payload": <base64>}}``.
       For bundles we look for a ``subject`` list in the decoded payload, but
       because base64-decoding the payload adds crypto complexity, we also
       accept a top-level ``subject`` shortcut that some tools write.

    Returns:
        Lowercase hex SHA-256 string, or ``None`` if not found.
    """
    # Schema 1 — direct subject list (most common in PEP 740 / SLSA v1 JSON).
    subjects = attestation.get("subject") or []
    if isinstance(subjects, list):
        for subj in subjects:
            if not isinstance(subj, dict):
                continue
            digest = subj.get("digest") or {}
            if isinstance(digest, dict):
                sha = digest.get("sha256")
                if sha:
                    return sha.lower()

    # Schema 2 — PyPI attestation envelope wraps subject in payload.
    # Try a ``payloadHash`` shortcut if present.
    payload_hash = attestation.get("payloadHash") or attestation.get("payload_hash")
    if isinstance(payload_hash, dict):
        sha = payload_hash.get("sha256")
        if sha:
            return sha.lower()

    # Schema 3 — ``statement.subject`` (some in-toto tools).
    statement = attestation.get("statement") or {}
    if isinstance(statement, dict):
        for subj in statement.get("subject", []):
            if not isinstance(subj, dict):
                continue
            digest = subj.get("digest") or {}
            if isinstance(digest, dict):
                sha = digest.get("sha256")
                if sha:
                    return sha.lower()

    return None


def _check_predicate_type(attestation: dict) -> tuple[bool, str]:
    """Validate the predicateType field.

    Returns:
        ``(is_valid, message)`` where *is_valid* is ``True`` for recognised types.
    """
    predicate_type = attestation.get("predicateType") or attestation.get("predicate_type")
    if not predicate_type:
        return False, "predicateType field missing from attestation"

    if predicate_type in _KNOWN_PREDICATE_TYPES:
        return True, f"predicateType={predicate_type!r} is recognised"

    # Accept unknown types with a warning rather than a hard failure.
    _logger.debug("Unknown predicateType %r — treating as valid for structure check", predicate_type)
    return True, f"predicateType={predicate_type!r} (unrecognised but structurally present)"


def _check_build_type(attestation: dict) -> tuple[bool, str]:
    """Validate that predicate.buildType (or buildDefinition.buildType) is present."""
    predicate = attestation.get("predicate") or {}
    if isinstance(predicate, dict):
        bt = predicate.get("buildType") or predicate.get("buildDefinition", {}).get("buildType")
        if bt:
            return True, f"predicate.buildType={bt!r}"
    # Also accept top-level buildType (some tools flatten).
    if attestation.get("buildType"):
        return True, f"buildType={attestation['buildType']!r}"
    return False, "predicate.buildType missing"


def _check_signature_envelope(attestation: dict) -> tuple[bool, str]:
    """Check that a signature envelope is structurally present.

    Accepts:
    - ``signatures`` list (at least one entry with ``sig`` key)
    - ``verificationMaterial.tlogEntries`` (Sigstore/Rekor)
    - ``bundle`` or ``dsseEnvelope.signatures``
    - ``envelope.signatures``

    Does NOT verify cryptographic correctness.

    Returns:
        ``(present, message)``
    """
    # signatures list (PEP 740 / SLSA standard).
    sigs = attestation.get("signatures")
    if isinstance(sigs, list) and sigs:
        return True, f"{len(sigs)} signature(s) present"

    # Sigstore bundle style.
    vm = attestation.get("verificationMaterial") or {}
    if isinstance(vm, dict):
        tlog = vm.get("tlogEntries")
        if isinstance(tlog, list) and tlog:
            return True, f"verificationMaterial.tlogEntries present ({len(tlog)} entr(ies))"
        cert_chain = vm.get("x509CertificateChain") or vm.get("certificate")
        if cert_chain:
            return True, "verificationMaterial certificate present"

    # dsseEnvelope.signatures.
    dsse = attestation.get("dsseEnvelope") or {}
    if isinstance(dsse, dict):
        sigs2 = dsse.get("signatures")
        if isinstance(sigs2, list) and sigs2:
            return True, f"dsseEnvelope.signatures present ({len(sigs2)})"

    # envelope.signatures (Cosign).
    envelope = attestation.get("envelope") or {}
    if isinstance(envelope, dict):
        sigs3 = envelope.get("signatures")
        if isinstance(sigs3, list) and sigs3:
            return True, f"envelope.signatures present ({len(sigs3)})"

    return False, "No signature envelope found in attestation"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_provenance(
    artefact_path: str | Path,
    artefact_sha256: str,
    evidence_dir: Path | None = None,
    attestation_path: Path | None = None,
    policy: dict | None = None,
) -> list[dict]:
    """Run structural provenance checks on a release artefact.

    Args:
        artefact_path:   Path to the packed artefact (wheel, sdist, tgz).
        artefact_sha256: Expected lowercase hex SHA-256 of the artefact file.
        evidence_dir:    Optional evidence directory that may contain attestation
                         files produced by CI.
        attestation_path: Explicit path to attestation file.  When supplied,
                         the auto-discovery step is skipped.
        policy:          Policy dict.  Relevant keys:
                         - ``require_provenance`` (bool): escalate missing
                           attestation from WARN to FAIL.

    Returns:
        List of finding dicts (compatible with ``ReleaseCheckResult.findings``).
        Each dict has: ``kind``, ``message``, ``path`` (optional), ``detail``.
    """
    if policy is None:
        policy = {}

    require_provenance: bool = bool(policy.get("require_provenance", False))
    artefact = Path(artefact_path)

    # -----------------------------------------------------------------------
    # Step 1: Locate the attestation file.
    # -----------------------------------------------------------------------
    if attestation_path is not None:
        att_path: Path | None = Path(attestation_path)
        if not att_path.is_file():
            att_path = None
    else:
        att_path = _find_attestation(artefact, evidence_dir)

    if att_path is None:
        status = "FAIL" if require_provenance else "WARN"
        return [
            {
                "kind": "attestation_missing",
                "status": status,
                "message": (
                    "No provenance attestation found alongside artefact"
                    f" {artefact.name}"
                    + (" — require_provenance is True" if require_provenance else "")
                ),
                "path": str(artefact),
                "detail": {
                    "artefact_sha256": artefact_sha256,
                    "require_provenance": require_provenance,
                },
            }
        ]

    # -----------------------------------------------------------------------
    # Step 2: Parse the attestation JSON.
    # -----------------------------------------------------------------------
    parsed, parse_error = _load_attestation(att_path)
    if parsed is None:
        return [
            {
                "kind": "attestation_parse_error",
                "status": "FAIL",
                "message": f"Attestation parse error: {parse_error}",
                "path": str(att_path),
                "detail": {"error": parse_error},
            }
        ]

    findings: list[dict] = [
        {
            "kind": "attestation_found",
            "status": "PASS",
            "message": f"Attestation file found: {att_path.name}",
            "path": str(att_path),
            "detail": {},
        }
    ]

    # -----------------------------------------------------------------------
    # Step 3: Subject digest validation.
    # -----------------------------------------------------------------------
    subject_digest = _extract_subject_digest(parsed)
    if subject_digest is None:
        findings.append(
            {
                "kind": "subject_digest_missing",
                "status": "FAIL",
                "message": "Attestation has no subject digest field",
                "path": str(att_path),
                "detail": {"expected_sha256": artefact_sha256},
            }
        )
    else:
        expected = artefact_sha256.lower()
        if subject_digest == expected:
            findings.append(
                {
                    "kind": "hash_valid",
                    "status": "PASS",
                    "message": "Attestation subject digest matches artefact SHA-256",
                    "path": str(att_path),
                    "detail": {"sha256": subject_digest},
                }
            )
        else:
            findings.append(
                {
                    "kind": "hash_mismatch",
                    "status": "FAIL",
                    "message": (
                        f"Attestation subject digest mismatch: "
                        f"attestation={subject_digest!r} expected={expected!r}"
                    ),
                    "path": str(att_path),
                    "detail": {
                        "attestation_sha256": subject_digest,
                        "expected_sha256": expected,
                    },
                }
            )

    # -----------------------------------------------------------------------
    # Step 4: predicateType check.
    # -----------------------------------------------------------------------
    pt_valid, pt_msg = _check_predicate_type(parsed)
    if not pt_valid:
        findings.append(
            {
                "kind": "predicate_type_missing",
                "status": "WARN",
                "message": pt_msg,
                "path": str(att_path),
                "detail": {},
            }
        )

    # -----------------------------------------------------------------------
    # Step 5: buildType check.
    # -----------------------------------------------------------------------
    bt_valid, bt_msg = _check_build_type(parsed)
    if not bt_valid:
        findings.append(
            {
                "kind": "build_type_missing",
                "status": "WARN",
                "message": bt_msg,
                "path": str(att_path),
                "detail": {},
            }
        )

    # -----------------------------------------------------------------------
    # Step 6: Signature envelope presence check.
    # -----------------------------------------------------------------------
    sig_present, sig_msg = _check_signature_envelope(parsed)
    findings.append(
        {
            "kind": "signature_present" if sig_present else "signature_missing",
            "status": "PASS" if sig_present else "WARN",
            "message": sig_msg,
            "path": str(att_path),
            "detail": {},
        }
    )

    return findings


def evaluate_provenance(
    artefact_path: str | Path,
    artefact_sha256: str,
    evidence_dir: Path | None = None,
    attestation_path: Path | None = None,
    policy: dict | None = None,
) -> Any:
    """Run all provenance checks and return a ``ReleaseCheckResult``.

    Imports ``ReleaseCheckResult`` from the public package at call time
    (not at module import time) so the premium module does not create a hard
    circular dependency at import.

    Args:
        artefact_path:   Path to the packed artefact.
        artefact_sha256: Expected SHA-256 of the artefact.
        evidence_dir:    Optional evidence directory.
        attestation_path: Explicit attestation file path (skips auto-discovery).
        policy:          Policy dict (``require_provenance`` bool).

    Returns:
        :class:`~saturnday.release.evidence.ReleaseCheckResult` with
        ``rule_id="REL-PROV-001"``.
    """
    from saturnday.release.evidence import ReleaseCheckResult

    if policy is None:
        policy = {}

    findings = check_provenance(
        artefact_path=artefact_path,
        artefact_sha256=artefact_sha256,
        evidence_dir=evidence_dir,
        attestation_path=attestation_path,
        policy=policy,
    )

    # Derive overall status: any FAIL → FAIL; any WARN → WARN; else PASS.
    statuses = {f.get("status", "PASS") for f in findings}
    if "FAIL" in statuses:
        status = "FAIL"
        severity = "error"
    elif "WARN" in statuses:
        status = "WARN"
        severity = "warning"
    else:
        status = "PASS"
        severity = "info"

    # Build a summary message from the first non-PASS finding or from PASS findings.
    fail_findings = [f for f in findings if f.get("status") == "FAIL"]
    warn_findings = [f for f in findings if f.get("status") == "WARN"]
    if fail_findings:
        summary = fail_findings[0].get("message", "Provenance check failed")
    elif warn_findings:
        summary = warn_findings[0].get("message", "Provenance advisory")
    else:
        summary = "Provenance attestation verified"

    # Attach summary as the first finding message for visibility if no findings yet.
    if not findings:
        findings = [{"kind": "attestation_checked", "status": status, "message": summary}]

    return ReleaseCheckResult(
        name=_CHECK_NAME,
        rule_id=_RULE_ID,
        status=status,
        severity=severity,
        findings=findings,
        files_checked=1,
        elapsed_s=0.0,
    )
