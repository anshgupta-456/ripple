"""Deadline-feasibility simulator.

Seeded Monte Carlo over *when funds are released* given the traced cause and a
chosen action path. All action paths share the same random draws (common random
numbers), so differences between actions are due to the actions, not noise.
Priors come from config; scenario internals are never read.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

import numpy as np
from pydantic import BaseModel

from app.engine.reverse_trace import TraceResult
from app.models.contracts import Source
from app.timeline.normalizer import Timeline


class Noise(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    approval: Any
    review: Any
    release: Any
    self_serve: Any
    jitter: Any


def draw_noise(rng: np.random.Generator, n: int, p: dict[str, Any]) -> Noise:
    rv, rd, ap = p["review_hours"], p["release_delay_hours"], p["approval_latency_hours"]
    return Noise(
        approval=rng.lognormal(np.log(ap["median"]), ap["sigma"], n),
        review=np.clip(rng.normal(rv["mean"], rv["sd"], n), rv["min"], rv["max"]),
        release=np.clip(rng.normal(rd["mean"], rd["sd"], n), rd["min"], rd["max"]),
        self_serve=rng.exponential(p["merchant_self_serve_delay_hours"]["mean"], n),
        jitter=rng.normal(0.0, p["processing_release_jitter_hours"], n),
    )


def _hours(dt: str | datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return (dt - now).total_seconds() / 3600


def release_hours(tl: Timeline, trace: TraceResult, submit_evidence: bool, nz: Noise, p: dict[str, Any]) -> np.ndarray:
    """Hours from now until funds are released (inf = never within the model)."""
    n = len(nz.review)
    status = tl.val("settlement.status")
    if status == "released":
        return np.zeros(n)
    if status == "processing":
        h = _hours(tl.val("settlement.expected_release"), tl.now)
        if h is None:
            return np.full(n, np.inf)
        return np.maximum(h + nz.jitter, 0.0)

    root = trace.root_kind
    if root == "evidence_gap":
        due_h = _hours(tl.val("dispute.evidence_due"), tl.now)
        submit_at = nz.approval if submit_evidence else nz.self_serve
        total = submit_at + nz.review + nz.release
        return np.where(submit_at > due_h, np.inf, total) if due_h is not None else total
    if root == "review_pending":
        ev = tl.last_event(Source.DISPUTE, "evidence_submitted")
        elapsed = _hours(ev.ts, tl.now) * -1 if ev else 0.0
        remaining = np.maximum(nz.review - elapsed, 0.5)
        return remaining + nz.release
    return np.full(n, np.inf)


def p_miss(release: np.ndarray, due_hours: float | None) -> float:
    if due_hours is None:
        return 0.0
    return float(np.mean(release > due_hours))


def perturb_params(p: dict[str, Any], rng: np.random.Generator, frac: float) -> dict[str, Any]:
    q = copy.deepcopy(p)
    def s() -> float:
        return float(1 + rng.uniform(-frac, frac))
    for key in ("review_hours", "release_delay_hours"):
        q[key]["mean"] *= s()
        q[key]["sd"] *= s()
    q["merchant_self_serve_delay_hours"]["mean"] *= s()
    q["approval_latency_hours"]["median"] *= s()
    q["supplier_notice"]["accept_prob"] = float(np.clip(q["supplier_notice"]["accept_prob"] * s(), 0, 1))
    return q
