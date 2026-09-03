# OpenClaw Publish-Readiness Checklist

A skill is ClawHub-ready when all required items below are satisfied.
This checklist is enforced by `saturnday publish-preflight`.

## Required

- [ ] **SKILL.md exists** — must be present at repo root with valid metadata
- [ ] **Description present** — SKILL.md has a non-empty Description section
- [ ] **Usage documented** — SKILL.md has a Usage section with at least one example
- [ ] **No blocking Guard findings** — `saturnday scan --skill .` returns PASS at configured severity
- [ ] **verify_cmd passes** — the skill's test/validation command exits 0
- [ ] **Policy file present** — `saturnday-policy.yaml` exists if required by target registry
- [ ] **No hardcoded secrets** — secret scan passes (no API keys, tokens, or credentials in code)
- [ ] **No dangerous shell patterns** — no unguarded `subprocess`, `os.system`, `eval`, or command interpolation

## Recommended

- [ ] **Examples section** — SKILL.md includes at least one concrete example
- [ ] **Limitations documented** — SKILL.md lists known limitations
- [ ] **License specified** — SKILL.md or repo root has a LICENSE file
- [ ] **Requirements listed** — all runtime dependencies documented
- [ ] **Ratchet baseline** — `.saturnday-baseline.json` exists with zero new findings on compare

## Verification Commands

```bash
# Run Guard scan
saturnday scan --skill .

# Run publish preflight
saturnday publish-preflight --skill .

# Run ratchet comparison
saturnday baseline compare --repo . --baseline .saturnday-baseline.json
```
