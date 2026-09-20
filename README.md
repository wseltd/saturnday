# Saturnday

AI coding tools generate code fast, but the output is weakly governed — no planning, no security checks, no evidence trail. Teams adopting AI coders risk shipping hallucinated imports, leaked secrets, placeholder stubs, and untested code at speed.

Saturnday adds governance around the AI coding workflow: structured planning, 60+ deterministic checks on every commit, automated repair of findings, and timestamped evidence for every change. The exact number that runs depends on your repo — TypeScript, Docker, Terraform and Kubernetes checks only fire when those files are present. It works with any AI coder that can commit to git — you keep your tools, Saturnday keeps quality, security, and release hygiene under control.

**v1.1.6** | Python 3.10+ | MIT License

## About this repository

Saturnday is not new. It has been published on PyPI since **13 March 2026**,
through 45 releases up to v0.3.44. This repository is a fresh public home for
the project as of v1.1.5 — the code has simply moved here, it did not start
here.

**Everything is now free.** Saturnday previously shipped as an open-core
product: a public package plus a separate paid `saturnday-premium` add-on that
required a licence key. As of v1.1.5 the two are merged into this single
package, released under the MIT licence. All formerly paid capabilities —
LLM security triage, the governed memory system, specification-aware bug
detection, impact analysis, the LLM code reviewer, document claim
verification and signoff, run metrics, and the premium evidence layer — are
included and enabled by default. There is no licence key, no entitlement
check, and no paid tier.

```bash
pip install saturnday
```

## What you can do with it

**Governance** — Review code against deterministic checks before it ships.

```bash
saturnday governance --repo . --full     # Review all tracked files
saturnday governance --repo . --staged   # Check staged changes before commit
```

**Governed build** — Turn a brief into governed tickets, execute them through your AI coder, check every change, and produce a full evidence trail.

```bash
saturnday start
# Describe what to build. Saturnday plans, executes, checks, repairs, and reports.
```

**Repair** — Fix governance findings automatically.

```bash
saturnday repair --repo . --backend claude-cli
```

**Document mode** — Produce governed documents (specs, reports, audits) with section-level checks, cross-section consistency validation, claim verification, and signoff evidence.

```bash
saturnday start --document --doc-spec doc-spec.yaml
```

**Release preflight** — Inspect built artefacts (wheels, sdists, npm tarballs, OCI images) for secrets, internal files, source maps, and allowlist violations before publishing.

```bash
saturnday release-preflight --repo . --wheel dist/mypackage-1.0-py3-none-any.whl
```

## Install

```bash
pip install saturnday
```

Minimal setup. Install Saturnday, choose a supported backend, and it configures project-local governance when you run `saturnday start`.

## Quick start

### Govern an existing project

```bash
cd ~/projects/my-project
saturnday governance --repo . --full
```

This runs all governance checks against tracked files and writes an evidence report to `.saturnday/evidence/`.

To install the pre-commit hook so governance runs on every commit:

```bash
saturnday hook install
```

### Start a new governed project

```bash
mkdir -p ~/projects/my-new-project
cd ~/projects/my-new-project
saturnday start
```

Saturnday detects installed backends, installs governance hooks, and launches the workflow. Describe what you want to build, and Saturnday will plan it into governed tickets, execute them through your chosen AI coder, check every change, and produce evidence.

## Recommended workflow (saves tokens and subscription usage)

The default `saturnday start` flow runs a full governance pass **after every
ticket**. That is the safest setting, but on a large plan it is also the most
expensive — each pass costs another round of coder tokens and usage.

If you are working interactively with Claude Code, this flow gives the same
governed result for a fraction of the usage:

**1. Start Saturnday**

```bash
saturnday start
```

**2. Ask for a plan, and say explicitly that it must not build yet**

```
Plan this with Saturnday but do not start coding yet:
build a REST API with authentication, rate limiting, and tests.
```

Saturnday plans the tickets and stops. Review them here — this is the cheapest
point at which to correct direction.

**3. Once the tickets exist, have the coder implement them directly**

```
The plan looks right. Now code ticket T001 directly.
```

Work through the tickets with the coder. Driving the coder directly, rather
than through the per-ticket run loop, avoids a full governance pass between
every ticket.

**4. Run governance once, at the end**

```bash
saturnday governance --repo . --full
saturnday repair --repo . --backend claude-cli
```

### Why this saves usage

| | Per-ticket governance | Plan → code → govern once |
|---|---|---|
| Governance passes | one per ticket | one at the end |
| Coder tokens | highest | lowest |
| Feedback speed | immediate per ticket | at the end |
| Best for | unattended runs, CI | interactive work with a subscription coder |

**Trade-off:** governing once at the end means a problem introduced early is not
caught until the end, so the fix may touch more code. For unattended runs or
security-sensitive work, keep the per-ticket loop. The pre-commit hook applies
either way — commits failing governance are blocked regardless.

## Supported backends

Saturnday works with these coder backends:

| Backend | Type | Notes |
|---------|------|-------|
| `claude-cli` | CLI subscription | Claude Code in the terminal |
| `codex-cli` | CLI subscription | OpenAI Codex CLI |
| `openclaude` | CLI | OpenClaude open-source backend |
| `cursor-cli` | CLI | Cursor in the terminal |
| `openai` | API key | OpenAI API directly |
| `anthropic` | API key | Anthropic API directly |

`saturnday start` detects which backends are installed and verifies login before launching.

## What it checks

Saturnday runs **48+ deterministic governance checks** on every Python project, covering code quality, AI-specific defects, and security. Additional checks activate based on project content — DevOps files, TypeScript/JavaScript code, and project metadata can each add more.

Release preflight and document governance are separate check surfaces with their own commands and evidence.

### AI-specific defects

- Hallucinated imports — packages that don't exist on npm or PyPI
- Fake tests — `assert True`, empty test bodies, tautological assertions
- Placeholder code — TODO stubs, `pass`-only functions, `NotImplementedError` bodies
- API version mismatches — calling methods that don't exist in the installed library version
- Dead code — functions defined but never referenced in any project file

### Security (OWASP-mapped)

19 security checks with CWE and OWASP A-category mappings:

- Leaked secrets, hardcoded JWT, weak randomness
- Auth bypass, CSRF, XSS, SQL injection, IDOR
- Cookie security, token revocation, OAuth flow integrity
- Rate limiting, websocket auth, security event logging
- Frontend secret exposure, payment provider secrets

### Code quality

- Ruff linting, Bandit security analysis, pip-audit
- Dependency declaration and version pinning
- Typosquat detection, module conflict detection
- README/LICENSE validation, doc-code consistency
- Python version compatibility, mypy type checking
- Test execution and test quality analysis

### DevOps (activates when relevant files are present)

- Dockerfile, GitHub Actions, GitLab CI, Jenkinsfile
- Terraform, Kubernetes manifests, config file security
- Optional Trivy integration for IaC scanning

### How the count works

The base check count for a Python project is 48. This number changes depending on what the project contains — a repo with Dockerfiles, CI configs, or TypeScript files will run additional checks on top of the base. The `Checks run` total in every governance report reflects exactly how many checks executed for that scan.

## Document mode

`saturnday start --document` produces governed documents from a specification file:

```bash
saturnday start --document --doc-spec doc-spec.yaml --backend claude-cli
```

The pipeline: parse spec, plan sections, generate each section through the coder backend, run section-level checks (structure, placeholders, citations, evidence coverage), retry failures, run cross-section consistency checks, extract and verify claims, and produce a signed-off evidence pack.

Document governance checks are separate from code governance and do not appear in `saturnday governance` output.

## Release preflight

`saturnday release-preflight` inspects built artefacts before publishing:

```bash
saturnday release-preflight --repo . --type python
saturnday release-preflight --wheel dist/mypackage-1.0-py3-none-any.whl
saturnday release-preflight --type npm --tarball my-package-1.0.0.tgz
saturnday release-preflight --github-release owner/repo:v1.0.0
```

It runs 5 release-security checks (REL-001 through REL-005): source map blocking, secrets in artefact, internal file blocking, allowlist manifest validation, and release diff comparison.

This is separate from code governance. Code governance checks your source. Release preflight checks what you're about to ship.

## How `saturnday start` works

When you run `saturnday start`, Saturnday:

1. **Detects your backend** — with real login and installation verification
2. **Installs governance** — pre-commit hook, coder-specific rules (CLAUDE.md/AGENTS.md), and hooks
3. **Launches the workflow** — you talk to the coder directly (Claude Code, OpenClaude) or Saturnday orchestrates silently (Codex, API backends)

When you ask it to build something:

1. **Plans** — generates design-decision-rich tickets
2. **Splits** — breaks large tickets into governed pieces
3. **Executes** — each ticket coded, governance-checked, and committed individually
4. **Retries** — failed tickets get governance findings fed back (up to 3 attempts)
5. **Auto-repairs** — remaining failures go through the repair pipeline
6. **Simplifies** — removes unnecessary complexity
7. **Reviews** — role passes evaluate the result (repo analyst, governance judge, definition of done, evidence gate)
8. **Reports** — governance report, run report, and action items for anything that still needs attention

Every commit is blocked by the pre-commit hook if it fails governance.

## Policy configuration

Create a `.saturnday-policy.yaml` in your repo root to exempt expected findings:

```yaml
expected_findings:
  - remote_download    # This is a web crawler — downloading is its job
  - broad_filesystem   # File organiser needs filesystem access
  - blast_radius       # New project, expected
```

`saturnday start` offers a strict/relaxed mode choice. Relaxed mode creates a policy file that exempts publish-readiness checks — useful for private repos and early development.

## Interface contract checks

`saturnday validate-plan` checks that a plan's tickets agree with each other
before any of them run.

```bash
saturnday validate-plan --plan .saturnday/plan.json --repo .
```

| Rule | Severity | Catches |
|------|----------|---------|
| `IC-001` | error | Two tickets each claiming the same file or the same exported symbol |
| `IC-002` | warning | A `verify_cmd` naming an executable that is not a baseline tool, not in `package.json`/`pyproject.toml`, and not on `PATH` |

IC-001 is opt-in. A ticket declares what it owns:

```json
{
  "ticket_id": "T031",
  "provides": {
    "files": ["tests/fixtures/loader.ts"],
    "exports": ["defineFixture(fixture: SyntheticFixture): SyntheticFixture"]
  }
}
```

Ownership is compared on the symbol name, not the full signature, so two
tickets exporting `defineFixture` with different parameter names still
collide. A plan with no `provides` anywhere produces no IC-001 findings, so
existing plans are unaffected.

IC-002 is a warning and never changes the exit code on its own: every plan has
`verify_cmd` values, and whether a tool resolves depends on the machine running
the check.

These checks run only from `validate-plan`. Plan loading and the run loop are
unchanged.

## Baseline ratchet

Lock your current finding count and prevent regressions:

```bash
saturnday baseline generate --repo .
saturnday baseline compare --repo . --baseline .saturnday-baseline.json
```

Findings can go down but not up. Useful in CI to ensure governance quality never degrades.

## GitHub Action

A composite action is included at `.github/actions/saturnday-check/`. Pin to a commit SHA or release tag:

```yaml
name: Saturnday Governance
on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: write

jobs:
  governance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: honouralexwill/saturnday/.github/actions/saturnday-check@v1.1.1
```

## Evidence and reports

Every governance scan, repair, and run produces timestamped evidence:

```
.saturnday/evidence/review_20260403T.../governance-report.md
.saturnday/evidence/release_20260403T.../release-report.md
.saturnday-repair-20260403T.../repair-report.md
```

Reports include: findings by category, check results with pass/fail/skip status, and action items.

## What Saturnday installs (and what it doesn't touch)

Saturnday only creates **per-project** files. It never modifies global settings.

When you run `saturnday start` in a project folder, it creates:

| File | What it does | Scope |
|------|-------------|-------|
| `.claude/settings.json` | Adds PreToolUse/PostToolUse hooks (Claude Code only) | This project only |
| `CLAUDE.md` or `AGENTS.md` | Governance rules for the coder | This project only |
| `.git/hooks/pre-commit` | Blocks commits that fail governance | This project only |
| `bin/sat-*` | Command wrappers (Codex only) | This project only |
| `.saturnday/` | Evidence, reports, session state | This project only |

**What it does NOT touch:**
- Your global `~/.claude/settings.json` — untouched
- Your existing hooks — Saturnday adds alongside them, never overwrites
- Your existing CLAUDE.md — Saturnday appends governance rules, preserves your content
- Any other project on your machine — completely isolated

### Removing Saturnday from a project

```bash
rm -rf .claude/settings.json CLAUDE.md AGENTS.md bin/sat-* .saturnday* .git/hooks/pre-commit
```

To keep the code but stop governance on future commits, just remove the pre-commit hook:

```bash
rm .git/hooks/pre-commit
```

## OpenClaw skill

Saturnday is available as an OpenClaw skill on [ClawHub](https://clawhub.ai):

```bash
clawhub install saturnday
```

Requires `pip install saturnday` on the machine running OpenClaw.

## Public vs premium

The public `saturnday` package provides the core governed local workflow: governance checks, planning, execution, repair, document mode, release preflight, and evidence production.

`saturnday-premium` adds stronger organisational and review capabilities — enhanced code review, failure prevention, security triage, and release governance. The public package works standalone. Premium extends it without gating core functionality.

## Trade-offs

- Governance adds time to every commit (typically 5-30 seconds for the pre-commit check)
- Planned builds are slower than direct AI coding (minutes vs seconds) but produce governed, evidence-tracked results
- Some checks produce false positives on specialised code (e.g. security scanners that legitimately handle secret patterns) — use `.saturnday-policy.yaml` to exempt these
- Governance checks are deterministic (regex, AST, external tools) — they catch patterns, not intent
- LLM-based role passes (doc-code consistency, security triage) are advisory, not deterministic

## Non-goals

- Replacing human code review — Saturnday augments review, doesn't replace it
- Supporting every IDE and editor — Saturnday is terminal-first by design

## License

MIT — Waynestark Enterprises Limited
