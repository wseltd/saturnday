"""Deterministic check modules for Saturnday Document Mode.

Each sub-module implements one check family and returns a list of
``DocumentFinding`` objects.  All check functions are pure (no I/O) so
they can be unit-tested with plain string inputs.

Available checks:

- :mod:`.structure` — heading presence, placeholder detection, empty blocks.
- :mod:`.citations` — citation existence, DOI/URL format, broken tokens.
- :mod:`.numeric` — arithmetic, percentages, table-to-text consistency.
- :mod:`.terminology` — required/banned terms, abbreviation consistency.
- :mod:`.evidence_coverage` — source utilization, uncited narrative.
"""
