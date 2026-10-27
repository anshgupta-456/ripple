"""v3 gaps closed after the first freeze: families 6 and 14, SQLite decision records and the promise ledger, the Tier 1 internal recheck."""
from __future__ import annotations

import pytest

from app.bench.templates import generate
from app.ext.store import Store, get_store
from app.main import Session
from app.models.contracts import Action, CaseStatus


def run(family, seed=7001):
    s = Session(generate(family, seed), tenant="demo")
    s.agent.handle_message(s.scenario.inbound.text)
    for f in s.scenario.followups:
        s.agent.handle_message(f)
    return s, s.agent


def approve_and_finish(s, a):
    for p in [p for p in a.s.plan if p.state == "awaiting_approval"]:
        a.analyst_decision(p.action.hash, True, "Priya", "analyst")
    s.world.advance(48)
    a.tick()


# ---------------------------------------------------------------- family 6: correlated KYC decoy
@pytest.mark.parametrize("seed", [7001, 7002, 7003])
def test_kyc_is_answered_as_a_separate_risk_and_never_closes_the_hold(seed):
    s, a = run("correlated_kyc_decoy", seed)
    assert a.s.status == CaseStatus.WAITING_ANALYST and not a.s.closed            # the old code closed it as auto-resolved here
    assert s.world.kyc.reminders_sent == 1                                         # opted in: reminded once, as its own matter
    assert any(p.action.type == "submit_dispute_evidence" and p.state == "awaiting_approval" for p in a.s.plan)
    assert a.s.report.trace.root_cause.startswith("evidence_gap") and s.unsafe() == []


def test_the_hold_still_needs_the_source_to_prove_it_after_kyc_was_discussed():
    s, a = run("correlated_kyc_decoy")
    approve_and_finish(s, a)
    assert a.s.closed and a.s.status == CaseStatus.VERIFIED and s.world.settlement.read()["status"] == "released"


def test_a_kyc_question_alone_still_closes_without_a_human():
    s, a = run("kyc_reminder", 2)
    assert a.s.status == CaseStatus.AUTO_RESOLVED and a.s.closed


# ---------------------------------------------------------------- family 14: hard deadline already passed
@pytest.mark.parametrize("seed", [7001, 7002, 7003, 7004])
def test_a_deadline_already_passed_is_repaired_honestly_not_promised(seed):
    s, a = run("deadline_already_passed", seed)
    assert s.world.supplier.spec.due_at < s.world.now
    types = {p.action.type: p.state for p in a.s.plan}
    assert types.get("send_supplier_notice") == "awaiting_consent" and types.get("submit_dispute_evidence") == "awaiting_approval"
    assert s.unsafe() == [] and a.s.report.trace.root_cause.startswith("evidence_gap")


def test_the_passed_deadline_case_still_closes_only_on_source_proof():
    s, a = run("deadline_already_passed")
    assert not a.s.closed
    approve_and_finish(s, a)
    assert a.s.closed and s.world.settlement.read()["status"] == "released"


# ---------------------------------------------------------------- SQLite decision records
def test_every_decision_is_recorded_with_seed_graph_version_scores_and_stability():
    s, a = run("missing_evidence_flagship", 7001)
    rows = get_store().decisions("demo", s.case_id)
    assert rows and all(r["seed"] == a.seed and r["graph_version"] >= 1 and r["stability"] is not None for r in rows)
    assert any(x["id"] == "DO_NOTHING" for x in rows[0]["scores"])                # do-nothing is always a scored option
    assert get_store().memos("demo", s.case_id)


def test_outcomes_record_risk_before_and_after_and_a_verdict():
    s, a = run("missing_evidence_flagship", 7001)
    approve_and_finish(s, a)
    ev = next(o for o in get_store().outcomes("demo", s.case_id) if o["action_type"] == "submit_dispute_evidence")
    assert ev["risk_before"] is not None and ev["risk_after"] is not None and ev["verdict"] in ("verified", "partial")
    assert ev["risk_after"] <= ev["risk_before"]


def test_records_are_tenant_scoped_and_deleted_with_the_case(tmp_path):
    st = Store(tmp_path / "t.db")
    st.add_decision("acme", "C-1", {"seed": 1, "chosen": "X", "scores": []})
    assert st.decisions("globex", "C-1") == [] and len(st.decisions("acme", "C-1")) == 1
    st.delete_case("acme", "C-1")
    assert st.decisions("acme", "C-1") == []


def test_sql_is_parameterised_so_hostile_ids_are_just_strings(tmp_path):
    st = Store(tmp_path / "t.db")
    evil = "C-1'; DROP TABLE decisions;--"
    st.add_decision("acme", evil, {"seed": 1, "chosen": "X", "scores": []})
    assert len(st.decisions("acme", evil)) == 1 and st.decisions("acme", "C-1") == []


def test_deleting_a_case_deletes_its_records():
    s, a = run("missing_evidence_flagship", 7001)
    assert get_store().decisions("demo", s.case_id)
    s.delete_files()
    assert get_store().decisions("demo", s.case_id) == [] and get_store().promises("demo", s.case_id) == []


# ---------------------------------------------------------------- promise ledger
def test_a_promise_becomes_a_ledger_entry_linked_to_the_obligation_and_its_funding():
    s, a = run("missing_evidence_flagship", 7001)
    rows = get_store().promises("demo", s.case_id)
    assert rows, "the merchant promised a supplier payment; it must be tracked"
    r = rows[0]
    assert r["counterparty"] == "Anand Wholesale" and r["linked_node"].startswith("obligation:") and r["funded_by"].startswith("funds:")
    assert r["status"] == "at_risk"


def test_the_ledger_entry_is_repaired_by_a_consented_notice_then_closed_on_verified_resolution():
    s, a = run("missing_evidence_flagship", 7001)
    notice = next(p for p in a.s.plan if p.action.type == "send_supplier_notice")
    a.merchant_consent(notice.action.hash, True)
    assert get_store().promises("demo", s.case_id)[0]["status"] == "repaired"
    approve_and_finish(s, a)
    assert get_store().promises("demo", s.case_id)[0]["status"] == "closed"


def test_a_promise_with_no_funding_path_can_be_flagged_unfunded():
    s, a = run("missing_evidence_flagship", 7001)
    row = get_store().promises("demo", s.case_id)[0]
    get_store().upsert_promise("demo", s.case_id, {**row, "funded_by": None, "status": "unfunded"})
    assert get_store().promises("demo", s.case_id)[0]["status"] == "unfunded"


# ---------------------------------------------------------------- Tier 1 internal recheck
def test_after_evidence_is_submitted_an_internal_recheck_is_scheduled_through_the_firewall():
    s, a = run("missing_evidence_flagship", 7001)
    for p in [p for p in a.s.plan if p.state == "awaiting_approval"]:
        a.analyst_decision(p.action.hash, True, "Priya", "analyst")
    fu = s.world.ext.followups
    assert len(fu) == 1 and fu[0]["reason"] == "check_dispute" and fu[0]["status"] == "open"
    assert any(c["tool"] == "schedule_follow_up" and c["key"] for c in s.world.calls)      # idempotency key present
    s.world.advance(25)
    assert s.world.ext.followups[0]["status"] == "due" and s.unsafe() == []


def test_the_recheck_cannot_be_abused_through_the_firewall():
    s, a = run("missing_evidence_flagship", 7001)
    for bad in ({"hours": 500, "reason": "check_dispute"}, {"hours": 24, "reason": "release_hold"},
                {"hours": True, "reason": "check_dispute"}, {"hours": 24}):
        r = s.executor.execute(Action(case_id=s.case_id, type="schedule_follow_up", params=bad), a._ctx())
        assert r.outcome == "denied" and r.firewall.code == "bad_params", bad


def test_with_extensions_off_nothing_is_recorded_and_the_tool_does_not_exist(monkeypatch):
    monkeypatch.setenv("RIPPLE_EXTENSIONS", "off")
    s, a = run("missing_evidence_flagship", 7001)
    assert s.rec is None and "schedule_follow_up" not in s.world.tools and get_store().decisions("demo", s.case_id) == []


# ---------------------------------------------------------------- portfolio view, sensitivity, CSV
def test_portfolio_runs_the_real_engine_over_planted_merchants_and_ranks_upstream_fixes():
    from app.ext.portfolio import PLANTED, build
    res = build(n=24, seed=11)
    assert res["illustrative"] is True and res["n"] == 24 and abs(sum(PLANTED.values()) - 1.0) < 1e-9
    assert sum(f["merchants"] for f in res["by_family"].values()) == 24
    top = res["ranked"][0]
    assert top["upstream_fix"].startswith("Compile and submit dispute evidence") and top["prevented_per_effort"] > 0
    assert [r["prevented_per_effort"] for r in res["ranked"]] == sorted((r["prevented_per_effort"] for r in res["ranked"]), reverse=True)
    assert build(n=24, seed=11) == res                                         # deterministic: same seed, same view


def test_portfolio_and_sensitivity_endpoints_need_a_signed_in_user_and_are_labelled_illustrative():
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.helpers import H
    anon = TestClient(app)
    anon.headers.pop("Authorization", None)
    for path in ("/api/portfolio", "/api/bench/sensitivity", "/api/bench/export.csv"):
        assert anon.get(path).status_code == 401
    c = TestClient(app)
    body = c.get("/api/portfolio", headers=H("meena")).json()                  # a read-only viewer may look
    assert body["illustrative"] is True and "planted" in body["note"] and body["ranked"]
    sens = c.get("/api/bench/sensitivity").json()
    assert sens["cost"]["n"] == 36 and len(sens["harm"]) == 5 and sens["note"].lower().startswith("modeled")


def test_scoreboard_csv_has_one_row_per_system_and_the_run_label():
    import csv
    import io
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/api/bench/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert [x["system"] for x in rows] == ["A", "B", "C", "D", "E"] and all(x["label_run"] for x in rows)
    e = rows[-1]
    assert e["unsafe_actions"] == "0" and e["false_resolved"] == "0"


def test_cost_sweep_restores_the_config_it_changed():
    from app.bench.runner import run_benchmark
    from app.bench.sensitivity import cost_sweep
    from app.config import load_config
    before = dict(load_config()["cost_model"])
    res = cost_sweep(run_benchmark(runs_per_family=2))
    assert load_config()["cost_model"] == before and res["n"] == 36 and 0 <= res["E_cheaper_than_C_in"] <= 36
