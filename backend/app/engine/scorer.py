"""Action options and scoring.

score(a) = E[harm | do nothing] - E[harm | do(a)] - normalized_cost(a) - risk_penalty(a)
Pick argmax over permitted, feasible options; if the best score is at or below the
minimum benefit threshold, choose DO_NOTHING.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
from pydantic import BaseModel

from app.engine.reverse_trace import TraceResult
from app.engine.simulator import Noise, _hours, p_miss, release_hours
from app.models.contracts import Tier
from app.timeline.normalizer import Timeline

DO_NOTHING = "DO_NOTHING"
STEP_TIER = {
    "submit_dispute_evidence": Tier.HUMAN_APPROVAL,   # risk-related action
    "send_supplier_notice": Tier.MERCHANT_CONSENT,
    "send_kyc_reminder": Tier.AGENT_ALONE,            # only if merchant already opted in
}
LABELS = {
    "submit_dispute_evidence": "Compile and submit dispute evidence",
    "send_supplier_notice": "Notify supplier of possible delay",
    "send_kyc_reminder": "Send KYC reminder",
}


class Option(BaseModel):
    id: str
    label: str
    steps: list[str]
    tier: int
    decoy: bool = False


class RankedAction(BaseModel):
    id: str
    label: str
    steps: list[str]
    tier: int
    permitted: bool
    feasible: bool
    reason: str
    p_miss: float
    expected_harm: float
    cost: float
    risk_penalty: float
    score: float
    decoy: bool = False
    release_p50_h: float | None = None   # hours from now until funds release, finite samples only
    release_p90_h: float | None = None


def _delay_cost(release: np.ndarray, p: dict[str, Any]) -> float:
    """Merchant harm from funds staying stuck, even when a dated deadline is already lost (capped)."""
    cap = p.get("delay_cap_hours", 168)
    return float(np.mean(np.minimum(release, cap))) / 24.0 * p.get("delay_w", 0.0)


def build_options(tl: Timeline, trace: TraceResult, tools: set[str]) -> list[Option]:
    opts = [Option(id=DO_NOTHING, label="Do nothing, reply and monitor", steps=[], tier=int(Tier.AGENT_ALONE))]
    has_supplier = bool(tl.val("supplier.supplier_id")) and "send_supplier_notice" in tools
    can_fix = "submit_dispute_evidence" in trace.fix_actions
    if can_fix:
        opts.append(_opt(["submit_dispute_evidence"]))
    if has_supplier:
        opts.append(_opt(["send_supplier_notice"]))
    if can_fix and has_supplier:
        opts.append(_opt(["submit_dispute_evidence", "send_supplier_notice"]))
    if tl.val("kyc.status") in ("expiring", "expired"):
        o = _opt(["send_kyc_reminder"])
        o.decoy = True  # separate risk: no confirmed path to the at-risk obligation
        opts.append(o)
    return opts


def _opt(steps: list[str]) -> Option:
    tier = max(int(STEP_TIER.get(s, Tier.AGENT_ALONE)) for s in steps)
    return Option(id="+".join(s.upper() for s in steps), label=" + ".join(LABELS[s] for s in steps),
                  steps=steps, tier=tier)


def _feasibility(o: Option, tl: Timeline, tools: set[str]) -> tuple[bool, bool, str]:
    """-> (permitted, feasible, reason)"""
    if o.id == DO_NOTHING:
        return True, True, "always available"
    if o.tier >= int(Tier.NEVER):
        return False, False, "tier 4: never"
    if any(s not in tools for s in o.steps):
        return False, False, "tool not available"
    if "submit_dispute_evidence" in o.steps:
        if tl.val("dispute.status") != "evidence_required":
            return True, False, "evidence already submitted or dispute closed"
        due = _hours(tl.val("dispute.evidence_due"), tl.now)
        if due is not None and due <= 0:
            return True, False, "evidence deadline passed"
    if "send_kyc_reminder" in o.steps and not tl.val("kyc.opted_in_reminders"):
        return False, False, "merchant has not opted in to reminders"
    return True, True, "ok"


def rank(
    tl: Timeline, trace: TraceResult, tools: set[str], nz: Noise, p: dict[str, Any], due_hours: float | None,
    harm_weight: float, obligation_present: bool,
) -> list[RankedAction]:
    notice_mit = p["supplier_notice"]["accept_prob"] * p["supplier_notice"]["harm_reduction_if_accepted"]
    base_release = release_hours(tl, trace, False, nz, p)
    base_harm = (p_miss(base_release, due_hours) * harm_weight) if obligation_present else 0.0
    base_harm += _delay_cost(base_release, p)

    out: list[RankedAction] = []
    for o in build_options(tl, trace, tools):
        permitted, feasible, reason = _feasibility(o, tl, tools)
        submit = "submit_dispute_evidence" in o.steps
        rel = release_hours(tl, trace, submit, nz, p)
        pm = p_miss(rel, due_hours) if obligation_present else 0.0
        fin = rel[np.isfinite(rel)]
        p50 = round(float(np.percentile(fin, 50)), 1) if len(fin) >= 0.5 * len(rel) else None
        p90 = round(float(np.percentile(fin, 90)), 1) if len(fin) >= 0.9 * len(rel) else None
        mit = notice_mit if "send_supplier_notice" in o.steps else 0.0
        harm = pm * harm_weight * (1 - mit) + _delay_cost(rel, p)
        cost = sum(p["action_costs"].get(s, 0.0) for s in o.steps)
        risk = p["risk_penalty_by_tier"].get(o.tier, 0.0) if o.steps else 0.0
        score = base_harm - harm - cost - risk
        if o.decoy and feasible:
            reason = "no confirmed causal path to the at-risk obligation (correlated decoy)"
        out.append(RankedAction(
            id=o.id, label=o.label, steps=o.steps, tier=o.tier, permitted=permitted, feasible=feasible,
            reason=reason, p_miss=round(pm, 4), expected_harm=round(harm, 4), cost=round(cost, 4),
            risk_penalty=round(risk, 4), score=round(score, 4) if o.steps else 0.0, decoy=o.decoy,
            release_p50_h=p50, release_p90_h=p90,
        ))
    out.sort(key=lambda r: (-r.score, r.id))
    return out


def choose(ranked: list[RankedAction], min_benefit: float) -> RankedAction:
    cands = [r for r in ranked if r.id != DO_NOTHING and r.permitted and r.feasible]
    if cands and cands[0].score > min_benefit:
        return cands[0]
    return next(r for r in ranked if r.id == DO_NOTHING)
