# RIPPLE (synthetic hackathon build)

AI teammate for merchant customer service: finds the earliest fixable cause, acts across systems under hard rules, escalates only when needed, verifies closure. **All data is synthetic.**

Plan: [RIPPLE_MVP_BUILD_PLAN.md](RIPPLE_MVP_BUILD_PLAN.md)

## Status
- Phase 0 done: repo, contracts (`backend/app/models/contracts.py`), frozen config (`config/frozen.yaml`), FastAPI + SSE, React/Vite/Tailwind shell.
- Phase 1 done: stateful mock sources with fault injection, idempotency ledger, seeded scenario templates (flagship, status query, no-action), evaluator-only ground truth.
- Next: Phase 2 (graph, reverse trace, simulator, scorer) and Phase 3 (firewall, executor, verifier).

## Run
```
# backend
cd backend && python -m pip install -e ".[dev]" && python -m uvicorn app.main:app --reload --port 8000
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
