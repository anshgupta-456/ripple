# Deploying RIPPLE

One container serves the API and the built web app on the same origin. Nothing about this has been run on a real host yet.

> **Status of the container files (2026-10-27).** The image builds (352 MB) and was run with compose in three modes: production (refuses
> everything without an IdP, anonymous calls get 401, runs as uid 10001, HSTS and CSP present), demo (`scripts/demo_check.py` passes
> against the container, audit files written to the volume as the unprivileged user) and with a real Keycloak (below). Building it
> found one bug, a health-check timeout too tight under load, now fixed. **Not yet tested:** a read-only root filesystem, https
> termination in front of it, image vulnerability scanning, Kubernetes. Serving the built app (CSP, path safety, fallbacks) is also
> covered without Docker: `backend/tests/test_web.py`.

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

## Model server (Ollama) from the container
The container cannot reach the host's `127.0.0.1`. With the SSH tunnel to the GPU server open on the host ([LOCAL_LLM.md](LOCAL_LLM.md)), set `RIPPLE_LOCAL_API=openai`, `RIPPLE_OLLAMA_URL=http://host.docker.internal:11509` and `RIPPLE_OLLAMA_ALLOWED_HOSTS=host.docker.internal` (in `.env` or the shell); `docker-compose.yml` passes them through. Verified: the container reached the host's Ollama and answered an ambiguous message through it. Details and measured quality: [LOCAL_LLM.md](LOCAL_LLM.md).

## Real-IdP check with Keycloak
```
docker compose -f docker-compose.yml -f docker-compose.keycloak.yml up --build     # then open http://localhost:8000
```
Starts Keycloak 26 (dev mode, port 8180) with the realm in `scripts/keycloak/ripple-realm.json`: a public client with PKCE S256, an audience
mapper for `ripple-api`, a tenant claim, realm roles, and throwaway users (priya = analyst, sam = admin, norole = no role; local test passwords
in that file). Verified in a real browser on 2026-10-27: discovery and endpoint validation against Keycloak, authorization code + PKCE sign-in,
RS256 token accepted and mapped to the analyst role (`realm_access.roles`), code stripped from the URL, IdP logout ends the Keycloak session
(the next SSO click shows the login form), a user with no role is refused with 403, and the API failed closed with 503 while Keycloak was still
starting. **Limits:** plain http on localhost, so RIPPLE runs with `RIPPLE_ENV=dev` here (production requires https, which was not tested against
Keycloak); silent renewal after token expiry and key rotation were not exercised against Keycloak (they are covered against the mock IdP); port 8080 was
blocked on the test machine so Keycloak uses 8180.

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
