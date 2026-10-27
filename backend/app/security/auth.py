"""Authentication and authorization.

Identity: the company's OIDC provider (OAuth2 / OIDC; SAML is bridged to OIDC by the IdP). We only VALIDATE tokens
(signature via the IdP's JWKS, issuer, audience, expiry) and never store passwords. Tokens must be short-lived.
Dev: a persona login mints 15-minute HS256 tokens signed with an ephemeral key. It exists only when RIPPLE_ENV=dev.

Authorization: RBAC with least privilege. The caller's roles come ONLY from the verified token, never from the request
body. Case access is also scoped by tenant, and merchants only by their own merchant_id.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

import jwt
from fastapi import Depends, Header, HTTPException

from app.security.governance import ACTOR, KILL, LIMITER
from app.security.secret_store import get_secret
from app.security.settings import DEV_TOKEN_LIFETIME_S, MAX_TOKEN_LIFETIME_S, env, is_dev

ROLES = {"viewer", "analyst", "merchant", "admin"}

# permission -> roles allowed. Approving risk actions is analyst-only (separation of duties: admins cannot approve).
PERMS: dict[str, set[str]] = {
    "console.read": {"viewer", "analyst", "admin"},
    "case.read": {"viewer", "analyst", "admin", "merchant"},
    "case.pii": {"analyst", "admin", "merchant"},       # raw (unredacted) conversation text
    "case.message": {"admin", "merchant"},               # admin acts as the merchant only for demos, and is audited as such
    "case.consent": {"admin", "merchant"},
    "approval.read": {"viewer", "analyst", "admin"},
    "approval.decide": {"analyst"},
    "audit.read": {"analyst", "admin"},
    "audit.verify": {"admin"},
    "bench.read": {"viewer", "analyst", "admin", "merchant"},
    "lab.run": {"analyst", "admin"},
    "demo.control": {"admin"},                           # seed / new case / advance time / reset / fault injection
    "killswitch.set": {"admin"},
    "data.delete": {"admin"},
}

PERSONAS: dict[str, tuple[str, set[str]]] = {
    "priya": ("Priya (risk analyst)", {"analyst"}),
    "sam": ("Sam (ops admin)", {"admin"}),
    "meena": ("Meena (read-only viewer)", {"viewer"}),
}
DEV_TENANT = "demo"
DEV_ISS = "ripple-dev"


@dataclass(frozen=True)
class User:
    sub: str
    name: str
    roles: frozenset[str]
    tenant: str
    merchant_id: str | None = None
    exp: int = 0
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def can(self, perm: str) -> bool:
        return bool(self.roles & PERMS.get(perm, set()))


class AuthError(Exception):
    def __init__(self, detail: str, status: int = 401):
        super().__init__(detail)
        self.detail, self.status = detail, status


# ---------------------------------------------------------------- token verification
_JWKS_CACHE: dict[str, tuple[float, Any]] = {}
_jwks_client_factory: Callable[[str], Any] | None = None     # tests inject a fake


def _oidc_conf() -> tuple[str, str]:
    iss = get_secret("RIPPLE_OIDC_ISSUER", required=False)
    aud = get_secret("RIPPLE_OIDC_AUDIENCE", required=False)
    if not iss or not aud:
        raise AuthError("authentication is not configured (RIPPLE_OIDC_ISSUER / RIPPLE_OIDC_AUDIENCE); refusing all requests", 503)
    return iss.rstrip("/"), aud


def _jwks_client(issuer: str):
    cached = _JWKS_CACHE.get(issuer)
    if cached and cached[0] > time.time():
        return cached[1]
    if _jwks_client_factory:
        client = _jwks_client_factory(issuer)
    else:
        if urllib.parse.urlparse(issuer).scheme != "https":
            raise AuthError("OIDC issuer must be https", 503)
        with urllib.request.urlopen(issuer + "/.well-known/openid-configuration", timeout=5) as r:     # noqa: S310
            conf = json.loads(r.read())
        uri = conf["jwks_uri"]
        if urllib.parse.urlparse(uri).hostname != urllib.parse.urlparse(issuer).hostname:
            raise AuthError("jwks_uri host does not match the issuer host", 503)       # domain allowlist
        client = jwt.PyJWKClient(uri, cache_keys=True, timeout=5)
    _JWKS_CACHE[issuer] = (time.time() + 600, client)
    return client


def _dev_key() -> str:
    return get_secret("RIPPLE_DEV_JWT_KEY", dev_ephemeral=True)      # type: ignore[return-value]


def _to_user(claims: dict[str, Any]) -> User:
    roles = frozenset(r for r in (claims.get("roles") or []) if r in ROLES)
    if not roles:
        raise AuthError("token carries no recognised role", 403)
    tenant = claims.get("tenant") or claims.get("tid")
    if not tenant:
        raise AuthError("token carries no tenant", 403)
    return User(sub=str(claims["sub"]), name=str(claims.get("name") or claims["sub"]), roles=roles, tenant=str(tenant),
                merchant_id=claims.get("merchant_id"), exp=int(claims["exp"]))


def verify_token(token: str) -> User:
    try:
        alg = jwt.get_unverified_header(token).get("alg", "")
    except jwt.PyJWTError:
        raise AuthError("malformed token")
    try:
        if alg == "HS256":
            if not is_dev():
                raise AuthError("symmetric dev tokens are not accepted outside RIPPLE_ENV=dev")
            claims = jwt.decode(token, _dev_key(), algorithms=["HS256"], issuer=DEV_ISS, audience="ripple-api",
                                options={"require": ["exp", "iat", "sub"]})
        elif alg in ("RS256", "ES256"):
            iss, aud = _oidc_conf()
            key = _jwks_client(iss).get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256", "ES256"], issuer=iss, audience=aud,
                                options={"require": ["exp", "iat", "sub"]})
        else:
            raise AuthError("unsupported token algorithm")
    except AuthError:
        raise
    except jwt.ExpiredSignatureError:
        raise AuthError("token expired")
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {type(e).__name__}")
    if int(claims["exp"]) - int(claims["iat"]) > MAX_TOKEN_LIFETIME_S:
        raise AuthError("token lifetime exceeds the one-hour maximum; long-lived tokens are refused")
    return _to_user(claims)


def issue_dev_token(persona: str, tenant: str = DEV_TENANT) -> tuple[str, User]:
    if not is_dev():
        raise AuthError("dev login is disabled outside RIPPLE_ENV=dev", 403)
    if persona not in PERSONAS:
        raise AuthError("unknown persona", 404)
    name, roles = PERSONAS[persona]
    now = int(time.time())
    claims = {"iss": DEV_ISS, "aud": "ripple-api", "sub": f"dev:{persona}", "name": name, "roles": sorted(roles),
              "tenant": tenant, "iat": now, "exp": now + DEV_TOKEN_LIFETIME_S}
    tok = jwt.encode(claims, _dev_key(), algorithm="HS256")
    return tok, _to_user(claims)


# ---------------------------------------------------------------- FastAPI dependencies
async def current_user(authorization: str | None = Header(default=None)) -> User:
    """async on purpose: it sets the actor context that the audit log reads, in the request's own task."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    try:
        user = verify_token(authorization.split(" ", 1)[1].strip())
    except AuthError as e:
        raise HTTPException(e.status, e.detail, headers={"WWW-Authenticate": "Bearer"} if e.status == 401 else None)
    ACTOR.set({"sub": user.sub, "name": user.name, "roles": sorted(user.roles), "tenant": user.tenant})
    return user


def require(perm: str, *, limit: tuple[str, int, int] | None = None):
    """Dependency factory: authenticated + permitted (+ optional rate limit (bucket, n, per-seconds))."""

    async def dep(user: User = Depends(current_user)) -> User:
        if not user.can(perm):
            from app.security.audit_security import security_event
            security_event("authz_denied", user, perm=perm)
            raise HTTPException(403, f"role {sorted(user.roles)} may not {perm}")
        if limit and not LIMITER.allow(limit[0], user.sub, limit[1], limit[2]):
            raise HTTPException(429, "rate limit exceeded; slow down")
        return user

    return dep


def blocked_reason(user: User) -> str | None:
    return KILL.blocked(user.sub)


def describe_env() -> dict[str, Any]:
    return {"env": env(), "dev_login": is_dev(), "oidc_configured": bool(get_secret("RIPPLE_OIDC_ISSUER", required=False))}
