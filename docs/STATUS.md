# RIPPLE: what is built and what is left

Also kept as a shared doc: https://claude.ai/artifact/17kymtPZBmwqe25b5so1BQ . This file is the copy that travels with the code and is updated as work lands.
Last updated: 2026-10-27. All data is synthetic.

## Summary
RIPPLE works end to end on synthetic data: it traces the cause of a merchant's problem, acts only through a rules gate, hands a person a one-tap approval, and closes a case only when the source system proves it.

- **Verified:** backend and browser-client tests pass; the benchmark ran 720 seeded runs at a frozen config; the dashboard and sign-in were clicked through in a real browser.
- **Not verified:** nothing has touched a real identity provider, Vault, AWS account, live model or real merchant data. Every source system is a mock.

## What is built
| Layer | What exists | How it was checked |
| --- | --- | --- |
| Mock world and scenarios | Mock settlement, dispute, KYC, supplier, refund and notification systems with injectable faults and an idempotency ledger; 12 seeded scenario families with hidden ground truth | Determinism and family tests |
| Decision engine | Timeline, provenance graph, reverse trace over confirmed edges only, seeded deadline simulator, scorer including do-nothing, stability check, escalation triggers | Decoy ignored, same seed same result, unreachable deadline escalates |
| Safety layer | Four-tier action firewall, hash-bound single-use approvals (analyst only), merchant consent for exact text, idempotent executor with timeout reconciliation, rule-based verifier | 44-case attack suite (zero unsafe); mutation test proves it can fail |
| Agent | Hindi/English/Hinglish intake, fact-bound replies, evidence bundle, analyst memo, multi-turn follow-ups, attack tripwire | Orchestrator tests |
| Benchmark | Five systems, 720 runs, intervals, independent auditor, frozen at tag `frozen-v1` | Reproducible; results committed |
| Dashboard | Ops Console, Case page with trace graph, Approvals, Scoreboard, Lab, Admin | Real browser; phone width |
| Security controls | Auth on every route, roles from token only, tenant isolation, PII redaction, model data gate, HMAC-chained encrypted audit log, kill switch, rate limits, retention | Security tests |
| Browser sign-in | Authorization code + PKCE, silent renewal, IdP logout; API validates RS256 tokens | Flow tests, client tests, real browser vs mock IdP |
| Secrets | Vault (AppRole, Kubernetes, Agent token file) and AWS Secrets Manager adapters | Mock Vault with TLS, moto |

## Benchmark results (frozen-v1)
| System | Autonomous | Verified | Unsafe actions | False "resolved" |
| --- | --- | --- | --- | --- |
| A. Chatbot | 0% | 0% | 0 | 0 |
| B. Deadline tracker | 0% | 0% | 0 | 0 |
| C. Plain agent (scripted proxy) | 100% | 63% | 240 | 72 |
| D. RIPPLE without tracing | 100% | 63% | 0 | 0 |
| E. Full RIPPLE | 100% | 100% | 0 | 0 |

Autonomy is a tie with the plain agent; the plain agent is cheaper in the modeled cost and missed no avoidable deadline; the benchmark is self-authored. See [BENCHMARK.md](BENCHMARK.md).

## What is left
Cannot be done from this machine, needs you or outside systems:
- Run against a real identity provider, a real Vault or AWS account, and a live model (needs credentials and endpoints).
- Held-out scenarios written by someone other than the author (the benchmark is self-authored).
- A backup screen recording of the demo.
- Docker image build and compose check (the Docker daemon was not running when this was written).
- Durable storage: cases live in memory and a restart wipes them; only meaningful once real source systems exist.

## Progress since this document was first written (2026-10-27)
| Item | State |
|---|---|
| Single-origin serving: API serves the built app; strict CSP (token origin from validated discovery); HSTS in prod; traversal-safe paths; unknown `/api` is a JSON 404 | **Done and tested** (22 tests; mutation of the traversal guard is caught). SSO sign-in checked in a real browser under the CSP, no violations |
| Dockerfile, compose, demo override, `docs/DEPLOY.md` | **Written, never built**: Docker daemon was not running. Treat as untested |
| Model-cache keys scoped by tenant | **Done and tested** (mutation caught) |
| Idle-case TTL and case cap with least-recently-used eviction | **Done and tested** |
| Audit chain-head external anchor (file or https sink; detects truncation and rewrite; dead sink counted, never blocks) | **Done and tested** (mutation caught); **off by default** until `RIPPLE_AUDIT_ANCHOR` is set to storage the app cannot rewrite |
| Cognito-style `client_id` audience mode | **Done, tested against a mock of the token shape only** |
| Larger labelled intake set and honest report | **Done**: 120 messages, intent 80%, 5 of 14 forbidden requests missed (no unsafe action resulted). Rules deliberately not tuned (benchmark is frozen). [INTAKE_EVAL.md](INTAKE_EVAL.md) |
| Demo rehearsal script (`scripts/demo_check.py`) | **Done**: five rounds, every beat asserted, about 2 s a round in-process. Does not check how pages look |
| Pitch deck | **Done**: 8 slides with speaker notes in a private shared deck (link in the session); numbers come from the frozen benchmark and the intake evaluation |

### Still left
- Build the container image and run compose once (needs Docker running).
- Run sign-in against a real identity provider (Keycloak in Docker is the cheapest), secrets against a real Vault or AWS account, and the agent with a live model.
- Held-out scenarios written by someone other than the author; a live-model comparison for System C.
- `frozen-v2`: fix the intake gaps listed in INTAKE_EVAL.md (Devanagari word boundaries, ETA rule order, a few patterns, stricter supplier flag), re-run the benchmark, evaluate on new messages.
- Security-event log file is re-created empty at each start (the anchor is the only surviving evidence of an earlier run); persistence for cases and the security log.
- A backup screen recording of the demo (only you can make it) and one full click-through before presenting.
