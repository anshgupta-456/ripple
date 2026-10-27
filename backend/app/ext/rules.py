"""Learned rules (Full Feature Spec feature 9) and autonomy that widens only on evidence (feature 8).

Two ways a rule is PROPOSED, never activated:
  * from a human edit: an analyst changes an evidence bundle; the system proposes "compile and submit this bundle automatically for chargebacks
    up to this amount";
  * from evidence: after enough human decisions on a class of action with a low override rate, it proposes widening autonomy for that class.
A rule becomes ACTIVE only when a named analyst confirms it (API: approval.decide, analysts only). The system never loosens its own limits.

What an active rule can do is deliberately narrow:
  * only the Tier 3 action types in RULE_APPROVABLE (today: submit_dispute_evidence), never Tier 4;
  * only up to rules.max_auto_amount from config, whatever the rule says;
  * only for a bundle whose documents are all within the rule's document set;
  * the approval is still bound to the exact action hash, single use, expiring, and is audited with the rule id and the confirming analyst.
"""
from __future__ import annotations

import math
from typing import Any

from app.ext.config import ext_config
from app.ext.store import Store, get_store
from app.firewall.stores import RULE_APPROVABLE


class RuleError(ValueError):
    pass


def _cfg() -> dict[str, Any]:
    return ext_config()["rules"]


def validate_condition(c: dict[str, Any]) -> dict[str, Any]:
    cfg = _cfg()
    if c.get("action") not in cfg["allowed_actions"] or c.get("action") not in RULE_APPROVABLE:
        raise RuleError(f"a rule cannot cover {c.get('action')!r}")
    amt, docs = c.get("max_amount"), c.get("docs")
    if not (isinstance(amt, int) and not isinstance(amt, bool) and 0 < amt <= cfg["max_auto_amount"]):
        raise RuleError(f"max_amount must be a whole number up to {cfg['max_auto_amount']}")
    if not (isinstance(docs, list) and docs and all(isinstance(d, str) for d in docs)):
        raise RuleError("docs must be a non-empty list")
    return {"action": c["action"], "max_amount": amt, "docs": sorted(set(docs))}


class Rules:
    def __init__(self, tenant: str, store: Store | None = None):
        self.tenant = tenant
        self.store = store or get_store()

    # ---- evidence about people ----
    def record(self, case_id: str, action_type: str, decision: str, approver: str = "", rule_id: str = "") -> None:
        self.store.add_human_decision(self.tenant, case_id, action_type, decision, approver, rule_id)

    def override_stats(self, action_type: str) -> dict[str, Any]:
        c = self.store.decision_counts(self.tenant, action_type)
        n = sum(c.values())
        overrides = c.get("edit", 0) + c.get("reject", 0)
        return {"n": n, "approve": c.get("approve", 0), "edit": c.get("edit", 0), "reject": c.get("reject", 0), "override_rate": round(overrides / n, 4) if n else None}

    # ---- proposals ----
    def propose_from_edit(self, case_id: str, action_type: str, dispute_amount: int, docs: list[str]) -> str | None:
        cfg = _cfg()
        if action_type not in cfg["allowed_actions"]:
            return None
        cap = min(cfg["max_auto_amount"], int(math.ceil(dispute_amount / 5000.0) * 5000))
        try:
            cond = validate_condition({"action": action_type, "max_amount": cap, "docs": docs})
        except RuleError:
            return None
        if any(r["condition"] == cond and r["status"] in ("proposed", "active") for r in self.store.rules(self.tenant)):
            return None
        return self.store.add_rule(self.tenant, "analyst_edit", "edit", cond, proposed_from=case_id,
                                   note=f"An analyst changed the evidence bundle. Proposed: submit this bundle automatically for disputes up to Rs {cap:,}.")

    def maybe_widen(self, case_id: str, action_type: str, dispute_amount: int, docs: list[str]) -> str | None:
        w, cfg = _cfg()["widening"], _cfg()
        st = self.override_stats(action_type)
        if action_type not in cfg["allowed_actions"] or st["n"] < w["min_decisions"] or st["override_rate"] is None or st["override_rate"] > w["max_override_rate"]:
            return None
        cap = min(cfg["max_auto_amount"], int(math.ceil(dispute_amount / 5000.0) * 5000))
        try:
            cond = validate_condition({"action": action_type, "max_amount": cap, "docs": docs})
        except RuleError:
            return None
        if any(r["kind"] == "widen" and r["condition"]["action"] == action_type and r["status"] in ("proposed", "active") for r in self.store.rules(self.tenant)):
            return None
        return self.store.add_rule(self.tenant, "evidence", "widen", cond, proposed_from=case_id,
                                   note=f"{st['n']} human decisions with a {st['override_rate'] * 100:.0f}% override rate. Proposed: let the agent submit this bundle for disputes up to Rs {cap:,}.")

    # ---- human decisions on a rule ----
    def confirm(self, rid: str, by: str) -> dict[str, Any]:
        r = self.store.rule(self.tenant, rid)
        if r is None:
            raise KeyError(rid)
        if r["status"] != "proposed":
            raise RuleError(f"rule is {r['status']}, not proposed")
        validate_condition(r["condition"])                   # re-check against today's limits before it can act
        self.store.set_rule_status(self.tenant, rid, "active", by)
        return self.store.rule(self.tenant, rid)

    def reject(self, rid: str, by: str) -> dict[str, Any]:
        r = self.store.rule(self.tenant, rid)
        if r is None:
            raise KeyError(rid)
        self.store.set_rule_status(self.tenant, rid, "rejected", by)
        return self.store.rule(self.tenant, rid)

    def deactivate(self, rid: str, by: str) -> dict[str, Any]:
        r = self.store.rule(self.tenant, rid)
        if r is None:
            raise KeyError(rid)
        self.store.set_rule_status(self.tenant, rid, "retired", by)
        return self.store.rule(self.tenant, rid)

    # ---- use ----
    def match(self, action_type: str, dispute_amount: int, docs: list[str]) -> dict[str, Any] | None:
        if action_type not in RULE_APPROVABLE:
            return None
        for r in self.store.rules(self.tenant, "active"):
            try:
                c = validate_condition(r["condition"])
            except RuleError:
                continue
            if c["action"] == action_type and dispute_amount <= c["max_amount"] and docs and set(docs) <= set(c["docs"]) and r.get("confirmed_by"):
                return r
        return None
