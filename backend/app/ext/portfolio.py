"""Portfolio view (v3 P3): lift from one merchant to a base of simulated merchants and ask which UPSTREAM fix would prevent the most cascades.

Every merchant here is synthetic, generated from the scenario templates with a PLANTED cause distribution, and run through the real decision
engine (trace, simulate, score). The output is therefore illustrative: it shows what the view does, not what a real merchant base looks like.
With real history the same code would surface repeated bottlenecks.

    python -m app.ext.portfolio [--merchants 200] [--out data/results/portfolio_v1.json]
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.analysis import analyze_world
from app.bench.templates import generate
from app.config import ROOT
from app.ext.config import ext_config
from app.mock_sources.world import World

# the planted distribution of causes: family -> share of merchants. Stated on the page, never hidden.
PLANTED = {"missing_evidence_flagship": 0.34, "correlated_kyc_decoy": 0.16, "supplier_promise_unsaveable": 0.14, "deadline_already_passed": 0.10,
           "weak_causal_edge": 0.10, "conflicting_source_states": 0.06, "timeout_after_write": 0.06, "state_unchanged": 0.04}
CAUSE_LABEL = {"evidence_gap": "Chargeback evidence not submitted", "none": "No evidence-backed cause (escalate)", "conflict": "Source records conflict"}
FIX_LABEL = {"submit_dispute_evidence": "Compile and submit dispute evidence early", "send_supplier_notice": "Warn the supplier early",
             "": "Escalate to a human with a memo"}


def _cause_kind(report: Any) -> str:
    rc = report.trace.root_cause or ""
    if rc.startswith("evidence_gap"):
        return "evidence_gap"
    return "conflict" if report.conflicts else "none"


def build(n: int | None = None, seed: int | None = None) -> dict[str, Any]:
    cfg = ext_config()["portfolio"]
    n, seed = n or cfg["merchants"], cfg["seed"] if seed is None else seed
    effort = {int(k): float(v) for k, v in cfg["effort"].items()}
    rng = random.Random(seed)
    fams, weights = list(PLANTED), list(PLANTED.values())
    rows: list[dict[str, Any]] = []
    for i in range(n):
        fam = rng.choices(fams, weights)[0]
        sc = generate(fam, seed * 1000 + i)
        w = World(sc.world_spec)
        for tool, mode in sc.ground_truth.get("harness", {}).get("faults", []):
            w.inject_fault(tool, mode)
        r = analyze_world(w, sc.seed)
        ob = sc.world_spec.supplier
        rows.append({"family": fam, "cause": _cause_kind(r), "fix": "+".join(r.chosen.steps) if r.chosen.steps else "",
                     "tier": r.chosen.tier, "at_risk": bool(r.obligation_at_risk), "amount": ob.amount if ob else 0,
                     "p_do_nothing": r.p_miss_do_nothing, "p_chosen": r.chosen.p_miss, "escalate": r.disposition.value == "escalate"})
    clusters: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"merchants": 0, "at_risk": 0, "prevented": 0.0, "amount_protected": 0.0, "tiers": []})
    for r in rows:
        c = clusters[(r["cause"], r["fix"])]
        c["merchants"] += 1
        c["tiers"].append(r["tier"])
        if r["at_risk"]:
            c["at_risk"] += 1
            gain = max(0.0, r["p_do_nothing"] - r["p_chosen"])
            c["prevented"] += gain
            c["amount_protected"] += gain * r["amount"]
    ranked = []
    for (cause, fix), c in clusters.items():
        e = max(effort.get(t, 3.0) for t in c["tiers"]) if c["tiers"] else 3.0
        ranked.append({"root_cause": CAUSE_LABEL[cause], "upstream_fix": FIX_LABEL.get(fix.split("+")[0], fix) if fix else FIX_LABEL[""],
                       "merchants": c["merchants"], "at_risk": c["at_risk"], "cascades_prevented": round(c["prevented"], 1), "effort": e,
                       "prevented_per_effort": round(c["prevented"] / e, 2), "amount_protected": round(c["amount_protected"])})
    ranked.sort(key=lambda x: -x["prevented_per_effort"])
    by_family: dict[str, dict[str, Any]] = {}
    for r in rows:
        f = by_family.setdefault(r["family"], {"merchants": 0, "at_risk": 0, "escalate": 0, "traced_cause": CAUSE_LABEL[r["cause"]]})
        f["merchants"] += 1
        f["at_risk"] += int(r["at_risk"])
        f["escalate"] += int(r["escalate"])
    return {"illustrative": True, "n": n, "seed": seed, "planted_distribution": PLANTED, "ranked": ranked, "by_family": by_family,
            "totals": {"at_risk": sum(r["at_risk"] for r in rows), "escalate": sum(r["escalate"] for r in rows),
                       "cascades_prevented": round(sum(c["prevented"] for c in clusters.values()), 1)},
            "note": "200 synthetic merchants with a planted cause distribution. Illustrative only: not a finding about real merchants."}


_CACHE: dict[str, Any] | None = None


def cached() -> dict[str, Any]:
    """Computed once per process (about half a minute), and kept in data/results so a restart is instant."""
    global _CACHE
    if _CACHE is None:
        f = ROOT / "data" / "results" / "portfolio_v1.json"
        _CACHE = json.loads(f.read_text(encoding="utf-8")) if f.exists() else build()
    return _CACHE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merchants", type=int, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "results" / "portfolio_v1.json")
    a = ap.parse_args()
    res = build(a.merchants)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    for r in res["ranked"]:
        print(f"{r['prevented_per_effort']:6.2f}  {r['upstream_fix'][:44]:44s} <- {r['root_cause'][:36]:36s} merchants={r['merchants']}")


if __name__ == "__main__":
    main()
