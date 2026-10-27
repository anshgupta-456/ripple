# RIPPLE (synthetic hackathon build)

AI teammate for merchant customer service: finds the earliest fixable cause, acts across systems under hard rules, escalates only when needed, verifies closure. **All data is synthetic.**

Plan: [RIPPLE_MVP_BUILD_PLAN.md](RIPPLE_MVP_BUILD_PLAN.md)

## Status
- Phase 0 done: repo, contracts (`backend/app/models/contracts.py`), frozen config (`config/frozen.yaml`), FastAPI + SSE, React/Vite/Tailwind shell.
- Phase 1 done: stateful mock sources with fault injection, idempotency ledger, seeded scenario templates (flagship, status query, no-action), evaluator-only ground truth.
- Phase 2 done: timeline normalizer, provenance graph (versioned), evidence-constrained reverse trace, seeded deadline-feasibility simulator, scorer with do-nothing, stability check, deterministic escalation triggers. `POST /api/session/analyze` runs it with no side effects.
- Phase 3 done: action firewall (`config/policy.yaml`), hash-bound single-use approvals (analyst only), merchant consent, content guards, idempotent executor with timeout reconciliation, rule-based verifier, audit log, 44-case adversarial suite (zero unsafe, with a mutation check proving it can fail). API: `/api/actions/propose|execute`, `/api/approvals/issue`, `/api/consents/issue`, `/api/audit`, `/api/adversarial/run`.
- Phase 4 done: intake (rules + optional cached LLM, Hindi/English/Hinglish), fact-bound replies in three languages, evidence bundle, analyst memo, orchestrator loop with multi-turn follow-ups, attack tripwire, case API (`/api/case/message|decide|consent`), chat + approval-card UI.
- Phase 5 done: 12 scenario families (14 since frozen-v2), baselines A-D plus full RIPPLE (E), seeded runner, metrics with Wilson CIs, independent tool-layer policy auditor, config frozen at tag `frozen-v1`, scored results in `data/results/bench_frozen-v1.json`, scoreboard card in the UI. Protocol and **disclosures**: [docs/BENCHMARK.md](docs/BENCHMARK.md).
- Phase 6 done: full dashboard. **Ops Console** (live queue, KPIs, simulate a merchant message), **Case** page (chat, source context, trace graph with the decoy dashed, ranked actions incl. do-nothing, Why? drawer, state ladder, audit log), **Approvals** (one-tap card, bundle edit changes the action hash), **Scoreboard** (metrics with CIs, per-family wins/losses, assumptions, disclosures), **Lab** (System C vs RIPPLE on the same world, chaos panel, judge-typed attacks, approval replay). Click-by-click script: [docs/DEMO.md](docs/DEMO.md).
- Security pass done: authentication on every route (OIDC validation + dev personas), RBAC from the token only, tenant isolation, PII redaction, LLM data gate/spend cap/host allowlist, HMAC-chained encrypted audit log, kill switch, rate limits, retention and deletion. Threat model, data flows and an honest control matrix with gaps: [docs/SECURITY.md](docs/SECURITY.md).
- Real sign-in and secrets: browser **OIDC authorization code + PKCE** sign-in ([docs/OIDC.md](docs/OIDC.md)) and **Vault / AWS Secrets Manager** adapters ([docs/SECRETS.md](docs/SECRETS.md)), tested against local mocks (mock IdP, mock Vault, moto) and in a real browser. Not yet tested against a production IdP, Vault or AWS account.
- Production shape: the API serves the built web app on one origin with a strict **Content-Security-Policy** (IdP token origin taken from validated discovery), HSTS in production, traversal-safe static paths; `Dockerfile` + compose built and run, including a real Keycloak sign-in (`docker-compose.keycloak.yml`). [docs/DEPLOY.md](docs/DEPLOY.md).
- Hardening: model-cache keys scoped by tenant, idle/over-cap cases evicted, audit chain-head **external anchor** (file or https sink; truncation and rewrite detected), Cognito-style `client_id` audience mode (mock-tested only).
- Measured honestly: intake rules on 120 hand-labelled Hindi/English/Hinglish messages (intent 80%, 5 of 14 forbidden requests missed, none caused an unsafe action): [docs/INTAKE_EVAL.md](docs/INTAKE_EVAL.md). `python scripts/demo_check.py` rehearses every demo beat through the API.
- What is built and what is left: [docs/STATUS.md](docs/STATUS.md). Still needs outside systems or other people: hosted IdP over https, Vault/AWS/live-model runs, held-out scenarios written by someone else, a backup demo video, an image scan.

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

One origin, production-shaped (see [docs/DEPLOY.md](docs/DEPLOY.md)): `cd frontend && npm ci && npm run build`, then `cd backend && python -m uvicorn app.main:app --port 8000` serves the app and the API together. Demo rehearsal: `python scripts/demo_check.py`.

## Phase 1 demo by hand
Open the UI, press **Reset** (flagship: Rs 72,000 held, Rs 60,000 due Tuesday, KYC decoy), **Submit full evidence**, then **+26h**: dispute `won`, settlement `released` before Tuesday 17:00. Try `release_risk_hold`: no such tool exists (404). Inject a timeout via `POST /api/dev/fault` and reconcile by idempotency key.

## Rules the code already enforces
- No tool exists for releasing holds / changing bank details.
- Write tools require an idempotency key; replays do not double-apply.
- Evaluator ground truth is written to `data/ground_truth/`, never returned by the API, and a test forbids agent-side packages from importing it.

## LLM usage
A model is optional: with none, everything runs on deterministic rules. Claude (`ANTHROPIC_API_KEY`, costs money) and **Ollama on a model server you control (free; opt-in by `RIPPLE_OLLAMA_URL`, never loaded on the app's own machine by default)** are both supported. By default Claude is used if a key exists, and the local model takes over whenever Claude is missing, failing or over its cap; `RIPPLE_LLM_PROVIDER=ollama` uses only the local model. The local model is boxed in (asked only when the rules are unsure, can only suggest read-only intents) and, measured honestly with a 3B model on this laptop, is safe; two 3B models added nothing and the server's Qwen3.5-9B added a little (2 of 24 unsure messages): [docs/LOCAL_LLM.md](docs/LOCAL_LLM.md). Outputs are validated and a model never decides, gates or executes. Responses are cached in `data/llm_cache.json`, keyed by tenant, provider and model.

## Benchmark
```
cd backend && python -m app.bench.run --label frozen-v2 --scored
```
840 runs (14 families x 12 seeds x 5 systems), about 20 s, deterministic, LLM off. Frozen as `frozen-v2` (the 720-run v1 stays at tag `frozen-v1`). System C is a scripted proxy, not a live model. The benchmark is self-authored and some dev results were inspected before the freeze; read the disclosures before quoting numbers.
