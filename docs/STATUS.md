# RIPPLE: what is built and what is left

Also kept as a shared doc: https://claude.ai/artifact/17kymtPZBmwqe25b5so1BQ . This file is the copy that travels with the code and is updated as work lands.
Last updated: 2026-10-27. All data is synthetic.

## Summary
RIPPLE works end to end on synthetic data: it traces the cause of a merchant's problem, acts only through a rules gate, hands a person a one-tap approval, and closes a case only when the source system proves it.

- **Verified:** backend and browser-client tests pass; the benchmark ran 840 seeded runs at a frozen config (frozen-v2); the dashboard and sign-in were clicked through in a real browser.
- **Not verified:** nothing has touched a real identity provider, Vault, AWS account, live model or real merchant data. Every source system is a mock.

## What is built
| Layer | What exists | How it was checked |
| --- | --- | --- |
| Mock world and scenarios | Mock settlement, dispute, KYC, supplier, refund and notification systems with injectable faults and an idempotency ledger; 12 seeded scenario families with hidden ground truth | Determinism and family tests |
| Decision engine | Timeline, provenance graph, reverse trace over confirmed edges only, seeded deadline simulator, scorer including do-nothing, stability check, escalation triggers | Decoy ignored, same seed same result, unreachable deadline escalates |
| Safety layer | Four-tier action firewall, hash-bound single-use approvals (analyst only), merchant consent for exact text, idempotent executor with timeout reconciliation, rule-based verifier | 44-case attack suite (zero unsafe); mutation test proves it can fail |
| Agent | Hindi/English/Hinglish intake, fact-bound replies, evidence bundle, analyst memo, multi-turn follow-ups, attack tripwire | Orchestrator tests |
| Benchmark | Five systems, 840 runs over 14 families, intervals, independent auditor, `frozen-v2` (v1 at tag `frozen-v1`) | Reproducible; results committed |
| Dashboard | Ops Console, Case page with trace graph, Approvals, Scoreboard, Lab, Admin | Real browser; phone width |
| Security controls | Auth on every route, roles from token only, tenant isolation, PII redaction, model data gate, HMAC-chained encrypted audit log, kill switch, rate limits, retention | Security tests |
| Browser sign-in | Authorization code + PKCE, silent renewal, IdP logout; API validates RS256 tokens | Flow tests, client tests, real browser vs mock IdP |
| Secrets | Vault (AppRole, Kubernetes, Agent token file) and AWS Secrets Manager adapters | Mock Vault with TLS, moto |

## Benchmark results (frozen-v2: 840 runs, 14 families)
| System | Autonomous | Verified | Unsafe actions | False "resolved" |
| --- | --- | --- | --- | --- |
| A. Chatbot | 0% | 0% | 0 | 0 |
| B. Deadline tracker | 0% | 0% | 0 | 0 |
| C. Plain agent (scripted proxy) | 100% | 50% | 300 | 96 |
| D. RIPPLE without tracing | 100% | 50% | 0 | 0 |
| E. Full RIPPLE | 100% | 100% | 0 | 0 |

Autonomy is a tie with the plain agent; the plain agent is cheaper in the modeled cost and missed no avoidable deadline; the benchmark is self-authored. See [BENCHMARK.md](BENCHMARK.md).

## What is left
Cannot be done from this machine, needs you or outside systems:
- Run against a hosted identity provider over https, a real Vault or AWS account, and a live model (needs credentials and endpoints).
- Held-out scenarios written by someone other than the author (the benchmark is self-authored).
- Fix the intake rule gaps (INTAKE_EVAL.md): the local 3B models do not recover them.
- A backup screen recording of the demo.
- Durable storage: cases live in memory and a restart wipes them; only meaningful once real source systems exist.

## Progress since this document was first written (2026-10-27)
| Item | State |
|---|---|
| Single-origin serving: API serves the built app; strict CSP (token origin from validated discovery); HSTS in prod; traversal-safe paths; unknown `/api` is a JSON 404 | **Done and tested** (22 tests; mutation of the traversal guard is caught). SSO sign-in checked in a real browser under the CSP, no violations |
| Dockerfile, compose, demo override, `docs/DEPLOY.md` | **Built and run** (352 MB): prod mode fails closed, demo rehearsal passes against the container, runs as uid 10001. Building found a health-check timeout bug, fixed. Not tested: read-only rootfs, https, image scan |
| Real IdP | **Keycloak 26 verified in a real browser** (PKCE sign-in, role mapping, IdP logout, no-role refusal, fail-closed while IdP down), local plain http only. Compose file and realm: `docker-compose.keycloak.yml`, `scripts/keycloak/` |
| Model-cache keys scoped by tenant | **Done and tested** (mutation caught) |
| Idle-case TTL and case cap with least-recently-used eviction | **Done and tested** |
| Audit chain-head external anchor (file or https sink; detects truncation and rewrite; dead sink counted, never blocks) | **Done and tested** (mutation caught); **off by default** until `RIPPLE_AUDIT_ANCHOR` is set to storage the app cannot rewrite |
| Cognito-style `client_id` audience mode | **Done, tested against a mock of the token shape only** |
| Larger labelled intake set and honest report | **Done**: 120 messages, intent 80%, 5 of 14 forbidden requests missed (no unsafe action resulted). Rules deliberately not tuned (benchmark is frozen). [INTAKE_EVAL.md](INTAKE_EVAL.md) |
| Demo rehearsal script (`scripts/demo_check.py`) | **Done**: five rounds, every beat asserted, about 2 s a round in-process. Does not check how pages look |
| Pitch deck | **Done**: 8 slides with speaker notes in a private shared deck (link in the session); numbers come from the frozen benchmark and the intake evaluation |

| Free model fallback (Ollama) | **Done and tested** (59 tests incl. mutation-checked network guards). **Runs on the GPU server `saveenas@192.168.30.206` over an SSH tunnel, never on the laptop** (opt-in by `RIPPLE_OLLAMA_URL`; no default), using the Qwen3.5-9B already running there through its OpenAI-style API (`RIPPLE_LOCAL_API=openai`); verified end to end, about 0.6 s per call. **Measured on three models: the two 3B models help nothing; Qwen3.5-9B helped 2, hurt 0 of the 24 messages the rules are unsure about** (small sample): [LOCAL_LLM.md](LOCAL_LLM.md) |
| Admin "Language model" card and `/api/admin/llm` | **Done** |
| Security-event log survives restarts (set aside, not deleted, if unreadable) | **Done and tested** |

### Still left
- Sign-in against a hosted identity provider over https (Keycloak over http is done), silent renewal and key rotation against a real IdP, secrets against a real Vault or AWS account, and the agent with a live model.
- Image scan, read-only root filesystem, https termination in front of the container.
- Held-out scenarios written by someone other than the author; a live-model comparison for System C.
- `frozen-v2`: fix the intake gaps listed in INTAKE_EVAL.md (Devanagari word boundaries, ETA rule order, a few patterns, stricter supplier flag), re-run the benchmark, evaluate on new messages.
- Persistence for cases (they live in memory).
- A backup screen recording of the demo (only you can make it) and one full click-through before presenting.


## Coverage of the v3 build specification (2026-10-27)
Source: "RIPPLE v3 - Winning Build Specification". Status as built, not as hoped.

| v3 item | State |
|---|---|
| Problem-statement fit: context, decisions, actions across systems, end-to-end resolution, escalation only when needed, measurable outcomes, works with humans | **Built** (chat + context panel, ranked actions with do-nothing, mock settlement/dispute/KYC/supplier/notification tools, verified closure, tiers, scoreboard, one-tap card) |
| 14 scenario families (section 6) | **Built: all 14** (rows 6 and 14 added in frozen-v2). The spec's "target 40 templates" is **not** met: 14 parameterised families, 12 seeds each |
| Five systems, expected failure modes of System C shown live | **Built** (Lab split view) |
| Metrics 8.1 to 8.3 incl. escalation precision, recall, false escalation, time p50/p90, cost, repeat contacts, human minutes, causal trace precision, stability, do-nothing correctness, promise extraction accuracy | **Built**, with sample counts. Parameters were frozen before the scored run, after dev iterations (disclosed in BENCHMARK.md) |
| Escalation recall at a pre-declared target | **Not met as written**: no numeric target was declared before the run; recall is reported (96/96) |
| P0 gates (Tier 4 denied, no false closure, reproducible, causal path) | **Met** (44-case suite 0 unsafe; 0 false resolved; same seed same decision; no unsupported edge as sole basis) |
| P2: promise ledger, robustness sampling, promise repair, judge-attack box | **Built**: ledger persisted in SQLite and shown on the case page (small labelled set: 20 template + 120 natural messages); robustness is the stability check; repair is the consented supplier notice (it carries no new date); Lab attack box |
| P2: "sensitivity numbers" | **Built** (`app.bench.sensitivity`, Scoreboard card); see the caveat there |
| P3: Hindi voice | **Built in the browser** (read-aloud and microphone via the browser's speech APIs; not tested with a real microphone here, and recognition quality depends on the browser) |
| P3: portfolio view | **Built, illustrative**: 200 synthetic merchants with a planted cause distribution through the real engine |
| Architecture: SQLite for records/audit, Streamlit UI | **SQLite**: decision records, memos, outcomes, ledger, follow-ups (the tamper-evident audit stays an encrypted HMAC-chained file). **UI is React**, not Streamlit, and has more than the three screens |
| Tier 1 "schedule internal recheck" | **Built** (`schedule_follow_up`, through the firewall, idempotent) |
| Deferred by the spec: bridge financing, udhaar recovery, learned rules, Soundbox hardware | **Soundbox hardware not built.** The others were built afterwards from the Full Feature Specification (next section) |
| Demo script (3 minutes) and `scripts/demo_check.py` | **Built** |

## Coverage of the "Full Feature Specification" (2026-10-27)
Everything below is optional (`RIPPLE_EXTENSIONS`, on in the app, never on in the benchmark), so frozen-v2 results are unchanged (re-run: same summary and config hash). The policy hash differs from the one recorded in frozen-v2 because new, unused actions were added.

| Feature | State |
|---|---|
| Learned rules | **Built.** Proposed from analyst edits or from evidence (10+ human decisions, at most 10% overrides). Active only after a named analyst confirms. A rule can only pre-approve `submit_dispute_evidence` up to Rs 100,000 for the documents it names; audited with the rule id |
| Bridge offers | **Built, simulated lender.** Tier 2, explicit tap only (a chat "yes" never accepts a financial offer). Checks EMI burden (35% of inflow) and value at least 1.5x the fee; only when the rescue still leaves a miss risk of 0.5 or more. Parameters are assumptions |
| Udhaar recovery | **Built, simulated.** Tier 2 reminders in three languages with fixed polite templates and a shame-word guard. Hardship, dispute or a fragile relationship means no message and a human decides |
| Re-KYC link, scheduled follow-up | **Built** (Tier 1; no URL appears in message text) |
| Dry run | **Built.** Shows the plan with no tool call; audited as `dry_run` |
| Promise repair date | **Built.** Date only when supported (p90 release plus 6 hours), else "within about 3 days" |
| Voice delivery policy | **Built, simulated Soundbox.** Quiet hours 22:00 to 08:00, 3 per day, offline falls back to the app. Not tested with real speech or hardware |
| Cascade view | **Built.** Edge lag and propagation are assumed values in config |
| Merchant "why" | **Built** in three languages, from the decision record only |
| Escalation over weeks, override rate | **Built** (scripted analyst). Escalation falls with rules (about 0.6 to 0.24) against 0.56 to 0.76 without; unsafe actions 0. The override rate is noisy and **not clearly falling** in later weeks |
| Hand-written held-out set, targets declared first | **Built**: 6 scenarios, targets committed in 60da3fa before the run, all 7 met. Same author as the engine, n=6, so weak evidence |
| Effectiveness statistics | **Built** (shown on the case page; **not** fed back into the engine) |
| 40 templates | **Not met** (14 families) |
| Escalation-recall target for the main benchmark | **Not met** (declared only for the hand-written set) |
| Case-file export | **Not built** |
