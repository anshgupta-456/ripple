"""OIDC relying-party configuration, discovery and claim mapping.

Configuration is NON-secret and comes from the environment (secrets such as signing keys never appear here):
  RIPPLE_OIDC_ISSUER        issuer URL (https; plain http only for loopback in RIPPLE_ENV=dev)
  RIPPLE_OIDC_AUDIENCE      the API audience the access token must carry
  RIPPLE_OIDC_CLIENT_ID     the SPA's PUBLIC client id (no client secret exists: PKCE replaces it)
  RIPPLE_OIDC_SCOPE         default "openid profile"
  RIPPLE_OIDC_AUTH_PARAMS   extra authorize params, urlencoded, e.g. "audience=ripple-api" (Auth0)
  RIPPLE_OIDC_ROLES_CLAIM   claim holding roles/groups; dotted path allowed (default "roles", Keycloak "realm_access.roles")
  RIPPLE_OIDC_ROLE_MAP      JSON mapping IdP group names to RIPPLE roles, e.g. {"Risk-Analysts":"analyst"}
  RIPPLE_OIDC_TENANT_CLAIM  claim holding the tenant (default: "tenant", then "tid")
  RIPPLE_OIDC_ALLOWED_HOSTS extra hosts allowed for authorize/token/jwks endpoints (default: the issuer host only)

Discovery results are validated: the document's issuer must equal the configured issuer (OIDC Discovery 4.3, defends
against mix-up), every endpoint must be https (or dev loopback) and on an allowed host.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app.security.settings import is_dev

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
MAX_DISCOVERY_BYTES = 256_000


class OidcError(Exception):
    def __init__(self, detail: str, status: int = 503):
        super().__init__(detail)
        self.detail, self.status = detail, status


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    client_id: str | None = None
    scope: str = "openid profile"
    auth_params: str = ""
    roles_claim: str = "roles"
    role_map: dict[str, str] = field(default_factory=dict, hash=False)
    tenant_claim: str | None = None
    allowed_hosts: frozenset[str] = frozenset()


def oidc_settings() -> OidcSettings | None:
    iss = os.environ.get("RIPPLE_OIDC_ISSUER", "").strip().rstrip("/")
    aud = os.environ.get("RIPPLE_OIDC_AUDIENCE", "").strip()
    if not iss or not aud:
        return None
    try:
        role_map = json.loads(os.environ.get("RIPPLE_OIDC_ROLE_MAP", "") or "{}")
        if not isinstance(role_map, dict):
            raise ValueError("not an object")
    except ValueError:
        raise OidcError("RIPPLE_OIDC_ROLE_MAP must be a JSON object")
    hosts = frozenset(h.strip().lower() for h in os.environ.get("RIPPLE_OIDC_ALLOWED_HOSTS", "").split(",") if h.strip())
    return OidcSettings(
        issuer=iss, audience=aud, client_id=os.environ.get("RIPPLE_OIDC_CLIENT_ID", "").strip() or None,
        scope=os.environ.get("RIPPLE_OIDC_SCOPE", "").strip() or "openid profile",
        auth_params=os.environ.get("RIPPLE_OIDC_AUTH_PARAMS", "").strip(),
        roles_claim=os.environ.get("RIPPLE_OIDC_ROLES_CLAIM", "").strip() or "roles",
        role_map={str(k): str(v) for k, v in role_map.items()},
        tenant_claim=os.environ.get("RIPPLE_OIDC_TENANT_CLAIM", "").strip() or None, allowed_hosts=hosts)


def check_url(url: str, what: str, allowed_hosts: set[str] | frozenset[str] | None = None, issuer_host: str | None = None) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        pass
    elif p.scheme == "http" and is_dev() and (p.hostname or "") in LOOPBACK:
        pass                                                         # local mock IdP in demo mode only
    else:
        raise OidcError(f"{what} must be https")
    if allowed_hosts is not None:
        host = (p.hostname or "").lower()
        if host != (issuer_host or "") and host not in allowed_hosts:
            raise OidcError(f"{what} host '{host}' is not the issuer host or an allowed host (RIPPLE_OIDC_ALLOWED_HOSTS)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # a discovery document must be served directly, never redirected
        return None


_DISC_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
DISC_TTL = 600


def discover(issuer: str, s: OidcSettings | None = None) -> dict[str, Any]:
    cached = _DISC_CACHE.get(issuer)
    if cached and cached[0] > time.time():
        return cached[1]
    check_url(issuer, "OIDC issuer")
    req = urllib.request.Request(issuer + "/.well-known/openid-configuration", headers={"Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=5) as r:      # noqa: S310 (scheme checked above)
            raw = r.read(MAX_DISCOVERY_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise OidcError("identity provider discovery failed")
    if len(raw) > MAX_DISCOVERY_BYTES:
        raise OidcError("discovery document too large")
    try:
        conf = json.loads(raw)
    except ValueError:
        raise OidcError("discovery document is not JSON")
    if str(conf.get("issuer", "")).rstrip("/") != issuer:
        raise OidcError("discovery issuer does not match the configured issuer")
    ih = (urllib.parse.urlparse(issuer).hostname or "").lower()
    allowed = (s.allowed_hosts if s else frozenset())
    if "jwks_uri" not in conf:
        raise OidcError("discovery document has no jwks_uri")
    check_url(conf["jwks_uri"], "jwks_uri", allowed, ih)
    _DISC_CACHE[issuer] = (time.time() + DISC_TTL, conf)
    return conf


def browser_config(s: OidcSettings) -> dict[str, Any]:
    """What the SPA needs to start an authorization-code + PKCE flow. All endpoints are validated server-side."""
    if not s.client_id:
        raise OidcError("browser sign-in is not configured (RIPPLE_OIDC_CLIENT_ID)", 404)
    conf = discover(s.issuer, s)
    ih = (urllib.parse.urlparse(s.issuer).hostname or "").lower()
    methods = conf.get("code_challenge_methods_supported")
    if methods is not None and "S256" not in methods:
        raise OidcError("the identity provider does not advertise PKCE S256 support")
    out: dict[str, Any] = {"issuer": s.issuer, "client_id": s.client_id, "scope": s.scope, "auth_params": s.auth_params}
    for key in ("authorization_endpoint", "token_endpoint", "end_session_endpoint"):
        if conf.get(key):
            check_url(conf[key], key, s.allowed_hosts, ih)
            out[key] = conf[key]
    if "authorization_endpoint" not in out or "token_endpoint" not in out:
        raise OidcError("discovery document lacks authorization_endpoint or token_endpoint")
    return out


def _dig(claims: dict[str, Any], path: str) -> Any:
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def roles_from_claims(claims: dict[str, Any], s: OidcSettings | None, known: set[str]) -> frozenset[str]:
    """Map the IdP's role/group claim to RIPPLE roles. Unmapped or unknown values grant nothing."""
    raw = _dig(claims, s.roles_claim if s else "roles")
    if isinstance(raw, str):
        raw = raw.split()
    out: set[str] = set()
    for item in raw or []:
        name = str(item)
        name = s.role_map.get(name, name) if s else name
        if name in known:
            out.add(name)
    return frozenset(out)


def tenant_from_claims(claims: dict[str, Any], s: OidcSettings | None) -> str | None:
    if s and s.tenant_claim:
        v = _dig(claims, s.tenant_claim)
    else:
        v = claims.get("tenant") or claims.get("tid")
    return str(v) if v else None
