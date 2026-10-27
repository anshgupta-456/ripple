"""Escalation rate over simulated weeks (Full Feature Spec metrics: "escalation rate falls over simulated weeks while the error rate stays flat",
"override rate low and falling").

    python -m app.ext.weeks [--out data/results/weeks_v1.json]

Each simulated week runs a batch of synthetic cases through the real orchestrator. The humans are SCRIPTED (an analyst who approves 90%, edits 7% and
rejects 3% of evidence bundles, and who confirms every proposed rule at the end of the week), so this shows how the learned-rules mechanism behaves
when humans behave in a stated way. It is not evidence about real analysts. The error rate is the independent auditor's unsafe-action count plus
cases closed without source proof.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any

from app.bench.templates import generate
from app.config import ROOT
from app.ext.config import ext_config
from app.ext.rules import Rules
from app.ext.store import get_store

MIX = {"status_query": 0.18, "refund_status": 0.10, "kyc_reminder": 0.07, "missing_evidence_flagship": 0.45, "correlated_kyc_decoy": 0.10, "supplier_promise_unsaveable": 0.10}
TENANT = "weeks-sim"


def run(weeks: int | None = None, cases_per_week: int | None = None, seed: int | None = None, use_rules: bool = True) -> dict[str, Any]:
    old_env = os.environ.get("RIPPLE_ENV")
    os.environ["RIPPLE_ENV"] = "dev"                          # a synthetic simulation: throwaway audit key, nothing real is touched
    from app.main import Session
    cfg = ext_config()["weeks"]
    weeks, per, seed = weeks or cfg["weeks"], cases_per_week or cfg["cases_per_week"], cfg["seed"] if seed is None else seed
    an = cfg["analyst"]
    rng = random.Random(seed)
    old = os.environ.get("RIPPLE_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="ripple-weeks-")
    os.environ["RIPPLE_DATA_DIR"] = tmp                       # records, audit files and rules of the simulation never touch the real ones
    try:
        store = get_store()
        rules = Rules(TENANT, store)
        out_weeks: list[dict[str, Any]] = []
        fams, wts = list(MIX), list(MIX.values())
        for w in range(1, weeks + 1):
            n = human = decisions = overrides = auto_rule = unsafe = false_closed = 0
            for i in range(per):
                fam = rng.choices(fams, wts)[0]
                s = Session(generate(fam, seed * 100 + w * 1000 + i), tenant=TENANT)
                if not use_rules:
                    s.agent.rules = None
                a = s.agent
                a.handle_message(s.scenario.inbound.text)
                for f in s.scenario.followups:
                    a.handle_message(f)
                touched = a.s.status.value in ("escalated",)
                for p in [p for p in a.s.plan if p.state == "awaiting_approval"]:
                    touched, decisions = True, decisions + 1
                    r = rng.random()
                    if r < an["approve"]:
                        a.analyst_decision(p.action.hash, True, "Priya", "analyst")
                    elif r < an["approve"] + an["edit"]:
                        overrides += 1
                        a.edit_plan_action(p.action.hash, list(p.action.params["docs"]) + (["courier_receipt"] if "courier_receipt" not in p.action.params["docs"] else []))
                        edited = next(q for q in a.s.plan if q.state == "awaiting_approval" and q.action.type == "submit_dispute_evidence")
                        a.analyst_decision(edited.action.hash, True, "Priya", "analyst")
                    else:
                        overrides += 1
                        a.analyst_decision(p.action.hash, False, "Priya", "analyst")
                auto_rule += sum(1 for st in a.s.steps if st.label.startswith("Approved under a confirmed rule"))
                s.world.advance(48)
                a.tick()
                n += 1
                human += int(touched)
                unsafe += len(s.unsafe())
                false_closed += int(a.s.closed and a.s.status.value == "verified" and s.world.settlement.read()["status"] != "released")
                s.delete_files()
            proposed = store.rules(TENANT, "proposed")
            for r in proposed:                                    # the scripted analyst confirms proposals at the end of the week
                rules.confirm(r["id"], "Priya")
            out_weeks.append({"week": w, "cases": n, "needed_a_person": human, "escalation_rate": round(human / n, 3), "human_decisions": decisions,
                              "overrides": overrides, "override_rate": round(overrides / decisions, 3) if decisions else None, "approved_by_rule": auto_rule,
                              "rules_confirmed_this_week": len(proposed), "rules_active": len(store.rules(TENANT, "active")), "unsafe_actions": unsafe, "false_closures": false_closed})
        return {"synthetic": True, "use_rules": use_rules, "seed": seed, "weeks": out_weeks, "analyst_model": an,
                "note": "Scripted analyst (approves 90%, edits 7%, rejects 3%, confirms every proposed rule). Shows the mechanism, not real analyst behaviour."}
    finally:
        for k, v in (("RIPPLE_DATA_DIR", old), ("RIPPLE_ENV", old_env)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "results" / "weeks_v1.json")
    ap.add_argument("--weeks", type=int, default=None)
    ap.add_argument("--cases", type=int, default=None)
    a = ap.parse_args()
    with_rules = run(a.weeks, a.cases, use_rules=True)
    without = run(a.weeks, a.cases, use_rules=False)
    res = {"with_rules": with_rules, "without_rules": without}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    for label, r in (("with rules", with_rules), ("without", without)):
        print(label)
        for w in r["weeks"]:
            print(f"  week {w['week']}: esc={w['escalation_rate']} override={w['override_rate']} byRule={w['approved_by_rule']} active={w['rules_active']} unsafe={w['unsafe_actions']} falseClose={w['false_closures']}")


if __name__ == "__main__":
    main()
