"""Bridge offers (feature 13), promise repair dates (feature 11) and the downstream cascade (features 1 and 2).

Everything is deterministic and uses assumptions from config/extensions.yaml, labelled as such.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.ext.config import ext_config


def _round500(x: float) -> int:
    return int(round(x / 500.0) * 500)


# ------------------------------------------------------------------ bridge offers
def plan_bridge(report: Any, supplier_amount: int | None, settlement_status: str, profile: dict[str, Any]) -> dict[str, Any]:
    """An offer is proposed only when it beats its cost and the merchant would not be worse off. Returns {"offer": {...}} or {"skip": reason}."""
    b = ext_config()["bridge"]
    if not supplier_amount or not report.obligation_at_risk:
        return {"skip": "no supplier payment at risk"}
    p = report.chosen.p_miss
    if p < b["min_p_miss_after_rescue"]:
        return {"skip": f"the rescue is likely to work (miss probability {p * 100:.0f}% after it), so no offer is needed"}
    amount = min(200_000, max(1_000, _round500(supplier_amount)))
    kind = "short_term_credit" if settlement_status == "held_risk" else "instant_settlement"
    fee_pct = b["credit_fee_pct"] if kind == "short_term_credit" else b["instant_settlement_fee_pct"]
    fee = round(amount * fee_pct / 100)
    value = b["harm_per_missed_rupee"] * amount * p
    if value < b["min_value_ratio"] * fee:
        return {"skip": f"expected harm avoided (Rs {value:,.0f}) does not beat {b['min_value_ratio']}x the fee (Rs {fee:,})"}
    if kind == "short_term_credit":
        burden = (profile["monthly_emi"] + amount + fee) / profile["monthly_inflow"] * 100
        if burden > b["max_emi_burden_pct"]:
            return {"skip": f"repayment plus EMI would take {burden:.0f}% of monthly inflow (limit {b['max_emi_burden_pct']}%): the merchant would be worse off"}
    return {"offer": {"kind": kind, "amount": amount, "fee": fee, "fee_pct": fee_pct, "value": round(value), "p_miss_after_rescue": round(p, 3),
                      "repay_days": b["credit_days"] if kind == "short_term_credit" else 0}}


# ------------------------------------------------------------------ promise repair date
def repair_date(report: Any, now: datetime, due_at: datetime | None) -> dict[str, Any]:
    """Offer only a date the simulator supports with high confidence (the 90th percentile of release time plus a buffer); otherwise a wider window."""
    p90 = report.chosen.release_p90_h
    if p90 is None:
        d = now + timedelta(days=3)
        return {"date": d, "supported": False, "text": "within about 3 days"}
    d = now + timedelta(hours=p90 + 6)
    if due_at is not None and d <= due_at:
        return {"date": None, "supported": True, "text": ""}              # on time with high confidence: no new date needed
    return {"date": d, "supported": True, "text": f"by {d.strftime('%a %d %b')}"}


# ------------------------------------------------------------------ cascade: what a missed supplier payment drags down
def cascade(report: Any, profile: dict[str, Any], now: datetime) -> dict[str, Any]:
    c = ext_config()["cascade"]
    p0 = report.p_miss_do_nothing if report.obligation_at_risk else 0.0
    p1 = report.chosen.p_miss if report.obligation_at_risk else 0.0
    nodes = []
    for e in c["edges"]:
        due = now + timedelta(days=profile["emi_due_in_days"] if e["dst"] == "emi_debit" else e["lag_days"])
        amount = profile["monthly_emi"] if e["dst"] == "emi_debit" else profile["festival_stock_order"]
        nodes.append({"id": e["dst"], "label": e["label"], "lag_days": e["lag_days"], "p_propagate": e["p_propagate"], "amount": amount,
                      "due_at": due.isoformat(), "p_miss_do_nothing": round(p0 * e["p_propagate"], 3), "p_miss_chosen": round(p1 * e["p_propagate"], 3)})
    emi = next((n for n in nodes if n["id"] == "emi_debit"), None)
    return {"edges": c["edges"], "nodes": nodes, "assumption": "edge lag and propagation probabilities are assumptions (config/extensions.yaml)",
            "expected_emi_penalty_do_nothing": round(emi["p_miss_do_nothing"] * c["emi_bounce_penalty"]) if emi else 0,
            "expected_emi_penalty_chosen": round(emi["p_miss_chosen"] * c["emi_bounce_penalty"]) if emi else 0}
