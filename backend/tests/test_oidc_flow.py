"""Authorization code + PKCE against a real HTTP mock IdP, with the RIPPLE API validating the resulting tokens over real
HTTP discovery/JWKS (no fake JWKS client here)."""
import secrets
import time
import urllib.parse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import SESSIONS, app
from app.security import auth, oidc
from app.security.audit_security import get_security_log
from tests.helpers import ADMIN
from tests.mock_idp import IdpConfig, IdpServer, b64url, s256

REDIRECT = "http://localhost:5173/"


@pytest.fixture
def make_idp(monkeypatch):
    servers: list[IdpServer] = []

    def make(env: dict[str, str] | None = None, **cfg) -> IdpServer:
        srv = IdpServer(IdpConfig(**cfg)).start()
        servers.append(srv)
        monkeypatch.setenv("RIPPLE_OIDC_ISSUER", srv.issuer)
        monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", srv.cfg.audience)
        monkeypatch.setenv("RIPPLE_OIDC_CLIENT_ID", srv.cfg.client_id)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(auth, "_jwks_client_factory", None)       # real HTTP discovery + JWKS
        auth._JWKS_CACHE.clear()
        oidc._DISC_CACHE.clear()
        return srv

    yield make
    for s in servers:
        s.stop()
    auth._JWKS_CACHE.clear()
    oidc._DISC_CACHE.clear()


def anon() -> TestClient:
    t = TestClient(app)
    t.headers.pop("Authorization", None)
    return t


def bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def authorize(srv, client, *, user="priya", verifier=None, method="S256", state="st-1", nonce="n-1", redirect=REDIRECT,
              challenge="auto", **extra):
    verifier = verifier or b64url(secrets.token_bytes(32))
    params = {"response_type": "code", "client_id": srv.cfg.client_id, "redirect_uri": redirect, "scope": "openid profile",
              "state": state, "nonce": nonce, "login_hint": user, **extra}
    if challenge == "auto":
        params["code_challenge"] = s256(verifier)
    elif challenge:
        params["code_challenge"] = challenge
    if method:
        params["code_challenge_method"] = method
    return client.get(srv.issuer + "/authorize", params=params, follow_redirects=False), verifier


def code_from(resp: httpx.Response) -> dict:
    assert resp.status_code == 302, resp.text
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(resp.headers["location"]).query))


def exchange(srv, client, code, verifier, redirect=REDIRECT):
    return client.post(srv.issuer + "/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                                    "client_id": srv.cfg.client_id, "code_verifier": verifier})


def login(srv, user="priya", **kw) -> dict:
    with httpx.Client(verify=False) as c:
        r, v = authorize(srv, c, user=user, **kw)
        q = code_from(r)
        t = exchange(srv, c, q["code"], v)
        assert t.status_code == 200, t.text
        return t.json()


def me(tokens) -> httpx.Response:
    return anon().get("/api/auth/me", headers=bearer(tokens))


# ---------------------------------------------------------------- the happy path
def test_rfc7636_test_vector_for_s256():
    assert s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_full_code_flow_then_api_accepts_the_rs256_access_token(make_idp):
    srv = make_idp()
    tok = login(srv, "priya", state="abc", nonce="nnn")
    r = me(tok)
    assert r.status_code == 200
    body = r.json()
    assert body["roles"] == ["analyst"] and body["sub"] == "idp|priya" and "approval.decide" in body["perms"]
    assert jwt.get_unverified_header(tok["access_token"])["alg"] == "RS256"
    assert "refresh_token" not in tok                                    # no long-lived credential is ever issued
    assert jwt.decode(tok["id_token"], options={"verify_signature": False})["nonce"] == "nnn"


def test_authorization_response_echoes_state_and_issuer(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:
        r, _ = authorize(srv, c, state="state-xyz")
    q = code_from(r)
    assert q["state"] == "state-xyz" and q["iss"] == srv.issuer


def test_the_api_validates_over_real_http_discovery_and_jwks(make_idp):
    srv = make_idp()
    assert me(login(srv)).status_code == 200
    assert oidc._DISC_CACHE[srv.issuer][1]["jwks_uri"] == srv.issuer + "/jwks"      # fetched by the API itself, over HTTP


def test_oidc_config_endpoint_publishes_validated_endpoints_and_no_secret(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_AUTH_PARAMS": "audience=ripple-api"})
    cfg = anon().get("/api/auth/oidc-config").json()
    assert cfg["client_id"] == "ripple-spa" and cfg["authorization_endpoint"] == srv.issuer + "/authorize"
    assert cfg["token_endpoint"] == srv.issuer + "/token" and cfg["auth_params"] == "audience=ripple-api"
    assert "secret" not in str(cfg).lower()
    assert anon().get("/api/config/public").json()["auth"]["oidc_login"] is True


def test_oidc_config_is_404_when_not_configured(monkeypatch):
    for k in ("RIPPLE_OIDC_ISSUER", "RIPPLE_OIDC_AUDIENCE", "RIPPLE_OIDC_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert anon().get("/api/auth/oidc-config").status_code == 404


# ---------------------------------------------------------------- the IdP is strict: a sloppy client would fail here
def test_idp_refuses_authorize_requests_without_s256_pkce(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:
        none, _ = authorize(srv, c, challenge=None, method=None)
        plain, _ = authorize(srv, c, challenge="plainverifier", method="plain")
    assert code_from_error(none)["error"] == "invalid_request" and code_from_error(plain)["error"] == "invalid_request"


def code_from_error(resp):
    assert resp.status_code == 302
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(resp.headers["location"]).query))


def test_idp_refuses_missing_state_and_unknown_redirect(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:
        assert code_from_error(authorize(srv, c, state="")[0])["error"] == "invalid_request"
        r, _ = authorize(srv, c, redirect="http://evil.example/cb")
        assert r.status_code == 400 and "location" not in r.headers          # never redirects to an unregistered URI
        r, _ = authorize(srv, c, redirect="http://localhost:5173/evil")       # exact match only
        assert r.status_code == 400


def test_wrong_code_verifier_is_rejected_and_burns_the_code(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:
        r, v = authorize(srv, c)
        code = code_from(r)["code"]
        assert exchange(srv, c, code, b64url(secrets.token_bytes(32))).json()["error"] == "invalid_grant"
        assert exchange(srv, c, code, v).json()["error"] == "invalid_grant"   # even the right verifier is too late


def test_authorization_codes_are_single_use(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:
        r, v = authorize(srv, c)
        code = code_from(r)["code"]
        assert exchange(srv, c, code, v).status_code == 200
        assert exchange(srv, c, code, v).status_code == 400


def test_token_exchange_requires_the_same_redirect_uri(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:
        r, v = authorize(srv, c)
        assert exchange(srv, c, code_from(r)["code"], v, redirect="http://127.0.0.1:5173/").status_code == 400


def test_prompt_none_silent_renewal_needs_an_idp_session(make_idp):
    srv = make_idp()
    with httpx.Client(verify=False) as c:                                                     # cookie jar = the browser's IdP session
        assert code_from_error(authorize(srv, c, user="nobody", prompt="none")[0])["error"] == "login_required"
        r, _ = authorize(srv, c, user="priya")                                    # interactive login creates the session
        assert "code" in code_from(r)
        r, v = authorize(srv, c, user="nobody", prompt="none")                    # now silent renewal works
        assert "code" in code_from(r)


def test_cors_allows_only_registered_spa_origins(make_idp):
    srv = make_idp()
    ok = httpx.options(srv.issuer + "/token", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}, verify=False)
    bad = httpx.options(srv.issuer + "/token", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"}, verify=False)
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers


# ---------------------------------------------------------------- the API refuses bad tokens from an otherwise real IdP
def test_expired_long_lived_wrong_audience_and_wrong_issuer_tokens_are_refused(make_idp):
    srv = make_idp(access_ttl=-5)
    r = me(login(srv))
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    srv2 = make_idp(access_ttl=86400)
    r = me(login(srv2))
    assert r.status_code == 401 and "lifetime" in r.json()["detail"]
    srv3 = make_idp(audience_override="someone-elses-api")
    assert me(login(srv3)).status_code == 401
    srv4 = make_idp(iss_claim_override="https://evil.example.com")
    assert me(login(srv4)).status_code == 401


def test_tampered_and_foreign_signed_tokens_are_refused(make_idp):
    srv = make_idp()
    tok = login(srv)
    h, p, s = tok["access_token"].split(".")
    assert me({"access_token": f"{h}.{p}.{s[:-3]}AAA"}).status_code == 401
    kid = jwt.get_unverified_header(tok["access_token"])["kid"]
    payload = jwt.decode(tok["access_token"], options={"verify_signature": False})
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({**payload, "roles": ["admin"]}, foreign, algorithm="RS256", headers={"kid": kid})
    assert me({"access_token": forged}).status_code == 401              # right kid, wrong key


def test_token_with_no_roles_or_no_tenant_is_refused(make_idp):
    assert me(login(make_idp(omit_roles=True))).status_code == 403
    assert me(login(make_idp(tenant_claim="org_id"))).status_code == 403   # tenant claim not where we look


def test_key_rotation_is_picked_up_without_a_restart(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_JWKS_COOLDOWN": "0"})
    assert me(login(srv)).status_code == 200
    first_kid = srv.state.signing[0]
    new_kid = srv.state.rotate()
    assert new_kid != first_kid
    assert me(login(srv)).status_code == 200                           # unknown kid triggers a JWKS refetch


def test_forged_key_ids_cannot_make_the_api_hammer_the_idp(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_JWKS_COOLDOWN": "300"})
    assert me(login(srv)).status_code == 200
    hits = srv.state.jwks_hits
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    for i in range(8):
        t = jwt.encode({"iss": srv.issuer, "aud": "ripple-api", "sub": "x", "roles": ["admin"], "tenant": "demo", "iat": now, "exp": now + 60},
                       foreign, algorithm="RS256", headers={"kid": f"random-{i}"})
        assert me({"access_token": t}).status_code == 401
    assert srv.state.jwks_hits - hits <= 1                              # at most one refresh inside the cooldown


def test_a_token_signed_by_a_newly_rotated_key_is_refused_until_the_cooldown_allows_a_refetch(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_JWKS_COOLDOWN": "300"})
    assert me(login(srv)).status_code == 200
    srv.state.rotate()
    assert me(login(srv)).status_code == 401                            # documented tradeoff: fail closed, recover after cooldown


def test_symmetric_token_signed_with_the_public_key_is_refused(make_idp):
    srv = make_idp()
    import base64 as b64
    import hashlib
    import hmac
    import json
    from cryptography.hazmat.primitives import serialization
    pem = srv.state.signing[1].public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    now = int(time.time())
    enc = lambda o: b64url(json.dumps(o, separators=(",", ":")).encode())          # noqa: E731
    head = enc({"alg": "HS256", "typ": "JWT", "kid": srv.state.signing[0]})
    body = enc({"iss": srv.issuer, "aud": "ripple-api", "sub": "x", "roles": ["admin"], "tenant": "demo", "iat": now, "exp": now + 60})
    sig = b64url(hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest())   # PyJWT refuses to build this, an attacker will not
    assert me({"access_token": f"{head}.{body}.{sig}"}).status_code == 401              # algorithm-confusion attempt


# ---------------------------------------------------------------- real-world claim shapes
def test_groups_claim_with_role_map(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_ROLES_CLAIM": "groups", "RIPPLE_OIDC_ROLE_MAP": '{"Risk-Analysts":"analyst"}'},
                   roles_claim="groups", role_names={"analyst": "Risk-Analysts"})
    assert me(login(srv)).json()["roles"] == ["analyst"]


def test_unmapped_group_names_grant_nothing(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_ROLES_CLAIM": "groups"}, roles_claim="groups", role_names={"admin": "Everyone-Admins"})
    r = me(login(srv, "sam"))
    assert r.status_code == 403 and "no recognised role" in r.json()["detail"]      # an IdP group named like a role is not enough


def test_nested_roles_claim_like_keycloak(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_ROLES_CLAIM": "realm_access.roles"}, roles_claim="realm_access.roles")
    assert me(login(srv, "meena")).json()["roles"] == ["viewer"]


def test_custom_tenant_claim(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_TENANT_CLAIM": "org_id"}, tenant_claim="org_id", tenant="acme")
    assert me(login(srv)).json()["tenant"] == "acme"


# ---------------------------------------------------------------- discovery hardening
def test_discovery_issuer_mismatch_is_refused(make_idp):
    srv = make_idp(discovery_issuer_override="https://someone-else.example.com")
    r = me(login(srv))
    assert r.status_code == 503 and "issuer" in r.json()["detail"]
    assert anon().get("/api/auth/oidc-config").status_code == 503


def test_jwks_on_another_host_is_refused_unless_allowed(make_idp):
    srv = make_idp(jwks_host_override="localhost:{port}")
    r = me(login(srv))
    assert r.status_code == 503 and "allowed host" in r.json()["detail"]


def test_allowed_hosts_lets_endpoints_live_on_another_host(make_idp):
    srv = make_idp(env={"RIPPLE_OIDC_ALLOWED_HOSTS": "localhost"}, jwks_host_override="localhost:{port}",
                   authorization_host_override="localhost:{port}")
    assert me(login(srv)).status_code == 200
    cfg = anon().get("/api/auth/oidc-config").json()
    assert cfg["authorization_endpoint"].startswith(f"http://localhost:{srv.port}/")


def test_authorization_endpoint_on_another_host_is_refused_by_default(make_idp):
    make_idp(authorization_host_override="localhost:{port}")
    r = anon().get("/api/auth/oidc-config")
    assert r.status_code == 503 and "host" in r.json()["detail"]


def test_idp_that_does_not_advertise_s256_is_refused(make_idp):
    make_idp(advertise_pkce=["plain"])
    r = anon().get("/api/auth/oidc-config")
    assert r.status_code == 503 and "PKCE" in r.json()["detail"]


def test_plain_http_issuer_only_works_on_loopback_in_dev(make_idp, monkeypatch):
    srv = make_idp()
    tok = login(srv)
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    oidc._DISC_CACHE.clear()
    auth._JWKS_CACHE.clear()
    r = me(tok)
    assert r.status_code == 503 and "https" in r.json()["detail"]        # production: https only, even for loopback
    monkeypatch.setenv("RIPPLE_ENV", "dev")
    monkeypatch.setenv("RIPPLE_OIDC_ISSUER", "http://idp.example.com")   # dev: loopback only, not arbitrary http hosts
    oidc._DISC_CACHE.clear()
    auth._JWKS_CACHE.clear()
    assert me(tok).status_code in (401, 503)
    with pytest.raises(oidc.OidcError):
        oidc.check_url("http://idp.example.com", "OIDC issuer")


def test_unreachable_idp_fails_closed_with_503(make_idp, monkeypatch):
    srv = make_idp()
    tok = login(srv)
    srv.stop()
    auth._JWKS_CACHE.clear()
    oidc._DISC_CACHE.clear()
    assert me(tok).status_code == 503


# ---------------------------------------------------------------- audit, RBAC and the rest of the app work with real OIDC tokens
def test_first_use_of_an_oidc_token_is_one_audited_login(make_idp):
    srv = make_idp()
    tok = login(srv, "priya")
    before = len([e for e in get_security_log().entries if e.kind == "security.login" and e.code == "oidc"])
    for _ in range(3):
        assert me(tok).status_code == 200
    after = [e for e in get_security_log().entries if e.kind == "security.login" and e.code == "oidc"]
    assert len(after) == before + 1 and after[-1].data["user"] == "idp|priya" and after[-1].data["issuer"] == srv.issuer


def test_rbac_with_real_tokens_viewer_cannot_approve_analyst_can(make_idp):
    srv = make_idp()
    c = TestClient(app)                                                       # dev admin creates the case
    c.post("/api/session/reset", json={})
    case = c.post("/api/case/message", json={"text": "mera settlement kyun ruka hai? Tuesday ko supplier ko paisa dena hai."}).json()
    h = next(p for p in case["plan"] if p["tier"] == 3)["action"]["hash"]
    body = {"action_hash": h, "approve": True}
    assert anon().post("/api/case/decide", json=body, headers=bearer(login(srv, "meena"))).status_code == 403
    assert anon().post("/api/case/decide", json=body, headers=bearer(login(srv, "sam"))).status_code == 403
    r = anon().post("/api/case/decide", json=body, headers=bearer(login(srv, "priya")))
    assert r.status_code == 200
    issued = [e for e in c.get("/api/audit").json() if e["kind"] == "approval" and e["outcome"] == "issued"][0]
    assert issued["actor"]["sub"] == "idp|priya"                              # the audit names the SSO identity


def test_merchant_from_the_idp_is_scoped_to_its_own_case(make_idp):
    srv = make_idp()
    c = TestClient(app)
    sid = c.post("/api/session/reset", json={}).json()["session_id"]
    assert SESSIONS[sid].merchant_id == "M-34205"                            # the mock IdP's merchant owns this case
    ravi = bearer(login(srv, "ravi"))
    assert anon().post(f"/api/case/message?sid={sid}", json={"text": "kitna time lagega?"}, headers=ravi).status_code == 200
    other = c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    assert anon().get(f"/api/case?sid={other}", headers=ravi).status_code == 404
    assert anon().get("/api/cases", headers=ravi).status_code == 403


def test_kill_switch_applies_to_oidc_users(make_idp):
    srv = make_idp()
    c = TestClient(app)
    sid = c.post("/api/session/reset", json={}).json()["session_id"]
    c.post("/api/admin/killswitch", json={"scope": "user", "target": "idp|ravi", "on": True})
    ravi = bearer(login(srv, "ravi"))
    assert anon().post(f"/api/case/message?sid={sid}", json={"text": "hi"}, headers=ravi).status_code == 503


def test_dev_persona_tokens_and_oidc_tokens_coexist_only_in_dev(make_idp):
    srv = make_idp()
    assert me(login(srv)).status_code == 200
    assert anon().get("/api/auth/me", headers=ADMIN).status_code == 200
