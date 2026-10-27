"""Turns raw tool reads into source-tagged, freshness-marked facts and detects conflicts.

Agent-side module: it only sees what tools return, never scenario internals.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.models.contracts import Event, Source

DISPUTE_PENDING = {"evidence_required", "submitted", "under_review"}


class Fact(BaseModel):
    key: str
    value: Any
    source: Source
    source_ref: str
    observed_at: datetime
    freshness: str = "fresh"


class Timeline(BaseModel):
    now: datetime
    merchant_id: str
    facts: dict[str, Fact] = Field(default_factory=dict)
    events: list[Event] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    def val(self, key: str, default: Any = None) -> Any:
        f = self.facts.get(key)
        return default if f is None else f.value

    def ref(self, key: str) -> str:
        f = self.facts.get(key)
        return f.source_ref if f else ""

    def last_event(self, source: Source, type_: str) -> Event | None:
        hits = [e for e in self.events if e.source == source and e.type == type_]
        return hits[-1] if hits else None


def collect(
    read: Callable[[str], Any],
    available_tools: set[str],
    now: datetime,
    merchant_id: str,
    events: list[Event],
    merchant_claims: dict[str, Any] | None = None,
) -> Timeline:
    tl = Timeline(now=now, merchant_id=merchant_id, events=list(events))

    def put(key: str, value: Any, source: Source, ref: str) -> None:
        tl.facts[key] = Fact(key=key, value=value, source=source, source_ref=ref, observed_at=now)

    if "read_settlement" in available_tools:
        s = read("read_settlement")
        ref = f"settlement:{merchant_id}"
        for k in ("status", "amount_total", "held_amount", "hold_reason_code", "linked_dispute_id", "expected_release"):
            put(f"settlement.{k}", s.get(k), Source.SETTLEMENT, ref)

    if "read_dispute" in available_tools:
        d = read("read_dispute")
        ref = f"dispute:{d['dispute_id']}"
        for k_src, k_out in (("dispute_id", "id"), ("status", "status"), ("evidence_due", "evidence_due"),
                             ("required_docs", "required_docs"), ("amount", "amount")):
            put(f"dispute.{k_out}", d.get(k_src), Source.DISPUTE, ref)

    if "read_kyc" in available_tools:
        k = read("read_kyc")
        ref = f"kyc:{merchant_id}"
        for key in ("status", "expires_at", "opted_in_reminders"):
            put(f"kyc.{key}", k.get(key), Source.KYC, ref)

    if "read_supplier_obligation" in available_tools:
        sp = read("read_supplier_obligation")
        if sp:
            ref = f"supplier:{sp['supplier_id']}"
            for key in ("supplier_id", "name", "amount", "due_at"):
                put(f"supplier.{key}", sp.get(key), Source.SUPPLIER, ref)

    for k, v in (merchant_claims or {}).items():
        put(f"merchant.{k}", v, Source.MERCHANT, "merchant:message")

    tl.conflicts = _conflicts(tl)
    return tl


def _conflicts(tl: Timeline) -> list[str]:
    out: list[str] = []
    linked = tl.val("settlement.linked_dispute_id")
    reason = tl.val("settlement.hold_reason_code") or ""
    dispute_id = tl.val("dispute.id")
    dstatus = tl.val("dispute.status")
    if tl.val("settlement.status") == "held_risk" and linked:
        if dispute_id is None or dispute_id != linked:
            out.append(f"settlement links dispute {linked} but dispute source reports {dispute_id}")
        elif reason.startswith("DISPUTE_") and dstatus not in DISPUTE_PENDING:
            out.append(f"hold reason {reason} but dispute status is {dstatus}")
    return out
