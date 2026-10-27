"""Hardening: tenant-scoped model cache, bounded in-memory cases, external audit-head anchoring, Cognito-style audience."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app import main
from app.audit import anchor
from app.audit.log import AuditLog
from app.llm.client import LLM
from tests.helpers import H


# ---------------------------------------------------------------- tenant-scoped model cache
class FakeClient:
    def __init__(self, reply):
        self.calls = 0
        self.reply = reply
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        blk = type("B", (), {"text": json.dumps(self.reply)})
        return type("R", (), {"content": [blk]})


def test_cached_model_output_is_never_served_across_tenants(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    path = tmp_path / "cache.json"
    a = LLM(mode="auto", cache_path=path, client=FakeClient({"who": "tenant-a"}), tenant="acme")
    assert a.complete_json("intake", "sys", "settlement not received") == ({"who": "tenant-a"}, "live")
    b_client = FakeClient({"who": "tenant-b"})
    b = LLM(mode="auto", cache_path=path, client=b_client, tenant="globex")
    data, src = b.complete_json("intake", "sys", "settlement not received")        # same text, same file, other tenant
    assert (data, src) == ({"who": "tenant-b"}, "live") and b_client.calls == 1
    again = LLM(mode="cache_only", cache_path=path, tenant="acme")
    assert again.complete_json("intake", "sys", "settlement not received") == ({"who": "tenant-a"}, "cache")
    assert LLM(mode="cache_only", cache_path=path, tenant="initech").complete_json("intake", "sys", "settlement not received") == (None, "none")


def test_sessions_pass_their_tenant_to_the_model_boundary():
    c = TestClient(main.app)
    sid = c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    assert main.SESSIONS[sid].agent.llm.tenant == "demo"
    c2 = TestClient(main.app)
    c2.headers.update(H("sam", "other-tenant"))
    other = c2.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    assert main.SESSIONS[other].agent.llm.tenant == "other-tenant"


# ---------------------------------------------------------------- bounded in-memory cases
def test_idle_cases_are_evicted_after_the_ttl_and_their_audit_files_go_too(monkeypatch):
    monkeypatch.setenv("RIPPLE_SESSION_TTL", "60")
    c = TestClient(main.app)
    sid = c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    path = main.SESSIONS[sid].audit_path
    assert path.exists()
    gone = main.evict_sessions(now=time.time() + 30)
    assert gone == [] and sid in main.SESSIONS                                    # not idle long enough
    gone = main.evict_sessions(now=time.time() + 120)
    assert gone == [sid] and sid not in main.SESSIONS and not path.exists()
    assert main.ACTIVE.get("demo") != sid
    assert c.get("/api/case", params={"sid": sid}).status_code == 404


def test_using_a_case_keeps_it_alive(monkeypatch):
    monkeypatch.setenv("RIPPLE_SESSION_TTL", "60")
    c = TestClient(main.app)
    sid = c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    main.SESSIONS[sid].touched = time.time() - 50
    assert c.get("/api/case", params={"sid": sid}).status_code == 200            # touches it
    assert main.evict_sessions(now=time.time() + 30) == []


def test_the_number_of_cases_is_capped_and_the_least_recently_used_go_first(monkeypatch):
    monkeypatch.setenv("RIPPLE_MAX_SESSIONS", "3")
    c = TestClient(main.app)
    ids = []
    for _ in range(5):
        ids.append(c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"])
        time.sleep(0.01)
    assert len(main.SESSIONS) == 3
    assert set(main.SESSIONS) == set(ids[-3:])                                    # oldest two evicted, newest kept


def test_the_newest_case_is_never_the_one_evicted_by_its_own_creation(monkeypatch):
    monkeypatch.setenv("RIPPLE_MAX_SESSIONS", "1")
    monkeypatch.setenv("RIPPLE_SESSION_TTL", "1")
    c = TestClient(main.app)
    sid = c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    assert sid in main.SESSIONS


# ---------------------------------------------------------------- audit head anchoring
def _log(tmp_path, n=5):
    log = AuditLog(tmp_path / "audit_x.jsonl", secret="s3cret")
    for i in range(n):
        log.add(datetime(2026, 1, 1, tzinfo=timezone.utc), "decision", case_id="C-1", outcome=f"o{i}")
    return log


@pytest.fixture
def file_anchor(tmp_path, monkeypatch):
    p = tmp_path / "anchors" / "heads.jsonl"
    monkeypatch.setenv("RIPPLE_AUDIT_ANCHOR", f"file:{p}")
    monkeypatch.delenv("RIPPLE_AUDIT_ANCHOR_EVERY", raising=False)
    anchor._CACHE = None
    yield p
    anchor._CACHE = None


def test_no_anchor_configured_is_reported_plainly(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_AUDIT_ANCHOR", raising=False)
    anchor._CACHE = None
    rep = _log(tmp_path).anchor_report()
    assert rep["configured"] is False and "truncating" in rep["note"]


def test_every_entry_is_anchored_and_an_untouched_log_checks_out(tmp_path, file_anchor):
    log = _log(tmp_path)
    lines = [json.loads(x) for x in file_anchor.read_text().splitlines()]
    assert [r["seq"] for r in lines] == [1, 2, 3, 4, 5] and all(r["log"] == log.log_id for r in lines)
    assert lines[-1]["head"] == log.entries[-1].mac
    rep = log.anchor_report()
    assert rep["check"]["ok"] is True and rep["check"]["anchors"] == 5 and rep["failures"] == 0
    assert "content" not in json.dumps(lines).lower() and "o3" not in json.dumps(lines)          # only name, number and keyed MAC


def test_cutting_the_end_off_the_log_is_detected_even_though_the_chain_still_verifies(tmp_path, file_anchor):
    log = _log(tmp_path)
    log.entries = log.entries[:3]                                                # attacker drops the newest entries
    assert log.verify()["ok"] is True                                             # the chain alone cannot see this
    chk = log.anchor_report()["check"]
    assert chk["ok"] is False and "cut off" in chk["reason"]


def test_replacing_the_log_with_a_freshly_computed_chain_is_detected(tmp_path, file_anchor):
    log = _log(tmp_path)
    forged = AuditLog(tmp_path / "forged.jsonl", secret="s3cret")                # same key, rewritten history
    for i in range(5):
        forged.add(datetime(2026, 1, 1, tzinfo=timezone.utc), "decision", case_id="C-1", outcome=f"x{i}")
    log.entries = forged.entries
    assert log.verify()["ok"] is True
    chk = log.anchor_report()["check"]
    assert chk["ok"] is False and "rewritten" in chk["reason"]


def test_anchor_every_n_publishes_sparsely_and_reports_the_unanchored_tail(tmp_path, file_anchor, monkeypatch):
    monkeypatch.setenv("RIPPLE_AUDIT_ANCHOR_EVERY", "2")
    anchor._CACHE = None
    log = _log(tmp_path, n=5)
    assert [json.loads(x)["seq"] for x in file_anchor.read_text().splitlines()] == [2, 4]
    chk = log.anchor_report()["check"]
    assert chk["ok"] is True and chk["unanchored_tail"] == 1


def test_each_process_run_has_its_own_chain_name_so_a_restart_is_not_mistaken_for_tampering(tmp_path, file_anchor):
    first = _log(tmp_path)
    second = AuditLog(tmp_path / "audit_x.jsonl", secret="s3cret")
    second.add(datetime(2026, 1, 1, tzinfo=timezone.utc), "decision", case_id="C-1")
    assert first.log_id != second.log_id
    assert second.anchor_report()["check"]["ok"] is True


def test_a_dead_sink_never_blocks_the_log_but_is_counted_and_shown(tmp_path, monkeypatch):
    class Dead:
        def publish(self, record):
            raise ConnectionError("down")
    monkeypatch.setenv("RIPPLE_AUDIT_ANCHOR", "https://collector.example/anchor")
    anchor._CACHE = None
    a = anchor.from_env()
    a.sink = Dead()
    log = _log(tmp_path, n=3)                                                     # no exception
    assert len(log.entries) == 3 and log.verify()["ok"]
    rep = log.anchor_report()
    assert rep["failures"] == 3 and rep["published"] == 0 and rep["last_error"] == "ConnectionError"
    anchor._CACHE = None


@pytest.mark.parametrize("spec", ["ftp://x", "s3://bucket/key", "/just/a/path"])
def test_malformed_anchor_settings_stop_startup_instead_of_silently_disabling(spec, monkeypatch):
    monkeypatch.setenv("RIPPLE_AUDIT_ANCHOR", spec)
    anchor._CACHE = None
    with pytest.raises(ValueError):
        anchor.from_env()
    anchor._CACHE = None


def test_http_sink_refuses_plain_http_outside_loopback_dev_and_redirects(monkeypatch):
    with pytest.raises(ValueError):
        anchor.HttpSink("http://collector.example/anchor")
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(ValueError):
        anchor.HttpSink("http://127.0.0.1:9/anchor")
    monkeypatch.setenv("RIPPLE_ENV", "dev")
    anchor.HttpSink("http://127.0.0.1:9/anchor")


def test_http_sink_posts_json_with_a_bearer_token_and_does_not_follow_redirects(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen = []

    class H_(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, self.headers.get("Authorization"), json.loads(body)))
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H_)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        anchor.HttpSink(base + "/ok", token="tok-1").publish({"log": "l", "seq": 1, "head": "h", "ts": "t"})
        assert seen[-1] == ("/ok", "Bearer tok-1", {"log": "l", "seq": 1, "head": "h", "ts": "t"})
        n = len(seen)
        with pytest.raises(Exception):
            anchor.HttpSink(base + "/redir").publish({"log": "l", "seq": 2, "head": "h", "ts": "t"})
        assert len(seen) == n + 1                                                 # the redirect target was never contacted
    finally:
        srv.shutdown()


def test_verify_endpoint_includes_the_anchor_report(file_anchor):
    c = TestClient(main.app)
    sid = c.post("/api/cases/new", json={"family": "status_query"}).json()["sid"]
    body = c.get("/api/audit/verify", params={"sid": sid}).json()
    assert body["anchor"]["configured"] is True and body["anchor"]["check"]["ok"] is True and body["anchor"]["published"] > 0
    assert c.get("/api/admin/security-log").json()["anchor"]["configured"] is True


# ---------------------------------------------------------------- Cognito-style audience (client_id, no aud)
from tests.mock_idp import IdpConfig, IdpServer  # noqa: E402
from app.security import auth, oidc  # noqa: E402
from tests.test_oidc_flow import REDIRECT, authorize, code_from, exchange  # noqa: E402


@pytest.fixture
def cognito(monkeypatch):
    srv = IdpServer(IdpConfig(cognito_style=True)).start()
    monkeypatch.setenv("RIPPLE_OIDC_ISSUER", srv.issuer)
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", srv.cfg.client_id)                 # Cognito: the app client id
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE_CLAIM", "client_id")
    monkeypatch.setenv("RIPPLE_OIDC_CLIENT_ID", srv.cfg.client_id)
    monkeypatch.setattr(auth, "_jwks_client_factory", None)
    auth._JWKS_CACHE.clear()
    oidc._DISC_CACHE.clear()
    yield srv
    srv.stop()
    auth._JWKS_CACHE.clear()
    oidc._DISC_CACHE.clear()


def _login(srv):
    import httpx
    with httpx.Client(verify=False) as c:
        r, v = authorize(srv, c)
        return exchange(srv, c, code_from(r)["code"], v).json()


def _me(tok):
    c = TestClient(main.app)
    c.headers.pop("Authorization", None)
    return c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})


def test_cognito_style_access_token_without_aud_is_accepted_in_client_id_mode(cognito):
    tokens = _login(cognito)
    assert "aud" not in jwt.decode(tokens["access_token"], options={"verify_signature": False})
    r = _me(tokens["access_token"])
    assert r.status_code == 200 and r.json()["roles"] == ["analyst"]


def test_the_same_token_is_refused_in_default_aud_mode(cognito, monkeypatch):
    tokens = _login(cognito)
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE_CLAIM", "aud")
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", "ripple-api")
    r = _me(tokens["access_token"])
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid token: MissingRequiredClaimError"        # standard aud mode: no aud, no entry


def test_id_tokens_and_other_apps_tokens_are_refused_in_client_id_mode(cognito):
    tokens = _login(cognito)
    assert _me(tokens["id_token"]).status_code == 401                              # no client_id, token_use=id
    other = cognito.state.sign({"iss": cognito.issuer, "sub": "idp|priya", "client_id": "someone-elses-app", "token_use": "access",
                                "iat": int(time.time()), "exp": int(time.time()) + 300, "roles": ["analyst"], "tenant": "demo"})
    assert _me(other).status_code == 401
    wrong_use = cognito.state.sign({"iss": cognito.issuer, "sub": "idp|priya", "client_id": cognito.cfg.client_id, "token_use": "id",
                                    "iat": int(time.time()), "exp": int(time.time()) + 300, "roles": ["analyst"], "tenant": "demo"})
    assert _me(wrong_use).status_code == 401


def test_client_id_mode_still_checks_signature_issuer_and_expiry(cognito):
    now = int(time.time())
    base = {"iss": cognito.issuer, "sub": "idp|priya", "client_id": cognito.cfg.client_id, "token_use": "access",
            "iat": now, "exp": now + 300, "roles": ["analyst"], "tenant": "demo"}
    assert _me(cognito.state.sign(base)).status_code == 200
    assert _me(cognito.state.sign({**base, "exp": now - 10, "iat": now - 400})).status_code == 401
    assert _me(cognito.state.sign({**base, "iss": "https://evil.example"})).status_code == 401
    forged = jwt.encode(base, "k" * 40, algorithm="HS256")
    assert _me(forged).status_code == 401


def test_audience_claim_setting_is_validated(monkeypatch):
    monkeypatch.setenv("RIPPLE_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", "x")
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE_CLAIM", "azp")
    with pytest.raises(oidc.OidcError):
        oidc.oidc_settings()
