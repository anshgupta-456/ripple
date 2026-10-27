"""Sensitivity of the benchmark conclusions to the modeled assumptions (v3: "published config, sensitivity analysis, illustrative labels").

    python -m app.bench.sensitivity [--runs 6] [--out data/results/sensitivity_v2.json]

Two checks, both on synthetic data:
  1. Cost assumptions (minutes per manual case, cost per human minute, minutes a pre-built memo saves): the recorded runs are re-scored over a
     grid. Reports the modeled cost per case of each system and whether full RIPPLE is cheaper than the plain agent.
  2. Harm weights (they drive decisions): the whole benchmark is re-run with the weights scaled, and the headline metrics are compared.
The config is restored afterwards, whatever happens.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Any

from app.bench.metrics import summarize_system
from app.bench.runner import SYSTEM_KEYS, run_benchmark
from app.config import ROOT, load_config

COST_GRID = {"cost_per_human_minute_inr": [3.0, 6.0, 12.0, 24.0], "manual_minutes_per_case": [7, 14, 28], "prebuilt_memo_minutes_saved": [0, 9, 18]}
HARM_SCENARIOS = {
    "baseline": {},
    "all weights x0.5": {"*": 0.5},
    "all weights x2": {"*": 2.0},
    "supplier miss x2, delay x0.5": {"missed_supplier_payment": 2.0, "settlement_delay_per_day": 0.5},
    "supplier miss x0.5, delay x2": {"missed_supplier_payment": 0.5, "settlement_delay_per_day": 2.0},
}


def _by_system(recs: list[Any]) -> dict[str, list[Any]]:
    return {k: [r for r in recs if r.system == k] for k in SYSTEM_KEYS}


def cost_sweep(recs: list[Any]) -> dict[str, Any]:
    cm = load_config()["cost_model"]
    saved = dict(cm)
    rows, e_cheaper = [], 0
    try:
        for rate, manual, memo in itertools.product(*COST_GRID.values()):
            cm["cost_per_human_minute_inr"], cm["manual_minutes_per_case"], cm["prebuilt_memo_minutes_saved"] = rate, manual, memo
            cost = {k: summarize_system(v)["cost_per_case_inr"]["mean"] for k, v in _by_system(recs).items()}
            rows.append({"cost_per_human_minute_inr": rate, "manual_minutes_per_case": manual, "prebuilt_memo_minutes_saved": memo, "cost": cost,
                         "E_cheaper_than_C": cost["E"] < cost["C"]})
            e_cheaper += cost["E"] < cost["C"]
    finally:
        cm.update(saved)
    return {"grid": COST_GRID, "n": len(rows), "E_cheaper_than_C_in": e_cheaper, "rows": rows}


def harm_runs(runs: int) -> list[dict[str, Any]]:
    cfg = load_config()
    hw = cfg["harm_weights"]
    saved = dict(hw)
    out = []
    try:
        for name, scale in HARM_SCENARIOS.items():
            hw.update(saved)
            for k, f in scale.items():
                for key in (saved if k == "*" else [k]):
                    hw[key] = saved[key] * f
            recs = run_benchmark(runs_per_family=runs)
            by = _by_system(recs)
            row: dict[str, Any] = {"scenario": name, "runs": len(recs)}
            for sysk in ("C", "E"):
                s = summarize_system(by[sysk])
                row[sysk] = {"verified": s["verified_resolution_rate"]["rate"], "unsafe": s["unsafe_actions"]["total"], "false_resolved": s["false_resolved"]["k"],
                             "escalation_recall": s["escalation_recall"]["rate"], "autonomous": s["autonomous_resolution_rate"]["rate"],
                             "stability": s.get("decision_stability_mean")}
            out.append(row)
    finally:
        hw.clear()
        hw.update(saved)
    return out


def build(runs: int) -> dict[str, Any]:
    cfg = load_config()
    base = run_benchmark(runs_per_family=cfg["benchmark"]["runs_per_family"])
    return {"synthetic_data": True, "note": "Modeled assumptions varied over stated ranges; synthetic results, not predictions about real merchants.",
            "cost": cost_sweep(base), "harm": harm_runs(runs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6, help="seeds per family for each harm-weight scenario")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "results" / "sensitivity_v2.json")
    a = ap.parse_args()
    res = build(a.runs)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    c = res["cost"]
    print(f"cost grid: {c['n']} combinations; full RIPPLE cheaper than the plain agent in {c['E_cheaper_than_C_in']}")
    for r in res["harm"]:
        print(f"{r['scenario']:34s} E verified={r['E']['verified']} unsafe={r['E']['unsafe']} falseRes={r['E']['false_resolved']} escR={r['E']['escalation_recall']} | "
              f"C verified={r['C']['verified']} unsafe={r['C']['unsafe']}")


if __name__ == "__main__":
    main()
