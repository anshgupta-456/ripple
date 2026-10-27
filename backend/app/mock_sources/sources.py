"""Stateful mock source systems.

Each source owns its state and emits events through the world. Agent-callable
operations are registered in World.tools. Operations prefixed with `_` are the
source system's own internal behaviour (e.g. the risk team releasing a hold)
and are deliberately NOT exposed as tools: there is no tool to release a hold.
"""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.mock_sources.spec import DisputeSpec, KycSpec, RefundSpec, SettlementSpec, SupplierSpec
from app.models.contracts import Source

if TYPE_CHECKING:  # pragma: no cover
    from app.mock_sources.world import World


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


class SettlementSource:
    def __init__(self, world: "World", merchant_id: str, spec: SettlementSpec):
        self.w = world
        self.merchant_id = merchant_id
        self.s = spec.model_copy(deep=True)
        if self.s.status == "processing" and self.s.release_at:
            world.schedule(self.s.release_at, "settlement_release", {})

    def read(self) -> dict[str, Any]:
        s = self.s
        return {
            "merchant_id": self.merchant_id,
            "status": s.status,
            "amount_total": s.amount_total,
            "held_amount": s.held_amount,
            "hold_reason_code": s.hold_reason_code,
            "linked_dispute_id": s.linked_dispute_id,
            "expected_release": _iso(s.release_at),
        }

    # --- internal source behaviour, not agent tools ---
    def _clear_hold(self, release_delay_hours: float) -> None:
        self.s.status = "processing"
        self.s.held_amount = 0
        self.s.hold_reason_code = None
        self.s.release_at = self.w.now + timedelta(hours=release_delay_hours)
        self.w.emit(Source.SETTLEMENT, "hold_cleared", {"expected_release": _iso(self.s.release_at)},
                    f"settlement:{self.merchant_id}")
        self.w.schedule(self.s.release_at, "settlement_release", {})

    def _release(self) -> None:
        if self.s.status == "processing":
            self.s.status = "released"
            self.w.emit(Source.SETTLEMENT, "released", {"amount": self.s.amount_total},
                        f"settlement:{self.merchant_id}")


class DisputeSource:
    def __init__(self, world: "World", spec: DisputeSpec, merchant_id: str):
        self.w = world
        self.spec = spec
        self.merchant_id = merchant_id
        self.status = spec.initial_status
        self.evidence: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        sp = self.spec
        return {
            "dispute_id": sp.dispute_id,
            "status": self.status,
            "amount": sp.amount,
            "evidence_due": _iso(sp.evidence_due),
            "required_docs": sp.required_docs,
            "evidence_docs_received": sorted((self.evidence or {}).keys()),
        }

    def submit_evidence(self, dispute_id: str, docs: list[str]) -> dict[str, Any]:
        ref = f"dispute:{self.spec.dispute_id}"
        if dispute_id != self.spec.dispute_id:
            return {"ok": False, "error": "unknown_dispute"}
        if self.status != "evidence_required":
            return {"ok": False, "error": f"invalid_state:{self.status}"}
        if self.w.now > self.spec.evidence_due:
            self.w.emit(Source.DISPUTE, "evidence_rejected", {"reason": "past_due"}, ref)
            return {"ok": False, "error": "past_due"}
        missing = [d for d in self.spec.required_docs if d not in docs]
        if missing:
            self.w.emit(Source.DISPUTE, "evidence_incomplete", {"missing": missing}, ref)
            return {"ok": False, "error": "incomplete", "missing": missing}
        self.evidence = {d: True for d in docs}
        self.status = "submitted"
        self.w.emit(Source.DISPUTE, "evidence_submitted", {"docs": sorted(docs)}, ref)
        self.w.schedule(self.w.now + timedelta(hours=1), "dispute_under_review", {})
        return {"ok": True, "status": self.status}

    # --- internal: the risk team's review pipeline ---
    def _start_review(self) -> None:
        self.status = "under_review"
        self.w.emit(Source.DISPUTE, "under_review", {}, f"dispute:{self.spec.dispute_id}")
        remaining = max(self.spec.review_hours - 1, 0)
        self.w.schedule(self.w.now + timedelta(hours=remaining), "dispute_decision", {})

    def _decide(self) -> None:
        ref = f"dispute:{self.spec.dispute_id}"
        self.status = self.spec.review_outcome
        self.w.emit(Source.DISPUTE, f"dispute_{self.status}", {}, ref)
        if self.status == "won":
            self.w.settlement._clear_hold(self.spec.release_delay_hours)


class KycSource:
    def __init__(self, world: "World", merchant_id: str, spec: KycSpec):
        self.w = world
        self.merchant_id = merchant_id
        self.spec = spec
        self.reminders_sent = 0

    def read(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "status": self.spec.status,
            "expires_at": _iso(self.spec.expires_at),
            "opted_in_reminders": self.spec.opted_in_reminders,
            "reminders_sent": self.reminders_sent,
        }

    def send_reminder(self) -> dict[str, Any]:
        if not self.spec.opted_in_reminders:
            return {"ok": False, "error": "not_opted_in"}
        self.reminders_sent += 1
        self.w.emit(Source.KYC, "reminder_sent", {"count": self.reminders_sent}, f"kyc:{self.merchant_id}")
        return {"ok": True, "reminders_sent": self.reminders_sent}


class SupplierSource:
    def __init__(self, world: "World", spec: SupplierSpec | None):
        self.w = world
        self.spec = spec
        self.notices: list[str] = []

    def read(self) -> dict[str, Any] | None:
        if not self.spec:
            return None
        sp = self.spec
        return {
            "supplier_id": sp.supplier_id,
            "name": sp.name,
            "amount": sp.amount,
            "due_at": _iso(sp.due_at),
            "notices_sent": len(self.notices),
        }

    def send_notice(self, text: str) -> dict[str, Any]:
        if not self.spec:
            return {"ok": False, "error": "no_supplier"}
        self.notices.append(text)
        self.w.emit(Source.SUPPLIER, "notice_sent", {"text": text}, f"supplier:{self.spec.supplier_id}")
        return {"ok": True, "notices_sent": len(self.notices)}


class NotificationSource:
    def __init__(self, world: "World", merchant_id: str):
        self.w = world
        self.merchant_id = merchant_id
        self.log: list[dict[str, Any]] = []

    def read(self) -> list[dict[str, Any]]:
        return list(self.log)

    def send_message(self, text: str) -> dict[str, Any]:
        entry = {"to": self.merchant_id, "text": text, "at": _iso(self.w.now)}
        self.log.append(entry)
        self.w.emit(Source.NOTIFICATION, "merchant_message_sent", {"text": text},
                    f"notification:{self.merchant_id}")
        return {"ok": True, "n": len(self.log)}


class RefundSource:
    def __init__(self, world: "World", merchant_id: str, spec: RefundSpec):
        self.w = world
        self.merchant_id = merchant_id
        self.spec = spec.model_copy(deep=True)
        if self.spec.status == "processing" and self.spec.complete_at:
            world.schedule(self.spec.complete_at, "refund_complete", {})

    def read(self) -> dict[str, Any]:
        sp = self.spec
        return {"refund_id": sp.refund_id, "status": sp.status, "amount": sp.amount,
                "expected_by": _iso(sp.complete_at)}

    def _complete(self) -> None:
        if self.spec.status == "processing":
            self.spec.status = "completed"
            self.w.emit(Source.SETTLEMENT, "refund_completed", {"amount": self.spec.amount},
                        f"refund:{self.spec.refund_id}")
