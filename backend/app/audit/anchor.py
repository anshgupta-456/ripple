"""External anchoring of the audit chain head.

The HMAC chain already exposes an edited, removed or reordered entry. It cannot expose two things: the newest entries being cut off
the end of the log, or the whole log being replaced by a freshly computed chain. Both leave a chain that verifies. Publishing the head
(entry number and chain MAC) to a place the application cannot rewrite closes that gap: a log that is shorter than an anchor, or
whose entry at an anchored number has a different MAC, no longer matches what was published.

An anchor carries no case content: only the log's name, an entry number and a keyed MAC.

Sinks (RIPPLE_AUDIT_ANCHOR):
  file:/path/anchors.jsonl   append one JSON line per anchor. Point it at storage the app user cannot truncate (a separate volume,
                             append-only attribute, object-lock bucket mount); on the same disk it only helps against partial tampering.
  https://host/path          POST the record as JSON (optional bearer token from the secret store, RIPPLE_AUDIT_ANCHOR_TOKEN), for a SIEM or
                             log collector. No redirects, 3 s timeout, https only (plain http for loopback in RIPPLE_ENV=dev).
RIPPLE_AUDIT_ANCHOR_EVERY=N publishes after every Nth entry (default 1).

Failure policy: a sink that is down never blocks a case; the failure is counted (`failures`) and shown by the verify endpoint so it is
visible, not silent. The tradeoff is availability over completeness. A deployment that wants the opposite should treat a non-zero
failure count as an alert.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from app.security.secret_store import get_secret
from app.security.settings import is_dev


class Sink(Protocol):
    def publish(self, record: dict[str, Any]) -> None: ...


class FileSink:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def publish(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def read(self, log: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and (log is None or r.get("log") == log):
                out.append(r)
        return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class HttpSink:
    def __init__(self, url: str, token: str | None = None):
        u = urllib.parse.urlparse(url)
        loopback = (u.hostname or "") in ("127.0.0.1", "localhost", "::1")
        if u.scheme != "https" and not (u.scheme == "http" and loopback and is_dev()):
            raise ValueError("audit anchor URL must be https (plain http only for loopback in RIPPLE_ENV=dev)")
        self.url, self.token = url, token

    def publish(self, record: dict[str, Any]) -> None:
        req = urllib.request.Request(self.url, data=json.dumps(record).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=3) as resp:
            if not 200 <= resp.status < 300:
                raise OSError(f"anchor sink answered {resp.status}")


class Anchorer:
    def __init__(self, sink: Sink, every: int = 1):
        self.sink, self.every = sink, max(1, every)
        self.published = 0
        self.failures = 0
        self.last_error = ""

    def maybe(self, log: str, seq: int, head: str, ts: str) -> None:
        if seq % self.every == 0:
            self.now(log, seq, head, ts)

    def now(self, log: str, seq: int, head: str, ts: str) -> bool:
        try:
            self.sink.publish({"log": log, "seq": seq, "head": head, "ts": ts})
            self.published += 1
            return True
        except Exception as e:                      # a down sink must not stop case handling; it is counted and surfaced
            self.failures += 1
            self.last_error = type(e).__name__
            return False

    def status(self) -> dict[str, Any]:
        return {"configured": True, "published": self.published, "failures": self.failures, "last_error": self.last_error, "every": self.every}


_CACHE: tuple[str, str, Anchorer | None] | None = None


def from_env() -> Anchorer | None:
    """The configured anchorer, or None. Rebuilt when the settings change; a bad setting raises so a typo is not mistaken for 'off'."""
    global _CACHE
    spec = os.environ.get("RIPPLE_AUDIT_ANCHOR", "").strip()
    every = os.environ.get("RIPPLE_AUDIT_ANCHOR_EVERY", "1")
    if _CACHE and _CACHE[0] == spec and _CACHE[1] == every:
        return _CACHE[2]
    anchorer: Anchorer | None = None
    if spec:
        try:
            n = int(every)
        except ValueError:
            n = 1
        if spec.startswith("file:"):
            anchorer = Anchorer(FileSink(Path(spec[5:])), n)
        elif spec.startswith(("https://", "http://")):
            anchorer = Anchorer(HttpSink(spec, get_secret("RIPPLE_AUDIT_ANCHOR_TOKEN", required=False)), n)
        else:
            raise ValueError("RIPPLE_AUDIT_ANCHOR must be file:<path> or an https URL")
    _CACHE = (spec, every, anchorer)
    return anchorer


def check_anchors(entries: list[Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare a log against the anchors published for it. `entries` need .mac; `records` come from the sink."""
    if not records:
        return {"ok": True, "anchors": 0, "note": "nothing anchored yet"}
    latest = max(r["seq"] for r in records)
    for r in sorted(records, key=lambda r: r["seq"]):
        if r["seq"] > len(entries):
            return {"ok": False, "anchors": len(records), "reason": f"log is shorter than an anchor (anchored {r['seq']} entries, log has {len(entries)}): the end was cut off"}
        if entries[r["seq"] - 1].mac != r["head"]:
            return {"ok": False, "anchors": len(records), "reason": f"entry {r['seq']} differs from what was anchored: the log was rewritten"}
    return {"ok": True, "anchors": len(records), "latest_anchored_seq": latest, "unanchored_tail": len(entries) - latest}
