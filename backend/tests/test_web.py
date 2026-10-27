"""Single-origin serving: the built SPA is served by the API with a strict CSP, safe static paths and no API fall-through."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import web
from app.security import auth, oidc
from tests.mock_idp import IdpConfig, IdpServer

INDEX = '<!doctype html><html><head><script type="module" src="/assets/app-abc123.js"></script></head><body><div id="root"></div></body></html>'


@pytest.fixture
def dist(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX)
    (d / "assets" / "app-abc123.js").write_text("console.log('ok')")
    (d / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("TOP-SECRET")           # sibling of dist: must never be reachable
    web.reset_csp_cache()
    yield d
    web.reset_csp_cache()


def spa_client(dist) -> TestClient:
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"pong": True}

    assert web.mount_spa(app, dist)
    return TestClient(app)


def test_no_build_means_nothing_is_mounted(tmp_path):
    app = FastAPI()
    assert web.mount_spa(app, tmp_path / "missing") is False
    assert TestClient(app).get("/").status_code == 404


def test_index_and_assets_are_served(dist):
    c = spa_client(dist)
    r = c.get("/")
    assert r.status_code == 200 and "root" in r.text
    assert c.get("/assets/app-abc123.js").text == "console.log('ok')"
    assert c.get("/favicon.svg").status_code == 200


def test_api_routes_still_win_and_unknown_api_paths_are_json_404_not_the_page(dist):
    c = spa_client(dist)
    assert c.get("/api/ping").json() == {"pong": True}
    r = c.get("/api/does-not-exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json") and "root" not in r.text
    assert c.get("/api").status_code == 404


def test_client_side_routes_fall_back_to_the_page_but_missing_files_do_not(dist):
    c = spa_client(dist)
    assert "root" in c.get("/cases/123").text
    assert c.get("/assets/missing.js").status_code == 404
    assert c.get("/nothing.png").status_code == 404


@pytest.mark.parametrize("path", [
    "/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "/%2e%2e%2fsecret.txt", "/assets/../../secret.txt",
    "/..\\secret.txt", "/%5c..%5csecret.txt", "/C:/Windows/win.ini", "/%2fetc/passwd", "/assets/%00.js", "//etc/passwd",
])
def test_path_traversal_never_reaches_files_outside_the_build(dist, path):
    c = spa_client(dist)
    r = c.get(path)
    assert "TOP-SECRET" not in r.text and "root:" not in r.text and "[fonts]" not in r.text.lower()
    assert r.status_code in (200, 404)                      # either the page (client route) or a clean 404
    if r.status_code == 200:
        assert r.text == INDEX


def test_a_symlink_pointing_outside_the_build_is_refused(dist, tmp_path):
    link = dist / "leak.txt"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    r = spa_client(dist).get("/leak.txt")
    assert "TOP-SECRET" not in r.text and r.status_code == 404


def test_only_get_and_head(dist):
    c = spa_client(dist)
    assert c.head("/").status_code == 200
    assert c.post("/").status_code == 405
    assert c.delete("/assets/app-abc123.js").status_code == 405


def test_page_csp_is_strict_and_assets_are_cached_forever_but_the_page_is_not(dist):
    c = spa_client(dist)
    page, asset = c.get("/"), c.get("/assets/app-abc123.js")
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp and "base-uri 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe" not in csp
    assert "style-src 'self';" in csp                       # no inline <style> blocks; attributes only via style-src-attr
    assert "connect-src 'self'" in csp and "*" not in csp
    assert page.headers["cache-control"] == "no-cache"
    assert "immutable" in asset.headers["cache-control"]


def test_csp_adds_exactly_the_idp_token_origin_from_validated_discovery(dist, monkeypatch):
    srv = IdpServer(IdpConfig()).start()
    try:
        monkeypatch.setenv("RIPPLE_ENV", "dev")                         # plain-http loopback issuer is dev-only
        monkeypatch.setenv("RIPPLE_OIDC_ISSUER", srv.issuer)
        monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", srv.cfg.audience)
        monkeypatch.setenv("RIPPLE_OIDC_CLIENT_ID", srv.cfg.client_id)
        monkeypatch.setattr(auth, "_jwks_client_factory", None)
        oidc._DISC_CACHE.clear()
        web.reset_csp_cache()
        csp = spa_client(dist).get("/").headers["content-security-policy"]
        origin = srv.issuer.rstrip("/")
        assert f"connect-src 'self' {origin};" in csp
        assert csp.count("http") == 1                                 # the token origin and nothing else
    finally:
        srv.stop()
        oidc._DISC_CACHE.clear()


def test_csp_falls_back_to_self_when_discovery_fails(dist, monkeypatch):
    monkeypatch.setenv("RIPPLE_OIDC_ISSUER", "https://idp.invalid.example")
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", "x")
    monkeypatch.setenv("RIPPLE_OIDC_CLIENT_ID", "spa")
    oidc._DISC_CACHE.clear()
    web.reset_csp_cache()
    csp = spa_client(dist).get("/").headers["content-security-policy"]
    assert "connect-src 'self';" in csp
    oidc._DISC_CACHE.clear()
    web.reset_csp_cache()


def test_the_real_app_sets_defensive_headers_on_api_responses():
    from app.main import app
    r = TestClient(app).get("/api/health")
    assert r.headers["content-security-policy"] == web.API_CSP
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_hsts_is_sent_in_production_mode_only(monkeypatch):
    from app.main import app
    c = TestClient(app)
    assert "strict-transport-security" not in c.get("/api/health").headers          # tests run in dev mode
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    assert "max-age=31536000" in c.get("/api/health").headers["strict-transport-security"]
