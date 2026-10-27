"""Secrets-manager adapters: HashiCorp Vault (KV v2) and AWS Secrets Manager.

Enable one with RIPPLE_SECRETS_BACKEND=vault|aws. The adapter is authoritative in app/security/secret_store.py (values are
cached for RIPPLE_SECRETS_TTL seconds so rotation needs no restart). Secret NAMES are the env-style names the app asks for
(RIPPLE_AUDIT_KEY, ANTHROPIC_API_KEY, ...) and are validated before they touch a URL or an API call.

Vault   RIPPLE_VAULT_ADDR (https; loopback http only in RIPPLE_ENV=dev), RIPPLE_VAULT_AUTH = approle | kubernetes | token_file
        (| token, dev only), RIPPLE_VAULT_MOUNT (secret), RIPPLE_VAULT_PREFIX (ripple), RIPPLE_VAULT_FIELD (value),
        RIPPLE_VAULT_NAMESPACE, RIPPLE_VAULT_CACERT, and per method:
          approle     RIPPLE_VAULT_ROLE_ID + RIPPLE_VAULT_SECRET_ID_FILE
          kubernetes  RIPPLE_VAULT_K8S_ROLE (+ RIPPLE_VAULT_K8S_JWT_FILE, default the service-account token)
          token_file  RIPPLE_VAULT_TOKEN_FILE (a Vault Agent sink; re-read on every use, so renewals are picked up)
        Tokens are short-lived by construction: AppRole/Kubernetes logins are renewed before expiry, a rejected token triggers
        exactly one re-login. A static token in an environment variable is refused outside dev. Redirects are never followed
        (the token header must not travel to another host), responses are size-capped, TLS verification cannot be disabled.
AWS     RIPPLE_AWS_SM_PREFIX (ripple/), RIPPLE_AWS_REGION. Credentials come ONLY from the standard provider chain (IAM role,
        IRSA, instance profile): the code never accepts or stores keys. Needs only secretsmanager:GetSecretValue on
        arn:aws:secretsmanager:<region>:<account>:secret:<prefix>*. RIPPLE_AWS_SM_ENDPOINT (LocalStack/moto) is dev + loopback only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from app.security import secret_store
from app.security.secret_store import SecretBackendError, track
from app.security.settings import is_dev

log = logging.getLogger("ripple.secrets")

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_BYTES = 1_000_000
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
TOKEN_SKEW = 30.0           # renew a Vault token this many seconds before it expires


def _check_addr(url: str, what: str) -> None:
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        return
    if p.scheme == "http" and is_dev() and (p.hostname or "") in LOOPBACK:
        return
    raise RuntimeError(f"{what} must be https (plain http is allowed only for loopback in RIPPLE_ENV=dev)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


# ===================================================================== Vault
class VaultBackend:
    def __init__(self, addr: str, *, auth: str, mount: str = "secret", prefix: str = "ripple", field: str = "value",
                 namespace: str | None = None, ca_cert: str | None = None, auth_mount: str | None = None,
                 role_id: str | None = None, secret_id_file: str | None = None, k8s_role: str | None = None,
                 k8s_jwt_file: str | None = None, token_file: str | None = None, static_token: str | None = None,
                 timeout: float = 3.0):
        addr = addr.rstrip("/")
        _check_addr(addr, "RIPPLE_VAULT_ADDR")
        if auth not in ("approle", "kubernetes", "token_file", "token"):
            raise RuntimeError("RIPPLE_VAULT_AUTH must be approle, kubernetes or token_file")
        if auth == "token" and not (is_dev() and static_token):
            raise RuntimeError("a static Vault token in the environment is refused outside RIPPLE_ENV=dev; use approle, "
                               "kubernetes or a Vault Agent token_file (short-lived, renewed)")
        if auth == "approle" and not (role_id and secret_id_file):
            raise RuntimeError("approle auth needs RIPPLE_VAULT_ROLE_ID and RIPPLE_VAULT_SECRET_ID_FILE")
        if auth == "kubernetes" and not k8s_role:
            raise RuntimeError("kubernetes auth needs RIPPLE_VAULT_K8S_ROLE")
        if auth == "token_file" and not token_file:
            raise RuntimeError("token_file auth needs RIPPLE_VAULT_TOKEN_FILE")
        self.addr, self.auth, self.field, self.timeout = addr, auth, field, timeout
        self.mount, self.prefix = mount.strip("/"), prefix.strip("/")
        self.namespace, self.auth_mount = namespace, (auth_mount or ("kubernetes" if auth == "kubernetes" else "approle"))
        self.role_id, self.secret_id_file = role_id, secret_id_file
        self.k8s_role = k8s_role
        self.k8s_jwt_file = k8s_jwt_file or "/var/run/secrets/kubernetes.io/serviceaccount/token"
        self.token_file, self.static_token = token_file, static_token
        self._token: str | None = None
        self._token_exp = 0.0
        ctx = ssl.create_default_context(cafile=ca_cert) if (addr.startswith("https") and ca_cert) else None   # verification stays ON
        self._opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx)) if ctx \
            else urllib.request.build_opener(_NoRedirect)
        self.logins = 0

    @classmethod
    def from_env(cls) -> "VaultBackend":
        addr = _env("RIPPLE_VAULT_ADDR", "VAULT_ADDR")
        if not addr:
            raise RuntimeError("RIPPLE_SECRETS_BACKEND=vault needs RIPPLE_VAULT_ADDR")
        return cls(addr, auth=_env("RIPPLE_VAULT_AUTH") or "approle", mount=_env("RIPPLE_VAULT_MOUNT") or "secret",
                   prefix=_env("RIPPLE_VAULT_PREFIX") or "ripple", field=_env("RIPPLE_VAULT_FIELD") or "value",
                   namespace=_env("RIPPLE_VAULT_NAMESPACE", "VAULT_NAMESPACE") or None, ca_cert=_env("RIPPLE_VAULT_CACERT", "VAULT_CACERT") or None,
                   auth_mount=_env("RIPPLE_VAULT_AUTH_MOUNT") or None, role_id=_env("RIPPLE_VAULT_ROLE_ID") or None,
                   secret_id_file=_env("RIPPLE_VAULT_SECRET_ID_FILE") or None, k8s_role=_env("RIPPLE_VAULT_K8S_ROLE") or None,
                   k8s_jwt_file=_env("RIPPLE_VAULT_K8S_JWT_FILE") or None, token_file=_env("RIPPLE_VAULT_TOKEN_FILE") or None,
                   static_token=_env("RIPPLE_VAULT_TOKEN") or None)

    # ---- http ----
    def _http(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Vault-Token"] = token
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        req = urllib.request.Request(self.addr + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:                      # noqa: S310 (scheme validated)
                status, raw = r.status, r.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:                                              # includes 3xx: redirects are not followed
            status, raw = e.code, (e.read(MAX_BYTES + 1) if e.fp else b"")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise SecretBackendError(f"vault unreachable ({type(getattr(e, 'reason', e)).__name__})")
        if len(raw) > MAX_BYTES:
            raise SecretBackendError("vault response too large")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        return status, payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_file(path: str, what: str) -> str:
        try:
            v = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            raise SecretBackendError(f"cannot read {what}")
        if not v:
            raise SecretBackendError(f"{what} is empty")
        track(v)
        return v

    # ---- auth ----
    def _login(self) -> str:
        if self.auth == "approle":
            body = {"role_id": self.role_id, "secret_id": self._read_file(self.secret_id_file or "", "the AppRole secret id file")}
        else:
            body = {"role": self.k8s_role, "jwt": self._read_file(self.k8s_jwt_file, "the Kubernetes service-account token")}
        status, p = self._http("POST", f"/v1/auth/{self.auth_mount}/login", body)
        auth = p.get("auth") if isinstance(p.get("auth"), dict) else None
        if status != 200 or not auth or not auth.get("client_token"):
            raise SecretBackendError(f"vault login failed (HTTP {status})")
        self.logins += 1
        tok = str(auth["client_token"])
        track(tok)
        lease = float(auth.get("lease_duration") or 0)
        self._token, self._token_exp = tok, secret_store._now() + (lease if lease > 0 else 300.0)
        return tok

    def _get_token(self, force: bool = False) -> str:
        if self.auth == "token":
            return self.static_token or ""
        if self.auth == "token_file":
            return self._read_file(self.token_file or "", "the Vault token file")      # re-read: the agent renews it
        if not force and self._token and self._token_exp - TOKEN_SKEW > secret_store._now():
            return self._token
        return self._login()

    def check(self) -> None:
        """Startup probe: fail fast if the backend cannot authenticate."""
        status, _ = self._http("GET", "/v1/auth/token/lookup-self", token=self._get_token())
        if status != 200:
            raise SecretBackendError(f"vault token check failed (HTTP {status})")

    # ---- read ----
    def __call__(self, name: str) -> str | None:
        if not NAME_RE.match(name):
            return None                                                                  # never let a name shape a URL
        path = "/v1/{}/data/{}/{}".format(urllib.parse.quote(self.mount), "/".join(urllib.parse.quote(s) for s in self.prefix.split("/") if s), name)
        status, p = 0, {}
        for attempt in (0, 1):
            status, p = self._http("GET", path, token=self._get_token(force=attempt == 1))
            if status == 403 and attempt == 0 and self.auth != "token":
                self._token = None                                                       # revoked/expired early: log in once more
                continue
            break
        if status == 404:
            return None
        if status == 403:
            raise SecretBackendError("vault denied access (check the token's policy for this path)")
        if status != 200:
            raise SecretBackendError(f"vault returned HTTP {status}")
        data = (p.get("data") or {}).get("data") if isinstance(p.get("data"), dict) else None
        if not isinstance(data, dict):
            return None
        val = data.get(self.field, data.get(name))
        if val is None:
            return None
        if not isinstance(val, str):
            raise SecretBackendError("secret value is not a string")
        return val


# ===================================================================== AWS Secrets Manager
class AwsSecretsManagerBackend:
    def __init__(self, *, prefix: str = "ripple/", region: str | None = None, endpoint_url: str | None = None,
                 json_field: str = "value", client: Any = None):
        self.prefix, self.json_field = prefix, json_field
        if endpoint_url:
            _check_addr(endpoint_url, "RIPPLE_AWS_SM_ENDPOINT")
            if not is_dev():
                raise RuntimeError("RIPPLE_AWS_SM_ENDPOINT is for local mocks and is refused outside RIPPLE_ENV=dev")
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise RuntimeError("RIPPLE_SECRETS_BACKEND=aws needs boto3 (pip install boto3)")
            client = boto3.client("secretsmanager", region_name=region, endpoint_url=endpoint_url,
                                  config=Config(retries={"max_attempts": 2, "mode": "standard"}, connect_timeout=3, read_timeout=3))
        self._client = client

    @classmethod
    def from_env(cls) -> "AwsSecretsManagerBackend":
        return cls(prefix=_env("RIPPLE_AWS_SM_PREFIX") or "ripple/", region=_env("RIPPLE_AWS_REGION", "AWS_REGION", "AWS_DEFAULT_REGION") or None,
                   endpoint_url=_env("RIPPLE_AWS_SM_ENDPOINT") or None, json_field=_env("RIPPLE_AWS_SM_FIELD") or "value")

    def __call__(self, name: str) -> str | None:
        if not NAME_RE.match(name):
            return None
        try:
            resp = self._client.get_secret_value(SecretId=self.prefix + name)
        except Exception as e:                                                          # botocore raises many shapes; classify below
            code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            if code == "ResourceNotFoundException":
                return None
            raise SecretBackendError(f"aws secrets manager error ({code or type(e).__name__})")
        raw = resp.get("SecretString")
        if raw is None:
            raise SecretBackendError("binary secrets are not supported")
        try:
            doc = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(doc, dict):                                                       # JSON secret: {"value": "..."} or {"NAME": "..."}
            v = doc.get(self.json_field, doc.get(name))
            if v is None:
                return None
            if not isinstance(v, str):
                raise SecretBackendError("secret value is not a string")
            return v
        return raw


# ===================================================================== wiring
def configure_from_env() -> str | None:
    """Register the backend selected by RIPPLE_SECRETS_BACKEND. A misconfigured backend raises: the app refuses to start
    rather than quietly running on weaker sources."""
    kind = _env("RIPPLE_SECRETS_BACKEND").lower()
    if kind in ("", "env"):
        return None
    if kind == "vault":
        backend: Callable[[str], str | None] = VaultBackend.from_env()
        try:
            backend.check()                                                              # type: ignore[attr-defined]
        except SecretBackendError as e:
            raise RuntimeError(f"secrets backend 'vault' is configured but unusable: {e}")
    elif kind in ("aws", "aws-sm", "aws_secrets_manager"):
        backend = AwsSecretsManagerBackend.from_env()
    else:
        raise RuntimeError(f"unknown RIPPLE_SECRETS_BACKEND '{kind}' (use vault or aws)")
    secret_store.clear_backends()
    secret_store.register_backend(backend)
    log.info("secrets backend configured: %s", kind)
    return kind
