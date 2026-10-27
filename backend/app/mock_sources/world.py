"""The simulated world: clock, event stream, idempotency ledger, fault injection, tool registry."""
from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.mock_sources.sources import (
    DisputeSource,
    KycSource,
    NotificationSource,
    SettlementSource,
    SupplierSource,
)
from app.mock_sources.spec import WorldSpec
from app.models.contracts import Event, Source

FAULT_MODES = ("timeout_after_write", "state_unchanged")


class ToolTimeout(Exception):
    """The write may or may not have been applied. Reconcile by idempotency key; never blind retry."""

    def __init__(self, tool: str, idem_key: str):
        super().__init__(f"timeout calling {tool} (idem_key={idem_key})")
        self.tool = tool
        self.idem_key = idem_key


class UnknownTool(Exception):
    """No such tool exists. Tier 4 actions (e.g. release_risk_hold) rely on this."""


class MissingIdempotencyKey(Exception):
    pass


@dataclass
class ToolDef:
    name: str
    fn: Callable[..., Any]
    write: bool


class World:
    def __init__(self, spec: WorldSpec):
        self.spec = spec
        self.now: datetime = spec.start
        self.events: list[Event] = []
        self.ledger: dict[str, dict[str, Any]] = {}
        self.faults: dict[str, list[str]] = {}
        self._seq = itertools.count(1)
        self._sched_seq = itertools.count(1)
        self._sched: list[tuple[datetime, int, str, dict[str, Any]]] = []
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}

        mid = spec.merchant_id
        self.settlement = SettlementSource(self, mid, spec.settlement)
        self.dispute = DisputeSource(self, spec.dispute, mid) if spec.dispute else None
        self.kyc = KycSource(self, mid, spec.kyc)
        self.supplier = SupplierSource(self, spec.supplier)
        self.notifications = NotificationSource(self, mid)

        self._handlers = {
            "settlement_release": lambda _p: self.settlement._release(),
            "dispute_under_review": lambda _p: self.dispute and self.dispute._start_review(),
            "dispute_decision": lambda _p: self.dispute and self.dispute._decide(),
        }
        self.tools = self._build_tools()

    # ---- tool surface (the ONLY things an agent may call) ----
    def _build_tools(self) -> dict[str, ToolDef]:
        t: dict[str, ToolDef] = {
            "read_settlement": ToolDef("read_settlement", lambda: self.settlement.read(), False),
            "read_kyc": ToolDef("read_kyc", lambda: self.kyc.read(), False),
            "read_supplier_obligation": ToolDef("read_supplier_obligation", lambda: self.supplier.read(), False),
            "read_notifications": ToolDef("read_notifications", lambda: self.notifications.read(), False),
            "send_kyc_reminder": ToolDef("send_kyc_reminder", lambda: self.kyc.send_reminder(), True),
            "send_supplier_notice": ToolDef("send_supplier_notice", lambda text: self.supplier.send_notice(text), True),
            "send_merchant_message": ToolDef("send_merchant_message",
                                             lambda text: self.notifications.send_message(text), True),
        }
        if self.dispute:
            t["read_dispute"] = ToolDef("read_dispute", lambda: self.dispute.read(), False)
            t["submit_dispute_evidence"] = ToolDef(
                "submit_dispute_evidence",
                lambda dispute_id, docs: self.dispute.submit_evidence(dispute_id, docs),
                True,
            )
        return t

    def call(self, tool: str, params: dict[str, Any] | None = None, idem_key: str | None = None) -> Any:
        td = self.tools.get(tool)
        if td is None:
            raise UnknownTool(tool)
        params = params or {}
        if not td.write:
            return td.fn(**params)
        if not idem_key:
            raise MissingIdempotencyKey(tool)
        if idem_key in self.ledger:
            return {**self.ledger[idem_key]["result"], "replayed": True}

        fault = self._take_fault(tool)
        if fault == "state_unchanged":
            # source acknowledges but does not change state
            result: dict[str, Any] = {"ok": True, "accepted": True}
            self._record(idem_key, tool, params, result)
            return result
        result = td.fn(**params)
        self._record(idem_key, tool, params, result)
        if fault == "timeout_after_write":
            raise ToolTimeout(tool, idem_key)
        return result

    def lookup_by_key(self, idem_key: str) -> dict[str, Any] | None:
        """Reconciliation: did a write with this key land? (query before any retry)"""
        return self.ledger.get(idem_key)

    def _record(self, key: str, tool: str, params: dict[str, Any], result: dict[str, Any]) -> None:
        self.ledger[key] = {"tool": tool, "params": params, "result": result, "at": self.now.isoformat()}

    # ---- fault injection (chaos panel / tests) ----
    def inject_fault(self, tool: str, mode: str, count: int = 1) -> None:
        if mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {mode}")
        self.faults.setdefault(tool, []).extend([mode] * count)

    def _take_fault(self, tool: str) -> str | None:
        q = self.faults.get(tool)
        return q.pop(0) if q else None

    # ---- clock, events, scheduling ----
    def emit(self, source: Source, type_: str, payload: dict[str, Any], source_ref: str) -> Event:
        ev = Event(id=f"evt_{next(self._seq):04d}", ts=self.now, source=source, type=type_,
                   payload=payload, source_ref=source_ref)
        self.events.append(ev)
        return ev

    def schedule(self, at: datetime, kind: str, payload: dict[str, Any]) -> None:
        heapq.heappush(self._sched, (at, next(self._sched_seq), kind, payload))

    def advance(self, hours: float) -> None:
        target = self.now + timedelta(hours=hours)
        while self._sched and self._sched[0][0] <= target:
            at, _, kind, payload = heapq.heappop(self._sched)
            self.now = max(self.now, at)
            self._handlers[kind](payload)
        self.now = target

    # ---- read-only snapshot (UI / verifier) ----
    def snapshot(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "merchant": {"id": self.spec.merchant_id, "name": self.spec.merchant_name,
                         "language": self.spec.language},
            "settlement": self.settlement.read(),
            "dispute": self.dispute.read() if self.dispute else None,
            "kyc": self.kyc.read(),
            "supplier": self.supplier.read(),
            "notifications": self.notifications.read(),
        }
