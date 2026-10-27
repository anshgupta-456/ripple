"""Deterministic decision pipeline: facts -> graph -> reverse trace -> simulate -> score -> disposition.

No LLM anywhere in this module. Same seed + config + facts => same report.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from app.config import load_config
from app.engine.reverse_trace import TraceResult, TraceStatus, reverse_trace
from app.engine.scorer import DO_NOTHING, RankedAction, choose, rank
from app.engine.simulator import _hours, draw_noise, perturb_params
from app.graph.builder import GraphSnapshot, build_graph
from app.timeline.normalizer import Timeline


class Disposition(str, Enum):
    NO_ACTION = "no_action"                      # reply plainly, monitor
    AUTONOMOUS = "autonomous"                    # tier 1 only
    NEEDS_MERCHANT_CONSENT = "needs_merchant_consent"
    NEEDS_HUMAN_APPROVAL = "needs_human_approval"
    ESCALATE = "escalate"                        # deterministic escalation trigger fired


class DecisionReport(BaseModel):
    seed: int
    params: dict[str, Any]
    n_samples: int
    graph: GraphSnapshot
    trace: TraceResult
    obligation_at_risk: bool
    p_miss_do_nothing: float
    due_hours: float | None
    ranked: list[RankedAction]
    chosen: RankedAction
    stability: float
    triggers: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    disposition: Disposition


def engine_params() -> dict[str, Any]:
    cfg = load_config()
    p = dict(cfg["engine"])
    p["harm_weight"] = cfg["harm_weights"]["missed_supplier_payment"]
    p["min_benefit"] = cfg["policy"]["min_benefit_threshold"]
    p["stability_min"] = cfg["policy"]["decision_stability_min"]
    p["samples"] = cfg["simulation"]["monte_carlo_samples"]
    return p


def decide(tl: Timeline, tools: set[str], seed: int, prev_graph: GraphSnapshot | None = None,
           params: dict[str, Any] | None = None) -> DecisionReport:
    p = params or engine_params()
    built = build_graph(tl, prev_graph)
    ob_id = f"obligation:{tl.val('supplier.supplier_id')}" if tl.val("supplier.supplier_id") else None
    trace = reverse_trace(built.g, ob_id)
    due_h = _hours(tl.val("supplier.due_at"), tl.now) if ob_id else None
    # an obligation matters only if the merchant has tied it to this settlement (OBLIGATION edge exists)
    present = ob_id is not None and any(True for _ in built.g.in_edges(ob_id))
    n = p["samples"]

    def run(pp: dict[str, Any], s: int, nn: int):
        nz = draw_noise(np.random.default_rng(s), nn, pp)
        return rank(tl, trace, tools, nz, pp, due_h, p["harm_weight"], present)

    ranked = run(p, seed, n)
    chosen = choose(ranked, p["min_benefit"])
    do_nothing = next(r for r in ranked if r.id == DO_NOTHING)
    at_risk = present and do_nothing.p_miss >= p["at_risk_p_miss"]
    if not at_risk:
        chosen = do_nothing

    # decision stability under +/-X% parameter perturbation and different seeds
    rng = np.random.default_rng(seed + 1)
    same = 0
    runs = p["stability_runs"]
    for i in range(runs):
        pr = perturb_params(p, rng, p["stability_perturbation"])
        rk = run(pr, seed + 100 + i, max(n // 4, 200))
        c = choose(rk, p["min_benefit"]) if at_risk else next(r for r in rk if r.id == DO_NOTHING)
        same += c.id == chosen.id
    stability = same / runs

    triggers: list[str] = []
    if at_risk:
        if chosen.tier == 3:
            triggers.append("tier3_action_needed")
        if trace.status == TraceStatus.NEEDS_EVIDENCE_OR_ESCALATE:
            triggers.append("soft_edge_dependency")
        if chosen.p_miss >= p["unreachable_p_miss"]:
            triggers.append("deadline_unreachable")
        if stability < p["stability_min"]:
            triggers.append("low_decision_stability")
    if tl.conflicts:
        triggers.append("conflicting_sources")

    hard = {"soft_edge_dependency", "deadline_unreachable", "low_decision_stability", "conflicting_sources"}
    if hard & set(triggers):
        disp = Disposition.ESCALATE
    elif chosen.id == DO_NOTHING:
        disp = Disposition.NO_ACTION
    elif chosen.tier == 3:
        disp = Disposition.NEEDS_HUMAN_APPROVAL
    elif chosen.tier == 2:
        disp = Disposition.NEEDS_MERCHANT_CONSENT
    else:
        disp = Disposition.AUTONOMOUS

    return DecisionReport(
        seed=seed, params=p, n_samples=n, graph=built.snapshot, trace=trace, obligation_at_risk=at_risk,
        p_miss_do_nothing=do_nothing.p_miss, due_hours=due_h, ranked=ranked, chosen=chosen, stability=stability,
        triggers=triggers, conflicts=tl.conflicts, disposition=disp,
    )
