"""Process-wide security audit log: authentication/authorization denials, kill-switch changes, deletions, impersonation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.audit.log import AuditLog
from app.security.settings import data_dir

_LOG: AuditLog | None = None


def get_security_log() -> AuditLog:
    global _LOG
    if _LOG is None:
        try:
            _LOG = AuditLog(data_dir() / "security_audit.jsonl")
        except RuntimeError:            # audit key missing in prod: keep events in memory rather than dropping them
            _LOG = AuditLog()
    return _LOG


def reset_security_log() -> None:
    global _LOG
    _LOG = None


def security_event(kind: str, user: Any | None = None, outcome: str = "", code: str = "", **data: Any) -> None:
    log = get_security_log()
    if user is not None:
        data.setdefault("user", getattr(user, "sub", str(user)))
        data.setdefault("roles", sorted(getattr(user, "roles", [])))
    log.add(datetime.now(timezone.utc), f"security.{kind}", outcome=outcome, code=code, **data)
