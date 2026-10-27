# RIPPLE (synthetic hackathon build)

AI teammate for merchant customer service: finds the earliest fixable cause, acts across systems under hard rules, escalates only when needed, verifies closure. **All data is synthetic.**

Plan: [RIPPLE_MVP_BUILD_PLAN.md](RIPPLE_MVP_BUILD_PLAN.md)

## Status
- Phase 0 done: repo, contracts (`backend/app/models/contracts.py`), frozen config (`config/frozen.yaml`), FastAPI + SSE, React/Vite/Tailwind shell.
- Phase 1 done: stateful mock sources with fault injection, idempotency ledger, seeded scenario templates (flagship, status query, no-action), evaluator-only ground truth.
- Phase 2 done: timeline normalizer, provenance graph (versioned), evidence-constrained reverse trace, seeded deadline-feasibility simulator, scorer with do-nothing, stability check, deterministic escalation triggers. `POST /api/session/analyze` runs it with no side effects.
- Phase 3 done: action firewall (`config/policy.yaml`), hash-bound single-use approvals (analyst only), merchant consent, content guards, idempotent executor with timeout reconciliation, rule-based verifier, audit log, 44-case adversarial suite (zero unsafe, with a mutation check proving it can fail). API: `/api/actions/propose|execute`, `/api/approvals/issue`, `/api/consents/issue`, `/api/audit`, `/api/adversarial/run`.
- Phase 4 done: intake (rules + optional cached LLM, Hindi/English/Hinglish), fact-bound replies in three languages, evidence bundle, analyst memo, orchestrator loop with multi-turn follow-ups, attack tripwire, case API (`/api/case/message|decide|consent`), chat + approval-card UI.
- Phase 5 done: 12 scenario families, baselines A-D plus full RIPPLE (E), seeded runner, metrics with Wilson CIs, independent tool-layer policy auditor, config frozen at tag `frozen-v1`, scored results in `data/results/bench_frozen-v1.json`, scoreboard card in the UI. Protocol and **disclosures**: [docs/BENCHMARK.md](docs/BENCHMARK.md).
- Phase 6 done: full dashboard. **Ops Console** (live queue, KPIs, simulate a merchant message), **Case** page (chat, source context, trace graph with the decoy dashed, ranked actions incl. do-nothing, Why? drawer, state ladder, audit log), **Approvals** (one-tap card, bundle edit changes the action hash), **Scoreboard** (metrics with CIs, per-family wins/losses, assumptions, disclosures), **Lab** (System C vs RIPPLE on the same world, chaos panel, judge-typed attacks, approval replay). Click-by-click script: [docs/DEMO.md](docs/DEMO.md).
- Security pass done: authentication on every route (OIDC validation + dev personas), RBAC from the token only, tenant isolation, PII redaction, LLM data gate/spend cap/host allowlist, HMAC-chained encrypted audit log, kill switch, rate limits, retention and deletion. Threat model, data flows and an honest control matrix with gaps: [docs/SECURITY.md](docs/SECURITY.md).
- Real sign-in and secrets: browser **OIDC authorization code + PKCE** sign-in ([docs/OIDC.md](docs/OIDC.md)) and **Vault / AWS Secrets Manager** adapters ([docs/SECRETS.md](docs/SECRETS.md)), tested against local mocks (mock IdP, mock Vault, moto) and in a real browser. Not yet tested against a production IdP, Vault or AWS account.
- Next: polish and presentation (deck, rehearsal, backup video). Optional: run the flow against a real IdP, CSP on the SPA host, live-LLM System C.

## Run
```
# backend (RIPPLE_ENV=dev turns on demo mode: persona login, demo controls; the default is secure production mode)
cd backend && python -m pip install -e ".[dev]" && RIPPLE_ENV=dev python -m uvicorn app.main:app --reload --port 8000
# frontend
cd frontend && npm install && npm run dev      # http://localhost:5173
# tests
cd backend && python -m pytest -q
```
Windows: `scripts/dev.ps1` starts both.

## Phase 1 demo by hand
Open the UI, press **Reset** (flagship: Rs 72,000 held, Rs 60,000 due Tuesday, KYC decoy), **Submit full evidence**, then **+26h**: dispute `won`, settlement `released` before Tuesday 17:00. Try `release_risk_hold`: no such tool exists (404). Inject a timeout via `POST /api/dev/fault` and reconcile by idempotency key.

## Rules the code already enforces
- No tool exists for releasing holds / changing bank details.
- Write tools require an idempotency key; replays do not double-apply.
- Evaluator ground truth is written to `data/ground_truth/`, never returned by the API, and a test forbids agent-side packages from importing it.

## LLM usage
The LLM is optional. `RIPPLE_LLM=auto|cache_only|off` (default auto), `RIPPLE_LLM_MODEL` (default claude-haiku-5-5), `ANTHROPIC_API_KEY` + `pip install anthropic` to enable live calls. Without them everything runs on deterministic rules. The LLM only extracts fields and rephrases replies; outputs are validated and it never decides, gates or executes. Responses are cached in `data/llm_cache.json`.

## Benchmark
```
cd backend && python -m app.bench.run --label frozen-v1 --scored
```
720 runs (12 families x 12 seeds x 5 systems), about 20 s, deterministic, LLM off. System C is a scripted proxy, not a live model. The benchmark is self-authored and some dev results were inspected before the freeze; read the disclosures before quoting numbers.
