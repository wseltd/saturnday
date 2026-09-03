# Engineering Constitution

This codebase prefers clarity, restraint, and correctness over cleverness.

## Core principles

1. Write the smallest correct change.
A patch should solve the stated problem without speculative extras.

2. Optimise for maintainability.
Code should still make sense to a careful engineer six months later.

3. Keep public contracts stable.
If behaviour, signatures, return types, config semantics, CLI output, or file formats change, treat that as a contract change and justify it explicitly.

4. Validate at boundaries.
Normalise and validate inputs at the edge. Keep internal logic simple.

5. Fail clearly.
Raise specific errors with useful messages. Do not hide failure behind vague exceptions or silent fallbacks.

6. Prefer explicitness.
Use clear names, named constants, and direct control flow. Avoid magical helpers and hidden state.

7. Tests must prove behaviour.
A test that only executes code is not enough. Every meaningful test should assert an observable outcome.

8. Keep modules cohesive.
Each module should own one concern. Do not mix parsing, IO, business rules, and presentation without reason.

9. Avoid speculative abstraction.
Do not introduce layers, base classes, registries, or generic helpers until there is real repeated pressure.

10. Preserve operational discipline.
Logs, CLI output, file writes, and errors should be predictable and automation-friendly.

## Naming rules

- Prefer literal names over clever names.
- Name functions for what they do, not how they feel.
- Name variables for the domain concept, not their type.
- Avoid `data`, `result`, `handler`, `manager`, `util`, `helper` unless the narrower name would be misleading.

## Function rules

- One function should do one coherent job.
- Keep parameter lists deliberate.
- If a function needs many booleans, the design is probably wrong.
- Return shapes should be predictable and documented by usage, typing, or both.

## Error handling rules

- Catch exceptions only when you can add context, translate them meaningfully, or recover safely.
- Never use broad exception handling to hide uncertainty.
- Do not swallow exceptions silently.
- User-facing errors should say what failed and what to do next.

## State and configuration rules

- Centralise configuration.
- Do not scatter magic strings or constants through the codebase.
- Prefer dependency injection or explicit parameters over hidden globals.
- Defaults must be visible and reasonable.

## Testing rules

- Test public behaviour first.
- Use regression tests for bugs.
- Avoid fake tests, tautological assertions, and empty smoke tests.
- Mock only at boundaries.
- If a mock hides the behaviour being tested, the test is weak.

## Review posture

Reject changes that:
- increase abstraction without reducing risk
- widen scope without necessity
- add complexity without a measurable gain
- claim confidence that the code does not justify

Prefer changes that:
- tighten contracts
- remove ambiguity
- improve naming
- improve tests
- reduce hidden behaviour
