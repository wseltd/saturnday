"""Release diff engine for ``saturnday.release``.

Compares two :class:`~saturnday.release._types.ArtefactInventory` instances
(candidate vs baseline) and produces a structured diff.  The comparison is
purely deterministic — no LLM involvement.

Typical usage::

    from saturnday.release.diff_engine import compute_release_diff

    diff = compute_release_diff(baseline=baseline_inv, candidate=candidate_inv)
    print(diff.summary)
    if diff.suspicious_additions:
        for item in diff.suspicious_additions:
            print(item["path"], item["reason"])

Design notes
------------
- Cross-type comparison (wheel vs npm, sdist vs wheel, etc.) is not supported
  and raises :class:`ValueError`.
- The engine operates on in-memory :class:`ArtefactInventory` objects; it does
  not touch the filesystem beyond what the inventory already contains.
- Suspicious-addition heuristics are intentionally conservative: false negatives
  are worse than false positives when inspecting release artefacts.
"""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

from saturnday.release._types import ArtefactFile, ArtefactInventory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk patterns for suspicious addition detection
# ---------------------------------------------------------------------------

#: Glob patterns (matched against the file's basename) that indicate a
#: suspicious or potentially dangerous addition to the release artefact.
#: Order does not affect results — all are evaluated independently.
_SUSPICIOUS_BASENAME_PATTERNS: list[str] = [
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "*.map",
    "*.secret",
    "*.secrets",
    "*.secret.*",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "credentials",
    "credentials.*",
    "credentials.json",
    ".credentials",
    ".netrc",
]

#: Glob patterns matched against the full relative path within the artefact.
_SUSPICIOUS_PATH_PATTERNS: list[str] = [
    ".env",
    ".env/*",
    ".env*",
    "**/.env",
    "**/.env.*",
    "**/*.key",
    "**/*.pem",
    "**/*.map",
    "**/*.secret",
    "**/*.secrets",
    "**/*.secret.*",
    "**/id_rsa",
    "**/id_rsa.*",
    "**/id_ed25519",
    "**/id_ed25519.*",
    "**/id_ecdsa",
    "**/id_ecdsa.*",
    "**/credentials",
    "**/credentials.json",
    "**/.credentials",
    "**/.netrc",
]

#: File extensions that are suspicious when added to a release artefact
#: (executable and platform-native binary types).
_SUSPICIOUS_EXTENSIONS: frozenset[str] = frozenset({
    ".sh",
    ".bat",
    ".exe",
    ".bin",
    ".cmd",
    ".ps1",
})

#: Files larger than this threshold are flagged as suspicious additions
#: regardless of their name, since very large files in a release artefact
#: are unusual and may indicate an accidental bundle of build artefacts.
_LARGE_FILE_THRESHOLD_BYTES: int = 1 * 1024 * 1024  # 1 MiB


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ChangedFile:
    """Metadata for a file that exists in both inventories but differs.

    Attributes:
        path:       Relative path within the artefact (forward-slash separated).
        old_size:   Size in bytes from the baseline inventory.
        new_size:   Size in bytes from the candidate inventory.
        old_sha256: Lowercase hex SHA-256 digest from the baseline.
        new_sha256: Lowercase hex SHA-256 digest from the candidate.
    """

    path: str
    old_size: int
    new_size: int
    old_sha256: str
    new_sha256: str


@dataclass
class ReleaseDiff:
    """Structured comparison between a candidate and baseline artefact inventory.

    Attributes:
        baseline_path:         Absolute path to the baseline artefact file.
        candidate_path:        Absolute path to the candidate artefact file.
        added_files:           Relative paths present in the candidate but
                               absent from the baseline.
        removed_files:         Relative paths present in the baseline but
                               absent from the candidate.
        changed_files:         Files that exist in both inventories but have a
                               different SHA-256 digest or size.
        size_delta:            Total size change in bytes
                               (``sum(candidate sizes) - sum(baseline sizes)``).
        suspicious_additions:  Subset of *added_files* that match risk patterns
                               (secrets, executables, very large files).  Each
                               entry is a dict with keys ``"path"``, ``"size"``,
                               ``"sha256"``, and ``"reason"``.
    """

    baseline_path: str
    candidate_path: str
    added_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    changed_files: list[ChangedFile] = field(default_factory=list)
    size_delta: int = 0
    suspicious_additions: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_release_diff(
    baseline: ArtefactInventory,
    candidate: ArtefactInventory,
) -> ReleaseDiff:
    """Compare two artefact inventories and return a structured diff.

    Args:
        baseline:  Inventory produced by inspecting the previous/reference
                   artefact.  Must be the same ``artefact_type`` as *candidate*.
        candidate: Inventory produced by inspecting the new artefact under
                   review.

    Returns:
        :class:`ReleaseDiff` with added, removed, changed, and suspicious files
        populated.

    Raises:
        ValueError: If *baseline* and *candidate* have different
                    ``artefact_type`` values.  Cross-type comparison is
                    undefined and refused to avoid misleading results.

    Example::

        diff = compute_release_diff(baseline=old_inv, candidate=new_inv)
        if diff.suspicious_additions:
            for item in diff.suspicious_additions:
                print(f"Suspicious: {item['path']} — {item['reason']}")
    """
    if baseline.artefact_type != candidate.artefact_type:
        raise ValueError(
            f"Cannot diff artefacts of different types: "
            f"baseline={baseline.artefact_type!r} vs candidate={candidate.artefact_type!r}. "
            f"Compare wheel-to-wheel, sdist-to-sdist, or npm-to-npm."
        )

    # Build lookup maps keyed by relative path.
    baseline_map: dict[str, ArtefactFile] = {f.path: f for f in baseline.files}
    candidate_map: dict[str, ArtefactFile] = {f.path: f for f in candidate.files}

    baseline_paths: set[str] = set(baseline_map)
    candidate_paths: set[str] = set(candidate_map)

    # --- Added files (in candidate, not in baseline) -------------------------
    added_paths: list[str] = sorted(candidate_paths - baseline_paths)

    # --- Removed files (in baseline, not in candidate) -----------------------
    removed_paths: list[str] = sorted(baseline_paths - candidate_paths)

    # --- Changed files (in both, but different hash or size) -----------------
    common_paths: set[str] = baseline_paths & candidate_paths
    changed: list[ChangedFile] = []
    for path in sorted(common_paths):
        b_file = baseline_map[path]
        c_file = candidate_map[path]
        if b_file.sha256 != c_file.sha256 or b_file.size != c_file.size:
            changed.append(
                ChangedFile(
                    path=path,
                    old_size=b_file.size,
                    new_size=c_file.size,
                    old_sha256=b_file.sha256,
                    new_sha256=c_file.sha256,
                )
            )

    # --- Size delta ----------------------------------------------------------
    candidate_total = sum(f.size for f in candidate.files)
    baseline_total = sum(f.size for f in baseline.files)
    size_delta = candidate_total - baseline_total

    # --- Suspicious additions ------------------------------------------------
    suspicious: list[dict] = []
    for path in added_paths:
        c_file = candidate_map[path]
        reasons = _check_suspicious(path, c_file.size)
        for reason in reasons:
            suspicious.append({
                "path": path,
                "size": c_file.size,
                "sha256": c_file.sha256,
                "reason": reason,
            })
            logger.debug("Suspicious addition: %s — %s", path, reason)

    logger.info(
        "release diff: +%d -%d ~%d suspicious=%d size_delta=%+d bytes",
        len(added_paths),
        len(removed_paths),
        len(changed),
        len(suspicious),
        size_delta,
    )

    return ReleaseDiff(
        baseline_path=baseline.artefact_path,
        candidate_path=candidate.artefact_path,
        added_files=added_paths,
        removed_files=removed_paths,
        changed_files=changed,
        size_delta=size_delta,
        suspicious_additions=suspicious,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_suspicious(path: str, size: int) -> list[str]:
    """Return a list of human-readable reasons why *path* is suspicious.

    An empty list means the file is not flagged.  Multiple reasons may be
    returned for a single file when it triggers more than one heuristic.

    Detection order:
    1. Basename pattern match (e.g. ``.env``, ``*.key``, ``id_rsa``).
    2. Full-path pattern match (e.g. ``**/*.pem``).
    3. Extension match (e.g. ``.sh``, ``.exe``).
    4. Very large file (> 1 MiB).

    Args:
        path: Relative path within the artefact (forward-slash separated).
        size: File size in bytes.

    Returns:
        List of reason strings; empty if not suspicious.
    """
    reasons: list[str] = []
    basename = path.rsplit("/", 1)[-1]
    suffix = ("." + basename.rsplit(".", 1)[-1]) if "." in basename else ""

    # --- Basename patterns ---------------------------------------------------
    for pattern in _SUSPICIOUS_BASENAME_PATTERNS:
        if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(basename.lower(), pattern):
            reasons.append(f"basename matches risk pattern {pattern!r}")
            break  # One basename reason is enough; avoid duplicates.

    # --- Full-path patterns --------------------------------------------------
    path_lower = path.lower()
    for pattern in _SUSPICIOUS_PATH_PATTERNS:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path_lower, pattern):
            # Don't duplicate if basename already matched.
            reason = f"path matches risk pattern {pattern!r}"
            if reason not in reasons:
                reasons.append(reason)
            break

    # --- Extension -----------------------------------------------------------
    if suffix.lower() in _SUSPICIOUS_EXTENSIONS:
        reasons.append(f"executable file extension {suffix.lower()!r}")

    # --- Large file ----------------------------------------------------------
    if size > _LARGE_FILE_THRESHOLD_BYTES:
        reasons.append(
            f"very large file added ({size:,} bytes > {_LARGE_FILE_THRESHOLD_BYTES:,} byte threshold)"
        )

    return reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ChangedFile",
    "ReleaseDiff",
    "compute_release_diff",
]
