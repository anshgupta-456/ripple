# RIPPLE security: threat model, data flows, control matrix

Scope: this is a hackathon prototype on **synthetic data**. The controls below are real code with tests, but several
production integrations are not present (called out as gaps). Do not point it at real merchant data until the
"Compliance review before real data" list is cleared.

## 1. Secure defaults and the tradeoffs they cost

| Secure default | Convenience cost | How to get the demo anyway |
|---|---|---|
| `RIPPLE_ENV` defaults to **prod**: OIDC tokens only, no persona login, no demo controls, no `/docs`, fails closed if OIDC is not configured | the app will not run "out of the box" | `RIPPLE_ENV=dev` (set by `make backend` and `scripts/dev.ps1`) |
| Access token held **in memory only** in the browser | a page reload means signing in again | demo mode remembers only the persona *name* and re-mints a 15-minute token |
| **Admins cannot approve** risk actions (separation of duties) | the demo needs a persona switch (Sam to Priya) | header persona selector |
| Raw `propose/execute/issue-approval` endpoints were **removed** | no manual API poking of the action pipeline | use the case flow (approvals only via the analyst endpoint) |
| Audit files are **encrypted**; the dev key is ephemeral | dev audit files are unreadable after a restart | set `RIPPLE_AUDIT_KEY` to keep them |
| Messages containing bank/card/PAN/Aadhaar/UPI never go to the model | those messages get the rules-based path only | none intended |
| Rate limits (lab 10/min, messages 30/min) and an LLM call cap | a fast demo can hit 429 | limits are in `app/main.py` |

## 2. Assets, actors, trust boundaries
- **Assets:** merchant conversations (may hold PII), settlement/dispute/supplier data (financial, confidential), approval authority (the ability to authorise a risk action), audit trail, model API key.
- **Actors:** merchant, risk analyst, ops admin, read-only viewer, the agent (untrusted-by-construction: it can only propose; code gates it), the LLM provider (external), attackers (unauthenticated callers, malicious merchants, a compromised or prompt-injected agent, a malicious insider).
- **Boundaries:** browser to API (TLS terminated at the deployment's proxy); API to mock source systems (in-process here; real systems in production); API to the LLM provider (the only outbound call, allowlisted to `api.anthropic.com`).

## 3. Data flows
```
Merchant text --(HTTPS)--> API --authn/authz/tenant--> Orchestrator
   Orchestrator --> Intake rules (always)
   Orchestrator --> [mask PII; BLOCK if regulated] --> LLM provider (optional) --> validated JSON
   Orchestrator --> Engine (deterministic) --> Firewall --> Executor --> Source systems
   Every step --> Audit log (PII redacted, HMAC-chained, encrypted file)
Analyst --(token, analyst role)--> approve/reject/edit --> hash-bound single-use approval
Admin --(token, admin role)--> kill switch / retention / security log
```
| Data | Class | Where it goes | Handling |
|---|---|---|---|
| Merchant message | confidential (regulated if it holds bank/ID numbers) | memory (case), model (masked), audit (redacted) | masked before any prompt/log; regulated never sent to the model; viewers see masked text |
| Bank/card/PAN/Aadhaar/UPI identifiers | regulated | nowhere beyond the request | blocked from LLM, redacted in audit/memo, outbound-text guard blocks them in replies |
| Settlement, dispute, supplier amounts | confidential/financial | memory, UI | tenant-scoped; not sent to the model beyond what a reply needs |
| Audit trail | confidential | encrypted file + memory | HMAC chain, redacted, retention purge, deletion endpoint |
| Tokens | credential | browser memory only | short-lived, never logged, never in prompts |
| Model API key | secret | SDK client only | from secret store, log-scrubbed, never in prompts |

## 4. Threats and mitigations
| Threat | Mitigation | Test |
|---|---|---|
| Unauthenticated access | every `/api` route except health and public config requires a verified bearer token | `test_every_protected_route_rejects_anonymous_callers` |
| Token forgery, expiry, alg confusion, long-lived tokens | signature+issuer+audience+expiry verified; explicit algorithm lists; `none` and HS256-in-prod refused; lifetime over 1 h refused | `test_security_core.py` |
| **Role escalation via request body** (the original hole) | roles come only from the token; body `role`/`approver` fields no longer exist | `test_roles_claimed_in_the_request_body_are_ignored` |
| Approving your own risk action | approvals are analyst-only; admin cannot; agent has no method that creates approvals; hash-bound, single-use, expiring | `test_role_matrix_on_the_api`, firewall and adversarial suites |
| Cross-tenant data access | sessions carry a tenant; other tenants get 404; lists and KPIs are filtered; seeding never touches another tenant | `test_tenants_cannot_see_or_touch_each_others_cases` |
| Prompt injection / fake authority | merchant text is data; attacks are flagged, never routed to the model; forbidden tools do not exist; firewall tripwire is audited | adversarial suite (44 cases, mutation-checked), `test_lab.py` |
| Tool-output injection | tool outputs are structured source fields, never executed as instructions | design + verifier tests |
| PII leakage via prompts or logs | masking and classification before prompts, audit, memo; regulated text blocked from the model | `test_data_protection.py`, `test_security_core.py` |
| Exfiltration to a rogue model endpoint | Claude: outbound host allowlist checked against the client's real endpoint. Local model: loopback only unless a host is named explicitly, no redirects, 1 MB reply cap | `test_outbound_host_allowlist`, `test_llm_local.py` (guards mutation-checked) |
| Runaway model spend | per-client and per-process live-call caps; over cap hands over to the free local model, else rules. A failing provider is paused for 60 s | `test_llm_spend_cap_degrades_to_rules`, `test_claude_spend_cap_hands_over_to_the_local_model` |
| A weak local model degrading decisions | asked only when rules are unsure; suggests read-only intents only, never consent or supplier messages; needs payments vocabulary; confidence capped at 0.75; same firewall after. See [LOCAL_LLM.md](LOCAL_LLM.md) | `test_llm_local.py` |
| Audit tampering | HMAC-SHA256 chain; encrypted lines; verify endpoint | `test_audit_chain_detects_edit_delete_and_reorder` |
| Misbehaving agent / emergency | global and per-user kill switch, persisted, enforced in API, orchestrator and firewall | `test_global_kill_switch_*`, `test_per_user_kill_switch_*` |
| Abuse / DoS | per-user sliding-window rate limits; message size limit; demo controls absent in prod | `test_rate_limits_return_429`, `test_oversized_messages_are_refused` |
| Info leakage | generic 500s, no stack traces, security headers, `no-store`, docs disabled in prod | `test_security_headers_and_no_cache` |
| Insider admin abuse | admin actions (kill switch, deletion, impersonation, purge) go to the HMAC-chained security log | `test_kill_switch_changes_are_in_the_security_log` |

## 5. Control matrix (your policy, point by point)
Status: **Enforced** = code plus tests. **Partial** = real but limited. **Gap** = not done here.

### Identity and access
| Requirement | Status | How / what is missing |
|---|---|---|
| SSO/OIDC, no custom password store | **Partial** | Browser sign-in is **authorization code + PKCE (S256)** with state, nonce, single-use pending record, code stripped from the URL, in-memory token, silent renewal with a loop guard and IdP logout (`frontend/src/oidc.ts`, [docs/OIDC.md](OIDC.md)). The API validates RS256/ES256 tokens over real HTTP discovery and JWKS (https only, issuer match, host allowlist, key-rotation cooldown), maps roles/groups/tenant claims, and audits each OIDC login. Tested end to end against a strict **mock** IdP (36 backend tests, 28 browser-client tests, real browser run). Also signed in through a real Keycloak 26 (local, plain http, [DEPLOY.md](DEPLOY.md)). **Not yet tested against a production IdP over https or a hosted provider**. Amazon Cognito-style access tokens (`client_id` instead of `aud`) are supported behind `RIPPLE_OIDC_AUDIENCE_CLAIM=client_id` (exact client-id match, `token_use=access`, ID tokens refused), tested against a mock of that token shape only. SAML is expected to be bridged by the IdP. There is no password store; dev personas are tokens, not passwords. |
| Agent acts with the user's permissions, not a shared super-account | **Partial** | Every request is authorised as the calling user's roles and the actor is in every audit entry. Risk actions need that user's (analyst) approval. **Gap:** against real downstream systems the executor would use a service identity; production needs OAuth2 token exchange (RFC 8693, on-behalf-of) so downstream calls carry the user's scope. |
| RBAC, least privilege | **Enforced** | `PERMS` table in `app/security/auth.py`; viewer < analyst/admin; merchant limited to its own case; separation of duties. |
| Short-lived tokens, no long-lived keys in the agent's context | **Enforced** | tokens over 1 h are refused; dev tokens last 15 min; browser keeps the token in memory only; the agent never holds a key. |

### Credentials and secrets
| Requirement | Status | How / what is missing |
|---|---|---|
| No hardcoded secrets; secrets manager; never in logs/prompts | **Partial** | `app/security/secret_store.py` plus `backends.py`: **HashiCorp Vault KV v2** (AppRole, Kubernetes auth, Agent token file; short-lived tokens renewed before expiry; no redirects followed; verified TLS) and **AWS Secrets Manager** (credentials only from the provider chain). A configured manager is authoritative, values refresh on a TTL (rotation without restart), a broken backend stops startup, and a log-record factory scrubs every secret and backend credential from every logger. A test scans the code for secret-like literals. Tested against a mock Vault (real HTTP/TLS) and moto (real boto3); **not yet against a real Vault or AWS account**; KMS-wrapped envelope encryption is not implemented. See [docs/SECRETS.md](SECRETS.md). |
| Third-party OAuth tokens encrypted per user, refreshed server-side | **N/A** | the app stores no third-party OAuth tokens. |
| Encrypt in transit | **Partial (deployment)** | TLS is terminated at your proxy/ingress; the app does not terminate TLS. In production mode it sends `Strict-Transport-Security` (1 year, includeSubDomains), which browsers ignore over plain http. See [DEPLOY.md](DEPLOY.md). |
| Encrypt at rest | **Partial** | audit files are Fernet-encrypted. Case state is in memory only (no database). The LLM cache file holds model outputs (inputs are hashed from masked text) in plaintext: low sensitivity, but not encrypted. |

### Data privacy and handling
| Requirement | Status | How / what is missing |
|---|---|---|
| Classify data and enforce handling per tier | **Partial** | `DataClass` (public/internal/confidential/regulated) drives masking and the model gate for text. No organisation-wide data catalog. |
| Redact PII before prompts and logs | **Enforced** | `app/security/redact.py` (phone, email, UPI, IFSC, PAN, Aadhaar, card, account) applied to audit data, memo, viewer responses and every model prompt. Pattern-based: misses names and free-form addresses. |
| Minimize context | **Enforced** | the model receives only the merchant message (masked) or a reply to rephrase; never source records. |
| Explicit retention and a deletion path | **Partial** | `RIPPLE_RETENTION_DAYS` (default 30) purges audit files; admin deletion endpoint removes a case and its file. In-memory cases are evicted after `RIPPLE_SESSION_TTL` idle seconds (default 4 h) and beyond `RIPPLE_MAX_SESSIONS` (default 500, least recently used first), taking their audit files with them. A restart still clears everything, and there is no database. `test_hardening.py`. |
| "Don't train on my data" | **Gap (contractual)** | cannot be enforced in code. Confirm retention and training terms in your agreement with the model provider. The code only minimises what is sent. |
| Tenant/user isolation | **Partial** | enforced at the API and tested. The model-output cache key now includes the tenant, so identical masked prompts from different tenants never share a cached output (`test_cached_model_output_is_never_served_across_tenants`). The cache key also includes the provider and model. **Still true:** one process, one cache file on disk, in-memory cases: a per-tenant store and process isolation are needed before genuinely multi-tenant use. |

### Agent actions and guardrails
| Requirement | Status | How / what is missing |
|---|---|---|
| Separate read from write; human confirmation for high impact | **Enforced** | tiers 1-4; evidence submission needs a hash-bound analyst approval; supplier notice needs exact-text merchant consent; no tool exists for releasing holds or changing bank details. |
| Tool outputs and retrieved docs are untrusted | **Enforced** | structured fields only; merchant text is flagged and never routed to the model if it looks like an attack. |
| Validate/sanitise every tool input and output; allowlist tools and domains | **Enforced** | policy schema validation, scope checks, outbound-text guard, tool allowlist, LLM host allowlist. |
| Rate-limit actions; spend/usage caps | **Enforced** | per-case action limits, per-user API limits, LLM call caps. The system has no tool that spends money. Caps are per process, not distributed. |

### Audit and observability
| Requirement | Status | How / what is missing |
|---|---|---|
| Log every action (who, what, when, tool, result), tamper-resistant, redacted | **Partial** | every firewall decision, approval, tool call, verification and engine decision is logged with the actor; entries are redacted, encrypted and HMAC-chained. This is tamper-**evident**: someone holding the key and file access can rebuild the chain. **External anchor (built, off by default):** `RIPPLE_AUDIT_ANCHOR=file:<path>` or an https collector receives the chain head (log name, entry number, keyed MAC) after every Nth entry (`RIPPLE_AUDIT_ANCHOR_EVERY`). A log whose end was cut off, or that was replaced with a freshly computed chain, then fails `GET /api/audit/verify` (`anchor.check`); a dead sink never blocks a case but is counted and shown (availability over completeness). It only resists tampering if the sink is somewhere the app user cannot truncate (separate volume, append-only, object lock, SIEM). Each process run uses its own chain name. The security-event log now **survives a restart** (it resumes the same chain); a log that cannot be read with the current key, or whose chain does not verify, is renamed aside (`.unreadable-<time>`), never deleted, and the new chain starts with a `security.log_rotated` entry. In dev the audit key is ephemeral, so each dev restart sets the old file aside. `test_hardening.py`. |
| Decisions traceable | **Enforced** | each engine decision logs seed, graph version and digest, trace root, rejected edges, chosen action, triggers; the UI shows the same in "Why?". |

### Governance
| Requirement | Status | How / what is missing |
|---|---|---|
| Kill switch, per user and global | **Enforced** | admin-only; persisted; enforced in API, orchestrator and firewall; audited. |
| Threat model and data flows documented | **Enforced** | this file. |
| Flag regulated data for compliance review | **Done below** | see section 6. |

## 6. Compliance review before real data
This build touches data that would be regulated in a real deployment. Get a compliance review before it sees any:
- **Personal data of merchants and their customers** (names, phones, emails, any bank or government identifiers): India's DPDP Act 2023 (consent, purpose limitation, retention, breach duty); GDPR if EU subjects are involved.
- **Payment and financial data** (settlements, disputes, bank-detail text): RBI rules on payment-system data storage and localisation; PCI DSS if card numbers can ever reach the system (the model gate and redaction reduce, but do not remove, the chance that they are typed into chat and held in memory).
- **Third-party processing**: sending even masked merchant text to a model provider is a processor relationship; needs a DPA, region and retention terms.
- **SOC 2 scope**: access control (CC6), change and monitoring (CC7), audit-log retention, and the kill switch as an incident control.
- Not covered by this prototype: penetration test, dependency/SBOM scanning, backup and DR, incident-response runbook.
- Browser app hardening: the API now serves the SPA itself with a strict **Content-Security-Policy** (scripts and styles from this origin only; `connect-src` adds only the IdP token origin from validated discovery; see [DEPLOY.md](DEPLOY.md)), checked in a real browser including the SSO round trip. The container image now builds and runs (production, demo and Keycloak modes); still open: image scanning, a read-only filesystem, and a real-IdP run over https.

## 7. Run it
```
make backend     # RIPPLE_ENV=dev: persona login, demo controls, ephemeral keys
# production-like (OIDC, fails closed):
RIPPLE_OIDC_ISSUER=https://idp.example.com RIPPLE_OIDC_AUDIENCE=ripple-api RIPPLE_AUDIT_KEY_FILE=/run/secrets/audit python -m uvicorn app.main:app
cd backend && python -m pytest -q      # the security-focused files alone hold 88 tests
```
