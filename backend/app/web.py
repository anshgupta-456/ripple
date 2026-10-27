"""Single-origin production serving: the API also serves the built single-page app.

Why one origin: the browser app and the API share an origin, so CORS stays closed (no allowed origins in production) and the
Content-Security-Policy can be strict. The policy allows scripts and styles from this origin only; the one extra place the page may
talk to is the identity provider's token endpoint, taken from the validated discovery document, never from the request.
"""
from __future__ import annotations

import os
import time
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import ROOT

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
_CSP_TTL = 60.0
_csp_cache: tuple[float, str] | None = None


def static_dir() -> Path:
    return Path(os.environ.get("RIPPLE_STATIC_DIR") or ROOT / "frontend" / "dist")


def _token_origin() -> str | None:
    """Origin of the IdP token endpoint, or None when browser sign-in is off or discovery fails (then 'self' only)."""
    from app.security import oidc
    try:
        st = oidc.oidc_settings()
        if st is None or not st.client_id:
            return None
        ep = oidc.browser_config(st)["token_endpoint"]
    except Exception:
        return None
    u = urllib.parse.urlparse(ep)
    return f"{u.scheme}://{u.netloc}" if u.scheme and u.netloc else None


def page_csp() -> str:
    global _csp_cache
    now = time.time()
    if _csp_cache and _csp_cache[0] > now:
        return _csp_cache[1]
    connect = "'self'" + (f" {o}" if (o := _token_origin()) else "")
    csp = "; ".join([
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "style-src-attr 'unsafe-inline'",      # React style={{...}} attributes only; no inline <style> blocks or scripts
        "img-src 'self' data:",
        "font-src 'self'",
        f"connect-src {connect}",
        "base-uri 'none'",
        "form-action 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    ])
    _csp_cache = (now + _CSP_TTL, csp)
    return csp


def reset_csp_cache() -> None:
    global _csp_cache
    _csp_cache = None


def _resolve(root: Path, rel: str) -> Path | None:
    """Map a URL path to a file under root, or None. Anything that resolves outside root is refused."""
    try:
        cand = (root / rel).resolve()
        if cand != root and not cand.is_relative_to(root):
            return None
        return cand if cand.is_file() else None
    except (OSError, ValueError):                 # NUL bytes, illegal names
        return None


def mount_spa(app: FastAPI, dist: Path | None = None) -> bool:
    """Register the catch-all that serves the built app. Call after every API route is registered. Returns False when there is no build."""
    root = (dist or static_dir()).resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(path: str, request: Request) -> Response:
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)         # unknown API paths never fall back to the page
        f = _resolve(root, path) if path else None
        if f is None:
            if "." in path.rsplit("/", 1)[-1]:                                    # looks like a file and is missing
                return JSONResponse({"detail": "not found"}, status_code=404)
            f = index                                                              # client-side route
        headers = {"Content-Security-Policy": page_csp()} if f == index else {}
        if f.parent.name == "assets":
            headers["Cache-Control"] = "public, max-age=31536000, immutable"       # content-hashed file names
        else:
            headers["Cache-Control"] = "no-cache"
        return FileResponse(f, headers=headers)

    return True
