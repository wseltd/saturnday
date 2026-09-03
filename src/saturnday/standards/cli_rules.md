# CLI Rules

## Goal

Command line interfaces must be obvious, stable, and automation-friendly.

## Rules

1. Command names should be literal.
A user should guess the command correctly.

2. Option names should be consistent.
Choose one naming pattern and keep it.

3. Help text should be concise and complete.
Show what the command does, required inputs, defaults, and examples where helpful.

4. Errors must be actionable.
Tell the user what failed and what to do next.

5. Output should be deterministic when possible.
Automation should not depend on unstable phrasing.

6. Human output and machine output should be separable.
Do not mix decorative chatter with structured output.

7. Exit codes must mean something.
Non-zero exit must indicate failure. Do not succeed silently on real errors.

8. Defaults should be safe.
A user running the command without flags should not accidentally trigger destructive or misleading behaviour.

## Good CLI behaviour

- visible defaults
- predictable output paths
- clear failure messages
- stable exit codes
- examples in help

## Bad CLI behaviour

- hidden defaults
- flags that parse but do nothing
- noisy output that breaks automation
- success messages when important work was skipped
