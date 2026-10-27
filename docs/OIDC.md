# Single sign-on: authorization code + PKCE

RIPPLE signs users in with your company's OpenID Connect provider. The browser app is a **public client**: it uses the
authorization code flow with PKCE (S256) and holds the access token **in memory only**. The API never sees a password and
validates every request's token itself.

> Tested against a strict local mock IdP (`backend/tests/mock_idp.py`) with real HTTP, real RS256 signatures and real PKCE
> verification, and in a real browser. **Not yet tested against a production IdP.** The provider notes below are guidance
> from how those products are generally configured; verify each against the vendor's documentation.

## How it works
```
Browser (SPA)                         IdP                                 RIPPLE API
 1. create verifier, state, nonce
    challenge = BASE64URL(SHA256(verifier))
 2. redirect ------------------------> /authorize?response_type=code&code_challenge=...&S256&state&nonce
                                        user signs in (MFA etc. is the IdP's business)
 3. <---------------------------------- 302 redirect_uri?code=...&state=...&iss=...
 4. check state (and iss), strip ?code from the address bar
 5. POST /token (code + verifier, no secret) ---> checks verifier against the challenge, code is single use
 6. <---------------------------------- access_token (short-lived JWT) + id_token (nonce checked)
 7. keep access_token in memory; Authorization: Bearer ------------------------> verify signature (JWKS), iss, aud, exp, roles
```
- The **verifier** and **state/nonce** live in `sessionStorage` only between steps 1 and 4, then are deleted (single use).
  The only other thing kept in the tab is a non-secret marker, "this tab signed in via SSO", plus the time of the last silent attempt.
- A **refresh token** is never kept. Renewal is a silent re-authorization (`prompt=none`) against the IdP session: it runs when a
  token expires (the API answers 401) and when a tab is reloaded (the in-memory token is gone, the marker says SSO was used).
  If it fails the user sees the sign-in screen and the marker is cleared. **Loop guard:** a silent attempt is refused within 30
  seconds of the previous one unless the API accepted the token that attempt produced, so an IdP/API disagreement can never
  become a redirect loop.
- **Sign out** clears memory and ends the IdP session (`end_session_endpoint` with `id_token_hint`).
- Tokens older than **one hour** are refused no matter who signed them. Ask your IdP for access tokens of 5 to 15 minutes.
- On the API: only `RS256`/`ES256` with keys from the IdP's JWKS. `none` and symmetric algorithms are refused. Discovery must
  repeat the configured issuer. Endpoints must be https. An unknown key id refetches the JWKS at most once per
  `RIPPLE_OIDC_JWKS_COOLDOWN` seconds (default 30): a rotated key is picked up after at most that long; forged key ids cannot
  make the API hammer the IdP.

## Configure the API (all non-secret)
| Variable | Meaning |
|---|---|
| `RIPPLE_OIDC_ISSUER` | issuer URL, https (plain http only for loopback in `RIPPLE_ENV=dev`) |
| `RIPPLE_OIDC_AUDIENCE` | the `aud` the **access token** must carry (your API identifier). With `RIPPLE_OIDC_AUDIENCE_CLAIM=client_id`: the app client id the token's `client_id` must equal |
| `RIPPLE_OIDC_AUDIENCE_CLAIM` | `aud` (default) or `client_id` for providers whose access tokens have no `aud` (Amazon Cognito). In `client_id` mode the API also requires `token_use` to be `access` (ID tokens are refused); signature, issuer and expiry are checked as always |
| `RIPPLE_OIDC_CLIENT_ID` | the SPA's public client id. Setting it turns on the browser sign-in |
| `RIPPLE_OIDC_SCOPE` | default `openid profile` |
| `RIPPLE_OIDC_AUTH_PARAMS` | extra authorize parameters, urlencoded, e.g. `audience=ripple-api`. Cannot override state, nonce, PKCE, redirect_uri, client_id, scope |
| `RIPPLE_OIDC_ROLES_CLAIM` | claim with the user's roles/groups (default `roles`). A dotted path (`realm_access.roles`) or an exact key containing dots (`https://example.com/roles`) both work |
| `RIPPLE_OIDC_ROLE_MAP` | JSON mapping IdP group names to RIPPLE roles, e.g. `{"Risk-Analysts":"analyst","Ops":"admin"}`. A group that is not mapped grants nothing |
| `RIPPLE_OIDC_TENANT_CLAIM` | claim with the tenant (default `tenant`, then `tid`) |
| `RIPPLE_OIDC_ALLOWED_HOSTS` | extra hosts the authorize/token/JWKS endpoints may live on (default: the issuer's host only) |
| `RIPPLE_OIDC_JWKS_COOLDOWN` | seconds between forced JWKS refetches (default 30) |

RIPPLE roles are `viewer`, `analyst`, `merchant`, `admin`. A token with no recognised role, or no tenant, is refused (403).
`merchant` users must also carry a `merchant_id` claim and only ever see their own case.

## Register the app at your IdP (checklist)
1. **Public / SPA client**, no client secret. Grant: authorization code. PKCE required (S256).
2. **Redirect URI**: exactly your app origin with a trailing slash, e.g. `https://ripple.example.com/` (exact match, no wildcards).
   Post-logout redirect URI: the same.
3. **Allow the SPA origin for CORS on the token endpoint** (the browser posts the code there). Some IdPs do this
   automatically for SPA-type clients.
4. **Access-token audience** must equal `RIPPLE_OIDC_AUDIENCE`, and the access token must be a JWT signed RS256/ES256.
5. **Roles and tenant** present in the access token under the claims you configured. Keep token lifetime short.
6. Enforce MFA and session policy at the IdP.

## Provider notes (unverified against real tenants)
| Provider | Things to know |
|---|---|
| Keycloak | add an *audience* mapper so `aud` contains your API id (default is `account`). Roles are at `realm_access.roles`: set `RIPPLE_OIDC_ROLES_CLAIM=realm_access.roles` |
| Auth0 | pass `RIPPLE_OIDC_AUTH_PARAMS=audience=<API identifier>`. Custom claims must be namespaced URLs: set the claim env vars to that exact URL |
| Okta | use a custom authorization server so `aud` is your API; add a `groups` claim and map it with `RIPPLE_OIDC_ROLE_MAP` |
| Microsoft Entra ID | register the redirect as **Single-page application**; use v2 tokens with an API scope; roles come from app roles in the `roles` claim |
| Amazon Cognito | set `RIPPLE_OIDC_AUDIENCE_CLAIM=client_id` and `RIPPLE_OIDC_AUDIENCE=<app client id>`. Issuer is `https://cognito-idp.<region>.amazonaws.com/<pool id>`; the hosted-UI authorize/token endpoints are on a different host (`<domain>.auth.<region>.amazoncognito.com`), so add it to `RIPPLE_OIDC_ALLOWED_HOSTS`. Groups arrive as `cognito:groups`: set `RIPPLE_OIDC_ROLES_CLAIM=cognito:groups` and map them with `RIPPLE_OIDC_ROLE_MAP`. Access tokens carry no custom attributes by default, so the tenant needs a token-customisation trigger or a `RIPPLE_OIDC_TENANT_CLAIM` your pool actually emits. **Tested against a mock that mimics the token shape, not against a real pool** |

## Try it locally with the mock IdP
```
cd backend
python -m tests.mock_idp --port 9100 --access-ttl 40       # users: priya (analyst), sam (admin), meena (viewer), ravi (merchant)
RIPPLE_ENV=dev RIPPLE_OIDC_ISSUER=http://127.0.0.1:9100 RIPPLE_OIDC_AUDIENCE=ripple-api RIPPLE_OIDC_CLIENT_ID=ripple-spa \
  python -m uvicorn app.main:app --port 8000
cd ../frontend && npm run dev                              # open http://localhost:5173 and use "Sign in with SSO"
```
The mock IdP is deliberately strict: PKCE S256 required, state required, exact redirect URI, single-use short-lived codes, no
refresh tokens. A client that skips a step fails against it the way it would against a real provider.

## Troubleshooting (API replies)
| Response | Meaning |
|---|---|
| 401 `token expired` | access token past its `exp`; the app silently renews, or asks you to sign in |
| 401 `token lifetime exceeds the one-hour maximum` | ask the IdP for shorter access tokens |
| 401 `invalid token: InvalidAudienceError` | `aud` does not equal `RIPPLE_OIDC_AUDIENCE` (Keycloak audience mapper, Auth0 `audience` param) |
| 401 `invalid token: InvalidIssuerError` | token's `iss` differs from `RIPPLE_OIDC_ISSUER` |
| 401 `invalid token: PyJWKClientError` | signing key not in the IdP's JWKS (rotation inside the cooldown, or a forged token) |
| 403 `token carries no recognised role` | the roles claim is missing, mis-named, or the group names are not mapped |
| 403 `token carries no tenant` | the tenant claim is missing |
| 503 `... must be https` / `discovery issuer does not match` / `identity provider unreachable` | configuration or network problem; the API fails closed |
