"""Vault and AWS Secrets Manager adapters, tested against a local mock Vault (real HTTP, optional TLS) and moto (real boto3)."""
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from app.audit.log import AuditLog, _keys
from app.security import secret_store
from app.security.backends import AwsSecretsManagerBackend, VaultBackend, configure_from_env
from app.security.secret_store import SecretBackendError, get_secret
from tests.mock_vault import VaultServer, make_tls_files
from tests.test_oidc_flow import anon, bearer, login, make_idp, me  # noqa: F401  (fixture + helpers reused for the combined test)

N1, N2 = "RIPPLE_T_ONE", "RIPPLE_T_TWO"


@pytest.fixture(autouse=True)
def clean_store(monkeypatch):
    secret_store.clear_backends()
    secret_store.forget_all()
    for k in list(os.environ):
        if k.startswith(("RIPPLE_VAULT", "RIPPLE_AWS", "RIPPLE_SECRETS_BACKEND")) or k in (N1, N2, N1 + "_FILE"):
            monkeypatch.delenv(k, raising=False)
    yield
    secret_store.clear_backends()
    secret_store.forget_all()


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(secret_store, "_now", lambda: t[0])
    return t


@pytest.fixture
def vault():
    v = VaultServer().start()
    yield v
    v.stop()


def approle(vault, tmp_path, **kw) -> VaultBackend:
    sid = tmp_path / "secret_id"
    sid.write_text("secret-id-1\n")
    return VaultBackend(vault.addr, auth="approle", role_id="role-1", secret_id_file=str(sid), **kw)


# ================================================================= Vault: reading and auth
def test_approle_login_then_kv2_read(vault, tmp_path):
    vault.state.put(N1, "hunter2-from-vault")
    b = approle(vault, tmp_path)
    assert b(N1) == "hunter2-from-vault"
    reads = [r for r in vault.state.log if "/data/" in r["path"]]
    assert reads[0]["path"] == f"/v1/secret/data/ripple/{N1}" and reads[0]["token"].startswith("s.")
    assert vault.state.logins == 1 and b.logins == 1


def test_missing_secret_is_none_and_deleted_version_is_none(vault, tmp_path):
    b = approle(vault, tmp_path)
    assert b(N1) is None
    vault.state.put(N2, "x")
    vault.state.deleted.add(f"ripple/{N2}")
    assert b(N2) is None


def test_field_conventions(vault, tmp_path):
    vault.state.put(N1, {"value": "a", "other": "b"})
    vault.state.put(N2, {N2: "named-field"})
    b = approle(vault, tmp_path)
    assert b(N1) == "a" and b(N2) == "named-field"


def test_namespace_header_is_sent(vault, tmp_path):
    vault.state.put(N1, "v")
    approle(vault, tmp_path, namespace="team-a")(N1)
    assert {r["namespace"] for r in vault.state.log if "/data/" in r["path"]} == {"team-a"}


def test_names_are_validated_before_they_touch_a_url(vault, tmp_path):
    b = approle(vault, tmp_path)
    for bad in ("../../sys/seal", "RIPPLE_X/../Y", "lower_case", "A" * 80, "", "A B"):
        assert b(bad) is None
    assert vault.state.reads() == 0 and vault.state.logins == 0


def test_non_string_values_are_rejected(vault, tmp_path):
    vault.state.put(N1, {"value": 12345})
    with pytest.raises(SecretBackendError, match="not a string"):
        approle(vault, tmp_path)(N1)


def test_oversized_responses_are_refused(vault, tmp_path):
    vault.state.huge = True
    with pytest.raises(SecretBackendError, match="too large"):
        approle(vault, tmp_path)(N1)


# ================================================================= Vault: token lifecycle
def test_cached_then_rotated_without_restart(vault, tmp_path, clock):
    vault.state.put(N1, "v1")
    secret_store.register_backend(approle(vault, tmp_path))
    assert get_secret(N1) == "v1"
    reads = vault.state.reads()
    assert get_secret(N1) == "v1" and vault.state.reads() == reads                 # served from the TTL cache
    vault.state.put(N1, "v2")                                                      # operator rotates the secret
    assert get_secret(N1) == "v1"                                                  # still inside the TTL
    clock[0] += 301
    assert get_secret(N1) == "v2" and vault.state.reads() == reads + 1             # picked up after the TTL, no restart


def test_short_lived_tokens_are_renewed_before_they_expire(vault, tmp_path, clock):
    vault.state.token_ttl = 100
    vault.state.put(N1, "v")
    secret_store.register_backend(approle(vault, tmp_path))
    get_secret(N1)
    assert vault.state.logins == 1
    clock[0] += 301                                                                # cache and token both stale
    vault.state.put(N1, "v-new")
    assert get_secret(N1) == "v-new" and vault.state.logins == 2                   # logged in again, not reusing an expired token


def test_a_revoked_token_triggers_exactly_one_relogin(vault, tmp_path, clock):
    vault.state.put(N1, "v1")
    secret_store.register_backend(approle(vault, tmp_path))
    get_secret(N1)
    vault.state.tokens.clear()                                                     # Vault revoked it early
    vault.state.put(N1, "v2")
    clock[0] += 301
    assert get_secret(N1) == "v2" and vault.state.logins == 2


def test_a_policy_that_denies_the_path_is_an_error_not_a_miss(vault, tmp_path, clock, monkeypatch):
    vault.state.put(N1, "v", prefix="other")
    secret_store.register_backend(approle(vault, tmp_path, prefix="other"))        # token policy only covers ripple/
    with pytest.raises(RuntimeError, match="backend failed"):
        get_secret(N1)                                                             # required: says the backend failed
    assert get_secret(N1, required=False) is None
    monkeypatch.setenv(N1, "from-env")
    clock[0] += 10
    assert get_secret(N1) == "from-env"                                            # fallback only because env is configured


def test_wrong_approle_credentials_fail_to_log_in(vault, tmp_path):
    sid = tmp_path / "bad"
    sid.write_text("wrong")
    b = VaultBackend(vault.addr, auth="approle", role_id="role-1", secret_id_file=str(sid))
    with pytest.raises(SecretBackendError, match="login failed"):
        b(N1)


def test_kubernetes_auth_uses_the_service_account_token_file(vault, tmp_path):
    vault.state.put(N1, "k8s-secret")
    jwt_file = tmp_path / "token"
    jwt_file.write_text("k8s-jwt-1")
    b = VaultBackend(vault.addr, auth="kubernetes", k8s_role="ripple-role", k8s_jwt_file=str(jwt_file))
    assert b(N1) == "k8s-secret"
    jwt_file.write_text("forged")
    b2 = VaultBackend(vault.addr, auth="kubernetes", k8s_role="ripple-role", k8s_jwt_file=str(jwt_file))
    with pytest.raises(SecretBackendError, match="login failed"):
        b2(N1)


def test_token_file_is_reread_on_every_use_so_agent_renewals_work(vault, tmp_path):
    vault.state.put(N1, "v")
    f = tmp_path / "agent-token"
    f.write_text("not-a-valid-token")
    b = VaultBackend(vault.addr, auth="token_file", token_file=str(f))
    with pytest.raises(SecretBackendError, match="denied"):
        b(N1)
    f.write_text("root-dev-token")                                                 # the Vault Agent renewed the sink file
    assert b(N1) == "v"


def test_static_token_only_in_dev(vault, monkeypatch):
    vault.state.put(N1, "v")
    assert VaultBackend(vault.addr, auth="token", static_token="root-dev-token")(N1) == "v"
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(RuntimeError, match="static Vault token"):
        VaultBackend("https://vault.example.com:8200", auth="token", static_token="x")


def test_constructor_validation():
    with pytest.raises(RuntimeError, match="https"):
        VaultBackend("http://vault.example.com:8200", auth="token_file", token_file="/x")     # http to a non-loopback host
    with pytest.raises(RuntimeError, match="approle"):
        VaultBackend("https://v.example.com", auth="approle")
    with pytest.raises(RuntimeError, match="kubernetes"):
        VaultBackend("https://v.example.com", auth="kubernetes")
    with pytest.raises(RuntimeError, match="RIPPLE_VAULT_AUTH"):
        VaultBackend("https://v.example.com", auth="userpass")


def test_plain_http_is_refused_in_production_even_for_loopback(vault, monkeypatch):
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(RuntimeError, match="https"):
        VaultBackend(vault.addr, auth="token_file", token_file="/x")


# ================================================================= Vault: transport safety
def test_redirects_are_never_followed_so_the_token_cannot_travel(vault, tmp_path):
    spy = VaultServer().start()
    try:
        vault.state.put(N1, "v")
        vault.state.redirect_to = f"{spy.addr}/steal"
        with pytest.raises(SecretBackendError):
            approle(vault, tmp_path)(N1)
        assert [r for r in spy.state.log if r["path"] == "/steal"] == []             # nothing ever reached the other host
    finally:
        spy.stop()


def test_tls_is_verified_against_the_configured_ca_and_cannot_be_skipped(tmp_path):
    ca, crt, key = make_tls_files(tmp_path)
    v = VaultServer(tls=(crt, key)).start()
    try:
        v.state.put(N1, "over-tls")
        sid = tmp_path / "sid"
        sid.write_text("secret-id-1")
        good = VaultBackend(v.addr, auth="approle", role_id="role-1", secret_id_file=str(sid), ca_cert=str(ca))
        assert good(N1) == "over-tls"
        no_ca = VaultBackend(v.addr, auth="approle", role_id="role-1", secret_id_file=str(sid))
        with pytest.raises(SecretBackendError, match="SSLCertVerificationError|SSLError|unreachable"):
            no_ca(N1)                                                                  # unknown CA: refused, never silently trusted
    finally:
        v.stop()


def test_vault_down_falls_back_without_hammering(vault, tmp_path, clock, monkeypatch):
    vault.state.put(N1, "v")
    b = approle(vault, tmp_path)
    vault.stop()
    secret_store.register_backend(b)
    assert get_secret(N1, required=False) is None                                      # outage is not a crash for optional secrets
    n = len(vault.state.log)
    for _ in range(5):
        assert get_secret(N1, required=False) is None
    assert len(vault.state.log) == n                                                    # error cached briefly: no request storm
    monkeypatch.setenv(N1, "from-env")
    assert get_secret(N1) == "from-env"


def test_misses_are_negative_cached_briefly(vault, tmp_path, clock):
    secret_store.register_backend(approle(vault, tmp_path))
    assert get_secret(N2, required=False) is None
    reads = vault.state.reads()
    for _ in range(5):
        assert get_secret(N2, required=False) is None
    assert vault.state.reads() == reads
    clock[0] += 31
    vault.state.put(N2, "now-exists")
    assert get_secret(N2) == "now-exists"


# ================================================================= scrubbing
def test_fetched_secrets_and_vault_tokens_never_reach_any_logger(vault, tmp_path, caplog):
    from app.security.secret_store import install_log_scrubber
    install_log_scrubber()
    vault.state.put(N1, "super-secret-value-9f3a")
    secret_store.register_backend(approle(vault, tmp_path))
    assert get_secret(N1) == "super-secret-value-9f3a"
    token = next(r["token"] for r in vault.state.log if r["token"])
    import io
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("some.library.deep.in.the.stack")       # a CHILD logger: root-logger filters would miss this
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.info("calling with token %s and value super-secret-value-9f3a", token)
    lg.removeHandler(h)
    out = buf.getvalue()
    assert "super-secret-value-9f3a" not in out and token not in out and "***" in out


# ================================================================= AWS Secrets Manager (real boto3 against moto)
@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SECURITY_TOKEN": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-east-1")


def test_aws_plain_json_and_named_field_secrets(aws):
    aws.create_secret(Name=f"ripple/{N1}", SecretString="plain-aws")
    aws.create_secret(Name="ripple/RIPPLE_T_JSON", SecretString=json.dumps({"value": "json-aws", "x": 1}))
    aws.create_secret(Name="ripple/RIPPLE_T_NAMED", SecretString=json.dumps({"RIPPLE_T_NAMED": "named-aws"}))
    b = AwsSecretsManagerBackend(region="us-east-1")
    assert b(N1) == "plain-aws" and b("RIPPLE_T_JSON") == "json-aws" and b("RIPPLE_T_NAMED") == "named-aws"


def test_aws_missing_binary_and_bad_names(aws):
    aws.create_secret(Name=f"ripple/{N2}", SecretBinary=b"\x00\x01")
    b = AwsSecretsManagerBackend(region="us-east-1")
    assert b("RIPPLE_T_ABSENT") is None
    with pytest.raises(SecretBackendError, match="binary"):
        b(N2)
    assert b("../other/secret") is None and b("lower") is None


def test_aws_access_denied_is_an_error_not_a_miss():
    class Denied:
        def get_secret_value(self, **kw):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:... is not authorized"}}, "GetSecretValue")
    b = AwsSecretsManagerBackend(client=Denied())
    with pytest.raises(SecretBackendError) as e:
        b(N1)
    assert "AccessDeniedException" in str(e.value) and "arn:aws" not in str(e.value)       # code only, not the verbose message


def test_aws_values_are_cached_and_rotation_is_picked_up(aws, clock):
    aws.create_secret(Name=f"ripple/{N1}", SecretString="v1")
    calls = []
    b = AwsSecretsManagerBackend(region="us-east-1")
    real = b._client.get_secret_value
    b._client.get_secret_value = lambda **kw: (calls.append(kw["SecretId"]), real(**kw))[1]
    secret_store.register_backend(b)
    assert get_secret(N1) == "v1" and get_secret(N1) == "v1" and calls == [f"ripple/{N1}"]
    aws.put_secret_value(SecretId=f"ripple/{N1}", SecretString="v2")
    clock[0] += 301
    assert get_secret(N1) == "v2" and len(calls) == 2


def test_aws_custom_prefix_and_endpoint_rules(aws, monkeypatch):
    aws.create_secret(Name=f"prod/ripple/{N1}", SecretString="scoped")
    assert AwsSecretsManagerBackend(prefix="prod/ripple/", region="us-east-1")(N1) == "scoped"
    with pytest.raises(RuntimeError, match="https"):
        AwsSecretsManagerBackend(endpoint_url="http://secrets.example.com", region="us-east-1")   # not loopback
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(RuntimeError, match="outside RIPPLE_ENV=dev|https"):
        AwsSecretsManagerBackend(endpoint_url="http://127.0.0.1:5000", region="us-east-1")


def test_the_adapter_never_accepts_aws_keys():
    import inspect
    params = set(inspect.signature(AwsSecretsManagerBackend.__init__).parameters)
    assert params == {"self", "prefix", "region", "endpoint_url", "json_field", "client"}      # no key/credential parameters exist


# ================================================================= wiring: configure_from_env and fail-closed startup
def test_configure_vault_from_env_registers_it_as_authoritative(vault, tmp_path, monkeypatch):
    vault.state.put(N1, "via-env-config")
    sid = tmp_path / "sid"
    sid.write_text("secret-id-1")
    for k, v in {"RIPPLE_SECRETS_BACKEND": "vault", "RIPPLE_VAULT_ADDR": vault.addr, "RIPPLE_VAULT_AUTH": "approle",
                 "RIPPLE_VAULT_ROLE_ID": "role-1", "RIPPLE_VAULT_SECRET_ID_FILE": str(sid)}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(N1, "env-value-should-lose")
    assert configure_from_env() == "vault"
    assert get_secret(N1) == "via-env-config"                                          # the secrets manager outranks the environment


def test_configure_aws_from_env(aws, monkeypatch):
    aws.create_secret(Name=f"ripple/{N1}", SecretString="aws-via-env")
    monkeypatch.setenv("RIPPLE_SECRETS_BACKEND", "aws")
    monkeypatch.setenv("RIPPLE_AWS_REGION", "us-east-1")
    assert configure_from_env() == "aws"
    assert get_secret(N1) == "aws-via-env"


def test_configure_rejects_unknown_and_unusable_backends(monkeypatch):
    assert configure_from_env() is None
    monkeypatch.setenv("RIPPLE_SECRETS_BACKEND", "env")
    assert configure_from_env() is None
    monkeypatch.setenv("RIPPLE_SECRETS_BACKEND", "keyring")
    with pytest.raises(RuntimeError, match="unknown"):
        configure_from_env()
    monkeypatch.setenv("RIPPLE_SECRETS_BACKEND", "vault")
    with pytest.raises(RuntimeError, match="RIPPLE_VAULT_ADDR"):
        configure_from_env()


def test_unreachable_vault_stops_startup(tmp_path, monkeypatch):
    sid = tmp_path / "sid"
    sid.write_text("secret-id-1")
    for k, v in {"RIPPLE_SECRETS_BACKEND": "vault", "RIPPLE_VAULT_ADDR": "http://127.0.0.1:1", "RIPPLE_VAULT_AUTH": "approle",
                 "RIPPLE_VAULT_ROLE_ID": "role-1", "RIPPLE_VAULT_SECRET_ID_FILE": str(sid)}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="configured but unusable"):
        configure_from_env()


def test_the_app_refuses_to_start_with_a_broken_secrets_backend(tmp_path):
    sid = tmp_path / "sid"
    sid.write_text("secret-id-1")
    base = {**os.environ, "RIPPLE_ENV": "dev", "RIPPLE_SECRETS_BACKEND": "vault", "RIPPLE_VAULT_ADDR": "http://127.0.0.1:1",
            "RIPPLE_VAULT_AUTH": "approle", "RIPPLE_VAULT_ROLE_ID": "role-1", "RIPPLE_VAULT_SECRET_ID_FILE": str(sid)}
    run = lambda env: subprocess.run([sys.executable, "-W", "ignore", "-c", "import app.main"], cwd=Path(__file__).resolve().parents[1],   # noqa: E731
                                     env=env, capture_output=True, text=True, timeout=90)
    r = run(base)                                                                        # complete config, Vault unreachable
    assert r.returncode != 0 and "secrets backend 'vault' is configured but unusable: vault unreachable" in r.stderr
    r = run({**base, "RIPPLE_SECRETS_BACKEND": "keyring"})                              # unknown backend
    assert r.returncode != 0 and "unknown RIPPLE_SECRETS_BACKEND" in r.stderr
    r = run({k: v for k, v in base.items() if k != "RIPPLE_SECRETS_BACKEND"})           # control: no backend configured starts fine
    assert r.returncode == 0, r.stderr


# ================================================================= both features together
def test_vault_supplies_the_audit_key_while_users_sign_in_through_the_oidc_flow(vault, tmp_path, make_idp, monkeypatch):
    """Sign in via authorization code + PKCE; the session's audit log is keyed from Vault; the key never leaks."""
    vault.state.put("RIPPLE_AUDIT_KEY", "audit-key-held-only-in-vault-0123456789")
    sid_file = tmp_path / "sid"
    sid_file.write_text("secret-id-1")
    for k, v in {"RIPPLE_SECRETS_BACKEND": "vault", "RIPPLE_VAULT_ADDR": vault.addr, "RIPPLE_VAULT_AUTH": "approle",
                 "RIPPLE_VAULT_ROLE_ID": "role-1", "RIPPLE_VAULT_SECRET_ID_FILE": str(sid_file)}.items():
        monkeypatch.setenv(k, v)
    assert configure_from_env() == "vault"
    srv = make_idp()
    admin = bearer(login(srv, "sam"))
    assert me({"access_token": admin["Authorization"].split()[1]}).json()["roles"] == ["admin"]
    from fastapi.testclient import TestClient
    from app.main import SESSIONS, app
    c = TestClient(app)
    c.headers.update(admin)
    sid = c.post("/api/session/reset", json={}).json()["session_id"]
    c.post(f"/api/case/message?sid={sid}", json={"text": "mera settlement kyun ruka hai? Tuesday ko supplier ko paisa dena hai."})
    # the audit file decrypts with the key derived from the VAULT value, and verifies
    path = SESSIONS[sid].audit_path
    _, enc = _keys("audit-key-held-only-in-vault-0123456789")
    from cryptography.fernet import Fernet
    first = json.loads(Fernet(enc).decrypt(path.read_text().splitlines()[0].encode()))
    assert first["kind"] and c.get(f"/api/audit/verify?sid={sid}").json()["file"]["ok"] is True
    assert "audit-key-held-only-in-vault" not in path.read_text()
    assert vault.state.logins >= 1
