# Testing Rules

## Goal

Tests must prove behaviour, catch regressions, and remain readable.

## Rules

1. Every non-trivial test must assert something meaningful.
Execution alone is not proof.

2. Test observable behaviour.
Prefer outputs, state changes, written files, raised errors, or user-facing messages over internal call structure.

3. Add regression tests for bugs.
If a bug was fixed, the test should fail if that bug returns.

4. Keep tests specific.
A reader should understand what contract is being protected.

5. Mock only at real boundaries.
Use mocks for external services, subprocesses, network IO, or clock behaviour. Do not mock your own logic unless forced.

6. Avoid tautological tests.
No `assert True`, no checks that simply mirror the implementation, no empty smoke tests presented as proof.

7. Name tests after behaviour and edge case.
The name should state what matters.

8. Test failure messages should be interpretable.
When a test fails, the next action should be obvious.

## Required anti-pattern bans

- empty test bodies
- skipped tests used as normal coverage
- asserting only that a mock was called when behaviour is the real contract
- snapshotting huge outputs instead of asserting the specific contract
- overly broad fixtures that hide setup and meaning

## Additional rules

9. Tests must cover the risk surface, not just the happy path.
If a function handles errors, edge cases, or boundary conditions, the tests must exercise them. Shape-only tests (assert result is not None) do not count.

10. Regression tests must fail without the fix.
Write the test first against the broken code path to confirm it catches the bug, then verify it passes with the fix applied. A regression test that never could have failed is worthless.

11. Test names must encode the scenario AND the expected outcome.
`test_empty_input_returns_none` is good. `test_function` is not. The name is documentation for the next person who sees a red line in CI.
