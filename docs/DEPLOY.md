# Deploying RIPPLE

One container serves the API and the built web app on the same origin. Nothing about this has been run on a real host yet.

> **Status of the container files.** `Dockerfile`, `docker-compose.yml` and `docker-compose.demo.yml` were written and reviewed but
> **never built**: the Docker daemon was not available where this was authored. Build once and fix whatever it finds before relying on them.
> Serving the built app from the API (CSP, path safety, fallbacks) *is* tested without Docker: `backend/tests/test_web.py`.

## Without Docker
```
cd frontend && npm ci && npm run build        # produces frontend/dist
cd ../backend && python -m pip install -e . && python -m uvicorn app.main:app --port 8000
```
The API serves `frontend/dist` at `/` when it exists (`RIPPLE_STATIC_DIR` points elsewhere). `/api/...` is the API; an unknown API path
is a JSON 404, never the page.

## With Docker
```
docker compose up --build                                            # production mode: refuses everything until an IdP is configured
docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build   # demo mode, personas, loopback only
```
Settings go in a `.env` next to the compose file (not committed): `RIPPLE_OIDC_ISSUER`, `RIPPLE_OIDC_AUDIENCE`, `RIPPLE_OIDC_CLIENT_ID`,
role/tenant claim names, and the secret-store settings in [SECRETS.md](SECRETS.md). Secrets (audit key, model key) should come from
Vault / AWS Secrets Manager, not from `.env`.

## What the image does
- Two stages: a Node build that type-checks and bundles the app, then a slim Python image with only the runtime dependencies from `pyproject.toml`.
- Runs as an unprivileged user (uid 10001), drops all Linux capabilities, `no-new-privileges`, port bound to loopback in compose.
- `RIPPLE_ENV` is not set in the image, so the default is **production**.
- Audit logs, kill-switch state and the security log live on the `/var/ripple` volume. Cases themselves are in memory and a restart clears them.
- Health check: `GET /api/health`.

## Response headers
| Header | Value | Where |
|---|---|---|
| `Content-Security-Policy` (page) | `default-src 'none'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self' <IdP token origin>; base-uri 'none'; form-action 'self'; object-src 'none'; frame-ancestors 'none'` | the page |
| `Content-Security-Policy` (API) | `default-src 'none'; frame-ancestors 'none'; base-uri 'none'` | every other response |
| `Strict-Transport-Security` | 1 year, includeSubDomains | production only |
| `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cache-Control: no-store` | nosniff, DENY, no-referrer | API responses |

The IdP token origin comes from the validated discovery document, never from the request. If discovery fails the policy falls back to
`'self'` only (sign-in then cannot complete, which fails closed).

**Tradeoffs.** `style-src-attr 'unsafe-inline'` is needed because the UI sets some `style=` attributes; it does not allow inline `<style>`
blocks or any inline script. Dependencies are version-ranged, not hash-locked; for a real deployment generate a hashed lockfile
(`pip-compile --generate-hashes`) and pin the base images by digest. TLS is expected from a reverse proxy; the app only sends HSTS.
