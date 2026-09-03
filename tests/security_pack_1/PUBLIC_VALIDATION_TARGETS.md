# Public Validation Targets for Security Pack 1

Curated list of intentionally vulnerable apps for later sanity-checking.
Do NOT run these as automated tests. Use for manual validation only.

| Target | URL | Why useful |
|--------|-----|-----------|
| OWASP Juice Shop | github.com/juice-shop/juice-shop | Covers IDOR, missing auth, client-trust, secrets |
| DVWA | github.com/digininja/DVWA | Classic auth bypass, CSRF, injection patterns |
| NodeGoat | github.com/OWASP/NodeGoat | Node.js specific: JWT, session, auth middleware gaps |
| Damn Vulnerable Web Application (Python) | github.com/anxolerd/dvpwa | Python Flask patterns |
| WebGoat | github.com/WebGoat/WebGoat | JWT, OAuth, CSRF training scenarios |
| Vulnerable-Node | github.com/cr0hn/vulnerable-node | Express.js auth and injection patterns |

## Rule-to-Target mapping

| Rule ID | Best public target | Notes |
|---------|-------------------|-------|
| SEC-001 | NodeGoat, WebGoat | Both have hardcoded secret examples |
| SEC-002 | Juice Shop, DVWA | Admin routes accessible without auth |
| SEC-003 | Vulnerable-Node | Socket.IO without auth handshake |
| SEC-004 | WebGoat (OAuth lab) | OAuth callback without state |
| SEC-005 | DVWA | Session cookies without secure flags |
| SEC-006 | NodeGoat | Math.random() for session tokens |
| SEC-007 | Juice Shop | Logout doesn't invalidate JWT |
| SEC-008 | NodeGoat | alg:none in JWT verification |
| SEC-009 | DVWA, WebGoat | CSRF labs with no token protection |
| SEC-010 | Juice Shop | No rate limiting on /login |
| SEC-012 | WebGoat | JWTs without exp claim |
| SEC-014 | Juice Shop | /rest/user/whoami IDOR pattern |
| SEC-016 | Juice Shop | Client-supplied quantity/price on order |
| SEC-FE-001 | Juice Shop frontend | React components with inline config |
| SEC-FE-002 | Any Stripe tutorial repo | Stripe key hardcoded in frontend demo code |

## Usage

Clone a target locally, then run:

```bash
# Scan the whole repo from first commit
saturnday check --repo /path/to/juice-shop \
  --diff $(git -C /path/to/juice-shop rev-list --max-parents=0 HEAD)..HEAD \
  --output /tmp/juice-shop-scan

# Or use governance subcommand (strict by default)
saturnday governance --repo /path/to/juice-shop \
  --diff $(git -C /path/to/juice-shop rev-list --max-parents=0 HEAD)..HEAD
```

Compare the scanner findings against the known vulnerability list for the
target app to assess false-negative rate and noise level.
