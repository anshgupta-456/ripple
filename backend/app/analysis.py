"""Glue between the mock world (tool reads) and the pure decision engine."""
from __future__ import annotations

from typing import Any

from app.engine.decide import DecisionReport, decide
from app.graph.builder import GraphSnapshot
from app.mock_sources.world import World
from app.timeline.normalizer import collect


def analyze_world(world: World, seed: int, prev_graph: GraphSnapshot | None = None,
                  merchant_claims: dict[str, Any] | None = None) -> DecisionReport:
    # Until Phase 4 intake exists, assume the merchant said the supplier is paid from the settlement.
    claims = {"supplier_paid_from_settlement": True} if merchant_claims is None else merchant_claims
    tools = set(world.tools)
    tl = collect(lambda t: world.call(t), tools, world.now, world.spec.merchant_id, world.events, claims)
    return decide(tl, tools, seed, prev_graph)
