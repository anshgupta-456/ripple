"""Typed obligation/cause graph with provenance-carrying edges and versioned snapshots."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from app.models.contracts import EdgeClass
from app.timeline.normalizer import Timeline


class GraphSnapshot(BaseModel):
    version: int
    digest: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class BuiltGraph(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    g: Any = Field(exclude=True)
    snapshot: GraphSnapshot


def _add_edge(g: nx.DiGraph, src: str, dst: str, cls: EdgeClass, prov: list[str], note: str = "") -> None:
    g.add_edge(src, dst, edge_class=cls, provenance=sorted(set(p for p in prov if p)), note=note)


def build_graph(tl: Timeline, prev: GraphSnapshot | None = None) -> BuiltGraph:
    g = nx.DiGraph()
    mid = tl.merchant_id
    funds = f"funds:{mid}"
    g.add_node(funds, kind="settlement_funds", label="Settlement funds reach merchant", controllable=False)

    status = tl.val("settlement.status")
    reason = tl.val("settlement.hold_reason_code") or ""
    hold = f"hold:{mid}"
    has_hold = status == "held_risk"
    if has_hold:
        g.add_node(hold, kind="risk_hold", label=f"Risk hold ({reason})", controllable=False)
        _add_edge(g, hold, funds, EdgeClass.CONFIRMED_CAUSAL, [tl.ref("settlement.status")],
                  "hold blocks release of funds")
        _add_causes(g, tl, hold, reason)

    if tl.val("supplier.supplier_id"):
        ob = f"obligation:{tl.val('supplier.supplier_id')}"
        g.add_node(ob, kind="supplier_obligation", label=f"Pay {tl.val('supplier.name')}", controllable=False,
                   amount=tl.val("supplier.amount"), due_at=tl.val("supplier.due_at"))
        if tl.val("merchant.supplier_paid_from_settlement"):
            _add_edge(g, funds, ob, EdgeClass.OBLIGATION,
                      [tl.ref("supplier.supplier_id"), tl.ref("merchant.supplier_paid_from_settlement")],
                      "merchant stated the supplier is paid from this settlement")

    nodes = [{"id": n, **{k: v for k, v in a.items()}} for n, a in sorted(g.nodes(data=True))]
    edges = [{"src": u, "dst": v, "edge_class": a["edge_class"].value, "provenance": a["provenance"],
              "note": a["note"], "decoy": a["edge_class"] in (EdgeClass.SOFT,)}
             for u, v, a in sorted(g.edges(data=True), key=lambda x: (x[0], x[1]))]
    digest = hashlib.sha256(json.dumps([nodes, edges], sort_keys=True, default=str).encode()).hexdigest()[:12]
    version = 1 if prev is None else (prev.version if prev.digest == digest else prev.version + 1)
    return BuiltGraph(g=g, snapshot=GraphSnapshot(version=version, digest=digest, nodes=nodes, edges=edges))


def _add_causes(g: nx.DiGraph, tl: Timeline, hold: str, reason: str) -> None:
    linked = tl.val("settlement.linked_dispute_id")
    did = tl.val("dispute.id")
    dstatus = tl.val("dispute.status")
    confirmed = False

    # Cause 1: dispute, only when BOTH settlement and dispute sources agree (reason code + link).
    if reason.startswith("DISPUTE_") and linked and did == linked:
        if dstatus == "evidence_required":
            n = f"evidence_gap:{did}"
            g.add_node(n, kind="evidence_gap", label="Chargeback evidence not submitted", controllable=True,
                       fix_actions=["submit_dispute_evidence"], due_at=tl.val("dispute.evidence_due"))
            _add_edge(g, n, hold, EdgeClass.CONFIRMED_CAUSAL, [tl.ref("settlement.hold_reason_code"), tl.ref("dispute.status")],
                      f"settlement hold code {reason} linked to dispute {did}, which awaits evidence")
            confirmed = True
        elif dstatus in ("submitted", "under_review"):
            n = f"review_pending:{did}"
            g.add_node(n, kind="review_pending", label="Evidence under risk review", controllable=False)
            _add_edge(g, n, hold, EdgeClass.CONFIRMED_CAUSAL, [tl.ref("settlement.hold_reason_code"), tl.ref("dispute.status")],
                      "evidence submitted; hold lifts after review")
            confirmed = True
        elif dstatus == "lost":
            n = f"dispute_lost:{did}"
            g.add_node(n, kind="dispute_lost", label="Dispute lost", controllable=False)
            _add_edge(g, n, hold, EdgeClass.CONFIRMED_CAUSAL, [tl.ref("dispute.status")], "dispute outcome")
            confirmed = True

    # Cause 2: KYC. Confirmed only if the hold reason code says so; otherwise a correlated decoy.
    kstatus = tl.val("kyc.status")
    if kstatus in ("expiring", "expired"):
        k = f"kyc:{tl.merchant_id}"
        g.add_node(k, kind="kyc_status", label=f"KYC {kstatus}", controllable=True,
                   fix_actions=["send_kyc_reminder"])
        if reason.startswith("KYC_"):
            _add_edge(g, k, hold, EdgeClass.CONFIRMED_CAUSAL, [tl.ref("settlement.hold_reason_code"), tl.ref("kyc.status")],
                      "hold reason code names KYC")
            confirmed = True
        else:
            _add_edge(g, k, hold, EdgeClass.SOFT, [tl.ref("kyc.status")],
                      f"co-occurs in time with the hold, but the reason code is {reason or 'unknown'}: non-causal")

    if not confirmed:
        u = f"unknown_cause:{tl.merchant_id}"
        g.add_node(u, kind="unknown", label="Cause not established", controllable=False)
        _add_edge(g, u, hold, EdgeClass.UNKNOWN, [tl.ref("settlement.status")], "no source-backed cause found")
