# Agent-Generated Fixture Provenance

These fixtures simulate code patterns that AI coding assistants commonly generate
when they fail to follow security best practices. They are used to verify that
the governance scanner correctly detects real-world vulnerable patterns.

## Generation Details

- **Agent**: Claude (Anthropic)
- **Model version**: claude-opus-4-6
- **Generation date**: 2026-03-17
- **Method**: One-time generation with security-focused prompts asking for
  common vulnerable patterns in web applications
- **Review**: All fixtures manually reviewed to confirm they contain the
  intended vulnerability class and no unintended issues

## Usage

These fixtures are **static files** checked into the repository. The test suite
does NOT depend on live model access to run. Each fixture has deterministic
expected findings documented in the test assertions.

## Fixture Inventory

### Python Fixtures

| File | Vulnerability Class | Rule IDs |
|------|-------------------|----------|
| `agent_jwt_hardcoded.py` | Hardcoded JWT secret | SEC-001 |
| `agent_auth_missing.py` | Missing auth on routes | SEC-002 |
| `agent_weak_random.py` | Weak randomness for security | SEC-006 |
| `agent_sql_format.py` | SQL injection via f-string | SEC-015 |
| `agent_xss_template.py` | XSS via unescaped output | SEC-013 |
| `agent_csrf_missing.py` | Missing CSRF protection | SEC-009 |
| `agent_cookie_insecure.py` | Insecure cookie settings | SEC-005 |

### TypeScript Fixtures

| File | Vulnerability Class | Rule IDs |
|------|-------------------|----------|
| `agent_jwt_hardcoded.ts` | Hardcoded JWT secret | SEC-001 |
| `agent_auth_missing.ts` | Missing auth on routes | SEC-002 |
| `agent_weak_random.ts` | Weak randomness for security | SEC-006 |
| `agent_sql_format.ts` | SQL injection via template literal | SEC-015 |
| `agent_xss_template.ts` | XSS via innerHTML | SEC-013 |
| `agent_csrf_missing.ts` | Missing CSRF protection | SEC-009 |
| `agent_cookie_insecure.ts` | Insecure cookie settings | SEC-005 |
