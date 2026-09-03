"""Shared dataclasses for the release subpackage.

These types are the canonical data model for all artefact inspection
results in ``saturnday.release``.  Both ``wheel_inspector`` and
``sdist_inspector`` return ``ArtefactInventory`` instances so that
downstream checks and the evidence writer can treat Python and npm
artefacts uniformly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArtefactFile:
    """Metadata for a single file found inside a packed artefact.

    Attributes:
        path:   Relative path of the file within the unpacked artefact,
                using forward slashes regardless of platform.
        size:   File size in bytes.
        sha256: Lowercase hex-encoded SHA-256 digest of the file content.
    """

    path: str
    size: int
    sha256: str


@dataclass
class ArtefactInventory:
    """Full inventory of a packed release artefact.

    Attributes:
        artefact_type:   One of ``"wheel"``, ``"sdist"``, or ``"npm"``.
        artefact_path:   Absolute path to the original packed artefact file.
        artefact_sha256: Lowercase hex-encoded SHA-256 digest of the
                         packed artefact file itself (not its contents).
        files:           List of :class:`ArtefactFile` instances, one per
                         file found inside the artefact.
        record_entries:  Raw lines from the wheel ``RECORD`` file (wheel
                         artefacts only).  Empty list for sdist and npm.
        metadata:        Key/value pairs parsed from the artefact's metadata
                         file (``METADATA`` for wheels, ``PKG-INFO`` for
                         sdists, ``package.json`` for npm).
    """

    artefact_type: str
    artefact_path: str
    artefact_sha256: str
    files: list[ArtefactFile] = field(default_factory=list)
    record_entries: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["ArtefactFile", "ArtefactInventory"]
