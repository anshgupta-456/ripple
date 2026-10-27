"""Secret loading. Secrets come from the environment, a mounted secret file (NAME_FILE, as Docker/K8s secrets do),
or a registered secrets-manager backend (Vault / AWS Secrets Manager / KMS adapters plug in via register_backend).
They are never hardcoded, never logged (a log filter scrubs every loaded value), and never put in prompts.

In dev only, a missing secret may fall back to an ephemeral random value that dies with the process.
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
from pathlib import Path
from typing import Callable

from app.security.settings import is_dev

_BACKENDS: list[Callable[[str], str | None]] = []
_LOADED: dict[str, str] = {}
_EPHEMERAL: dict[str, str] = {}          # dev-only random values; never returned outside RIPPLE_ENV=dev


def register_backend(fn: Callable[[str], str | None]) -> None:
    """Adapter point for a real secrets manager: fn(name) -> value or None."""
    _BACKENDS.append(fn)


def get_secret(name: str, *, required: bool = True, dev_ephemeral: bool = False) -> str | None:
    if name in _LOADED:
        return _LOADED[name]
    val = os.environ.get(name)
    if not val and os.environ.get(name + "_FILE"):
        val = Path(os.environ[name + "_FILE"]).read_text(encoding="utf-8").strip()
    if not val:
        for b in _BACKENDS:
            val = b(name)
            if val:
                break
    if not val and dev_ephemeral and is_dev():
        return _EPHEMERAL.setdefault(name, _secrets.token_urlsafe(48))   # ephemeral: restart invalidates anything derived from it
    if not val:
        if required:
            raise RuntimeError(f"secret {name} is not configured (set {name}, {name}_FILE, or register a secrets backend)")
        return None
    _LOADED[name] = val
    return val


def forget_all() -> None:
    """Drop cached REAL secrets (tests / rotation). Ephemeral dev keys live separately and only ever apply in dev."""
    _LOADED.clear()


def mask(value: str | None) -> str:
    return "***" if value else ""


class SecretScrubFilter(logging.Filter):
    """Replaces any loaded secret value that ends up in a log message with ***."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for v in list(_LOADED.values()) + list(_EPHEMERAL.values()):
            if v and v in msg:
                msg = msg.replace(v, "***")
                record.msg, record.args = msg, ()
        return True


def install_log_scrubber() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, SecretScrubFilter) for f in root.filters):
        root.addFilter(SecretScrubFilter())
        for h in root.handlers:
            h.addFilter(SecretScrubFilter())
