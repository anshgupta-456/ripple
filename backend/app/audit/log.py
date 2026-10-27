"""Append-only, tamper-evident audit trail.

Every decision, denial, approval, tool call and verification lands here with WHO did it (actor), WHEN, WHICH tool and
the RESULT. Controls:
  * integrity: each entry carries an HMAC-SHA256 over (previous MAC + the entry), forming a chain; editing, deleting or
    reordering any entry breaks verification from that point on.
  * confidentiality: PII/regulated identifiers are redacted before the entry is built; file lines are encrypted at rest
    (Fernet); the key comes from the secrets store (RIPPLE_AUDIT_KEY), never from code.
  * the chain key is separate from the encryption key (both derived from the one secret with distinct labels).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field

from app.models.contracts import canonical_json
from app.security.governance import ACTOR
from app.security.redact import redact_obj
from app.security.secret_store import get_secret

GENESIS = "0" * 64


def _keys(secret: str) -> tuple[bytes, bytes]:
    mac = hashlib.sha256(b"ripple-audit-mac:" + secret.encode()).digest()
    enc = base64.urlsafe_b64encode(hashlib.sha256(b"ripple-audit-enc:" + secret.encode()).digest())
    return mac, enc


class AuditEntry(BaseModel):
    seq: int
    ts: datetime                      # simulated time (or real UTC for security events)
    kind: str                         # firewall | tool_call | approval | consent | verification | injection_flag | reconcile | decision | ...
    case_id: str = ""
    action_hash: str = ""
    outcome: str = ""                 # allow | deny | ok | timeout | ...
    code: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    gseq: int = 0
    actor: dict[str, Any] | None = None
    prev: str = GENESIS
    mac: str = ""


def _body(e: AuditEntry) -> str:
    return canonical_json(e.model_dump(mode="json", exclude={"mac"}))


class AuditLog:
    def __init__(self, path: Path | None = None, secret: str | None = None):
        self.entries: list[AuditEntry] = []
        self.path = path
        self.log_id = f"{path.name if path else 'memory'}#{uuid.uuid4().hex[:8]}"     # names this chain in external anchors (one per process run)
        if secret is None:
            secret = get_secret("RIPPLE_AUDIT_KEY", required=path is not None, dev_ephemeral=True) if path else None
        self._secret = secret or hashlib.sha256(repr(id(self)).encode() + b"mem").hexdigest()
        self._mac_key, enc_key = _keys(self._secret)
        self._fernet = Fernet(enc_key)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def add(self, ts: datetime, kind: str, *, case_id: str = "", action_hash: str = "", outcome: str = "",
            code: str = "", **data: Any) -> AuditEntry:
        from app.audit.trace import next_seq
        actor = ACTOR.get()
        e = AuditEntry(gseq=next_seq(), seq=len(self.entries) + 1, ts=ts, kind=kind, case_id=case_id,
                       action_hash=action_hash, outcome=outcome, code=code, data=redact_obj(data),
                       actor={k: actor[k] for k in ("sub", "name", "roles")} if actor else None,
                       prev=self.entries[-1].mac if self.entries else GENESIS)
        e.mac = hmac.new(self._mac_key, _body(e).encode(), hashlib.sha256).hexdigest()
        self.entries.append(e)
        if self.path:
            line = self._fernet.encrypt(e.model_dump_json().encode()).decode()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            from app.audit import anchor
            a = anchor.from_env()
            if a:
                a.maybe(self.log_id, e.seq, e.mac, e.ts.isoformat())
        return e

    def anchor_report(self) -> dict[str, Any]:
        """Is the head published somewhere the app cannot rewrite, and does this log still match what was published?"""
        from app.audit import anchor
        a = anchor.from_env()
        if a is None:
            return {"configured": False, "note": "no external anchor: truncating the end of the log would go unnoticed (RIPPLE_AUDIT_ANCHOR)"}
        out = {**a.status(), "log": self.log_id}
        if isinstance(a.sink, anchor.FileSink):
            out["check"] = anchor.check_anchors(self.entries, a.sink.read(self.log_id))
        else:
            out["check"] = {"ok": None, "note": "anchors are held by the remote collector; compare there"}
        return out

    # ---- integrity ----
    def verify(self) -> dict[str, Any]:
        return verify_entries(self.entries, self._mac_key)

    def verify_file(self) -> dict[str, Any]:
        if not self.path:
            return {"ok": False, "reason": "no file"}
        entries = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                entries.append(AuditEntry(**json.loads(self._fernet.decrypt(line.encode()))))
            except (InvalidToken, ValueError):
                return {"ok": False, "first_bad_line": i, "reason": "line cannot be decrypted (tampered or wrong key)"}
        return verify_entries(entries, self._mac_key)

    def find(self, kind: str | None = None, code: str | None = None, outcome: str | None = None) -> list[AuditEntry]:
        return [e for e in self.entries if (kind is None or e.kind == kind)
                and (code is None or e.code == code) and (outcome is None or e.outcome == outcome)]

    def dump(self) -> list[dict[str, Any]]:
        return [json.loads(e.model_dump_json()) for e in self.entries]


def verify_entries(entries: list[AuditEntry], mac_key: bytes) -> dict[str, Any]:
    prev = GENESIS
    for i, e in enumerate(entries, 1):
        want = hmac.new(mac_key, _body(e).encode(), hashlib.sha256).hexdigest()
        if e.prev != prev or not hmac.compare_digest(want, e.mac) or e.seq != i:
            return {"ok": False, "first_bad_entry": i, "reason": "chain broken: entry was edited, removed or reordered"}
        prev = e.mac
    return {"ok": True, "entries": len(entries), "head": prev}
