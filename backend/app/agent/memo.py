"""Analyst memo: the decision-ready card. Deterministic, built only from verified facts and engine output."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.evidence import EvidenceBundle
from app.agent.reply import fmt_dt, inr
from app.config import load_config
from app.engine.decide import DecisionReport
from app.intake.extract import IntakeResult
from app.timeline.normalizer import Timeline


class MemoFact(BaseModel):
    label: str
    value: str
    source_ref: str


class MemoOption(BaseModel):
    id: str
    label: str
    tier: int
    p_miss: float
    score: float
    chosen: bool = False
    note: str = ""


class AnalystMemo(BaseModel):
    merchant_ask: str
    facts: list[MemoFact]
    cause_path: list[str]
    cause_edges: list[dict[str, Any]]
    rejected_edges: list[dict[str, Any]]
    evidence: EvidenceBundle | None
    options: list[MemoOption]
    deadline: str | None
    hours_to_deadline: float | None
    if_nothing_p_miss: float
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list)
    triggers: list[str]
    disposition: str
    stability: float
    seed: int
    graph_version: int
    minutes_saved: int
    summary: str


def build_memo(intake: IntakeResult, raw_message: str, tl: Timeline, report: DecisionReport,
               bundle: EvidenceBundle | None, proposed: list[dict[str, Any]]) -> AnalystMemo:
    f = lambda label, key: MemoFact(label=label, value=str(tl.val(key)), source_ref=tl.ref(key))  # noqa: E731
    facts: list[MemoFact] = []
    for label, key in (("Settlement status", "settlement.status"), ("Hold reason code", "settlement.hold_reason_code"),
                       ("Dispute status", "dispute.status"), ("KYC status", "kyc.status")):
        if tl.facts.get(key) is not None and tl.val(key) is not None:
            facts.append(f(label, key))
    if tl.val("settlement.held_amount"):
        facts.append(MemoFact(label="Held amount", value=inr(tl.val("settlement.held_amount")),
                              source_ref=tl.ref("settlement.held_amount")))
    if tl.val("supplier.amount"):
        facts.append(MemoFact(label="Supplier obligation",
                              value=f"{inr(tl.val('supplier.amount'))} to {tl.val('supplier.name')}, due {fmt_dt(tl.val('supplier.due_at'))}",
                              source_ref=tl.ref("supplier.amount")))
    mins = load_config()["cost_model"]["prebuilt_memo_minutes_saved"]
    chosen = report.chosen
    cause = report.trace.root_cause or "none"
    summary = (f"{raw_message.strip()[:140]} | cause: {cause} | recommended: {chosen.label} "
               f"(P(miss) {chosen.p_miss:.0%} vs {report.p_miss_do_nothing:.0%} if nothing) | "
               f"disposition: {report.disposition.value}.")
    return AnalystMemo(
        merchant_ask=raw_message.strip()[:200], facts=facts, cause_path=report.trace.path, cause_edges=report.trace.edges,
        rejected_edges=report.trace.rejected_edges, evidence=bundle,
        options=[MemoOption(id=r.id, label=r.label, tier=r.tier, p_miss=r.p_miss, score=r.score,
                            chosen=r.id == chosen.id, note=r.reason) for r in report.ranked],
        deadline=tl.val("supplier.due_at"), hours_to_deadline=report.due_hours,
        if_nothing_p_miss=report.p_miss_do_nothing, proposed_actions=proposed, triggers=report.triggers,
        disposition=report.disposition.value, stability=report.stability, seed=report.seed,
        graph_version=report.graph.version, minutes_saved=mins, summary=summary,
    )
