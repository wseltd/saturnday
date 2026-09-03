# API Design Rules

## Goal

Public APIs must be predictable, narrow, and difficult to misuse.

## Rules

1. Public functions should have explicit parameters.
Do not force callers to rely on hidden global state.

2. Defaults must be visible.
A caller should be able to see the normal behaviour from the signature and docstring.

3. Return values must be stable.
Prefer one clear return shape. Avoid mixed return types unless truly justified.

4. Boundary validation belongs at the edge.
Reject invalid inputs early with specific errors.

5. Internal complexity should stay internal.
Expose a small surface. Hide implementation detail behind well named private helpers.

6. Configuration should flow through one path.
Do not let callers configure the same behaviour in multiple inconsistent ways.

7. Error messages must help the caller recover.
State what is wrong, which input caused it, and how to fix it.

8. Avoid convenience overload.
A short function with three ambiguous modes is worse than two explicit functions.

## Good signs

- clear parameter names
- stable output shape
- few surprising side effects
- private helpers with narrow responsibilities
- strong tests against the public surface

## Bad signs

- multiple hidden defaults
- boolean flag soup
- internal objects leaking into public return values
- undocumented side effects
- generic helpers that obscure the real behaviour
