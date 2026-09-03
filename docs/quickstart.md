# Saturnday Quick-Start Guide

Build, govern, and publish an OpenClaw skill in 10 minutes.

## Prerequisites

- Python 3.10+
- git
- Either Codex CLI (`codex login`) or Claude Code CLI (`claude login`) — your subscription

## Step 1: Install

```bash
pip install saturnday
```

Verify:
```bash
saturnday version
saturnday validate-plan --help
```

## Step 2: Start from the example

```bash
git clone https://github.com/honouralexwill/saturnday.git
cd saturnday/examples/example-skill
```

Or create your own skill repo with a `SKILL.md` (see `templates/SKILL.md` for the template).

## Step 3: Generate a plan

```bash
saturnday plan --brief "Build a skill that summarizes text" --repo .
```

This produces a `plan.json` with engineering-dossier-quality tickets.

## Step 4: Execute the plan

```bash
saturnday run --plan plan.json --repo . --backend codex-cli
```

Or use `--backend claude-cli` if you prefer Claude Code.

Each ticket is:
1. Coded by the AI with senior-engineer quality enforcement
2. Governance-checked (security + quality)
3. Post-checked (5 senior-judgment heuristics)
4. Committed only if everything passes

**Expected output:** Evidence in `output/` — `run-metadata.json`, `analytics.json`, per-ticket evidence under `tickets/`, ledger at `evidence/run/ledger.json`.

## Step 5: Guard scan

```bash
saturnday scan --skill .
```

This runs the OpenClaw scanner: checks SKILL.md structure, detects dangerous patterns (shell exec, credentials, remote download), validates publish hygiene.

**Expected output:** PASS or findings list with severity.

## Step 6: Publish preflight

```bash
saturnday publish-preflight --skill .
```

Checks ClawHub readiness against the publish-readiness checklist (see `docs/publish_readiness_checklist.md`).

## Step 7: GitHub enforcement

Add the Saturnday GitHub Action to your skill repo:

```yaml
# .github/workflows/saturnday.yml
name: Saturnday
on:
  pull_request:
    branches: [main]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: honouralexwill/saturnday/.github/actions/saturnday-check@master
```

Every PR is governance-checked. Findings are posted as PR comments. The ratchet blocks new findings.

## What to do when something fails

| Failure | What to do |
|---------|-----------|
| Governance FAIL | Read the findings. Fix the flagged code. Re-run. |
| Post-check FAIL | Check which of the 5 checks fired (silent swallow, README, domain duplicates, terminology, per-request rebuild). Fix and retry. |
| Ticket exhausts retries | Run `saturnday explain-failure --output-dir output/ --ticket T001` to understand why. |
| Run stops early | Check `evidence/run/ledger.json` for `stop_reason`. Usually 3 consecutive failures. |
| Resume after partial run | `saturnday resume --output-dir output/` — skips passed tickets automatically. |

## Command Reference

`saturnday` is the unified CLI for Guard, Run, and Repair.
