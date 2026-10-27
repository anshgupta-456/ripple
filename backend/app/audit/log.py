"""Append-only audit trail. Every decision, denial, approval, tool call and verification lands here."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AuditEntry(BaseModel):
    seq: int
    ts: datetime                      # simulated time
    kind: str                         # firewall | tool_call | approval | consent | verification | injection_flag | reconcile
    case_id: str = ""
    action_hash: str = ""
    outcome: str = ""                 # allow | deny | ok | timeout | ...
    code: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    gseq: int = 0


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.entries: list[AuditEntry] = []
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def add(self, ts: datetime, kind: str, *, case_id: str = "", action_hash: str = "", outcome: str = "",
            code: str = "", **data: Any) -> AuditEntry:
        from app.audit.trace import next_seq
        e = AuditEntry(gseq=next_seq(), seq=len(self.entries) + 1, ts=ts, kind=kind, case_id=case_id, action_hash=action_hash,
                       outcome=outcome, code=code, data=data)
        self.entries.append(e)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(e.model_dump_json() + "\n")
        return e

    def find(self, kind: str | None = None, code: str | None = None, outcome: str | None = None) -> list[AuditEntry]:
        return [e for e in self.entries if (kind is None or e.kind == kind)
                and (code is None or e.code == code) and (outcome is None or e.outcome == outcome)]

    def dump(self) -> list[dict[str, Any]]:
        return [json.loads(e.model_dump_json()) for e in self.entries]
