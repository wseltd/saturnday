"""Release check REL-003: Internal-only file blocker.

Blocks obviously sensitive or internal-only material from appearing inside a
packed release artefact.  Operates against the ``ArtefactInventory`` returned
by the wheel, sdist, or npm inspector — does not re-read files from disk.

The check maintains a built-in deny list of 14 forbidden-content categories
expressed as filename/extension patterns (``FORBIDDEN_FILE_PATTERNS``).
Pattern matching uses :mod:`fnmatch` applied to both the bare filename *and*
the full relative path so that patterns like ``".env.*"`` or ``"node_modules/"``
correctly match regardless of nesting depth.

Manifest exemptions
-------------------
If the caller supplies a manifest dict (loaded from
``.saturnday-release-manifest.yaml``) with an ``allowed_internal`` key, that
key is treated as a list of glob patterns.  Any artefact file whose relative
path matches an ``allowed_internal`` pattern is **exempted** from the deny
list.  Exempted files are recorded in the findings with ``status: "exempted"``
and do **not** contribute to a FAIL verdict.

Return value
------------
- ``ReleaseCheckResult(name="internal_file_blocker", rule_id="REL-003", ...)``
- ``status="FAIL"`` / ``severity="error"`` when at least one non-exempted
  forbidden file is present.
- ``status="PASS"`` / ``severity="info"`` when the artefact is clean.

Example::

    from pathlib import Path
    from saturnday.release._types import ArtefactInventory, ArtefactFile
    from saturnday.release.checks.internal_file_blocker import run_check, FORBIDDEN_FILE_PATTERNS

    inv = ArtefactInventory(
        artefact_type="wheel",
        artefact_path="/tmp/mylib-1.0-py3-none-any.whl",
        artefact_sha256="abc123",
        files=[ArtefactFile(path=".env", size=80, sha256="def456")],
    )
    result = run_check(inventory=inv, unpack_dir=Path("/tmp/unpacked"))
    assert result.status == "FAIL"
"""
from __future__ import annotations

import fnmatch
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 14-category forbidden-content deny list
# ---------------------------------------------------------------------------

#: Built-in deny list of filename/extension patterns that must never appear
#: inside a packed release artefact.
#:
#: These patterns are matched against both the bare filename (``os.path.basename``)
#: and the full relative path inside the artefact, using :func:`fnmatch.fnmatch`.
#: The first matching pattern wins and the file is flagged.
#:
#: Categories (comments identify the intent of each group):
FORBIDDEN_FILE_PATTERNS: list[str] = [
    # --- Signing keys and certificate/key bundles ---
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    # --- SSH keys ---
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "*.pub",
    # --- .env files and local env variants ---
    ".env",
    ".env.*",
    # --- Credentials files ---
    ".credentials",
    ".netrc",
    "credentials.json",
    # --- Private JWT fixtures and test licence files ---
    "*.jwt",
    "license.key",
    "test_licence.*",
    # --- Internal runbooks and ops documentation ---
    "runbook*",
    "RUNBOOK*",
    "ops-*",
    # --- CI-only and ops-only configuration ---
    ".gitlab-ci.yml",
    "Jenkinsfile",
    ".circleci/",
    # --- Debug artefacts ---
    "*.prof",
    "*.hprof",
    "core",
    "core.*",
    "*.heap",
    # --- Source maps (also covered by REL-001) ---
    "*.map",
    # --- Signing scripts and signing material ---
    "generate_licence*",
    "sign_*",
    "*_signing*",
    # --- Unintended config files ---
    ".docker/config.json",
    "kubeconfig",
    "*.vault-token",
    # --- Build/tool artefacts that should not ship ---
    "*.pyc",
    "__pycache__/",
    ".git/",
    "*.swp",
    "*.swo",
    ".DS_Store",
    "Thumbs.db",
    ".idea/",
    # --- npm-specific ---
    "node_modules/",
    ".npmrc",
    ".yarnrc",
    ".pnp.*",
]

# Human-readable category labels keyed on the pattern (first-match wins).
# Used only for the ``kind`` field in findings; does not affect blocking logic.
_PATTERN_CATEGORY: dict[str, str] = {
    "*.pem": "signing_key_or_cert",
    "*.key": "signing_key_or_cert",
    "*.p12": "signing_key_or_cert",
    "*.pfx": "signing_key_or_cert",
    "*.jks": "signing_key_or_cert",
    "id_rsa": "ssh_key",
    "id_ed25519": "ssh_key",
    "id_ecdsa": "ssh_key",
    "*.pub": "ssh_key",
    ".env": "env_file",
    ".env.*": "env_file",
    ".credentials": "credentials_file",
    ".netrc": "credentials_file",
    "credentials.json": "credentials_file",
    "*.jwt": "jwt_fixture",
    "license.key": "license_key",
    "test_licence.*": "test_licence",
    "runbook*": "internal_runbook",
    "RUNBOOK*": "internal_runbook",
    "ops-*": "ops_doc",
    ".gitlab-ci.yml": "ci_config",
    "Jenkinsfile": "ci_config",
    ".circleci/": "ci_config",
    "*.prof": "debug_artefact",
    "*.hprof": "debug_artefact",
    "core": "debug_artefact",
    "core.*": "debug_artefact",
    "*.heap": "debug_artefact",
    "*.map": "source_map",
    "generate_licence*": "signing_script",
    "sign_*": "signing_script",
    "*_signing*": "signing_script",
    ".docker/config.json": "unintended_config",
    "kubeconfig": "unintended_config",
    "*.vault-token": "unintended_config",
    "*.pyc": "build_artefact",
    "__pycache__/": "build_artefact",
    ".git/": "build_artefact",
    "*.swp": "build_artefact",
    "*.swo": "build_artefact",
    ".DS_Store": "build_artefact",
    "Thumbs.db": "build_artefact",
    ".idea/": "build_artefact",
    "node_modules/": "npm_internal",
    ".npmrc": "npm_internal",
    ".yarnrc": "npm_internal",
    ".pnp.*": "npm_internal",
}


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _match_forbidden(file_path: str) -> Optional[tuple[str, str]]:
    """Return ``(matched_pattern, category)`` for the first forbidden pattern
    that matches *file_path*, or ``None`` if no pattern matches.

    Matching is performed against both:
    - The **bare filename** (last path component), so ``"*.pem"`` catches
      ``"certs/server.pem"``.
    - The **full relative path** as given, so path-anchored patterns like
      ``".circleci/"`` or ``"__pycache__/"`` can match directory prefixes.

    Args:
        file_path: Relative path of the artefact file, using forward slashes.

    Returns:
        A 2-tuple ``(pattern, category)`` on the first match, or ``None``.
    """
    basename = file_path.split("/")[-1] if "/" in file_path else file_path

    for pattern in FORBIDDEN_FILE_PATTERNS:
        # Match against bare filename
        if fnmatch.fnmatch(basename, pattern):
            return pattern, _PATTERN_CATEGORY.get(pattern, "forbidden_file")

        # Match against full path (catches path-anchored patterns and
        # directory-style patterns like "__pycache__/", ".git/")
        if fnmatch.fnmatch(file_path, pattern):
            return pattern, _PATTERN_CATEGORY.get(pattern, "forbidden_file")

        # For directory-style patterns ending in "/" check if any path component
        # matches the stripped pattern, so "__pycache__/" catches
        # "mylib/__pycache__/foo.pyc" etc.
        if pattern.endswith("/"):
            dir_name = pattern.rstrip("/")
            parts = file_path.split("/")
            for part in parts[:-1]:  # all directory components
                if fnmatch.fnmatch(part, dir_name):
                    return pattern, _PATTERN_CATEGORY.get(pattern, "forbidden_file")

    return None


def _is_exempted(file_path: str, allowed_patterns: list[str]) -> bool:
    """Return ``True`` if *file_path* matches any pattern in *allowed_patterns*.

    Args:
        file_path:        Relative path of the artefact file.
        allowed_patterns: List of glob patterns from the manifest
                          ``allowed_internal`` key.

    Returns:
        ``True`` when the file is explicitly allowed, ``False`` otherwise.
    """
    basename = file_path.split("/")[-1] if "/" in file_path else file_path
    for pattern in allowed_patterns:
        if fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Public check entry point
# ---------------------------------------------------------------------------


def run_check(
    inventory: Any,  # saturnday.release._types.ArtefactInventory
    unpack_dir: Path,
    manifest: Optional[dict[str, Any]] = None,
) -> Any:  # saturnday.release.evidence.ReleaseCheckResult
    """Check for internal-only or sensitive files inside a release artefact.

    Iterates over every file recorded in *inventory* and tests its relative
    path against :data:`FORBIDDEN_FILE_PATTERNS`.  Files that match a manifest
    ``allowed_internal`` exemption are flagged as ``status: "exempted"`` in the
    findings list but do **not** trigger a FAIL.

    The parameter is named ``unpack_dir`` to match the orchestrator's calling
    convention (``saturnday.release.orchestrator._run_check``).

    Args:
        inventory:  :class:`~saturnday.release._types.ArtefactInventory`
                    produced by the wheel, sdist, or npm inspector.
        unpack_dir: Path to the unpacked artefact directory on disk.
                    Not read by this check (path-only inspection), but
                    required by the common check interface contract.
        manifest:   Parsed ``.saturnday-release-manifest.yaml`` dict, or
                    ``None`` if no manifest was provided.

    Returns:
        :class:`~saturnday.release.evidence.ReleaseCheckResult` with:

        - ``name="internal_file_blocker"``
        - ``rule_id="REL-003"``
        - ``status="FAIL"`` and ``severity="error"`` when at least one
          non-exempted forbidden file is found.
        - ``status="PASS"`` and ``severity="info"`` when the artefact is clean.
        - ``files_checked`` set to the total number of files in *inventory*.
        - Each finding dict contains: ``file``, ``kind``, ``pattern``, and
          optionally ``status="exempted"``.
    """
    from saturnday.release.evidence import ReleaseCheckResult

    t0 = time.monotonic()

    # Resolve exemption patterns from the manifest, if any.
    allowed_patterns: list[str] = []
    if manifest is not None:
        raw = manifest.get("allowed_internal", [])
        if isinstance(raw, list):
            allowed_patterns = [str(p) for p in raw]
        elif raw is not None:
            logger.warning(
                "REL-003: manifest 'allowed_internal' is not a list (%s) — ignored",
                type(raw).__name__,
            )

    findings: list[dict[str, Any]] = []
    blocked_count: int = 0
    files = getattr(inventory, "files", []) or []

    for artefact_file in files:
        file_path: str = getattr(artefact_file, "path", "")
        if not file_path:
            continue

        match = _match_forbidden(file_path)
        if match is None:
            continue

        matched_pattern, category = match

        if _is_exempted(file_path, allowed_patterns):
            logger.debug(
                "REL-003: %s matches %r but is exempted by manifest",
                file_path,
                matched_pattern,
            )
            findings.append(
                {
                    "file": file_path,
                    "kind": category,
                    "pattern": matched_pattern,
                    "status": "exempted",
                }
            )
        else:
            logger.info(
                "REL-003 BLOCKED: %s matches forbidden pattern %r (category: %s)",
                file_path,
                matched_pattern,
                category,
            )
            findings.append(
                {
                    "file": file_path,
                    "kind": category,
                    "pattern": matched_pattern,
                }
            )
            blocked_count += 1

    elapsed = time.monotonic() - t0
    files_checked = len(files)

    if blocked_count > 0:
        return ReleaseCheckResult(
            name="internal_file_blocker",
            rule_id="REL-003",
            status="FAIL",
            severity="error",
            findings=findings,
            files_checked=files_checked,
            elapsed_s=elapsed,
        )

    return ReleaseCheckResult(
        name="internal_file_blocker",
        rule_id="REL-003",
        status="PASS",
        severity="info",
        findings=findings,  # may contain exempted entries
        files_checked=files_checked,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "FORBIDDEN_FILE_PATTERNS",
    "run_check",
]
