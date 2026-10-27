"""Evidence-constrained reverse trace: from an at-risk obligation back to the earliest controllable cause.

Only CONFIRMED_CAUSAL and OBLIGATION edges may be walked. SOFT/UNKNOWN edges are
recorded as rejected and can never be the sole basis for a consequential action.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

import networkx as nx
from pydantic import BaseModel, Field

from app.models.contracts import EdgeClass

WALKABLE = (EdgeClass.CONFIRMED_CAUSAL, EdgeClass.OBLIGATION)


class TraceStatus(str, Enum):
    TRACED = "traced"                                  # root cause found and controllable
    TRACED_NOT_CONTROLLABLE = "traced_not_controllable"  # root cause known, only waiting helps
    NEEDS_EVIDENCE_OR_ESCALATE = "needs_evidence_or_escalate"  # only soft/unknown links to the blocker
    NO_BLOCKER = "no_blocker"                          # nothing is blocking funds


class TraceResult(BaseModel):
    obligation_id: str | None
    status: TraceStatus
    path: list[str] = Field(default_factory=list)       # obligation -> ... -> root
    edges: list[dict[str, Any]] = Field(default_factory=list)
    root_cause: str | None = None
    root_kind: str | None = None
    fix_actions: list[str] = Field(default_factory=list)
    rejected_edges: list[dict[str, Any]] = Field(default_factory=list)


def _edge_dict(g: nx.DiGraph, u: str, v: str) -> dict[str, Any]:
    a = g.edges[u, v]
    return {"src": u, "dst": v, "edge_class": a["edge_class"].value, "provenance": a["provenance"], "note": a["note"]}


def reverse_trace(g: nx.DiGraph, obligation_id: str | None, max_hops: int | None = None) -> TraceResult:
    if obligation_id is None or obligation_id not in g:
        return TraceResult(obligation_id=obligation_id, status=TraceStatus.NO_BLOCKER)

    path, edges, rejected = [obligation_id], [], []
    cur = obligation_id
    seen = {cur}
    while True:
        ins = sorted(g.in_edges(cur), key=lambda e: e[0])
        walk = [(u, v) for u, v in ins if g.edges[u, v]["edge_class"] in WALKABLE and u not in seen]
        rejected += [_edge_dict(g, u, v) for u, v in ins if g.edges[u, v]["edge_class"] not in WALKABLE]
        if not walk or (max_hops is not None and len(edges) >= max_hops):
            break
        u, v = walk[0]
        path.append(u)
        edges.append(_edge_dict(g, u, v))
        seen.add(u)
        cur = u

    root = cur
    attrs = g.nodes[root]
    kind = attrs.get("kind")
    only_soft = bool(rejected) and kind in ("risk_hold", "unknown")
    if kind == "settlement_funds" and not rejected:
        status = TraceStatus.NO_BLOCKER
    elif only_soft:
        status = TraceStatus.NEEDS_EVIDENCE_OR_ESCALATE
    elif attrs.get("controllable"):
        status = TraceStatus.TRACED
    else:
        status = TraceStatus.TRACED_NOT_CONTROLLABLE
    return TraceResult(
        obligation_id=obligation_id, status=status, path=path, edges=edges, root_cause=root, root_kind=kind,
        fix_actions=list(attrs.get("fix_actions", [])) if status == TraceStatus.TRACED else [],
        rejected_edges=rejected,
    )
