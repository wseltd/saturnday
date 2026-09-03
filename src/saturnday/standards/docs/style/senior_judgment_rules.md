# Senior Judgment Rules

## Purpose

These rules catch patterns that pass lint and governance but betray a lack of
senior engineering judgment.  They are enforced by deterministic post-checks
that run after governance PASS, before commit.

## Rules

1. **Never silently swallow exceptions.**
   `except Exception: pass` is banned.  Log at WARNING and continue, or let it
   propagate.  Silent swallowing hides real failures and makes debugging
   impossible.

2. **README must contain Trade-offs, Limitations, and Non-goals sections.**
   Every project README needs these headings.  Without them the reader has no
   way to evaluate fitness for purpose.  This is a floor check — presence is
   required, quality is reviewed separately.

3. **Centralise domain vocabularies.**
   Never duplicate frozensets, StrEnum classes, or constant dicts that define
   overlapping domain concepts across production modules.  Divergent copies
   drift and cause silent bugs.  Put the canonical set in one module and import
   it everywhere.

4. **Do not use assurance language for heuristic code.**
   Functions named `safety_check`, `secure_filter`, or `containment_scan` that
   are implemented as substring matches or keyword-in-set checks are
   misleading.  Name them for what they actually do (e.g. `keyword_filter`,
   `blocklist_match`).

5. **Never rebuild expensive resources per request.**
   Knowledge stores, database engines, policy objects, and search indices must
   be constructed once (at module level or in `__init__`) and reused.
   Per-request construction wastes time, memory, and often causes connection
   leaks.
