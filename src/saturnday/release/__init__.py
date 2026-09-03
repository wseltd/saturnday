"""Release subpackage for saturnday.

Provides artefact building, unpacking, and inspection capabilities for
Python wheel/sdist and npm tarballs. Used by the ``release-preflight``
command to inspect packed release artefacts as the source of truth —
distinct from source-tree governance checks.

Modules:
    _types          — shared dataclasses (ArtefactFile, ArtefactInventory)
    python_builder  — wraps ``python -m build`` to produce wheel and sdist
    wheel_inspector — unpacks and inventories Python wheel artefacts
    sdist_inspector — unpacks and inventories Python sdist artefacts
"""
