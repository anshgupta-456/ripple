"""A local mock OpenID Provider for tests and demos. TEST/DEMO CODE ONLY: it lives under tests/, binds to loopback and is
never part of the application package.

It implements the parts of an IdP that the RIPPLE sign-in relies on, and is deliberately STRICT so a client that skips a
step fails here the way it would at a real IdP:
  * discovery (/.well-known/openid-configuration) and JWKS (RS256), with key rotation
  * /authorize: exact redirect_uri match, response_type=code only, PKCE S256 REQUIRED, state required, openid scope required,
    prompt=none (silent re-auth against the IdP session cookie), login_hint auto-login for headless tests, login page otherwise
  * /token: public client (no secret), single-use short-lived codes, redirect_uri + code_verifier (S256) checks,
    no refresh tokens, no-store; CORS only for registered SPA origins
  * /logout with registered post-logout redirect
Run for a browser demo:  python -m tests.mock_idp --port 9100
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def s256(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode()).digest())


USERS: dict[str, dict[str, Any]] = {
    "priya": {"name": "Priya (risk analyst)", "roles": ["analyst"]},
    "sam": {"name": "Sam (ops admin)", "roles": ["admin"]},
    "meena": {"name": "Meena (viewer)", "roles": ["viewer"]},
    "ravi": {"name": "Ravi (merchant)", "roles": ["merchant"], "merchant_id": "M-34205"},
}


@dataclass
class IdpConfig:
    client_id: str = "ripple-spa"
    audience: str = "ripple-api"
    redirect_uris: list[str] = field(default_factory=lambda: ["http://localhost:5173/", "http://127.0.0.1:5173/"])
    post_logout_uris: list[str] = field(default_factory=lambda: ["http://localhost:5173/", "http://127.0.0.1:5173/"])
    spa_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    access_ttl: int = 600
    code_ttl: int = 60
    tenant: str = "demo"
    roles_claim: str = "roles"                       # "roles" | "groups" | "realm_access.roles" (nested)
    role_names: dict[str, str] = field(default_factory=dict)      # RIPPLE role -> the IdP's own name for it
    tenant_claim: str = "tenant"
    # fault injection for negative tests
    iss_claim_override: str | None = None
    audience_override: str | None = None
    discovery_issuer_override: str | None = None
    jwks_host_override: str | None = None
    authorization_host_override: str | None = None
    advertise_pkce: list[str] | None = field(default_factory=lambda: ["S256"])
    omit_roles: bool = False


_POOL: list[Any] = []


def _next_key() -> Any:
    """RSA keygen is the slow part of every test; keys are cheap to reuse across mock IdP instances."""
    if len(_POOL) < 24:
        _POOL.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _POOL[secrets.randbelow(len(_POOL))] if len(_POOL) > 8 else _POOL[-1]


class IdpState:
    def __init__(self, cfg: IdpConfig):
        self.cfg = cfg
        self.issuer = ""
        self.port = 0
        self.codes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, str] = {}           # idp session id -> username
        self.issued: list[dict[str, Any]] = []       # every token response, for assertions
        self.token_requests: list[dict[str, str]] = []
        self.jwks_hits = 0
        self.keys: list[tuple[str, Any]] = []
        self.rotate()

    def rotate(self) -> str:
        kid = "k-" + secrets.token_hex(4)
        key = _next_key()
        while any(k is key for _, k in self.keys):          # a rotated-in key must be a different key
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys.append((kid, key))
        return kid

    @property
    def signing(self) -> tuple[str, Any]:
        return self.keys[-1]

    def jwks(self) -> dict[str, Any]:
        keys = []
        for kid, priv in self.keys:
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
            keys.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": keys}

    def sign(self, claims: dict[str, Any], key: Any | None = None) -> str:
        kid, priv = self.signing
        return jwt.encode(claims, key or priv, algorithm="RS256", headers={"kid": kid})

    def make_tokens(self, user: str, client_id: str, nonce: str | None) -> dict[str, Any]:
        c, now, u = self.cfg, int(time.time()), USERS[user]
        roles = [c.role_names.get(r, r) for r in u["roles"]]
        access: dict[str, Any] = {"iss": c.iss_claim_override or self.issuer, "sub": f"idp|{user}", "aud": c.audience_override or c.audience,
                                  "iat": now, "exp": now + c.access_ttl, "jti": secrets.token_hex(8), "azp": client_id, "name": u["name"]}
        if not c.omit_roles:
            cur: Any = access
            parts = c.roles_claim.split(".")
            for part in parts[:-1]:
                cur = cur.setdefault(part, {})
            cur[parts[-1]] = roles
        access[c.tenant_claim] = c.tenant
        if "merchant_id" in u:
            access["merchant_id"] = u["merchant_id"]
        idt = {"iss": self.issuer, "sub": access["sub"], "aud": client_id, "iat": now, "exp": now + 300, "name": u["name"]}
        if nonce:
            idt["nonce"] = nonce
        resp = {"access_token": self.sign(access), "id_token": self.sign(idt), "token_type": "Bearer", "expires_in": c.access_ttl,
                "scope": "openid profile"}
        self.issued.append({"user": user, "access": access, "id": idt})
        return resp


def _err_redirect(redirect_uri: str, error: str, desc: str, state: str | None) -> RedirectResponse:
    q = {"error": error, "error_description": desc}
    if state:
        q["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + sep + urllib.parse.urlencode(q), status_code=302)


def create_app(st: IdpState) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    cfg = st.cfg
    app.add_middleware(CORSMiddleware, allow_origins=cfg.spa_origins, allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Content-Type"])

    @app.get("/.well-known/openid-configuration")
    def discovery():
        iss = st.issuer
        jwks_base = iss if not cfg.jwks_host_override else "http://" + cfg.jwks_host_override.format(port=st.port)
        auth_base = iss if not cfg.authorization_host_override else "http://" + cfg.authorization_host_override.format(port=st.port)
        doc = {"issuer": cfg.discovery_issuer_override or iss, "authorization_endpoint": f"{auth_base}/authorize",
               "token_endpoint": f"{iss}/token", "jwks_uri": f"{jwks_base}/jwks", "end_session_endpoint": f"{iss}/logout",
               "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
               "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
               "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": ["openid", "profile"]}
        if cfg.advertise_pkce is not None:
            doc["code_challenge_methods_supported"] = cfg.advertise_pkce
        return doc

    @app.get("/jwks")
    def jwks():
        st.jwks_hits += 1
        return st.jwks()

    def _issue_code(user: str, q: dict[str, str]) -> str:
        code = secrets.token_urlsafe(32)
        st.codes[code] = {"user": user, "client_id": q["client_id"], "redirect_uri": q["redirect_uri"],
                          "challenge": q["code_challenge"], "nonce": q.get("nonce"), "exp": time.time() + cfg.code_ttl, "used": False}
        return code

    def _finish(user: str, q: dict[str, str]) -> RedirectResponse:
        code = _issue_code(user, q)
        out = {"code": code, "state": q["state"], "iss": st.issuer}
        sep = "&" if "?" in q["redirect_uri"] else "?"
        return RedirectResponse(q["redirect_uri"] + sep + urllib.parse.urlencode(out), status_code=302)

    def _validate(q: dict[str, str]):
        """-> (error_response | None). Errors that cannot be safely redirected are rendered, never redirected."""
        if q.get("client_id") != cfg.client_id:
            return HTMLResponse("unknown client_id", status_code=400)
        if q.get("redirect_uri") not in cfg.redirect_uris:        # EXACT match, no prefixes or wildcards
            return HTMLResponse("redirect_uri is not registered", status_code=400)
        ru, state = q["redirect_uri"], q.get("state")
        if q.get("response_type") != "code":
            return _err_redirect(ru, "unsupported_response_type", "only the authorization code flow is allowed", state)
        if not state:
            return _err_redirect(ru, "invalid_request", "state is required", None)
        if not q.get("code_challenge") or q.get("code_challenge_method") != "S256":
            return _err_redirect(ru, "invalid_request", "PKCE with code_challenge_method=S256 is required", state)
        if "openid" not in q.get("scope", "").split():
            return _err_redirect(ru, "invalid_scope", "openid scope is required", state)
        return None

    @app.get("/authorize")
    def authorize(request: Request):
        q = dict(request.query_params)
        bad = _validate(q)
        if bad:
            return bad
        sess = st.sessions.get(request.cookies.get("idp_session", ""))
        if q.get("prompt") == "none":
            if not sess:
                return _err_redirect(q["redirect_uri"], "login_required", "no active IdP session", q["state"])
            return _finish(sess, q)
        hint = q.get("login_hint")
        if hint in USERS:                                              # headless tests: behave like an existing session
            resp = _finish(hint, q)
            sid = secrets.token_urlsafe(16)
            st.sessions[sid] = hint
            resp.set_cookie("idp_session", sid, httponly=True, samesite="lax")
            return resp
        buttons = "".join(f'<button name="user" value="{u}">{html.escape(v["name"])} ({", ".join(v["roles"])})</button>' for u, v in USERS.items())
        hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in q.items())
        return HTMLResponse(f"<html><body style='font-family:sans-serif;max-width:420px;margin:3em auto'><h2>Mock IdP sign-in</h2>"
                            f"<p>Local test provider. Pick an identity.</p><form method='post' action='/authorize/login'>{hidden}"
                            f"<div style='display:grid;gap:8px'>{buttons}</div></form></body></html>")

    @app.post("/authorize/login")
    async def login(request: Request):
        form = await request.form()
        q = {k: str(v) for k, v in form.items() if k != "user"}
        bad = _validate(q)
        user = str(form.get("user", ""))
        if bad:
            return bad
        if user not in USERS:
            return HTMLResponse("unknown user", status_code=400)
        resp = _finish(user, q)
        sid = secrets.token_urlsafe(16)
        st.sessions[sid] = user
        resp.set_cookie("idp_session", sid, httponly=True, samesite="lax")
        return resp

    @app.post("/token")
    async def token(request: Request):
        form = {k: str(v) for k, v in (await request.form()).items()}
        st.token_requests.append(form)
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        bad = lambda e, d="": JSONResponse({"error": e, "error_description": d}, status_code=400, headers=headers)  # noqa: E731
        if form.get("grant_type") != "authorization_code":
            return bad("unsupported_grant_type", "only authorization_code (no refresh tokens are issued)")
        rec = st.codes.get(form.get("code", ""))
        if rec is None or rec["used"] or rec["exp"] < time.time():
            if rec is not None:
                rec["used"] = True
            return bad("invalid_grant", "code is unknown, expired or already used")
        rec["used"] = True                                              # single use, whatever happens next
        if form.get("client_id") != rec["client_id"] or form.get("redirect_uri") != rec["redirect_uri"]:
            return bad("invalid_grant", "client_id or redirect_uri mismatch")
        verifier = form.get("code_verifier", "")
        if not (43 <= len(verifier) <= 128) or not hmac.compare_digest(s256(verifier), rec["challenge"]):
            return bad("invalid_grant", "PKCE verification failed")
        return JSONResponse(st.make_tokens(rec["user"], rec["client_id"], rec["nonce"]), headers=headers)

    @app.get("/logout")
    def logout(request: Request):
        st.sessions.pop(request.cookies.get("idp_session", ""), None)
        target = request.query_params.get("post_logout_redirect_uri")
        resp: Response = RedirectResponse(target, status_code=302) if target in cfg.post_logout_uris else HTMLResponse("signed out")
        resp.delete_cookie("idp_session")
        return resp

    return app


class IdpServer:
    """Runs the mock IdP on a loopback port in a background thread."""

    def __init__(self, cfg: IdpConfig | None = None, port: int | None = None):
        self.state = IdpState(cfg or IdpConfig())
        self.port = port or self._free_port()
        self.state.issuer = f"http://127.0.0.1:{self.port}"
        self.state.port = self.port
        self._server = uvicorn.Server(uvicorn.Config(create_app(self.state), host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @property
    def issuer(self) -> str:
        return self.state.issuer

    @property
    def cfg(self) -> IdpConfig:
        return self.state.cfg

    def start(self) -> "IdpServer":
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("mock IdP did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    a = ap.parse_args()
    srv = IdpServer(port=a.port).start()
    print(f"mock IdP at {srv.issuer}  client_id={srv.cfg.client_id}  audience={srv.cfg.audience}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
