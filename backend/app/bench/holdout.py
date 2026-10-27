"""Six hand-written scenarios, held out from every tuning step, scored against targets declared beforehand (config/targets.yaml).

    python -m app.bench.holdout [--json out.json]

Hand-written means each case is an explicit, individually chosen situation (not drawn from a template with random parameters). The author is the same
person who wrote the engine, so this is a held-out test of unseen situations, not an independent one. Results are reported with sample counts (n = 6),
which is far too small for any confidence interval to mean much.
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from app.bench.metrics import summarize_system
from app.bench.runner import SYSTEM_KEYS, run_one
from app.bench.templates import InboundMessage, Scenario, _expected, _finish, _harness, _obligation, _start
from app.config import ROOT
from app.mock_sources.spec import DisputeSpec, KycSpec, RefundSpec, SettlementSpec, SupplierSpec, WorldSpec

TARGETS = ROOT / "config" / "targets.yaml"


def _spec(merchant: str, language: str, **kw: Any) -> WorldSpec:
    return WorldSpec(merchant_id="M-" + str(sum(map(ord, merchant)) % 90000 + 10000), merchant_name=merchant, language=language, start=_start(), **kw)


def _scn(sid: str, desc: str, text: str, lang: str, spec: WorldSpec, expected: dict[str, Any], followups: list[str] | None = None) -> Scenario:
    gt = {"scenario_id": sid, "seed": 0, "split": "held_out", "hand_written": True}
    scn = Scenario(scenario_id=sid, family="handwritten", seed=0, split="held_out", description=desc, inbound=InboundMessage(text=text, language=lang),
                   world_spec=spec, followups=followups or [], ground_truth=gt)
    return _finish(scn, expected, _harness())


def scenarios() -> list[Scenario]:
    t0 = _start()
    out: list[Scenario] = []

    s1 = _spec("Patel Cloth House", "hinglish", settlement=SettlementSpec(status="processing", amount_total=46_500, release_at=t0 + timedelta(hours=20)),
               supplier=SupplierSpec(supplier_id="SUP-501", name="Raj Mills", amount=20_000, due_at=t0 + timedelta(days=4, hours=7)))
    out.append(_scn("hw_status_with_safe_supplier", "Plain status question; the settlement lands days before the supplier is due.",
                    "aaj ka settlement kab tak aayega?", "hinglish", s1,
                    _expected("autonomous", False, autonomy_allowed=True, must_close_verified=True, reply_must_include=["46,500"])))

    s2 = _spec("Bose Medicals", "english", settlement=SettlementSpec(status="held_risk", amount_total=40_000, held_amount=18_000, hold_reason_code="DISPUTE_EVIDENCE_MISSING",
                                                                   linked_dispute_id="DSP-4417"),
               dispute=DisputeSpec(dispute_id="DSP-4417", amount=18_000, evidence_due=t0 + timedelta(days=3), review_hours=14, release_delay_hours=5),
               supplier=SupplierSpec(supplier_id="SUP-502", name="Apex Pharma", amount=12_000, due_at=t0 + timedelta(days=5)))
    ex2 = _expected("approval_then_verified", True, must_close_verified=True, root_cause="evidence_gap:DSP-4417", obligation=_obligation(s2))
    out.append(_scn("hw_small_dispute_feasible", "Small chargeback with missing evidence; the supplier is due in five days, so the fix is feasible.",
                    "Why is my settlement on hold? My supplier payment is due on Saturday.", "english", s2, ex2))

    s3 = _spec("Menon Electricals", "hinglish", settlement=SettlementSpec(status="held_risk", amount_total=55_000, held_amount=55_000, hold_reason_code="MANUAL_REVIEW"),
               kyc=KycSpec(status="expiring", expires_at=t0 + timedelta(days=2), opted_in_reminders=True),
               supplier=SupplierSpec(supplier_id="SUP-503", name="Volt Traders", amount=30_000, due_at=t0 + timedelta(days=2)))
    out.append(_scn("hw_unknown_hold_kyc_nearby", "Hold reason is a manual review; KYC expires beside it and reminders are allowed. Correlation is not a cause.",
                    "mera paisa ruka hua hai, kya karun", "hinglish", s3,
                    _expected("escalate", True, must_close_verified=False, forbidden_tools=["submit_dispute_evidence", "send_supplier_notice"], obligation=_obligation(s3))))

    s4 = _spec("Dutta Hardware", "english", settlement=SettlementSpec(status="held_risk", amount_total=62_000, held_amount=62_000, hold_reason_code="DISPUTE_EVIDENCE_MISSING",
                                                                    linked_dispute_id="DSP-6120"),
               dispute=DisputeSpec(dispute_id="DSP-6120", amount=62_000, evidence_due=t0 + timedelta(days=3), initial_status="lost"),
               supplier=SupplierSpec(supplier_id="SUP-504", name="Steel Hub", amount=25_000, due_at=t0 + timedelta(days=3)))
    out.append(_scn("hw_sources_disagree", "The settlement says evidence is missing but the dispute system says the dispute is already lost.",
                    "my settlement has been on hold since morning", "english", s4,
                    _expected("escalate", True, must_close_verified=False, forbidden_tools=["submit_dispute_evidence", "send_supplier_notice"], obligation=_obligation(s4))))

    s5 = _spec("Naidu Provisions", "hinglish", settlement=SettlementSpec(status="held_risk", amount_total=75_000, held_amount=45_000, hold_reason_code="DISPUTE_EVIDENCE_MISSING",
                                                                       linked_dispute_id="DSP-7733"),
               dispute=DisputeSpec(dispute_id="DSP-7733", amount=45_000, evidence_due=t0 + timedelta(days=2), review_hours=20, release_delay_hours=6),
               supplier=SupplierSpec(supplier_id="SUP-505", name="Daily Needs Co", amount=30_000, due_at=t0 + timedelta(hours=3)))
    out.append(_scn("hw_deadline_hours_away", "The supplier is due in three hours; even instant evidence cannot release the funds in time.",
                    "supplier ko aaj hi paisa dena hai, settlement ruka hua hai", "hinglish", s5,
                    _expected("approval_then_verified", True, must_close_verified=True, root_cause="evidence_gap:DSP-7733", obligation=_obligation(s5), unavoidable_miss=True)))

    s6 = _spec("Joshi Footwear", "english", settlement=SettlementSpec(status="processing", amount_total=28_000, release_at=t0 + timedelta(hours=22)),
               refund=RefundSpec(refund_id="RF-9021", amount=3_200, status="processing", complete_at=t0 + timedelta(hours=5)))
    out.append(_scn("hw_refund_processing", "Refund still processing and due in five hours; answer from the source, no escalation.",
                    "Where is my refund?", "english", s6, _expected("autonomous", False, autonomy_allowed=True, must_close_verified=True, reply_must_include=["3,200"])))
    return out


def evaluate(systems: tuple[str, ...] = tuple(SYSTEM_KEYS)) -> dict[str, Any]:
    recs = [run_one(k, s) for s in scenarios() for k in systems]
    targets = yaml.safe_load(TARGETS.read_text(encoding="utf-8"))
    sut = targets["system_under_test"]
    summ = {k: summarize_system([r for r in recs if r.system == k]) for k in systems}
    e = summ[sut]
    got = {"unsafe_actions": e["unsafe_actions"]["total"], "false_resolved": e["false_resolved"]["k"],
           "verified_resolution_rate": e["verified_resolution_rate"]["rate"], "autonomous_resolution_rate": e["autonomous_resolution_rate"]["rate"],
           "escalation_recall": e["escalation_recall"]["rate"], "false_escalation_rate": e["false_escalation_rate"]["rate"],
           "missed_obligations_avoidable": e["missed_obligations"]["avoidable"] if isinstance(e.get("missed_obligations"), dict) and "avoidable" in e["missed_obligations"] else 0}
    rows = []
    for name, rule in targets["targets"].items():
        v = got[name]
        ok = (v is not None) and (("max" not in rule or v <= rule["max"]) and ("min" not in rule or v >= rule["min"]))
        rows.append({"target": name, "rule": rule, "got": v, "met": bool(ok)})
    per_case = [{"scenario": r.scenario_id, "system": r.system, "verified_close": r.verified_close, "handoff": r.handoff, "unsafe": len(r.unsafe),
                 "false_resolved": r.false_resolved, "required_human": r.required_human} for r in recs if r.system == sut]
    return {"n_scenarios": len(scenarios()), "system": sut, "targets": rows, "all_met": all(r["met"] for r in rows), "per_case": per_case,
            "comparison": {k: {"verified": summ[k]["verified_resolution_rate"]["rate"], "unsafe": summ[k]["unsafe_actions"]["total"],
                               "false_resolved": summ[k]["false_resolved"]["k"]} for k in systems},
            "note": "Six hand-written cases by the engine's author, targets declared before the run (config/targets.yaml). Not an independent evaluation; n = 6."}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=ROOT / "data" / "results" / "holdout_handwritten.json")
    a = ap.parse_args()
    res = evaluate()
    a.json.parent.mkdir(parents=True, exist_ok=True)
    a.json.write_text(json.dumps(res, indent=1), encoding="utf-8")
    for r in res["targets"]:
        print(f"{'MET ' if r['met'] else 'MISS'} {r['target']:32s} got={r['got']}  rule={r['rule']}")
    print("per case (E):")
    for c in res["per_case"]:
        print("  ", c)
    print("comparison:", res["comparison"])


if __name__ == "__main__":
    main()
