"""Per-case records written to SQLite: the decision record (seed, graph version, candidate scores, chosen action, tier, stability), the
analyst memo, the outcome of each executed action (risk before and after, verdict), and the promise ledger.

v3 audit requirements: "Decisions, seeds, metrics". The audit log (HMAC-chained, encrypted) stays the tamper-evident record of what happened;
this is the queryable record of why. Nothing here is read by the decision engine, so it cannot change a decision.
"""
from __future__ import annotations

from typing import Any

from app.ext.store import Store, get_store

PROMISE_CONFIRM_MIN = 0.8        # a promise below this confidence is a candidate: the merchant confirms before it counts


class Records:
    def __init__(self, tenant: str, case_id: str, store: Store | None = None):
        self.tenant, self.case_id = tenant, case_id
        self.store = store or get_store()
        self.last_decision_id = ""
        self.risk_before: float | None = None          # miss probability if nothing is done, at the first analysis
        self._promise_ids: list[str] = []

    # ---- decisions, memos, outcomes ----
    def decision(self, r: Any) -> None:
        if self.risk_before is None and r.obligation_at_risk:
            self.risk_before = r.p_miss_do_nothing
        scores = [{"id": x.id, "score": round(x.score, 4), "p_miss": round(x.p_miss, 4), "tier": x.tier, "permitted": x.permitted}
                  for x in r.ranked]
        self.last_decision_id = self.store.add_decision(self.tenant, self.case_id, {
            "graph_version": r.graph.version, "seed": r.seed, "scores": scores, "chosen": r.chosen.id,
            "tier_result": f"tier_{r.chosen.tier}", "stability": round(r.stability, 4), "p_miss": round(r.p_miss_do_nothing, 4),
            "disposition": r.disposition.value})

    def memo(self, memo: Any, report: Any) -> None:
        self.store.add_memo(self.tenant, self.case_id, self.last_decision_id, {
            "summary": memo.summary, "options": memo.options, "recommendation": report.chosen.label, "deadline": memo.deadline})

    def outcome(self, action_type: str, verification_state: str, report: Any | None) -> None:
        """risk_after is the miss probability if nothing more is done, re-simulated after the action. 'partial' means the source accepted
        the action but the risk did not fall: acknowledged is not effective."""
        after = report.p_miss_do_nothing if report is not None and report.obligation_at_risk else (0.0 if report is not None else None)
        before = self.risk_before
        if verification_state == "verified":
            verdict = "verified" if (before is None or after is None or after < before * 0.9 or before == 0) else "partial"
        else:
            verdict = "failed" if verification_state == "failed" else verification_state
        self.store.add_outcome(self.tenant, self.case_id, self.last_decision_id, action_type,
                               None if before is None else round(before, 4), None if after is None else round(after, 4), verdict)

    # ---- promise ledger ----
    def promises(self, intake: Any, report: Any, supplier: dict[str, Any]) -> None:
        nodes = {n["id"]: n for n in report.graph.nodes}
        ob = next((n for n in nodes if n.startswith("obligation:")), None)
        funds = next((n for n in nodes if n.startswith("funds:")), None)
        for i, p in enumerate(intake.promises):
            pid = f"P-{self.case_id}-{i + 1}"
            funded = funds if (ob and funds) else None
            status = "unfunded" if ob is None or funded is None else ("at_risk" if report.obligation_at_risk else "on_track")
            if pid not in self._promise_ids:
                self._promise_ids.append(pid)
            self.store.upsert_promise(self.tenant, self.case_id, {
                "id": pid, "counterparty": supplier.get("name") or p.payee, "amount": p.amount or supplier.get("amount"),
                "due_text": p.due_text, "due_at": supplier.get("due_at"), "confidence": p.confidence,
                "confirmed": int(p.confidence >= PROMISE_CONFIRM_MIN), "linked_node": ob, "funded_by": funded, "status": status})

    def set_promise_status(self, status: str) -> None:
        for r in self.store.promises(self.tenant, self.case_id):
            if r["status"] not in ("closed",):
                self.store.upsert_promise(self.tenant, self.case_id, {**r, "status": status})

    def confirm_promise(self, pid: str, yes: bool) -> bool:
        for r in self.store.promises(self.tenant, self.case_id):
            if r["id"] == pid:
                self.store.upsert_promise(self.tenant, self.case_id, {**r, "confirmed": int(yes), "status": r["status"] if yes else "rejected"})
                return True
        return False

    # ---- read ----
    def view(self) -> dict[str, Any]:
        return {"ledger": self.store.promises(self.tenant, self.case_id), "decisions": self.store.decisions(self.tenant, self.case_id),
                "outcomes": self.store.outcomes(self.tenant, self.case_id), "followups": self.store.followups(self.tenant, self.case_id)}
