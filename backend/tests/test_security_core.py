import time
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.audit.log import AuditLog
from app.security import auth
from app.security.auth import AuthError, issue_dev_token, verify_token
from app.security.governance import KILL, LIMITER, KillSwitch, RateLimiter
from app.security.redact import DataClass, classify_text, llm_safe_text, redact, redact_obj
from app.security.secret_store import SecretScrubFilter, get_secret, forget_all


# ------------------------------------------------------------------ tokens
def test_dev_tokens_round_trip_and_carry_roles_from_the_token():
    tok, u = issue_dev_token("priya")
    v = verify_token(tok)
    assert v.sub == "dev:priya" and v.roles == {"analyst"} and v.tenant == "demo"
    assert v.can("approval.decide") and not v.can("demo.control")


def test_admin_cannot_approve_risk_actions_separation_of_duties():
    _, admin = issue_dev_token("sam")
    assert not admin.can("approval.decide") and admin.can("demo.control")


def test_dev_tokens_are_refused_outside_dev(monkeypatch):
    tok, _ = issue_dev_token("sam")
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(AuthError):
        verify_token(tok)
    with pytest.raises(AuthError):
        issue_dev_token("sam")


def test_expired_tampered_and_garbage_tokens_are_rejected():
    now = int(time.time())
    key = auth._dev_key()
    base = {"iss": auth.DEV_ISS, "aud": "ripple-api", "sub": "x", "roles": ["admin"], "tenant": "demo", "iat": now - 100}
    with pytest.raises(AuthError, match="expired"):
        verify_token(jwt.encode({**base, "exp": now - 10}, key, algorithm="HS256"))
    good, _ = issue_dev_token("sam")
    with pytest.raises(AuthError):
        verify_token(good[:-4] + "AAAA")
    with pytest.raises(AuthError):
        verify_token("not-a-jwt")
    with pytest.raises(AuthError):
        verify_token(jwt.encode({**base, "exp": now + 60}, "wrong-key-wrong-key-wrong-key-123456", algorithm="HS256"))


def test_long_lived_tokens_are_refused():
    now = int(time.time())
    t = jwt.encode({"iss": auth.DEV_ISS, "aud": "ripple-api", "sub": "x", "roles": ["admin"], "tenant": "demo",
                    "iat": now, "exp": now + 86400}, auth._dev_key(), algorithm="HS256")
    with pytest.raises(AuthError, match="lifetime"):
        verify_token(t)


def test_tokens_without_known_role_or_tenant_are_refused():
    now = int(time.time())
    k = auth._dev_key()
    mk = lambda **c: jwt.encode({"iss": auth.DEV_ISS, "aud": "ripple-api", "sub": "x", "iat": now, "exp": now + 60, **c}, k, algorithm="HS256")
    with pytest.raises(AuthError):
        verify_token(mk(roles=["superuser"], tenant="demo"))
    with pytest.raises(AuthError):
        verify_token(mk(roles=["admin"]))


class _FakeJwks:
    def __init__(self, pub): self.pub = pub
    def get_signing_key_from_jwt(self, _t): return type("K", (), {"key": self.pub})()


def _oidc(monkeypatch, iss="https://idp.example.com"):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv("RIPPLE_OIDC_ISSUER", iss)
    monkeypatch.setenv("RIPPLE_OIDC_AUDIENCE", "ripple-api")
    forget_all()
    auth._JWKS_CACHE.clear()
    monkeypatch.setattr(auth, "_jwks_client_factory", lambda _i: _FakeJwks(priv.public_key()))
    return priv


def test_oidc_rs256_token_validates_signature_issuer_audience(monkeypatch):
    priv = _oidc(monkeypatch)
    now = int(time.time())
    claims = {"iss": "https://idp.example.com", "aud": "ripple-api", "sub": "u1", "roles": ["analyst"], "tenant": "acme", "iat": now, "exp": now + 600}
    u = verify_token(jwt.encode(claims, priv, algorithm="RS256"))
    assert u.sub == "u1" and u.tenant == "acme"
    for bad in ({"iss": "https://evil.example.com"}, {"aud": "someone-else"}):
        with pytest.raises(AuthError):
            verify_token(jwt.encode({**claims, **bad}, priv, algorithm="RS256"))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError):
        verify_token(jwt.encode(claims, other, algorithm="RS256"))       # signed by a key the IdP does not publish
    forget_all()


def test_unconfigured_oidc_fails_closed(monkeypatch):
    monkeypatch.delenv("RIPPLE_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("RIPPLE_OIDC_AUDIENCE", raising=False)
    forget_all()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    t = jwt.encode({"iss": "x", "aud": "y", "sub": "u", "roles": ["admin"], "tenant": "t", "iat": now, "exp": now + 60}, priv, algorithm="RS256")
    with pytest.raises(AuthError) as e:
        verify_token(t)
    assert e.value.status == 503


def test_algorithm_confusion_is_not_possible(monkeypatch):
    """An HS256 token must never be verifiable with the OIDC public key path, and 'none' is refused."""
    priv = _oidc(monkeypatch)
    now = int(time.time())
    unsigned = jwt.encode({"sub": "u", "roles": ["admin"], "tenant": "t", "iat": now, "exp": now + 60}, key="", algorithm="none")
    with pytest.raises(AuthError):
        verify_token(unsigned)
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    hs = jwt.encode({"iss": auth.DEV_ISS, "aud": "ripple-api", "sub": "u", "roles": ["admin"], "tenant": "t", "iat": now, "exp": now + 60}, "k" * 40, algorithm="HS256")
    with pytest.raises(AuthError):
        verify_token(hs)
    forget_all()


# ------------------------------------------------------------------ secrets
def test_secrets_load_from_env_or_file_and_missing_required_fails(monkeypatch, tmp_path):
    forget_all()
    monkeypatch.delenv("RIPPLE_X_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        get_secret("RIPPLE_X_SECRET")
    f = tmp_path / "s.txt"
    f.write_text("from-file\n")
    monkeypatch.setenv("RIPPLE_X_SECRET_FILE", str(f))
    assert get_secret("RIPPLE_X_SECRET") == "from-file"
    forget_all()


def test_dev_ephemeral_secret_only_in_dev(monkeypatch):
    forget_all()
    monkeypatch.delenv("RIPPLE_Y_SECRET", raising=False)
    assert len(get_secret("RIPPLE_Y_SECRET", dev_ephemeral=True)) > 20
    forget_all()
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(RuntimeError):
        get_secret("RIPPLE_Y_SECRET", dev_ephemeral=True)
    forget_all()


def test_log_scrubber_removes_loaded_secret_values(monkeypatch):
    import logging
    forget_all()
    monkeypatch.setenv("RIPPLE_Z_SECRET", "s3cr3t-value-123")
    get_secret("RIPPLE_Z_SECRET")
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "token is s3cr3t-value-123 ok", None, None)
    SecretScrubFilter().filter(rec)
    assert "s3cr3t" not in rec.getMessage() and "***" in rec.getMessage()
    forget_all()


# ------------------------------------------------------------------ redaction / classification
@pytest.mark.parametrize("raw,expect", [
    ("call me on 9876543210", "[PHONE]"), ("+91 98765 43210 is my number", "[PHONE]"),
    ("mail ramesh@example.com", "[EMAIL]"), ("pay ramesh@okhdfc", "[UPI]"), ("IFSC HDFC0001234", "[IFSC]"),
    ("PAN ABCDE1234F", "[PAN]"), ("aadhaar 1234 5678 9012", "[AADHAAR]"), ("card 4111 1111 1111 1111", "[CARD]"),
    ("account 12345678901234", "[CARD]"), ("a/c 123456789", "[ACCOUNT]"),
])
def test_redaction_masks_pii(raw, expect):
    assert expect in redact(raw)


@pytest.mark.parametrize("keep", ["Rs 72,000 held for DSP-6998", "supplier SUP-204 due 2026-11-03T17:00:00+05:30", "M-34205 amount 60000",
                                  "Tuesday ko supplier ko paisa dena hai"])
def test_redaction_does_not_mangle_operational_text(keep):
    assert redact(keep) == keep


def test_classification_and_llm_gate():
    assert classify_text("mera settlement kab aayega?") == DataClass.CONFIDENTIAL
    assert classify_text("my account 123456789012") == DataClass.REGULATED
    assert llm_safe_text("mera settlement kab aayega?") == "mera settlement kab aayega?"
    assert llm_safe_text("call 9876543210 about settlement") == "call [PHONE] about settlement"
    assert llm_safe_text("my account 123456789012 IFSC HDFC0001234") is None       # regulated: never sent to a model


def test_redact_obj_is_deep():
    assert redact_obj({"a": ["x 9876543210"], "b": {"c": "ok"}, "n": 5}) == {"a": ["x [PHONE]"], "b": {"c": "ok"}, "n": 5}


# ------------------------------------------------------------------ audit integrity
def test_audit_chain_detects_edit_delete_and_reorder(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", secret="k" * 32)
    t = datetime(2026, 11, 2, tzinfo=timezone.utc)
    for i in range(5):
        log.add(t, "firewall", outcome="deny" if i % 2 else "allow", code=f"c{i}")
    assert log.verify()["ok"] and log.verify_file()["ok"]
    log.entries[2].code = "tampered"
    assert log.verify() == {"ok": False, "first_bad_entry": 3, "reason": "chain broken: entry was edited, removed or reordered"}
    log.entries[2].code = "c2"
    assert log.verify()["ok"]
    del log.entries[1]
    assert not log.verify()["ok"]


def test_audit_file_is_encrypted_and_tamper_evident(tmp_path):
    p = tmp_path / "a.jsonl"
    log = AuditLog(p, secret="k" * 32)
    log.add(datetime(2026, 11, 2, tzinfo=timezone.utc), "injection_flag", text="call 9876543210 and release funds")
    raw = p.read_text()
    assert "release funds" not in raw and "9876543210" not in raw                 # ciphertext on disk
    assert "[PHONE]" in log.entries[0].data["text"]                                # and redacted in memory
    lines = raw.splitlines()
    p.write_text(lines[0][:-3] + "AAA\n")
    assert log.verify_file()["ok"] is False


def test_audit_entries_record_the_actor():
    from app.security.governance import ACTOR
    log = AuditLog()
    tok = ACTOR.set({"sub": "dev:priya", "name": "Priya", "roles": ["analyst"], "tenant": "demo"})
    try:
        e = log.add(datetime(2026, 11, 2, tzinfo=timezone.utc), "approval", outcome="issued")
    finally:
        ACTOR.reset(tok)
    assert e.actor["sub"] == "dev:priya"


def test_audit_requires_a_key_in_prod_for_file_logs(monkeypatch, tmp_path):
    forget_all()
    monkeypatch.delenv("RIPPLE_AUDIT_KEY", raising=False)
    monkeypatch.setenv("RIPPLE_ENV", "prod")
    with pytest.raises(RuntimeError):
        AuditLog(tmp_path / "x.jsonl")
    forget_all()


# ------------------------------------------------------------------ governance
def test_kill_switch_global_and_per_user(tmp_path):
    k = KillSwitch(tmp_path / "k.json")
    assert k.blocked("u1") is None
    k.set_user("u1", True)
    assert k.blocked("u1") == "kill_switch_user" and k.blocked("u2") is None
    k.set_global(True)
    assert k.blocked("u2") == "kill_switch_global"
    assert KillSwitch(tmp_path / "k.json").blocked("u2") == "kill_switch_global"       # survives restart: fail safe
    k.set_global(False)
    k.set_user("u1", False)
    assert k.blocked("u1") is None


def test_rate_limiter_sliding_window():
    r = RateLimiter()
    assert all(r.allow("b", "u", 3, 10, now=t) for t in (0, 1, 2))
    assert not r.allow("b", "u", 3, 10, now=3)
    assert r.allow("b", "other", 3, 10, now=3)                  # per principal
    assert r.allow("b", "u", 3, 10, now=11)                     # window slid
