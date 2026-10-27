"""Governance controls: kill switch (global and per user), rate limiting, per-request actor context."""
from __future__ import annotations

import contextvars
import json
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from app.security.settings import data_dir

# who is acting right now (set by the API layer; read by the audit log so every entry records the actor)
ACTOR: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ripple_actor", default=None)


class KillSwitch:
    """Fail-safe: state persists to disk, so a restart does not silently re-enable a disabled agent."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self.global_off = False
        self.users: set[str] = set()
        self._load()

    def _file(self) -> Path:
        return self._path or (data_dir() / "killswitch.json")

    def _load(self) -> None:
        try:
            d = json.loads(self._file().read_text(encoding="utf-8"))
            self.global_off, self.users = bool(d.get("global")), set(d.get("users", []))
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._file().write_text(json.dumps({"global": self.global_off, "users": sorted(self.users)}), encoding="utf-8")
        except Exception:
            pass

    def blocked(self, sub: str | None) -> str | None:
        if self.global_off:
            return "kill_switch_global"
        if sub and sub in self.users:
            return "kill_switch_user"
        return None

    def set_global(self, on: bool) -> None:
        with self._lock:
            self.global_off = on
            self._save()

    def set_user(self, sub: str, on: bool) -> None:
        with self._lock:
            (self.users.add if on else self.users.discard)(sub)
            self._save()

    def state(self) -> dict:
        return {"global": self.global_off, "users": sorted(self.users)}

    def reset_for_tests(self) -> None:
        self.global_off, self.users = False, set()


KILL = KillSwitch()


def agent_blocked() -> str | None:
    """Reason the agent must not act for the current actor, or None."""
    a = ACTOR.get()
    return KILL.blocked(a["sub"] if a else None)


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, principal). In-process: put a shared store behind it when scaling out."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, bucket: str, principal: str, limit: int, window_s: float, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits[(bucket, principal)]
            while q and q[0] <= t - window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True

    def reset(self) -> None:
        self._hits.clear()


LIMITER = RateLimiter()
