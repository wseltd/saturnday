"""Condensed engineering standards digest for non-coder phases.

The full standards (26K chars) go to ticket execution and repair where
the AI is writing code.  This digest (~2.5K chars) captures the key
rules for phases that need awareness of standards but don't need the
full corpus: planning, enrichment, DoD evaluation, and simplification.
"""

STANDARDS_DIGEST = """\
ENGINEERING STANDARDS DIGEST (key rules the coder will be held to):

CODE QUALITY:
- Smallest correct patch. Do not over-engineer.
- Preserve existing function signatures and contracts.
- Readable over clever. Explicit over implicit.
- Validate inputs at system boundaries only.
- No speculative abstractions — build for today's requirements.
- No dead code. No stubs. No placeholder TODOs.
- Type hints on all public APIs.
- Google-style docstrings on public functions.
- No except Exception: pass — log at WARNING and continue.
- raise X from e for exception chaining.
- logging.getLogger(__name__) with %s formatting, not f-strings.
- Named constants instead of magic numbers.

TESTING:
- Assert observable behaviour, not implementation details.
- 3x more tests for risky logic. FORBIDDEN: even test distribution.
- No tautological assertions (assert True, assert 1==1).
- No empty test bodies (pass, ..., docstring-only).
- Mock only at boundaries (network, filesystem, time).
- Test names encode scenario + expected outcome.
- Regression tests must fail without the fix.

PROJECT HYGIENE:
- README must have Trade-offs, Limitations, Non-goals sections.
- LICENSE file required (MIT default).
- .gitignore required — never commit agent artifacts.
- pyproject.toml with build-backend = "setuptools.build_meta".
- Project must run from a clean checkout.

ARCHITECTURE:
- Centralise domain constants — never duplicate across modules.
- One config path. Visible defaults.
- Small public surface. Cohesive modules.
- Prefer stdlib. Minimise dependencies.
- Functions under ~40 lines.

SECURITY:
- Parameterised SQL/Cypher queries (never f-string user input into queries).
- html.escape() for user content in templates.
- SecretStr for secrets in Pydantic models.
- No hardcoded API keys, tokens, or credentials.
"""
