"""Structured remediation guidance for Saturnday findings.

Maps finding kinds to actionable remediation advice including
why the finding matters, how to fix it, and optionally a patch
template. This is a pure data module with no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RemediationGuidance:
    """Remediation advice for a finding family."""

    rule_family: str
    why_it_matters: str
    how_to_fix: str
    patch_template: str | None = None
    references: tuple[str, ...] = field(default_factory=tuple)


GUIDANCE_REGISTRY: dict[str, RemediationGuidance] = {}


def _register(g: RemediationGuidance) -> None:
    """Register a RemediationGuidance entry by its rule_family key."""
    GUIDANCE_REGISTRY[g.rule_family] = g


def get_guidance(finding_kind: str) -> RemediationGuidance | None:
    """Look up remediation guidance for a finding kind.

    Args:
        finding_kind: The kind string emitted by the scanner or security check.

    Returns:
        RemediationGuidance if an entry exists, otherwise None.
    """
    return GUIDANCE_REGISTRY.get(finding_kind)


# ---------------------------------------------------------------------------
# Scanner findings (OpenClaw passive scanner)
# ---------------------------------------------------------------------------

_register(RemediationGuidance(
    rule_family="missing_skill_md",
    why_it_matters=(
        "SKILL.md is the canonical metadata contract for every OpenClaw skill. "
        "Without it the skill cannot be discovered, indexed, or published to ClawHub."
    ),
    how_to_fix=(
        "1. Create a SKILL.md at the root of the skill directory.\n"
        "2. Add a top-level heading (# Skill Name).\n"
        "3. Include a description, usage examples, and any required environment "
        "variables or dependencies.\n"
        "4. Aim for at least 50 characters of substantive content."
    ),
    patch_template=None,
    references=(),
))

_register(RemediationGuidance(
    rule_family="missing_heading",
    why_it_matters=(
        "A SKILL.md without a Markdown heading cannot be parsed into a skill "
        "title by the ClawHub indexer, leaving the skill unnamed in search results."
    ),
    how_to_fix=(
        "1. Open SKILL.md.\n"
        "2. Add a top-level heading as the first non-blank line: # Skill Name.\n"
        "3. The heading text becomes the skill's canonical display name."
    ),
    patch_template="# My Skill Name\n\nShort description of what this skill does.",
    references=(),
))

_register(RemediationGuidance(
    rule_family="thin_skill_md",
    why_it_matters=(
        "A SKILL.md shorter than 50 characters provides no useful context to "
        "consumers. Thin documentation leads to misuse and failed integrations."
    ),
    how_to_fix=(
        "1. Expand SKILL.md to include: a heading, a one-paragraph description, "
        "usage examples with sample inputs/outputs, and a list of required "
        "environment variables or permissions.\n"
        "2. Cover error conditions and known limitations."
    ),
    patch_template=None,
    references=(),
))

_register(RemediationGuidance(
    rule_family="shell_danger",
    why_it_matters=(
        "Unguarded shell execution allows command injection if any input is "
        "attacker-controlled. os.system() and subprocess with a string argument "
        "pass the command through a shell, expanding metacharacters."
    ),
    how_to_fix=(
        "1. Replace os.system(cmd) with subprocess.run(cmd_list, check=True).\n"
        "2. Pass commands as a list of strings, never as a single interpolated string.\n"
        "3. If you must accept user input as part of a command argument, quote it "
        "with shlex.quote() before including it.\n"
        "4. Set shell=False (the default) to prevent metacharacter expansion."
    ),
    patch_template=(
        'import shlex, subprocess\n'
        '# Before: os.system(f"rm {user_path}")\n'
        '# After:\n'
        'subprocess.run(["rm", shlex.quote(user_path)], check=True)'
    ),
    references=("CWE-78", "OWASP A03:2021"),
))

_register(RemediationGuidance(
    rule_family="remote_download",
    why_it_matters=(
        "Skills that download remote content at runtime may fetch malicious "
        "payloads if the source URL is compromised or attacker-supplied. "
        "Unverified downloads are a common supply-chain attack vector."
    ),
    how_to_fix=(
        "1. Pin remote URLs to a specific known-good version tag or commit hash.\n"
        "2. Verify the downloaded content against a known SHA-256 checksum before "
        "executing or unpacking it.\n"
        "3. Prefer vendoring dependencies over downloading them at runtime.\n"
        "4. If runtime download is unavoidable, require an explicit approval gate "
        "before fetching from any URL that was not hardcoded at build time."
    ),
    patch_template=(
        'import hashlib, urllib.request\n'
        'EXPECTED_SHA256 = "abc123..."  # pin this\n'
        'urllib.request.urlretrieve(url, dest)\n'
        'actual = hashlib.sha256(open(dest, "rb").read()).hexdigest()\n'
        'if actual != EXPECTED_SHA256:\n'
        '    raise RuntimeError(f"Checksum mismatch: {actual}")'
    ),
    references=("CWE-494", "OWASP A08:2021"),
))

_register(RemediationGuidance(
    rule_family="credential_leak",
    why_it_matters=(
        "Hardcoded credentials committed to a repository are exposed in git "
        "history permanently, even after deletion. Leaked API keys can be used "
        "to incur charges, exfiltrate data, or pivot into production systems."
    ),
    how_to_fix=(
        "1. Immediately rotate any key that has been committed.\n"
        "2. Remove the hardcoded value and load it from an environment variable "
        "or a secrets manager (e.g. AWS Secrets Manager, Vault, GitHub Secrets).\n"
        "3. Add the relevant .env file to .gitignore.\n"
        "4. Add a pre-commit hook (e.g. detect-secrets or gitleaks) to prevent "
        "future leaks."
    ),
    patch_template=(
        'import os\n'
        '# Before: api_key = "sk-abc123..."\n'
        '# After:\n'
        'api_key = os.environ["MY_SERVICE_API_KEY"]  # set in CI secrets / .env'
    ),
    references=("CWE-798", "OWASP A02:2021"),
))

_register(RemediationGuidance(
    rule_family="command_interpolation",
    why_it_matters=(
        "Building a shell command string with f-strings or .format() lets "
        "attacker-controlled values inject arbitrary shell metacharacters. "
        "This is command injection (CWE-77) and is trivially exploitable."
    ),
    how_to_fix=(
        "1. Switch from a string argument to a list argument in subprocess calls.\n"
        "2. Each list element is passed as a literal argument — no shell "
        "interpretation occurs.\n"
        "3. Never build shell command strings by concatenating or interpolating "
        "external input.\n"
        "4. If a shell pipeline is genuinely required, sanitise every "
        "user-supplied value with shlex.quote()."
    ),
    patch_template=(
        'import shlex, subprocess\n'
        '# Before: subprocess.run(f"convert {user_file} output.png", shell=True)\n'
        '# After:\n'
        'subprocess.run(["convert", user_file, "output.png"], check=True)'
    ),
    references=("CWE-77", "CWE-78", "OWASP A03:2021"),
))

_register(RemediationGuidance(
    rule_family="broad_filesystem",
    why_it_matters=(
        "Operations like shutil.rmtree or writes to absolute paths can destroy "
        "data outside the skill's intended working directory if a path variable "
        "is attacker-controlled or misconfigured."
    ),
    how_to_fix=(
        "1. Resolve paths relative to a known safe base directory and validate "
        "that the resolved path is still under that base (path traversal check).\n"
        "2. Avoid shutil.rmtree on paths derived from external input; prefer "
        "removing only specific known files.\n"
        "3. Log and require confirmation before any recursive deletion.\n"
        "4. Run the skill in a sandboxed directory with restricted OS permissions."
    ),
    patch_template=(
        'from pathlib import Path\n'
        'BASE = Path("/safe/working/dir").resolve()\n'
        'target = (BASE / user_input).resolve()\n'
        'if not target.is_relative_to(BASE):\n'
        '    raise ValueError("Path traversal detected")\n'
        '# Now safe to operate on target'
    ),
    references=("CWE-22", "CWE-73"),
))

_register(RemediationGuidance(
    rule_family="missing_approval_gate",
    why_it_matters=(
        "Destructive actions (delete, drop, purge) executed without user "
        "confirmation in an autonomous agent context are irreversible and can "
        "cause catastrophic data loss if the agent misinterprets intent."
    ),
    how_to_fix=(
        "1. Wrap every destructive call in an explicit confirmation prompt or "
        "approval gate that requires human sign-off before proceeding.\n"
        "2. In CLI tools, use a --dry-run flag to preview the action.\n"
        "3. In agent workflows, emit a PENDING_APPROVAL event and wait for an "
        "approved signal before executing.\n"
        "4. Log all destructive actions with the actor identity and timestamp."
    ),
    patch_template=(
        '# CLI pattern\n'
        'if not dry_run:\n'
        '    confirm = input(f"Delete {target}? [y/N] ")\n'
        '    if confirm.lower() != "y":\n'
        '        raise SystemExit("Aborted by user")\n'
        'perform_deletion(target)'
    ),
    references=("CWE-284",),
))

_register(RemediationGuidance(
    rule_family="no_tests",
    why_it_matters=(
        "Skills published without tests cannot be verified to work correctly "
        "after updates or dependency changes. Untested code is a reliability "
        "risk for any consumer that depends on the skill."
    ),
    how_to_fix=(
        "1. Create a tests/ subdirectory in the skill directory.\n"
        "2. Add at least one test file named test_<skill>.py.\n"
        "3. Cover the main happy path and at least one error condition.\n"
        "4. Ensure tests pass in CI before publishing."
    ),
    patch_template=None,
    references=(),
))

_register(RemediationGuidance(
    rule_family="no_license",
    why_it_matters=(
        "Without a LICENSE file, the default copyright law applies and "
        "consumers have no legal right to use, modify, or distribute the skill. "
        "ClawHub publish gates require an explicit license declaration."
    ),
    how_to_fix=(
        "1. Choose an appropriate open-source license (e.g. MIT, Apache-2.0).\n"
        "2. Add a LICENSE file at the root of the skill directory.\n"
        "3. Alternatively, add a LICENSE.md if you prefer Markdown format.\n"
        "4. Reference the license in SKILL.md."
    ),
    patch_template=None,
    references=(),
))

# ---------------------------------------------------------------------------
# Security pack findings (review.py / security_pack_1.py)
# ---------------------------------------------------------------------------

_register(RemediationGuidance(
    rule_family="hardcoded_jwt",
    why_it_matters=(
        "A JWT secret hardcoded in source code is exposed to anyone with "
        "repository access. An attacker who obtains it can forge valid tokens "
        "for any user, including admin accounts, bypassing all authentication."
    ),
    how_to_fix=(
        "1. Immediately rotate the JWT secret.\n"
        "2. Load the secret exclusively from an environment variable or secrets "
        "manager — never from source code or a committed config file.\n"
        "3. Ensure the secret is at least 256 bits of cryptographic randomness.\n"
        "4. Add a pre-commit hook to detect secret patterns before they land."
    ),
    patch_template=(
        'import os\n'
        '# Before: JWT_SECRET = "my-hardcoded-secret"\n'
        '# After:\n'
        'JWT_SECRET = os.environ["JWT_SECRET"]  # min 32 random bytes, base64-encoded'
    ),
    references=("CWE-798", "OWASP A02:2021", "SEC-001"),
))

_register(RemediationGuidance(
    rule_family="frontend_secret_exposure",
    why_it_matters=(
        "Secrets embedded in frontend JavaScript bundles are delivered to every "
        "browser that loads the page. Anyone with DevTools can extract them "
        "in seconds, regardless of obfuscation."
    ),
    how_to_fix=(
        "1. Move any secret-dependent logic to a server-side API route.\n"
        "2. The frontend should call your backend endpoint, which holds the secret.\n"
        "3. For Next.js, use API routes in pages/api/ or app/api/ — never "
        "NEXT_PUBLIC_ prefixed variables for secrets.\n"
        "4. Audit all .js/.ts/.jsx/.tsx files for direct use of secret env vars."
    ),
    patch_template=(
        '// Before (frontend, exposed in bundle):\n'
        '// const client = new SomeClient(process.env.SECRET_KEY)\n'
        '\n'
        '// After: proxy through a server-side API route\n'
        '// pages/api/action.ts\n'
        'const SECRET_KEY = process.env.SECRET_KEY  // server-side only\n'
        'export default async function handler(req, res) {\n'
        '  const result = await callExternalService(SECRET_KEY, req.body)\n'
        '  res.json(result)\n'
        '}'
    ),
    references=("CWE-312", "OWASP A02:2021", "SEC-FE-001"),
))

_register(RemediationGuidance(
    rule_family="payment_secret_frontend",
    why_it_matters=(
        "Payment-provider secret keys (Stripe sk_, Braintree production keys, etc.) "
        "in frontend code give any user direct API access to charge, refund, or "
        "enumerate payment data on your account. This is a critical P0 exposure."
    ),
    how_to_fix=(
        "1. Rotate the payment secret key immediately.\n"
        "2. All payment operations that require a secret key must execute "
        "server-side only.\n"
        "3. The frontend should use only publishable/public keys "
        "(e.g. Stripe pk_test_ / pk_live_) for client-side tokenisation.\n"
        "4. Create a backend endpoint that accepts a payment token from the "
        "frontend and completes the charge using the secret key."
    ),
    patch_template=(
        '// frontend — only publishable key is safe here\n'
        "const stripe = Stripe(process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY)\n"
        '\n'
        '// server-side API route — secret key stays here\n'
        'const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY)\n'
        'const charge = await stripe.paymentIntents.create({ amount, currency })'
    ),
    references=("CWE-312", "OWASP A02:2021", "SEC-FE-002"),
))

_register(RemediationGuidance(
    rule_family="auth_bypass",
    why_it_matters=(
        "An endpoint that handles sensitive data or mutations without authentication "
        "checks can be called by any unauthenticated actor, making all access "
        "controls on other endpoints irrelevant."
    ),
    how_to_fix=(
        "1. Add an authentication middleware or decorator to every sensitive route.\n"
        "2. Prefer a centralised auth layer (e.g. FastAPI dependency, Express middleware) "
        "over per-route checks, which are easier to forget.\n"
        "3. Verify the token/session on every request — do not cache auth state "
        "across requests without explicit design.\n"
        "4. Write an integration test that asserts a 401 response for "
        "unauthenticated requests."
    ),
    patch_template=(
        '# FastAPI example\n'
        'from fastapi import Depends, HTTPException\n'
        '\n'
        'def require_auth(token: str = Depends(oauth2_scheme)):\n'
        '    user = verify_token(token)\n'
        '    if not user:\n'
        '        raise HTTPException(status_code=401)\n'
        '    return user\n'
        '\n'
        '@app.get("/sensitive")\n'
        'def sensitive_endpoint(user=Depends(require_auth)):\n'
        '    ...'
    ),
    references=("CWE-306", "OWASP A07:2021", "SEC-002"),
))

_register(RemediationGuidance(
    rule_family="websocket_auth",
    why_it_matters=(
        "WebSocket connections that skip authentication allow any client to "
        "connect and receive or send real-time data. HTTP-level auth middleware "
        "does not apply to the WebSocket upgrade by default."
    ),
    how_to_fix=(
        "1. Authenticate the WebSocket handshake: validate a token passed as a "
        "query parameter or in the first message after connection.\n"
        "2. Close the socket immediately with a 4001 code if auth fails — "
        "do not process any messages from unauthenticated connections.\n"
        "3. Re-validate the token periodically for long-lived connections.\n"
        "4. Use the same token verification function as your HTTP endpoints."
    ),
    patch_template=(
        'async def websocket_endpoint(ws: WebSocket):\n'
        '    await ws.accept()\n'
        '    token = ws.query_params.get("token")\n'
        '    user = verify_token(token)\n'
        '    if not user:\n'
        '        await ws.close(code=4001)  # Unauthorised\n'
        '        return\n'
        '    # proceed with authenticated session'
    ),
    references=("CWE-306", "OWASP A07:2021", "SEC-003"),
))

_register(RemediationGuidance(
    rule_family="oauth_flow_integrity",
    why_it_matters=(
        "An OAuth flow without a cryptographically random state parameter is "
        "vulnerable to CSRF attacks. An attacker can trick a user into authorising "
        "a malicious app or linking an attacker-controlled account."
    ),
    how_to_fix=(
        "1. Generate a cryptographically random state value (secrets.token_urlsafe(32)) "
        "before redirecting to the OAuth provider.\n"
        "2. Store the state in the user's session.\n"
        "3. On callback, verify the returned state matches the stored value "
        "before exchanging the code for tokens.\n"
        "4. Reject callbacks where state is missing or mismatched."
    ),
    patch_template=(
        'import secrets\n'
        '\n'
        '# On initiation\n'
        'state = secrets.token_urlsafe(32)\n'
        'session["oauth_state"] = state\n'
        'redirect_url = f"{provider_url}?state={state}&..."\n'
        '\n'
        '# On callback\n'
        'if request.args["state"] != session.pop("oauth_state", None):\n'
        '    abort(400, "Invalid OAuth state")'
    ),
    references=("CWE-352", "OWASP A01:2021", "RFC-6749 Section 10.12", "SEC-004"),
))

_register(RemediationGuidance(
    rule_family="cookie_security_hard",
    why_it_matters=(
        "Session cookies without HttpOnly, Secure, and SameSite attributes are "
        "readable by JavaScript (XSS theft), transmittable over HTTP (interception), "
        "and vulnerable to cross-site request forgery."
    ),
    how_to_fix=(
        "1. Set HttpOnly=True to prevent JavaScript access.\n"
        "2. Set Secure=True so the cookie is only sent over HTTPS.\n"
        "3. Set SameSite=Strict or SameSite=Lax to mitigate CSRF.\n"
        "4. Set an appropriate Max-Age or Expires — never omit it for session tokens.\n"
        "5. In Flask/FastAPI, configure these at the framework level so they apply "
        "to all cookies, not per-response."
    ),
    patch_template=(
        '# Flask example\n'
        'response.set_cookie(\n'
        '    "session",\n'
        '    value=session_token,\n'
        '    httponly=True,\n'
        '    secure=True,\n'
        '    samesite="Lax",\n'
        '    max_age=3600,\n'
        ')'
    ),
    references=("CWE-614", "CWE-1004", "OWASP A05:2021", "SEC-005"),
))

_register(RemediationGuidance(
    rule_family="csrf_state_change",
    why_it_matters=(
        "State-changing endpoints (POST, PUT, DELETE, PATCH) without CSRF "
        "protection can be triggered by a malicious page the victim visits, "
        "performing actions on their behalf without consent."
    ),
    how_to_fix=(
        "1. Use a CSRF token tied to the user's session and validate it on "
        "every state-changing request.\n"
        "2. For SPA / API-only backends, enforce a SameSite cookie policy and "
        "require a custom request header (e.g. X-Requested-With) that "
        "cross-origin requests cannot set.\n"
        "3. Verify the Origin or Referer header as an additional defence.\n"
        "4. Use a framework-provided CSRF middleware rather than rolling your own."
    ),
    patch_template=(
        '# Flask-WTF example\n'
        'from flask_wtf.csrf import CSRFProtect\n'
        'csrf = CSRFProtect(app)\n'
        '\n'
        '# Template\n'
        '# <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">'
    ),
    references=("CWE-352", "OWASP A01:2021", "SEC-009"),
))

_register(RemediationGuidance(
    rule_family="rate_limit_wiring",
    why_it_matters=(
        "Endpoints without rate limiting are vulnerable to brute-force attacks, "
        "credential stuffing, and denial-of-service. Auth endpoints are especially "
        "high-value targets without rate limits."
    ),
    how_to_fix=(
        "1. Apply rate limiting to all authentication endpoints and any "
        "endpoint that is computationally expensive or sends messages.\n"
        "2. Use a distributed rate limiter (e.g. Redis-backed) in production "
        "so limits hold across multiple server instances.\n"
        "3. Return 429 Too Many Requests with a Retry-After header.\n"
        "4. Log rate-limit events for abuse detection."
    ),
    patch_template=(
        '# FastAPI with slowapi\n'
        'from slowapi import Limiter\n'
        'from slowapi.util import get_remote_address\n'
        '\n'
        'limiter = Limiter(key_func=get_remote_address)\n'
        '\n'
        '@app.post("/login")\n'
        '@limiter.limit("5/minute")\n'
        'async def login(request: Request, credentials: LoginForm):\n'
        '    ...'
    ),
    references=("CWE-307", "OWASP A07:2021", "SEC-010"),
))

_register(RemediationGuidance(
    rule_family="token_expiry",
    why_it_matters=(
        "Tokens without an expiry (exp claim) are valid forever. A stolen token "
        "grants permanent access until the secret is rotated, which requires "
        "a full re-authentication of all users."
    ),
    how_to_fix=(
        "1. Always set an exp claim when issuing JWTs.\n"
        "2. Use short expiry for access tokens (5–15 minutes) and longer for "
        "refresh tokens (days), with a separate refresh endpoint.\n"
        "3. Validate the exp claim in every token verification call — most JWT "
        "libraries do this by default but confirm it is not disabled.\n"
        "4. Implement refresh token rotation to detect token theft."
    ),
    patch_template=(
        'from datetime import datetime, timedelta, timezone\n'
        'import jwt\n'
        '\n'
        'payload = {\n'
        '    "sub": user_id,\n'
        '    "exp": datetime.now(timezone.utc) + timedelta(minutes=15),\n'
        '    "iat": datetime.now(timezone.utc),\n'
        '}\n'
        'token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")'
    ),
    references=("CWE-613", "OWASP A07:2021", "RFC-7519 Section 4.1.4", "SEC-012"),
))

_register(RemediationGuidance(
    rule_family="token_revocation",
    why_it_matters=(
        "JWTs are stateless and cannot be invalidated server-side without a "
        "blocklist. If a token is stolen or a user is force-logged-out, the "
        "token remains valid until expiry — which could be days."
    ),
    how_to_fix=(
        "1. Maintain a server-side blocklist (Redis set or DB table) of revoked "
        "token JTIs (jwt ID claim).\n"
        "2. On logout, add the token's JTI to the blocklist with a TTL equal "
        "to the token's remaining lifetime.\n"
        "3. Check the blocklist in your token verification middleware before "
        "trusting the token.\n"
        "4. Alternatively, keep access tokens very short-lived (≤5 min) so "
        "the revocation window is small."
    ),
    patch_template=(
        'import redis, uuid\n'
        '\n'
        'r = redis.Redis()\n'
        '\n'
        'def revoke_token(token: str) -> None:\n'
        '    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])\n'
        '    jti = payload["jti"]\n'
        '    ttl = payload["exp"] - int(datetime.now(timezone.utc).timestamp())\n'
        '    r.setex(f"revoked:{jti}", ttl, "1")\n'
        '\n'
        'def verify_token(token: str) -> dict:\n'
        '    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])\n'
        '    if r.exists(f"revoked:{payload[\'jti\']}"):\n'
        '        raise ValueError("Token has been revoked")\n'
        '    return payload'
    ),
    references=("CWE-613", "OWASP A07:2021", "SEC-007"),
))

_register(RemediationGuidance(
    rule_family="idor_check",
    why_it_matters=(
        "Insecure Direct Object Reference (IDOR) occurs when a user can access "
        "another user's resources by changing an ID in the request. Without "
        "ownership checks, any authenticated user can read or modify any record."
    ),
    how_to_fix=(
        "1. On every request that references a resource by ID, verify that the "
        "authenticated user owns or has explicit permission to access that resource.\n"
        "2. Never rely on obscurity (e.g. UUIDs instead of sequential IDs) as "
        "the sole access control.\n"
        "3. Write a test that proves user A cannot access user B's resource.\n"
        "4. Apply the check in a reusable helper to avoid forgetting it on new endpoints."
    ),
    patch_template=(
        'def get_document(doc_id: int, current_user: User, db: Session):\n'
        '    doc = db.query(Document).filter(Document.id == doc_id).first()\n'
        '    if doc is None:\n'
        '        raise HTTPException(status_code=404)\n'
        '    if doc.owner_id != current_user.id:\n'
        '        raise HTTPException(status_code=403)  # not 404 — avoids enumeration\n'
        '    return doc'
    ),
    references=("CWE-639", "OWASP A01:2021", "SEC-014"),
))

_register(RemediationGuidance(
    rule_family="client_trusted_logic",
    why_it_matters=(
        "Using client-supplied values (user role, price, discount, permissions) "
        "directly in server-side logic without re-validation lets any user "
        "escalate privileges or manipulate business logic by tampering with "
        "their request."
    ),
    how_to_fix=(
        "1. Never use client-supplied values for security decisions. Always "
        "re-fetch authoritative values from the database using the authenticated "
        "user's identity.\n"
        "2. Treat all request body fields as untrusted input — validate and "
        "sanitise before use.\n"
        "3. For roles/permissions, look them up from the user record on each "
        "request, not from a claim in the request body.\n"
        "4. For prices and quantities, always re-fetch from the product catalogue "
        "server-side — never accept them from the client."
    ),
    patch_template=(
        '# Before (dangerous): trusting client-supplied role\n'
        '# user_role = request.json["role"]\n'
        '\n'
        '# After: load from DB using authenticated identity\n'
        'user = db.query(User).filter(User.id == current_user.id).first()\n'
        'user_role = user.role  # authoritative server-side value'
    ),
    references=("CWE-602", "OWASP A04:2021", "SEC-016"),
))

# ---------------------------------------------------------------------------
# Review findings (full repo governance review)
# ---------------------------------------------------------------------------

_register(RemediationGuidance(
    rule_family="dependency_declaration",
    why_it_matters="Code imports a package not declared in pyproject.toml or requirements.txt. The project will fail to install in a clean environment.",
    how_to_fix="Add the package to [project].dependencies in pyproject.toml with a version constraint. Example: 'pandas>=2.0,<3.0'. If the import is first-party or conditional, add a comment explaining why it is not declared.",
))

_register(RemediationGuidance(
    rule_family="import_check",
    why_it_matters="The imported module cannot be imported in the current Python environment. The code will crash at startup with ImportError.",
    how_to_fix="Either add the package to pyproject.toml dependencies, install it in the current environment, or guard the import with a try/except if it is optional.",
))

_register(RemediationGuidance(
    rule_family="missing_type_hint",
    why_it_matters="Function or parameter lacks type annotations. This reduces IDE support, documentation clarity, and static analysis coverage.",
    how_to_fix="Add type annotations to the function signature. Use standard types from typing module if needed. Do not change function behavior — only add annotations.",
    patch_template="def function_name(param: ParamType) -> ReturnType:",
))

_register(RemediationGuidance(
    rule_family="deprecated_utcnow",
    why_it_matters="datetime.utcnow() was deprecated in Python 3.12 and returns a naive datetime without timezone info, leading to timezone bugs.",
    how_to_fix="Replace datetime.utcnow() with datetime.now(tz=timezone.utc). Add 'from datetime import timezone' if not already imported.",
    patch_template="datetime.now(tz=timezone.utc)",
))

_register(RemediationGuidance(
    rule_family="missing_repr",
    why_it_matters="Classes without __repr__ produce unhelpful debug output like '<MyClass object at 0x7f...>'.",
    how_to_fix="Add a __repr__ method that returns a string showing the class name and key fields.",
    patch_template='def __repr__(self) -> str:\n    return f"{type(self).__name__}(field={self.field!r})"',
))

_register(RemediationGuidance(
    rule_family="unpinned_dependency",
    why_it_matters="A dependency without a version constraint means pip will grab whatever latest version is available, leading to non-reproducible builds.",
    how_to_fix="Add a version constraint to the dependency in pyproject.toml. Use >= for minimum version and optionally < for upper bound. Example: 'fastapi>=0.100.0,<1.0'.",
))

_register(RemediationGuidance(
    rule_family="route_no_auth",
    why_it_matters="An API route has no authentication check. Any unauthenticated user can access it, which may expose sensitive data or operations.",
    how_to_fix="Add authentication to the route. For FastAPI: add Depends(get_current_user) as a parameter. For Flask: add @login_required decorator. If the route is intentionally public, add a comment explaining why.",
))

_register(RemediationGuidance(
    rule_family="package_not_importable",
    why_it_matters="The code imports from a package that is neither installed nor declared in dependencies. The scanner cannot verify API correctness and the code will crash at runtime.",
    how_to_fix="Declare the package in pyproject.toml [project].dependencies with a version constraint, then install it. If the package is environment-specific (e.g. GPU-only), add it to [project.optional-dependencies].",
))

_register(RemediationGuidance(
    rule_family="api_not_found",
    why_it_matters="The code uses a name that does not exist in the installed version of the package. This is often an API version hallucination — the AI wrote code using a function that was renamed, removed, or never existed.",
    how_to_fix="Check the installed version of the package and use the correct API. Run 'pip show <package>' to see the version, then check the changelog or docs for the correct name.",
))

_register(RemediationGuidance(
    rule_family="declared_not_installed",
    why_it_matters="The package is declared in project dependencies but not installed in the current environment. The scanner cannot verify whether imports from this package are correct.",
    how_to_fix="Install the package: pip install -e . or pip install <package>. This is an environment issue, not a code issue.",
))

_register(RemediationGuidance(
    rule_family="placeholders",
    why_it_matters="The code contains placeholder stubs (TODO, FIXME, NotImplementedError, bare ellipsis) that need to be implemented before the code is production-ready.",
    how_to_fix="Implement the placeholder. Replace '...' or 'raise NotImplementedError' with actual logic. If the placeholder is intentional (e.g. abstract method), add a comment explaining why.",
))

_register(RemediationGuidance(
    rule_family="version_pinning",
    why_it_matters="Dependencies without version constraints lead to non-reproducible builds. A new release of any dependency could break the project without warning.",
    how_to_fix="Add version constraints to all dependencies in pyproject.toml. Use >= for minimum version. Example: 'requests>=2.28.0'.",
))

_register(RemediationGuidance(
    rule_family="code_quality",
    why_it_matters="Code quality issue detected by the governance review. This may include missing type hints, deprecated API usage, or missing class methods.",
    how_to_fix="Address the specific issue described in the finding detail. Do not change function behavior — only improve code quality.",
))

_register(RemediationGuidance(
    rule_family="api_version_check",
    why_it_matters="The scanner could not verify that the imports and API calls in this file are valid. This usually means a package is not installed or an API name does not exist.",
    how_to_fix="Check that all imported packages are installed and declared in dependencies. Verify that function/class names match the installed package version.",
))

_register(RemediationGuidance(
    rule_family="auth_bypass",
    why_it_matters="An API endpoint lacks authentication, potentially exposing sensitive operations to unauthenticated users.",
    how_to_fix="Add authentication middleware or decorators to the route. For FastAPI use Depends(). For Flask use @login_required. If intentionally public, document why.",
))

_register(RemediationGuidance(
    rule_family="ruff",
    why_it_matters="Ruff linting found code style or correctness issues.",
    how_to_fix="Fix the linting issue as described. Common fixes: remove unused imports, fix formatting, add missing whitespace.",
))

_register(RemediationGuidance(
    rule_family="bandit",
    why_it_matters="Bandit security analysis found a potential security issue in the code.",
    how_to_fix="Address the security finding. Common fixes: avoid eval(), use parameterized queries, avoid hardcoded passwords, use secure random generation.",
))

_register(RemediationGuidance(
    rule_family="missing_readme",
    why_it_matters="Every project must have a README.md. Without it, users and contributors cannot understand what the project does, how to install it, or how to use it.",
    how_to_fix="Create a README.md at the project root with: what the project does, how to install it, how to use it, and required sections: Trade-offs, Limitations, and Non-goals.",
))

_register(RemediationGuidance(
    rule_family="readme_missing_section",
    why_it_matters="The README.md is missing a required section. Trade-offs, Limitations, and Non-goals sections are required by the senior engineering standards.",
    how_to_fix="Add the missing section heading to README.md. Each section should contain honest, substantive content — not just placeholder text.",
))

_register(RemediationGuidance(
    rule_family="readme",
    why_it_matters="README issues detected. A complete README is required for every project.",
    how_to_fix="Ensure README.md exists and contains: project description, installation instructions, usage instructions, Trade-offs, Limitations, and Non-goals sections.",
))

_register(RemediationGuidance(
    rule_family="dead_code",
    why_it_matters="A function is defined but never called. Dead code is confusing, signals incomplete refactoring, and is a hallmark of generated code. Senior engineers either use a helper or delete it — never both.",
    how_to_fix="If the function is needed, call it instead of duplicating its logic inline. If it is not needed, delete it. Do not leave unused functions in the codebase.",
))

_register(RemediationGuidance(
    rule_family="missing_license",
    why_it_matters="Every project must include a LICENSE file. Without one, the code has no legal terms for use, modification, or distribution.",
    how_to_fix="Create a LICENSE file at the project root. Use MIT unless the project requirements specify otherwise.",
))

_register(RemediationGuidance(
    rule_family="license",
    why_it_matters="License issues detected. A LICENSE file is required for every project.",
    how_to_fix="Ensure a LICENSE file exists at the project root with appropriate license text.",
))

_register(RemediationGuidance(
    rule_family="excessive_blast_radius",
    why_it_matters=(
        "More than 50% of the repo's Python files were changed in a single "
        "changeset. This heuristic catches full rewrites disguised as small "
        "incremental changes. Full rewrites bypass incremental review, break "
        "blame history, and signal that generated code was accepted uncritically."
    ),
    how_to_fix=(
        "1. Split the change into a sequence of smaller, independently reviewable commits.\n"
        "2. Ask: does each file actually need to change to satisfy the ticket scope?\n"
        "3. If refactoring was mixed with feature work, separate them into distinct commits.\n"
        "4. If a file was rewritten from scratch when a small edit would have worked, "
        "revert to the targeted edit.\n"
        "5. Add a comment on the PR explaining why broad changes were necessary "
        "if they genuinely cannot be avoided."
    ),
    patch_template=None,
    references=(),
))

_register(RemediationGuidance(
    rule_family="missing_package_json",
    why_it_matters="TypeScript/JavaScript project has no package.json. Cannot install dependencies, run scripts, or distribute the project.",
    how_to_fix="Create package.json with: npm init -y. Add dependencies, scripts (build, test, start), and the project entry point.",
))

_register(RemediationGuidance(
    rule_family="missing_tsconfig",
    why_it_matters="TypeScript project has no tsconfig.json. Cannot compile TypeScript to JavaScript.",
    how_to_fix="Create tsconfig.json with compiler options. At minimum: target, module, outDir, rootDir, strict.",
))

_register(RemediationGuidance(
    rule_family="missing_project_config",
    why_it_matters="Python project has no pyproject.toml or setup.py. Cannot install, distribute, or declare dependencies.",
    how_to_fix="Create pyproject.toml with [build-system], [project] name/version/dependencies, and [project.scripts] if CLI.",
))

_register(RemediationGuidance(
    rule_family="project_runnable",
    why_it_matters="Project is missing configuration files needed to actually run. Without them the code exists but cannot be executed.",
    how_to_fix="Add the appropriate project config: package.json for JS/TS, pyproject.toml for Python, Cargo.toml for Rust.",
))

_register(RemediationGuidance(
    rule_family="tests_failing",
    why_it_matters="Tests are failing, which means the code changes have broken existing behaviour. Merging or publishing with failing tests produces unreliable software.",
    how_to_fix=(
        "1. Read the test failure output carefully.\n"
        "2. Identify whether the failure is in the test itself or the production code.\n"
        "3. Fix the root cause — do not skip or delete tests to make the suite pass.\n"
        "4. Re-run the test suite locally to confirm the fix before committing."
    ),
))

_register(RemediationGuidance(
    rule_family="tests_timeout",
    why_it_matters="Tests timed out, indicating either an infinite loop, blocking I/O, or an external dependency that is unavailable. Timed-out tests give no signal.",
    how_to_fix=(
        "1. Run the test suite locally to reproduce the timeout.\n"
        "2. Check for blocking network calls, missing mocks, or runaway loops.\n"
        "3. Add timeouts to individual tests where appropriate.\n"
        "4. If the suite requires external services, ensure they are available or properly mocked."
    ),
))

_register(RemediationGuidance(
    rule_family="readme_language_mismatch",
    why_it_matters="README install or setup instructions reference the wrong language ecosystem. This causes contributors and users to attempt incorrect setup steps and fail silently.",
    how_to_fix=(
        "1. Identify the actual primary language of the project from the source files.\n"
        "2. Update the README install/setup section to match that language.\n"
        "3. Remove or replace any instructions that reference the wrong ecosystem.\n"
        "4. Verify the updated instructions work from a clean checkout."
    ),
))

# ---------------------------------------------------------------------------
# DevOps governance checks
# ---------------------------------------------------------------------------

_register(RemediationGuidance(
    rule_family="unpinned_base_image",
    why_it_matters="Mutable image tags (including :latest) can change without notice, breaking builds or introducing vulnerabilities. Only digests (@sha256:...) are immutable.",
    how_to_fix="Pin the base image to a digest: FROM image@sha256:abc123... You can find the digest with: docker inspect --format='{{index .RepoDigests 0}}' image:tag",
))
_register(RemediationGuidance(
    rule_family="dockerfile_run_as_root",
    why_it_matters="Containers running as root have full host-level privileges if they escape the container. Most applications do not need root.",
    how_to_fix="Add a USER directive after installing dependencies: RUN adduser --disabled-password appuser && USER appuser",
))
_register(RemediationGuidance(
    rule_family="dockerfile_add_over_copy",
    why_it_matters="ADD has implicit behaviours (auto-extracting tars, fetching URLs) that can introduce unexpected files. COPY is explicit and predictable.",
    how_to_fix="Replace ADD with COPY unless you specifically need tar extraction or URL fetching.",
))
_register(RemediationGuidance(
    rule_family="dockerfile_missing_dockerignore",
    why_it_matters="Without .dockerignore, the entire build context (including .git, node_modules, .env, secrets) is sent to the Docker daemon and may end up in image layers.",
    how_to_fix="Create a .dockerignore file listing: .git, node_modules, .env, *.pyc, __pycache__, .saturnday*, dist, build",
))
_register(RemediationGuidance(
    rule_family="dockerfile_secret_in_build",
    why_it_matters="ARG and ENV values persist in image layers and can be extracted by anyone with access to the image. Secrets should never be baked into images.",
    how_to_fix="Use Docker BuildKit secret mounts: RUN --mount=type=secret,id=mysecret cat /run/secrets/mysecret",
))
_register(RemediationGuidance(
    rule_family="github_action_not_sha_pinned",
    why_it_matters="Tags and branches are mutable — a compromised or updated action can change without your knowledge. Only full commit SHAs are immutable references.",
    how_to_fix="Pin to a full SHA: uses: actions/checkout@abc123def456... Find the SHA on the action's releases page.",
))
_register(RemediationGuidance(
    rule_family="github_broad_permissions",
    why_it_matters="Overly broad GITHUB_TOKEN permissions let any step in the workflow read/write to repos, packages, and deployments. A compromised step gains full access.",
    how_to_fix="Add explicit permissions at workflow or job level: permissions: { contents: read, pull-requests: write }",
))
_register(RemediationGuidance(
    rule_family="github_pull_request_target",
    why_it_matters="pull_request_target gives the workflow a read-write GITHUB_TOKEN even when triggered by a public fork. Untrusted code from forks can exploit this.",
    how_to_fix="Use pull_request instead, or ensure no untrusted code runs with the elevated token. Never checkout PR head code with pull_request_target.",
))
_register(RemediationGuidance(
    rule_family="github_self_hosted_runner_risk",
    why_it_matters="Self-hosted runners are persistent — unlike GitHub-hosted runners, they are not ephemeral. Untrusted workflow code from forks can permanently compromise them.",
    how_to_fix="Use GitHub-hosted runners for public repos. If self-hosted is required, restrict fork PRs from triggering workflows.",
))
_register(RemediationGuidance(
    rule_family="github_cloud_secret_reference",
    why_it_matters="Long-lived cloud provider secrets stored in GitHub Secrets can be leaked if a workflow is compromised. Short-lived OIDC tokens are safer.",
    how_to_fix="Configure OIDC federation with your cloud provider (AWS, GCP, Azure all support it) to use short-lived tokens instead of stored secrets.",
))
_register(RemediationGuidance(
    rule_family="cicd_secret_in_plaintext",
    why_it_matters="Plaintext secrets in workflow files are visible to anyone with repo access and appear in git history forever.",
    how_to_fix="Move secrets to GitHub Secrets (Settings > Secrets) and reference them as ${{ secrets.MY_SECRET }}.",
))
_register(RemediationGuidance(
    rule_family="cicd_no_verify",
    why_it_matters="--no-verify bypasses pre-commit hooks and governance checks, defeating the purpose of governed execution.",
    how_to_fix="Remove --no-verify flags. If a hook is failing in CI, fix the underlying issue rather than bypassing it.",
))
_register(RemediationGuidance(
    rule_family="gitlab_secret_in_yaml",
    why_it_matters="Variables defined in .gitlab-ci.yml are visible to anyone with repository access. Sensitive values belong in CI/CD settings or external secret stores.",
    how_to_fix="Move secrets to GitLab CI/CD Settings > Variables. Mark them as Protected and Masked.",
))
_register(RemediationGuidance(
    rule_family="gitlab_dind_or_privileged_pattern",
    why_it_matters="Docker-in-Docker and privileged mode give CI jobs full host access. A compromised job can escape the container and access the runner host.",
    how_to_fix="Use Kaniko or BuildKit for rootless image builds. If DinD is required, use it only on dedicated, isolated runners.",
))
_register(RemediationGuidance(
    rule_family="jenkins_hardcoded_credential",
    why_it_matters="Hardcoded credentials in Jenkinsfiles are visible in SCM history and to anyone with repo access.",
    how_to_fix="Use Jenkins credentials store and withCredentials binding: withCredentials([string(credentialsId: 'my-cred', variable: 'TOKEN')]) { ... }",
))
_register(RemediationGuidance(
    rule_family="jenkins_plaintext_password",
    why_it_matters="Plaintext passwords in pipeline scripts are visible in build logs, SCM history, and to anyone with repo access.",
    how_to_fix="Store credentials in Jenkins Credentials and bind them with withCredentials().",
))
_register(RemediationGuidance(
    rule_family="tf_hardcoded_credential",
    why_it_matters="Credentials in .tf files end up in version control and Terraform state, which may be stored unencrypted.",
    how_to_fix="Use variables with sensitive=true, or reference credentials from a secret manager (AWS Secrets Manager, HashiCorp Vault, etc.).",
))
_register(RemediationGuidance(
    rule_family="tf_public_access_cidr",
    why_it_matters="0.0.0.0/0 allows access from any IP address on the internet. This is rarely appropriate for production resources.",
    how_to_fix="Restrict CIDR blocks to specific IP ranges. Use VPN or bastion hosts for administrative access.",
))
_register(RemediationGuidance(
    rule_family="tf_sensitive_in_state",
    why_it_matters="Literal values in Terraform files can leak into .terraform metadata and plan files if not marked sensitive.",
    how_to_fix="Mark variables as sensitive = true and use a remote backend with encryption for state storage.",
))
_register(RemediationGuidance(
    rule_family="k8s_privileged_container",
    why_it_matters="Privileged containers have full access to the host kernel. They bypass all Pod Security Standards and can compromise the entire node.",
    how_to_fix="Remove privileged: true. Use specific Linux capabilities (securityContext.capabilities.add) if elevated permissions are needed.",
))
_register(RemediationGuidance(
    rule_family="k8s_unpinned_image",
    why_it_matters="The :latest tag is mutable and can change between pulls. This makes deployments non-reproducible and can introduce unexpected changes.",
    how_to_fix="Pin to a specific version tag or digest: image: nginx:1.25.3 or image: nginx@sha256:abc...",
))
_register(RemediationGuidance(
    rule_family="k8s_secret_in_manifest",
    why_it_matters="Kubernetes Secrets committed to a repo are visible in git history. Kubernetes stores Secrets unencrypted by default unless encryption at rest is configured.",
    how_to_fix="Use ExternalSecret (external-secrets operator) or SealedSecret (sealed-secrets) to manage secrets outside of git.",
))
_register(RemediationGuidance(
    rule_family="k8s_no_resource_limits",
    why_it_matters="Without resource limits, a single pod can consume all node resources, starving other workloads.",
    how_to_fix="Add resources.limits and resources.requests to container specs. Alternatively, configure a LimitRange at the namespace level.",
))
_register(RemediationGuidance(
    rule_family="k8s_no_readiness_probe",
    why_it_matters="Without a readiness probe, Kubernetes sends traffic to pods before they are ready, causing errors for users.",
    how_to_fix="Add a readinessProbe with an HTTP GET, TCP socket, or exec check that verifies the application is ready to serve.",
))
_register(RemediationGuidance(
    rule_family="config_permissive_cors",
    why_it_matters="Access-Control-Allow-Origin: * allows any website to make requests to your API, which can enable data theft via cross-site requests.",
    how_to_fix="Restrict CORS to specific trusted origins: Access-Control-Allow-Origin: https://yourdomain.com",
))
_register(RemediationGuidance(
    rule_family="config_secret_in_config",
    why_it_matters="Credentials in config files are visible in version control and to anyone with repo access.",
    how_to_fix="Use environment variables or a secret manager. Reference secrets as os.getenv('KEY') or ${KEY} instead of hardcoding them.",
))

# ---------------------------------------------------------------------------
# Usability completeness
# ---------------------------------------------------------------------------

_register(RemediationGuidance(
    rule_family="skill_md_no_usage",
    why_it_matters="An OpenClaw SKILL.md without usage instructions is useless to the agent. The agent reads SKILL.md to decide when and how to invoke the skill. Without instructions, the skill exists but can never be used.",
    how_to_fix=(
        "1. Add a '## When to Use' section explaining when the agent should invoke this skill.\n"
        "2. Add a '## Usage' section with example invocations (CLI commands or function calls).\n"
        "3. Include at least one concrete example showing input and expected output."
    ),
))
_register(RemediationGuidance(
    rule_family="skill_md_no_io",
    why_it_matters="Without input/output documentation, the agent cannot construct correct invocations or parse results. The skill may work internally but is inaccessible.",
    how_to_fix=(
        "1. Add an '## Input' section describing what the skill accepts (file paths, JSON, text).\n"
        "2. Add an '## Output' section describing what the skill returns (markdown, JSON, exit codes).\n"
        "3. Show example inputs and their corresponding outputs."
    ),
))
_register(RemediationGuidance(
    rule_family="project_no_entrypoint",
    why_it_matters="A project with no entry point cannot be run by users or agents. It exports code but has no way to invoke it from the command line or as a module.",
    how_to_fix=(
        "For Node/TypeScript: add a 'bin' field to package.json pointing to a CLI script, "
        "or add a 'start' script.\n"
        "For Python: add [project.scripts] to pyproject.toml with a console entry point, "
        "or add a __main__.py for python -m invocation."
    ),
))
