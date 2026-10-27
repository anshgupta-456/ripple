"""SQLite persistence (the v3 architecture's choice) for the records that must outlive a case: decision records (seed, graph version, scores,
stability) with their memos and outcomes, the promise ledger and follow-ups. One file, no server, enough for the demo (the plan's
choice). Every row carries its tenant and every query filters on it, so tenants cannot read each other's records.

Cases themselves still live in memory; what is stored here is the evidence about them. Values are bound as parameters, never formatted
into SQL. Personal data is not stored: message text is redacted before it is written.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.security.redact import redact as redact_text
from app.security.settings import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, ts REAL NOT NULL, case_id TEXT NOT NULL, graph_version INTEGER,
  seed INTEGER, scores TEXT, chosen TEXT, tier_result TEXT, stability REAL, p_miss REAL, disposition TEXT);
CREATE TABLE IF NOT EXISTS memos(memo_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, ts REAL NOT NULL, decision_id TEXT, case_id TEXT NOT NULL, summary TEXT,
  options TEXT, recommendation TEXT, deadline TEXT, human_choice TEXT, edits TEXT);
CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, ts REAL NOT NULL, case_id TEXT NOT NULL, decision_id TEXT,
  action_type TEXT, risk_before REAL, risk_after REAL, verdict TEXT);
CREATE TABLE IF NOT EXISTS promises(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, case_id TEXT NOT NULL, counterparty TEXT, amount INTEGER, due_text TEXT,
  due_at TEXT, revised_due_at TEXT, confidence REAL, confirmed INTEGER NOT NULL DEFAULT 0, linked_node TEXT, funded_by TEXT, status TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS followups(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, case_id TEXT NOT NULL, due_at TEXT, reason TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_c ON decisions(tenant, case_id);
CREATE INDEX IF NOT EXISTS ix_prom_c ON promises(tenant, case_id);
"""

_LOCK = threading.RLock()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "ripple.db")
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _run(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with _LOCK, self._conn() as c:
            cur = c.execute(sql, args)
            rows = cur.fetchall()
            c.commit()
            return rows

    # ------------------------------------------------------------------ decision records, memos, outcomes
    def add_decision(self, tenant: str, case_id: str, rec: dict[str, Any]) -> str:
        did = new_id("D")
        self._run("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (did, tenant, time.time(), case_id, rec.get("graph_version"), rec.get("seed"), json.dumps(rec.get("scores", [])),
                   rec.get("chosen"), rec.get("tier_result", ""), rec.get("stability"), rec.get("p_miss"), rec.get("disposition")))
        return did

    def decisions(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        out = []
        for r in self._run("SELECT * FROM decisions WHERE tenant=? AND case_id=? ORDER BY ts", (tenant, case_id)):
            d = dict(r)
            d["scores"] = json.loads(d["scores"] or "[]")
            out.append(d)
        return out

    def add_memo(self, tenant: str, case_id: str, decision_id: str, memo: dict[str, Any]) -> str:
        mid = new_id("M")
        self._run("INSERT INTO memos VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (mid, tenant, time.time(), decision_id, case_id, redact_text(str(memo.get("summary", ""))),
                   json.dumps(memo.get("options", []), default=str), memo.get("recommendation", ""), str(memo.get("deadline", "")), "", "[]"))
        return mid

    def update_memo_choice(self, tenant: str, case_id: str, choice: str, edits: list[str] | None = None) -> None:
        self._run("UPDATE memos SET human_choice=?, edits=? WHERE tenant=? AND memo_id=(SELECT memo_id FROM memos WHERE tenant=? AND case_id=? "
                  "ORDER BY ts DESC LIMIT 1)", (choice, json.dumps(edits or []), tenant, tenant, case_id))

    def memos(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._run("SELECT * FROM memos WHERE tenant=? AND case_id=? ORDER BY ts", (tenant, case_id))]

    def add_outcome(self, tenant: str, case_id: str, decision_id: str, action_type: str, before: float | None, after: float | None, verdict: str) -> None:
        self._run("INSERT INTO outcomes(tenant,ts,case_id,decision_id,action_type,risk_before,risk_after,verdict) VALUES(?,?,?,?,?,?,?,?)",
                  (tenant, time.time(), case_id, decision_id, action_type, before, after, verdict))

    def outcomes(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._run("SELECT * FROM outcomes WHERE tenant=? AND case_id=? ORDER BY ts", (tenant, case_id))]

    # ------------------------------------------------------------------ promise ledger
    def upsert_promise(self, tenant: str, case_id: str, p: dict[str, Any]) -> None:
        row = {"revised_due_at": None, "linked_node": None, "funded_by": None, "due_at": None, "due_text": None, "amount": None,
               "counterparty": None, "confidence": 0.0, "confirmed": 0, "status": "pending", **p, "tenant": tenant, "case_id": case_id,
               "updated": time.time()}
        with _LOCK, self._conn() as c:
            c.execute("INSERT INTO promises(id,tenant,case_id,counterparty,amount,due_text,due_at,revised_due_at,confidence,confirmed,linked_node,funded_by,status,updated) "
                      "VALUES(:id,:tenant,:case_id,:counterparty,:amount,:due_text,:due_at,:revised_due_at,:confidence,:confirmed,:linked_node,:funded_by,:status,:updated) "
                      "ON CONFLICT(id) DO UPDATE SET counterparty=:counterparty, amount=:amount, due_text=:due_text, due_at=:due_at, "
                      "revised_due_at=:revised_due_at, confidence=:confidence, confirmed=:confirmed, linked_node=:linked_node, funded_by=:funded_by, "
                      "status=:status, updated=:updated", row)
            c.commit()

    def promises(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._run("SELECT * FROM promises WHERE tenant=? AND case_id=? ORDER BY updated", (tenant, case_id))]

    # ------------------------------------------------------------------ follow-ups
    def add_followup(self, tenant: str, case_id: str, due_at: str, reason: str) -> str:
        fid = new_id("F")
        self._run("INSERT INTO followups VALUES(?,?,?,?,?,?)", (fid, tenant, case_id, due_at, reason, "open"))
        return fid

    def followups(self, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._run("SELECT * FROM followups WHERE tenant=? AND case_id=? ORDER BY due_at", (tenant, case_id))]

    def close_followup(self, tenant: str, fid: str) -> None:
        self._run("UPDATE followups SET status='done' WHERE tenant=? AND id=?", (tenant, fid))

    # ------------------------------------------------------------------ retention
    def purge_older_than(self, days: int) -> int:
        cutoff = time.time() - days * 86400
        n = 0
        for t in ("decisions", "memos", "outcomes"):
            n += len(self._run(f"DELETE FROM {t} WHERE ts < ? RETURNING 1", (cutoff,)))      # table names are the fixed list above
        n += len(self._run("DELETE FROM promises WHERE updated < ? RETURNING 1", (cutoff,)))
        return n

    def delete_case(self, tenant: str, case_id: str) -> None:
        for t in ("decisions", "memos", "outcomes", "promises", "followups"):
            self._run(f"DELETE FROM {t} WHERE tenant=? AND case_id=?", (tenant, case_id))        # fixed table list, bound values


_STORE: Store | None = None
_STORE_PATH: Path | None = None


def get_store() -> Store:
    """Process-wide store; follows RIPPLE_DATA_DIR if it changes (tests)."""
    global _STORE, _STORE_PATH
    p = data_dir() / "ripple.db"
    if _STORE is None or _STORE_PATH != p:
        _STORE, _STORE_PATH = Store(p), p
    return _STORE
