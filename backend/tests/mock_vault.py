"""A local mock HashiCorp Vault for tests (KV v2 + AppRole + Kubernetes auth + token lookup). TEST CODE ONLY.

Faithful to the behaviours the adapter depends on: tokens expire and can be revoked, policies are path-scoped, a missing secret
is 404, a deleted version is 404, redirects are possible (the client must not follow them), and every request is recorded so
tests can assert how many logins/reads happened and which headers were sent. Optional TLS for verifying CA handling.
"""
from __future__ import annotations

import datetime
import ipaddress
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


class VaultState:
    def __init__(self) -> None:
        self.kv: dict[str, dict[str, Any]] = {}        # "ripple/NAME" -> fields
        self.deleted: set[str] = set()
        self.approle = {"role-1": "secret-id-1"}       # role_id -> secret_id
        self.k8s = {"ripple-role": "k8s-jwt-1"}        # role -> accepted service-account jwt
        self.static_tokens = {"root-dev-token": "ripple/"}    # token -> allowed path prefix
        self.tokens: dict[str, dict[str, Any]] = {}    # issued tokens -> {exp, prefix}
        self.token_ttl = 3600
        self.allowed_prefix = "ripple/"
        self.log: list[dict[str, Any]] = []
        self.logins = 0
        self.redirect_to: str | None = None
        self.huge = False
        self.mount = "secret"

    def put(self, name: str, value: Any, prefix: str = "ripple", field: str = "value") -> None:
        self.kv[f"{prefix}/{name}"] = {field: value} if not isinstance(value, dict) else value

    def reads(self) -> int:
        return len([r for r in self.log if r["method"] == "GET" and "/data/" in r["path"]])

    def revoke_all(self) -> None:
        self.tokens.clear()
        self.static_tokens.clear()


def create_app(st: VaultState) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def record(request: Request, call_next):
        st.log.append({"method": request.method, "path": request.url.path, "token": request.headers.get("x-vault-token"),
                       "namespace": request.headers.get("x-vault-namespace")})
        return await call_next(request)

    def _issue(prefix: str) -> JSONResponse:
        tok = "s." + secrets.token_urlsafe(16)
        st.tokens[tok] = {"exp": time.time() + st.token_ttl, "prefix": prefix}
        st.logins += 1
        return JSONResponse({"auth": {"client_token": tok, "lease_duration": st.token_ttl, "renewable": True}})

    @app.post("/v1/auth/approle/login")
    async def approle(request: Request):
        b = await request.json()
        if st.approle.get(b.get("role_id")) == b.get("secret_id"):
            return _issue(st.allowed_prefix)
        return JSONResponse({"errors": ["invalid role or secret ID"]}, status_code=400)

    @app.post("/v1/auth/kubernetes/login")
    async def kube(request: Request):
        b = await request.json()
        if st.k8s.get(b.get("role")) == b.get("jwt"):
            return _issue(st.allowed_prefix)
        return JSONResponse({"errors": ["service account name not authorized"]}, status_code=403)

    def _token_prefix(request: Request) -> str | None:
        t = request.headers.get("x-vault-token", "")
        if t in st.static_tokens:
            return st.static_tokens[t]
        rec = st.tokens.get(t)
        if rec and rec["exp"] > time.time():
            return rec["prefix"]
        return None

    @app.get("/v1/auth/token/lookup-self")
    def lookup(request: Request):
        return {"data": {"policies": ["ripple"]}} if _token_prefix(request) is not None else JSONResponse({"errors": ["permission denied"]}, status_code=403)

    @app.get("/v1/{mount}/data/{path:path}")
    def read(mount: str, path: str, request: Request):
        prefix = _token_prefix(request)
        if prefix is None or mount != st.mount or not path.startswith(prefix):
            return JSONResponse({"errors": ["permission denied"]}, status_code=403)
        if st.redirect_to:
            return RedirectResponse(st.redirect_to, status_code=307)
        if st.huge:
            return Response(b"x" * 2_000_000, media_type="application/json")
        if path in st.deleted or path not in st.kv:
            return JSONResponse({"errors": []}, status_code=404)
        return {"data": {"data": st.kv[path], "metadata": {"version": 1}}}

    @app.get("/{rest:path}")
    def catch_all(rest: str):                              # lets a second instance act as the redirect target / spy
        return JSONResponse({"errors": ["not found"]}, status_code=404)

    return app


def make_tls_files(dirpath: Path) -> tuple[Path, Path, Path]:
    """Create a throwaway CA and a server certificate for 127.0.0.1/localhost. Returns (ca.pem, server.pem, server.key)."""
    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def name(cn: str):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, srv_key = key(), key()
    ca = (x509.CertificateBuilder().subject_name(name("test-ca")).issuer_name(name("test-ca")).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
          .not_valid_after(now + datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    srv = (x509.CertificateBuilder().subject_name(name("127.0.0.1")).issuer_name(ca.subject).public_key(srv_key.public_key())
           .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
           .not_valid_after(now + datetime.timedelta(days=1))
           .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), critical=False)
           .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
           .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
           .add_extension(x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False)
           .sign(ca_key, hashes.SHA256()))
    ca_p, crt_p, key_p = dirpath / "ca.pem", dirpath / "server.pem", dirpath / "server.key"
    ca_p.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    crt_p.write_bytes(srv.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(srv_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return ca_p, crt_p, key_p


class VaultServer:
    def __init__(self, tls: tuple[Path, Path] | None = None):
        self.state = VaultState()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.scheme = "https" if tls else "http"
        kw: dict[str, Any] = {"ssl_certfile": str(tls[0]), "ssl_keyfile": str(tls[1])} if tls else {}
        self._server = uvicorn.Server(uvicorn.Config(create_app(self.state), host="127.0.0.1", port=self.port, log_level="warning", **kw))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def addr(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def start(self) -> "VaultServer":
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("mock vault did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)
