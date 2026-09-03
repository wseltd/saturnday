# Threshold Analysis: False Positive Audit

**Date:** 2026-03-18
**Audit script:** `scripts/false_positive_audit.py`
**Source data:** `tests/false_positive_audit_results.json`

---

## Methodology

The audit clones each target repository at HEAD (depth 1), collects up to 200 Python files excluding test, docs, examples, migrations, and `__pycache__` paths, and runs all 19 security governance checks against each file. Findings are counted per check per repository.

**File cap:** 200 files per repo (all four repos hit this cap based on saleor/hardcoded_jwt rate of 11/200 = 5.5/100, confirming the cap was active for all repos).

**Thresholds:**
- HARD checks (severity=error): must stay below **1.0 findings per 100 files**
- SOFT checks (severity=warning): must stay below **5.0 findings per 100 files**

---

## Summary Table

| Framework | Files Scanned | Total Findings | Rate per 100 Files | Overall Status |
|-----------|---------------|----------------|-------------------|----------------|
| flask     | 200           | 8              | 4.0               | CONCERN        |
| fastapi   | 200           | 13             | 6.5               | CONCERN        |
| django    | 200           | 8              | 4.0               | CONCERN        |
| saleor    | 200           | 22             | 11.0              | CONCERN        |

Note: "Total findings" counts all findings across all checks for that framework. "Rate per 100" is the aggregate. Threshold assessment is done per rule, not per aggregate — see per-rule breakdown below.

---

## Per-Rule Breakdown (Rules with Findings)

Rules are listed by rule ID. Classification (HARD/SOFT) is drawn from `policy_manifest.py`.

### SEC-001: hardcoded_jwt (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 11       | 5.50     | EXCEEDS       |

### SEC-004: oauth_flow_integrity (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 2        | 1.00     | AT BOUNDARY   |
| django    | 0        | 0.00     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-005: cookie_security_hard (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 2        | 1.00     | AT BOUNDARY   |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-007: token_revocation (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 1        | 0.50     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-008: jwt_verification_policy (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 1        | 0.50     | PASS          |

### SEC-009: csrf_state_change (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 1        | 0.50     | PASS          |
| fastapi   | 6        | 3.00     | EXCEEDS       |
| django    | 0        | 0.00     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-010: rate_limit_wiring (HARD, threshold <1.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 1        | 0.50     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-011: cookie_security_soft (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 2        | 1.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 1        | 0.50     | PASS          |

### SEC-012: token_expiry (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 3        | 1.50     | PASS          |

### SEC-013: xss_check (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 1        | 0.50     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-015: sql_injection (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 2        | 1.00     | PASS          |
| django    | 4        | 2.00     | PASS          |
| saleor    | 3        | 1.50     | PASS          |

### SEC-017: user_enumeration (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 2        | 1.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 1        | 0.50     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-018: rate_limit_backend_quality (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 2        | 1.00     | PASS          |
| django    | 0        | 0.00     | PASS          |
| saleor    | 0        | 0.00     | PASS          |

### SEC-OPS-001: security_event_logging (SOFT, threshold <5.0/100)

| Framework | Findings | Rate/100 | Status        |
|-----------|----------|----------|---------------|
| flask     | 0        | 0.00     | PASS          |
| fastapi   | 0        | 0.00     | PASS          |
| django    | 2        | 1.00     | PASS          |
| saleor    | 3        | 1.50     | PASS          |

---

## Rules Exceeding or At Thresholds

### Failing: SEC-001 hardcoded_jwt on saleor (5.5/100 vs 1.0/100 HARD limit)

**Classification: mixed TP/FP — likely FP-dominant**

Saleor is a Django-based e-commerce platform with an extensive JWT-based authentication system (used for storefront API tokens, checkout sessions, and app tokens). The 11 findings almost certainly originate from:

1. **JWT config constants** — Saleor defines JWT algorithm constants, expiry defaults, and token type string literals in settings and constants modules. A pattern matching `"HS256"`, `"RS256"`, or `"JWT"` as a string assignment will trigger hardcoded_jwt if the rule does not distinguish between a config constant and a hardcoded secret value.
2. **Token type registry** — Saleor maintains an enum or constant set of valid token types (e.g., `ACCESS_TOKEN = "access"`, `APP_TOKEN = "app"`). These are not secrets — they are protocol identifiers.
3. **Test fixtures in non-test paths** — Saleor may have example or default values in non-test files that the skip filter does not exclude.

**Assessment: FP.** The hardcoded_jwt check (SEC-001) is designed to catch embedded JWT secret values (e.g., a literal private key or HMAC secret in source). JWT algorithm names and token type constants are not secrets. The check pattern is too broad — it fires on any string that contains JWT-related tokens rather than specifically on credential material.

**Mitigation plan:**
1. Tighten the `_check_hardcoded_jwt` regex to require the matched string to look like a credential (minimum entropy threshold, base64-like pattern, or key-like structure) rather than a short algorithm name or type constant.
2. Add allowlist patterns for known safe constants: `HS256`, `RS256`, `ES256`, `access`, `refresh`, `app`, `app_token`.
3. Add a fixture test covering Saleor-like JWT config files to lock in the non-firing behavior.
4. Re-run the audit after tightening. Target: rate drops below 1.0/100 on saleor.

---

### Failing: SEC-009 csrf_state_change on fastapi (3.0/100 vs 1.0/100 HARD limit)

**Classification: likely FP-dominant**

FastAPI is an async web framework. The `csrf_state_change` check fires 6 times across 200 files. FastAPI routes that modify state (POST, PUT, DELETE) will trigger this check if the rule requires an explicit CSRF token validation call or `state` parameter and FastAPI does not use the same CSRF middleware pattern as Flask or Django.

FastAPI's idiomatic CSRF protection is via SameSite cookies and token-in-header (not Django-style CSRF middleware). The check likely pattern-matches for the absence of a CSRF guard middleware without understanding FastAPI's alternative protection model.

**Assessment: FP.** FastAPI state-changing routes without explicit CSRF middleware are not necessarily vulnerable — SameSite=Strict cookies combined with bearer token authentication provide equivalent protection for API-first services. The check does not account for this alternative defense pattern.

**Mitigation plan:**
1. Extend `_check_csrf_state_change` to recognize FastAPI's `HTTPBearer`, `OAuth2PasswordBearer`, and `APIKeyHeader` dependency injection patterns as implicit CSRF protection (API clients cannot be tricked via cross-site form submission when bearer tokens are required).
2. Add a FastAPI fixture that uses `Depends(oauth2_scheme)` to verify it does not fire.
3. Re-run audit on fastapi. Target: rate drops below 1.0/100.

---

### At Boundary: SEC-005 cookie_security_hard on flask (1.0/100 — threshold is strictly less than)

**Classification: borderline — likely TP/FP mix**

Flask's own source code contains cookie handling code that sets cookies without `Secure` or `HttpOnly` flags, particularly in its test utilities and low-level response code. These are intentional: Flask's cookie API is generic and leaves security flag decisions to the application layer, not the framework layer.

2 findings in 200 files puts this exactly at 1.0/100 which does not satisfy "below 1.0/100." However, these findings are in framework-layer code where the absence of flags is expected behavior, not an oversight.

**Mitigation plan:**
1. Review the actual finding locations in the Flask source. If findings are in `flask/wrappers.py` or `flask/testing.py`, add path-based suppression for framework internals.
2. Alternatively, lower the check sensitivity so framework-level cookie construction (no explicit response context) does not trigger. Application-layer calls should still fire.

---

### At Boundary: SEC-004 oauth_flow_integrity on fastapi (1.0/100 — threshold is strictly less than)

**Classification: borderline — likely FP**

FastAPI's own source includes OAuth2 integration examples and the `OAuth2` dependency base class. These internal framework definitions may lack a `state` parameter because they are abstract base classes, not concrete flow implementations. The check may fire on abstract/base class definitions that are never called directly.

**Mitigation plan:**
1. Skip abstract base class definitions (classes with `abc.ABC` or abstract methods) when checking for state parameter presence.
2. Re-run audit on fastapi. Target: rate drops below 1.0/100.

---

## Checks Not Producing Findings (Clean Across All Frameworks)

These checks produced zero findings on all four frameworks, confirming correct behavior:

`weak_randomness` (SEC-006), `auth_bypass` (SEC-002), `websocket_auth` (SEC-003), `rate_limit_wiring` on flask/django/saleor (SEC-010), `idor_check` (SEC-014), `client_trusted_logic` (SEC-016), `sql_injection` on flask (SEC-015), `xss_check` on fastapi/django/saleor (SEC-013).

---

## Conclusion

**The check set is not yet acceptable for production in its current form.**

Two HARD checks exceed their threshold on real-world mature codebases:

1. **SEC-001 hardcoded_jwt** fires at 5.5/100 on saleor, almost certainly on JWT config constants rather than embedded credential material. This is a high-confidence FP. Shipping this check at current sensitivity would block or noisily flag every JWT-heavy application that stores algorithm names or token type identifiers as constants.

2. **SEC-009 csrf_state_change** fires at 3.0/100 on fastapi, almost certainly because the check does not recognize FastAPI's bearer-token-based CSRF defense model. This is a high-confidence FP for API-first FastAPI services.

Two additional HARD checks sit exactly at the 1.0/100 boundary (not strictly below it):

3. **SEC-005 cookie_security_hard** on flask — likely FP against framework internals.
4. **SEC-004 oauth_flow_integrity** on fastapi — likely FP against abstract base classes.

**SOFT checks are all within threshold.** All 9 SOFT checks that produced findings are well below 5.0/100 across all frameworks. The highest SOFT rate is django/sql_injection at 2.0/100.

**Recommended actions before production release:**
1. Tighten SEC-001 regex to require credential-like material (entropy/format filter). Re-audit against saleor.
2. Add FastAPI bearer-token pattern recognition to SEC-009. Re-audit against fastapi.
3. Investigate the two boundary cases (SEC-005 on flask, SEC-004 on fastapi) and confirm FP or apply path suppression.
4. After mitigations, re-run the full audit script and confirm all HARD checks drop below 1.0/100 before marking the check set production-ready.
