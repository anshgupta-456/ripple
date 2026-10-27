"""Secret loading. Secrets come from a secrets manager (Vault / AWS Secrets Manager, see app/security/backends.py), a mounted
secret file (NAME_FILE, as Docker/K8s secrets do), or the environment. They are never hardcoded, never logged (a log filter
scrubs every loaded value and every backend credential), and never put in prompts.

Precedence: a configured secrets backend is authoritative, then NAME_FILE, then the environment. Values from a backend or a
file are cached for a short TTL so a rotated secret is picked up without a restart; misses and backend outages are cached
only briefly so an outage does not become a request storm. A backend FAILURE never silently weakens anything: the secret is
looked up in the next source, and if it is required and absent the error says the backend failed.

In dev only, a missing secret may fall back to an ephemeral random value that dies with the process.
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import time
from pathlib import Path
from typing import Callable

from app.security.settings import is_dev

log = logging.getLogger("ripple.secrets")

SECRETS_TTL = float(os.environ.get("RIPPLE_SECRETS_TTL", "300"))
MISS_TTL = 30.0           # a secret a backend does not have
ERROR_TTL = 5.0           # a backend that is failing

_now: Callable[[], float] = time.monotonic        # tests replace this to move time

_BACKENDS: list[Callable[[str], str | None]] = []
_LOADED: dict[str, tuple[str, float | None]] = {}        # name -> (value, expires_at or None for process-lifetime)
_MISS: dict[str, float] = {}
_TRACKED: set[str] = set()                               # backend credentials (tokens) that must never appear in logs
_EPHEMERAL: dict[str, str] = {}                          # dev-only random values; never returned outside RIPPLE_ENV=dev


class SecretBackendError(Exception):
    """The secrets manager could not be used (unreachable, auth failed, access denied, bad response). Not 'not found'."""


def register_backend(fn: Callable[[str], str | None], *, first: bool = True) -> None:
    """Adapter point for a real secrets manager: fn(name) -> value, None if the secret does not exist; raises
    SecretBackendError when the manager itself failed."""
    _BACKENDS.insert(0, fn) if first else _BACKENDS.append(fn)


def clear_backends() -> None:
    _BACKENDS.clear()


def track(value: str | None) -> None:
    """Register a credential (e.g. a Vault token) so the log scrubber removes it from any message."""
    if value:
        _TRACKED.add(value)


def get_secret(name: str, *, required: bool = True, dev_ephemeral: bool = False) -> str | None:
    now = _now()
    hit = _LOADED.get(name)
    if hit and (hit[1] is None or hit[1] > now):
        return hit[0]
    val: str | None = None
    ttl: float | None = None
    backend_failed = False

    if _BACKENDS and _MISS.get(name, 0) <= now:
        for b in _BACKENDS:
            try:
                val = b(name)
            except SecretBackendError as e:
                backend_failed = True
                log.warning("secrets backend %s failed for a lookup: %s", type(b).__name__ if not hasattr(b, "__self__") else type(b.__self__).__name__, e)
                continue
            if val:
                ttl = SECRETS_TTL
                break
        if not val:
            _MISS[name] = now + (ERROR_TTL if backend_failed else MISS_TTL)

    if not val and os.environ.get(name + "_FILE"):
        try:
            val = Path(os.environ[name + "_FILE"]).read_text(encoding="utf-8").strip()
            ttl = SECRETS_TTL
        except OSError:
            val = None
    if not val:
        val = os.environ.get(name)
        ttl = None
    if not val and dev_ephemeral and is_dev():
        return _EPHEMERAL.setdefault(name, _secrets.token_urlsafe(48))   # ephemeral: restart invalidates anything derived from it
    if not val:
        if required:
            hint = " (the secrets backend failed, see logs)" if backend_failed else ""
            raise RuntimeError(f"secret {name} is not configured (set {name}, {name}_FILE, or configure RIPPLE_SECRETS_BACKEND){hint}")
        return None
    _LOADED[name] = (val, None if ttl is None else now + ttl)
    return val


def forget_all() -> None:
    """Drop cached REAL secrets (tests / forced rotation). Ephemeral dev keys live separately and only ever apply in dev."""
    _LOADED.clear()
    _MISS.clear()


def mask(value: str | None) -> str:
    return "***" if value else ""


def scrub(msg: str) -> str:
    """Replace every loaded secret value and tracked backend credential with ***."""
    for v in [v for v, _ in _LOADED.values()] + list(_EPHEMERAL.values()) + list(_TRACKED):
        if v and v in msg:
            msg = msg.replace(v, "***")
    return msg


class SecretScrubFilter(logging.Filter):
    """Scrubs a record. NOTE: a filter on the root *logger* does not see records from child loggers; the process-wide
    protection is the record factory installed by install_log_scrubber()."""

    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True


def _scrub_record(record: logging.LogRecord) -> None:
    try:
        msg = record.getMessage()
    except Exception:
        return
    clean = scrub(msg)
    if clean != msg:
        record.msg, record.args = clean, ()


_FACTORY_INSTALLED = False


def install_log_scrubber() -> None:
    """Install a log-record factory so EVERY record from EVERY logger (uvicorn, libraries, our code) is scrubbed at creation."""
    global _FACTORY_INSTALLED
    if _FACTORY_INSTALLED:
        return
    base = logging.getLogRecordFactory()

    def factory(*a, **k):
        record = base(*a, **k)
        _scrub_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _FACTORY_INSTALLED = True
