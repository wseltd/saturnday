# Repository Hygiene Rules

## Purpose

Keep the repository clean and predictable for both humans and automated tools.

## Rules

1. **Always include a `.gitignore`.**
   Every generated repository must have a `.gitignore` that excludes common
   artifacts: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`,
   `*.egg-info/`, `dist/`, `build/`, `.env`, and agent-specific output
   directories.  The `.gitignore` is written before any tickets execute, not
   auto-committed.

2. **Never commit agent artifacts.**
   Evidence files, state files, and intermediate outputs from the orchestration
   pipeline must be written outside the repository (or to an ignored path
   inside it).  They are audit trails, not deliverables.

3. **Evidence lives outside the repo.**
   The default evidence directory is `../.saturnday-<project_id>/`
   relative to the repository root.  This keeps the working tree clean and
   avoids accidental commits of large JSON evidence files.

4. **Keep generated files out of diffs.**
   Lock files, build outputs, and auto-generated configs should be in
   `.gitignore`.  If a generated file must be committed, it must be
   deterministic (same inputs → same output).

5. **One concern per commit.**
   Each ticket produces one commit.  Do not bundle unrelated changes.  This
   makes bisect, revert, and review straightforward.
