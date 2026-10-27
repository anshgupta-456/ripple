"""Mock systems for the features that need their own state: the merchant profile (Soundbox, EMI), extra signals (transaction response codes,
Soundbox health, EMI schedule, support contacts), udhaar receivables, re-KYC links, scheduled follow-ups and bridge offers.
Registered on a World only when extensions are enabled, so the benchmark's worlds never have these tools.

Same rules as the other mock sources: agent-callable operations become tools (and so must pass the firewall and carry an idempotency key);
anything else is the system's own behaviour. Everything is synthetic and deterministic per merchant.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.ext.config import ext_config
from app.mock_sources.world import ToolDef
from app.models.contracts import Source

if TYPE_CHECKING:  # pragma: no cover
    from app.mock_sources.world import World

NAMES = ["Gupta Wholesale", "Verma Traders", "Sharma Kirana", "Khan Textiles", "Iyer Sweets", "Das Provision"]


def _h(merchant_id: str, salt: str) -> int:
    return int(hashlib.sha256(f"{merchant_id}:{salt}".encode()).hexdigest()[:8], 16)


class ExtSources:
    def __init__(self, world: "World"):
        self.w = world
        mp = ext_config()["merchant_profile"]
        mid = world.spec.merchant_id
        self.profile: dict[str, Any] = {k: mp[k] for k in ("soundbox_online", "monthly_emi", "monthly_inflow", "festival_stock_order", "emi_due_in_days")}
        self.rekyc_links: list[dict[str, Any]] = []
        self.followups: list[dict[str, Any]] = []
        self.offers: list[dict[str, Any]] = []
        self.receivables: list[dict[str, Any]] = self._make_receivables(mid)

    def _make_receivables(self, mid: str) -> list[dict[str, Any]]:
        out = []
        for i in range(2 + _h(mid, "n") % 2):
            seed = _h(mid, f"r{i}")
            out.append({"debtor_id": f"deb_{i + 1}", "name": NAMES[seed % len(NAMES)], "amount": 5000 + (seed % 9) * 2500, "overdue_days": 3 + seed % 20,
                        "base_prob": round(0.35 + (seed % 5) * 0.08, 2), "relationship": round(0.4 + (seed % 6) * 0.1, 2),
                        "hardship": False, "disputed": False, "reminders": 0, "messages": []})
        return out

    # ---- read ----
    def read(self) -> dict[str, Any]:
        return {"profile": dict(self.profile), "rekyc_links": [dict(x) for x in self.rekyc_links], "followups": [dict(x) for x in self.followups],
                "offers": [dict(x) for x in self.offers], "receivables": [{**r, "messages": list(r["messages"])} for r in self.receivables]}

    # ---- extra signals (feature 1): synthetic and deterministic, emitted once when the world is built ----
    def emit_signals(self) -> None:
        mid = self.w.spec.merchant_id
        h = _h(mid, "tx")
        ok, bad = 380 + h % 60, 4 + h % 9
        self.w.emit(Source.SYSTEM, "transactions_summary", {"window": "7d", "success": ok, "failed": bad, "top_failure_codes": {"U30": bad // 2 + 1, "U16": bad - bad // 2 - 1}},
                    f"transactions:{mid}")
        self.w.emit(Source.SYSTEM, "soundbox_status", {"online": self.profile["soundbox_online"]}, f"soundbox:{mid}")
        self.w.emit(Source.SYSTEM, "emi_schedule", {"amount": self.profile["monthly_emi"], "due_in_days": self.profile["emi_due_in_days"]}, f"loan:{mid}")
        self.w.emit(Source.SYSTEM, "support_contacts", {"last_7d": 1 + h % 4}, f"support:{mid}")

    def set_soundbox(self, online: bool) -> None:
        self.profile["soundbox_online"] = online
        self.w.emit(Source.SYSTEM, "soundbox_status", {"online": online}, f"soundbox:{self.w.spec.merchant_id}")

    # ---- tools (all writes need an idempotency key via World.call) ----
    def send_rekyc_link(self) -> dict[str, Any]:
        link = {"link_id": f"rk_{len(self.rekyc_links) + 1:03d}", "at": self.w.now.isoformat()}
        self.rekyc_links.append(link)
        # The link itself lives in the app (deep link); the message carries no URL, which the outbound guard would block anyway.
        self.w.notifications.send_message("Aapka re-KYC link app mein ready hai: do tap mein KYC document update ho jayega.")
        self.w.emit(Source.KYC, "rekyc_link_sent", {"link_id": link["link_id"]}, f"kyc:{self.w.spec.merchant_id}")
        return {"ok": True, "link_id": link["link_id"]}

    def schedule_follow_up(self, hours: int, reason: str) -> dict[str, Any]:
        due = self.w.now + timedelta(hours=hours)
        f = {"followup_id": f"fu_{len(self.followups) + 1:03d}", "reason": reason, "due_at": due.isoformat(), "status": "open", "hours": hours}
        self.followups.append(f)
        self.w.schedule(due, "followup_due", {"followup_id": f["followup_id"]})
        self.w.emit(Source.SYSTEM, "followup_scheduled", {"followup_id": f["followup_id"], "due_at": f["due_at"]}, f"followup:{f['followup_id']}")
        return {"ok": True, "followup_id": f["followup_id"], "due_at": f["due_at"]}

    def accept_bridge_offer(self, kind: str, amount: int) -> dict[str, Any]:
        b = ext_config()["bridge"]
        fee_pct = b["instant_settlement_fee_pct"] if kind == "instant_settlement" else b["credit_fee_pct"]
        offer = {"offer_id": f"of_{len(self.offers) + 1:03d}", "kind": kind, "amount": amount, "fee": round(amount * fee_pct / 100), "fee_pct": fee_pct,
                 "status": "accepted", "disbursed_at": self.w.now.isoformat(),
                 "repay_by": (self.w.now + timedelta(days=b["credit_days"])).isoformat() if kind == "short_term_credit" else None}
        self.offers.append(offer)
        self.w.emit(Source.SYSTEM, "bridge_offer_accepted", {"offer_id": offer["offer_id"], "kind": kind, "amount": amount}, f"offer:{offer['offer_id']}")
        return {"ok": True, **offer}

    def send_debtor_reminder(self, debtor_id: str, text: str) -> dict[str, Any]:
        r = next((x for x in self.receivables if x["debtor_id"] == debtor_id), None)
        if r is None:
            return {"ok": False, "error": "no such debtor"}
        r["reminders"] += 1
        r["messages"].append({"text": text, "at": self.w.now.isoformat()})
        self.w.emit(Source.NOTIFICATION, "debtor_reminder_sent", {"debtor_id": debtor_id}, f"udhaar:{debtor_id}")
        return {"ok": True, "n": r["reminders"]}

    def _followup_due(self, payload: dict[str, Any]) -> None:
        for f in self.followups:
            if f["followup_id"] == payload.get("followup_id") and f["status"] == "open":
                f["status"] = "due"
                self.w.emit(Source.SYSTEM, "followup_due", {"followup_id": f["followup_id"], "reason": f["reason"]}, f"followup:{f['followup_id']}")

    def attach(self) -> "ExtSources":
        t = self.w.tools
        t["read_ext_state"] = ToolDef("read_ext_state", lambda: self.read(), False)
        t["send_rekyc_link"] = ToolDef("send_rekyc_link", lambda: self.send_rekyc_link(), True)
        t["schedule_follow_up"] = ToolDef("schedule_follow_up", lambda hours, reason: self.schedule_follow_up(hours, reason), True)
        t["accept_bridge_offer"] = ToolDef("accept_bridge_offer", lambda kind, amount: self.accept_bridge_offer(kind, amount), True)
        t["send_debtor_reminder"] = ToolDef("send_debtor_reminder", lambda debtor_id, text: self.send_debtor_reminder(debtor_id, text), True)
        self.w._handlers["followup_due"] = self._followup_due
        self.w.ext = self
        self.emit_signals()
        return self


def attach_ext_sources(world: "World") -> ExtSources:
    return ExtSources(world).attach()
