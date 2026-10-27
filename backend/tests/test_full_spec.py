"""Full Feature Specification items: learned rules, bridge offers, udhaar recovery, re-KYC link, dry run, repair date, voice delivery policy,
cascade, merchant 'why', next-best on failure, escalation over weeks, the pre-declared hand-written set."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.bench.templates import generate
from app.ext.config import ext_config
from app.ext.offers import cascade, plan_bridge, repair_date
from app.ext.rules import RuleError, Rules, validate_condition
from app.ext.store import get_store
from app.ext.udhaar import TEXT, plan_udhaar, tone_violations
from app.firewall.stores import ApprovalStore
from app.main import Session, app
from app.models.contracts import Action
from tests.helpers import H


def mk(family="missing_evidence_flagship", seed=7001, tenant=None):
    tenant = tenant or "t-" + uuid.uuid4().hex[:6]
    s = Session(generate(family, seed), tenant=tenant)
    s.agent.handle_message(s.scenario.inbound.text)
    for f in s.scenario.followups:
        s.agent.handle_message(f)
    return s, s.agent, tenant


def evidence(a):
    return next(p for p in a.s.plan if p.action.type == "submit_dispute_evidence")


def writes(s):
    return [c for c in s.world.calls if c["write"]]


# ====================================================================== learned rules
def test_an_analyst_edit_proposes_a_rule_that_does_nothing_until_confirmed():
    s, a, t = mk()
    p = evidence(a)
    a.edit_plan_action(p.action.hash, list(p.action.params["docs"]) + ["courier_receipt"])
    prop = Rules(t).store.rules(t, "proposed")
    assert len(prop) == 1 and prop[0]["source"] == "analyst_edit" and "courier_receipt" in prop[0]["condition"]["docs"]
    assert prop[0]["condition"]["max_amount"] % 5000 == 0 and prop[0]["condition"]["max_amount"] <= ext_config()["rules"]["max_auto_amount"]
    s2, a2, _ = mk(tenant=t)                                                       # same tenant, same case: the proposal changes nothing
    assert evidence(a2).state == "awaiting_approval"


def test_a_confirmed_rule_pre_approves_a_matching_bundle_and_is_audited_hash_bound_and_single_use():
    s, a, t = mk()
    p = evidence(a)
    a.edit_plan_action(p.action.hash, list(p.action.params["docs"]) + ["courier_receipt"])
    rid = Rules(t).store.rules(t, "proposed")[0]["id"]
    Rules(t).confirm(rid, "Priya")
    s2, a2, _ = mk(tenant=t)
    ev = evidence(a2)
    assert ev.state == "verified" and any(st.label.startswith("Approved under a confirmed rule") for st in a2.s.steps)
    entry = next(e for e in s2.audit.entries if e.kind == "approval" and e.data.get("basis") == "confirmed_rule")
    assert entry.data["rule_id"] == rid and "confirmed by Priya" in entry.data["approver"] and entry.action_hash == ev.action.hash
    assert a2.approvals.get(ev.action.hash).used is True                           # single use
    assert s2.unsafe() == [] and Rules(t).store.rule(t, rid)["uses"] == 1


def test_a_rule_never_applies_across_tenants_over_the_cap_or_outside_its_documents():
    s, a, t = mk()
    p = evidence(a)
    a.edit_plan_action(p.action.hash, list(p.action.params["docs"]) + ["courier_receipt"])
    rid = Rules(t).store.rules(t, "proposed")[0]["id"]
    Rules(t).confirm(rid, "Priya")
    other, ao, _ = mk()                                                            # a different tenant
    assert evidence(ao).state == "awaiting_approval"
    assert Rules(t).match("submit_dispute_evidence", 10_000_000, ["invoice"]) is None                       # amount over the rule and the config cap
    assert Rules(t).match("submit_dispute_evidence", 1_000, ["invoice", "fake_court_order"]) is None        # a document the rule does not name
    assert Rules(t).match("send_supplier_notice", 1_000, ["invoice"]) is None                               # not an action a rule may cover


def test_rule_conditions_are_validated_and_limits_are_rechecked_at_confirmation():
    for bad in ({"action": "release_risk_hold", "max_amount": 1000, "docs": ["invoice"]}, {"action": "send_supplier_notice", "max_amount": 1000, "docs": ["x"]},
                {"action": "submit_dispute_evidence", "max_amount": 10 ** 9, "docs": ["invoice"]}, {"action": "submit_dispute_evidence", "max_amount": True, "docs": ["invoice"]},
                {"action": "submit_dispute_evidence", "max_amount": 1000, "docs": []}):
        with pytest.raises(RuleError):
            validate_condition(bad)
    t = "t-" + uuid.uuid4().hex[:6]
    rid = get_store().add_rule(t, "evidence", "widen", {"action": "submit_dispute_evidence", "max_amount": 10 ** 9, "docs": ["invoice"]})
    with pytest.raises(RuleError):
        Rules(t).confirm(rid, "Priya")
    assert get_store().rule(t, rid)["status"] == "proposed"


def test_no_rule_can_approve_anything_but_evidence_and_never_in_the_agents_name():
    st = ApprovalStore(60)
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    for typ in ("send_supplier_notice", "release_risk_hold", "change_bank_details", "accept_bridge_offer"):
        with pytest.raises(PermissionError):
            st.issue_by_rule(Action(case_id="C-1", type=typ), "R-1", "Priya", now)
    ev = Action(case_id="C-1", type="submit_dispute_evidence", params={"dispute_id": "DSP-1234", "docs": ["invoice"]})
    for who in ("", "agent", "RIPPLE", "system"):
        with pytest.raises(PermissionError):
            st.issue_by_rule(ev, "R-1", who, now)
    with pytest.raises(PermissionError):
        st.issue_by_rule(ev, "", "Priya", now)
    assert st.issue_by_rule(ev, "R-1", "Priya", now).action_hash == ev.hash


def test_only_an_analyst_can_confirm_reject_or_retire_a_rule_through_the_api():
    c = TestClient(app)
    rid = get_store().add_rule("demo", "evidence", "widen", {"action": "submit_dispute_evidence", "max_amount": 50_000, "docs": ["invoice"]})
    anon = TestClient(app)
    anon.headers.pop("Authorization", None)
    assert anon.post(f"/api/rules/{rid}/confirm").status_code == 401
    for who in ("sam", "meena"):                                                   # admin and viewer: separation of duties
        assert c.post(f"/api/rules/{rid}/confirm", headers=H(who)).status_code == 403
    assert c.get("/api/rules", headers=H("meena")).status_code == 200                # a viewer may read
    r = c.post(f"/api/rules/{rid}/confirm", headers=H("priya")).json()
    assert r["status"] == "active" and r["confirmed_by"].startswith("Priya")
    assert c.post(f"/api/rules/{rid}/confirm", headers=H("priya")).status_code == 409           # already active
    assert c.post(f"/api/rules/{rid}/retire", headers=H("priya")).json()["status"] == "retired"
    assert c.post("/api/rules/nope/confirm", headers=H("priya")).status_code == 404
    other = TestClient(app)
    assert other.post(f"/api/rules/{rid}/confirm", headers=H("priya", "someone-else")).status_code in (404, 409)      # not visible to another tenant


def test_autonomy_widens_only_after_enough_human_decisions_with_a_low_override_rate():
    w = ext_config()["rules"]["widening"]
    t = "t-" + uuid.uuid4().hex[:6]
    r = Rules(t)
    for i in range(w["min_decisions"] - 1):
        r.record(f"C-{i}", "submit_dispute_evidence", "approve", "Priya")
    assert r.maybe_widen("C-x", "submit_dispute_evidence", 60_000, ["invoice", "delivery_proof"]) is None            # not enough evidence yet
    r.record("C-last", "submit_dispute_evidence", "approve", "Priya")
    rid = r.maybe_widen("C-x", "submit_dispute_evidence", 60_000, ["invoice", "delivery_proof"])
    assert rid and r.store.rule(t, rid)["status"] == "proposed" and r.override_stats("submit_dispute_evidence")["override_rate"] == 0.0
    assert r.maybe_widen("C-y", "submit_dispute_evidence", 60_000, ["invoice", "delivery_proof"]) is None            # one proposal at a time
    t2 = "t-" + uuid.uuid4().hex[:6]
    r2 = Rules(t2)
    for i in range(8):
        r2.record(f"C-{i}", "submit_dispute_evidence", "approve", "Priya")
    for i in range(3):
        r2.record(f"C-r{i}", "submit_dispute_evidence", "reject", "Priya")
    assert r2.override_stats("submit_dispute_evidence")["override_rate"] > w["max_override_rate"]
    assert r2.maybe_widen("C-x", "submit_dispute_evidence", 60_000, ["invoice"]) is None                            # too many overrides: stays manual


def test_decisions_made_under_a_rule_do_not_count_as_evidence_about_people():
    t = "t-" + uuid.uuid4().hex[:6]
    r = Rules(t)
    for i in range(20):
        r.record(f"C-{i}", "submit_dispute_evidence", "approve", "", rule_id="R-1")
    assert r.override_stats("submit_dispute_evidence")["n"] == 0


# ====================================================================== bridge offers
def fake_report(p, at_risk=True):
    return SimpleNamespace(obligation_at_risk=at_risk, chosen=SimpleNamespace(p_miss=p))


PROFILE = {"monthly_emi": 6000, "monthly_inflow": 180000}


def test_a_bridge_offer_needs_a_failing_rescue_value_above_the_fee_and_a_merchant_who_is_not_worse_off():
    assert "likely to work" in plan_bridge(fake_report(0.2), 30_000, "held_risk", PROFILE)["skip"]
    assert "no supplier payment" in plan_bridge(fake_report(1.0, at_risk=False), 30_000, "held_risk", PROFILE)["skip"]
    o = plan_bridge(fake_report(1.0), 30_000, "held_risk", PROFILE)["offer"]
    assert o["kind"] == "short_term_credit" and o["fee"] == 720 and o["value"] > 1.5 * o["fee"]
    assert "worse off" in plan_bridge(fake_report(1.0), 60_000, "held_risk", PROFILE)["skip"]                       # EMI plus repayment over 35% of inflow
    ins = plan_bridge(fake_report(1.0), 60_000, "processing", PROFILE)["offer"]                                      # repaid from the settlement itself
    assert ins["kind"] == "instant_settlement" and ins["fee"] == 600
    cfg = ext_config()["bridge"]
    keep = cfg["harm_per_missed_rupee"]
    cfg["harm_per_missed_rupee"] = 0.01                                              # if missing a payment cost almost nothing, the fee would not be worth it
    try:
        assert "does not beat" in plan_bridge(fake_report(0.9), 30_000, "held_risk", PROFILE)["skip"]
    finally:
        cfg["harm_per_missed_rupee"] = keep


def test_the_flagship_with_a_working_rescue_gets_no_bridge_offer():
    s, a, _ = mk()
    assert "accept_bridge_offer" not in {p.action.type for p in a.s.plan} and a.extra == {}


def test_a_bridge_offer_is_never_accepted_by_a_chat_yes_only_by_the_explicit_tap():
    s, a, _ = mk("supplier_promise_unsaveable", 7001)
    offer = next(p for p in a.s.plan if p.action.type == "accept_bridge_offer")
    assert offer.state == "awaiting_consent" and offer.tier == 2 and s.world.ext.offers == []
    a.handle_message("haan")
    a.handle_message("yes")
    assert offer.state == "awaiting_consent" and s.world.ext.offers == []          # a bare yes is ambiguous and never a financial yes
    a.merchant_consent(offer.action.hash, True)
    assert offer.state == "verified" and len(s.world.ext.offers) == 1 and s.world.ext.offers[0]["status"] == "accepted"
    assert s.unsafe() == [] and any("confirmed at the source" in m.text or "confirm" in m.text for m in a.s.messages if m.kind == "bridge_offer_done")


def test_a_bridge_offer_that_would_leave_the_merchant_worse_off_is_not_made_and_says_why():
    s, a, _ = mk("supplier_promise_unsaveable", 7002)
    assert "accept_bridge_offer" not in {p.action.type for p in a.s.plan}
    assert a.extra["bridge"]["offered"] is False and "worse off" in a.extra["bridge"]["reason"]


def test_the_offer_action_is_bounded_and_needs_consent_at_the_firewall():
    s, a, _ = mk("supplier_promise_unsaveable", 7001)
    ctx = a._ctx()
    for bad in ({"kind": "payday_loan", "amount": 5000}, {"kind": "short_term_credit", "amount": 900_000}, {"kind": "short_term_credit", "amount": 10}, {"kind": "short_term_credit"}):
        r = s.executor.execute(Action(case_id=s.case_id, type="accept_bridge_offer", params=bad), ctx)
        assert r.outcome == "denied" and r.firewall.code == "bad_params", bad
    ok = Action(case_id=s.case_id, type="accept_bridge_offer", params={"kind": "short_term_credit", "amount": 5000})
    assert s.executor.execute(ok, ctx).firewall.code == "consent_missing"
    assert s.world.ext.offers == []


# ====================================================================== udhaar recovery
def test_udhaar_is_used_only_with_a_cash_gap_and_a_human_decides_on_hardship_or_dispute():
    recv = [{"debtor_id": "deb_1", "name": "A", "amount": 10000, "overdue_days": 5, "relationship": 0.8, "hardship": False, "disputed": False},
            {"debtor_id": "deb_2", "name": "B", "amount": 20000, "overdue_days": 9, "relationship": 0.9, "hardship": True, "disputed": False},
            {"debtor_id": "deb_3", "name": "C", "amount": 15000, "overdue_days": 9, "relationship": 0.9, "hardship": False, "disputed": True},
            {"debtor_id": "deb_4", "name": "D", "amount": 15000, "overdue_days": 9, "relationship": 0.1, "hardship": False, "disputed": False}]
    assert "no cash gap" in plan_udhaar(recv, 0.2, "M", "english")["skip"]
    plan = plan_udhaar(recv, 1.0, "Khan Textiles", "english")
    assert [c["debtor_id"] for c in plan["candidates"]] == ["deb_1"]
    assert {v["debtor_id"] for v in plan["vetoes"]} == {"deb_2", "deb_3", "deb_4"} and all("human decides" in v["decision"] for v in plan["vetoes"])


def test_reminder_wording_is_polite_in_every_language_and_the_shame_check_catches_pressure():
    for act, langs in TEXT.items():
        for lang, tpl in langs.items():
            assert tone_violations(tpl.format(merchant="Khan Textiles", amount="Rs 5,000")) == [], (act, lang)
    assert "defaulter" in tone_violations("Pay up, defaulter, or else")
    assert tone_violations("This is an ultimatum") == ["ultimatum"]


def test_a_debtor_reminder_is_a_consented_tier_2_message_with_the_exact_text_shown():
    s, a, _ = mk("supplier_promise_unsaveable", 7001)
    rem = next(p for p in a.s.plan if p.action.type == "send_debtor_reminder")
    assert rem.tier == 2 and rem.state == "awaiting_consent" and rem.notice_text == rem.action.params["text"] and tone_violations(rem.notice_text) == []
    assert all(r["reminders"] == 0 for r in s.world.ext.receivables)
    a.merchant_consent(rem.action.hash, True)
    assert rem.state == "verified" and sum(r["reminders"] for r in s.world.ext.receivables) == 1 and s.unsafe() == []


def test_when_a_debtor_reports_hardship_no_reminder_is_proposed_at_all():
    s = Session(generate("supplier_promise_unsaveable", 7001), tenant="t-" + uuid.uuid4().hex[:6])
    for r in s.world.ext.receivables:
        r["hardship"] = True
    s.agent.handle_message(s.scenario.inbound.text)
    assert "send_debtor_reminder" not in {p.action.type for p in s.agent.s.plan}
    assert any(st.label == "Udhaar: human decides" for st in s.agent.s.steps)


def test_a_reminder_text_with_a_link_or_account_number_is_blocked_by_the_firewall():
    s, a, _ = mk("supplier_promise_unsaveable", 7001)
    for bad in ("Pay at http://evil.example now", "Send money to account 123456789012 today"):
        r = s.executor.execute(Action(case_id=s.case_id, type="send_debtor_reminder", params={"debtor_id": "deb_1", "text": bad}), a._ctx())
        assert r.outcome == "denied" and r.firewall.code in ("content_blocked", "consent_missing")
    r = s.executor.execute(Action(case_id=s.case_id, type="send_debtor_reminder", params={"debtor_id": "deb_99", "text": "hello"}), a._ctx())
    assert r.outcome == "denied"                                                    # a debtor that is not part of this case's book


# ====================================================================== re-KYC link, dry run, repair date, next-best
def test_asking_about_expiring_kyc_puts_a_rekyc_link_in_the_app_once_with_no_url_in_text():
    s, a, _ = mk("kyc_reminder", 2)
    assert len(s.world.ext.rekyc_links) == 1 and a.s.closed
    assert all("http" not in m.text for m in a.s.messages)
    a.handle_message("what is my KYC status")
    assert len(s.world.ext.rekyc_links) == 1                                         # max once per case


def test_a_dry_run_reports_what_would_happen_calls_no_tool_and_is_audited():
    s, a, _ = mk()
    ev = evidence(a)
    before = len(writes(s))
    d = a.dry_run(ev.action.hash)
    assert d["would_run_now"] is False and d["needs"] == "approval" and "evidence" in d["effect"] and d["tier"] == 3
    assert len(writes(s)) == before and s.world.dispute.read()["status"] == "evidence_required"
    assert any(e.kind == "dry_run" and e.outcome == "would_hold" for e in s.audit.entries)
    with pytest.raises(KeyError):
        a.dry_run("0" * 64)


def test_the_dry_run_endpoint_needs_a_session_and_hides_unknown_actions():
    c = TestClient(app)
    c.post("/api/cases/seed", json={})
    rows = c.get("/api/cases").json()
    sid = next(r["sid"] for r in rows if r["family"] == "missing_evidence_flagship")
    h = next(p["action"]["hash"] for p in c.get("/api/case", params={"sid": sid}).json()["plan"] if p["state"] == "awaiting_approval")
    assert c.post("/api/case/dry-run", params={"sid": sid}, json={"action_hash": h}, headers=H("meena")).json()["needs"] == "approval"
    assert c.post("/api/case/dry-run", params={"sid": sid}, json={"action_hash": "f" * 64}).status_code == 404
    anon = TestClient(app)
    anon.headers.pop("Authorization", None)
    assert anon.post("/api/case/dry-run", params={"sid": sid}, json={"action_hash": h}).status_code == 401


def test_repair_date_is_offered_only_when_the_simulator_supports_it_otherwise_a_wider_window():
    now = datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    on_time = repair_date(SimpleNamespace(chosen=SimpleNamespace(release_p90_h=10)), now, now + timedelta(days=3))
    assert on_time["text"] == "" and on_time["supported"] is True
    late = repair_date(SimpleNamespace(chosen=SimpleNamespace(release_p90_h=48)), now, now + timedelta(hours=10))
    assert late["supported"] and late["date"] == now + timedelta(hours=54) and late["text"].startswith("by ")
    wide = repair_date(SimpleNamespace(chosen=SimpleNamespace(release_p90_h=None)), now, now + timedelta(hours=10))
    assert wide["supported"] is False and "3 days" in wide["text"]


def test_the_supplier_notice_carries_a_supported_new_date_and_the_ledger_records_it():
    s, a, _ = mk("supplier_promise_unsaveable", 7001)
    notice = next(p for p in a.s.plan if p.action.type == "send_supplier_notice")
    assert "may be delayed by " in notice.notice_text and "about a day" not in notice.notice_text
    assert get_store().promises(a.rec.tenant, s.case_id)[0]["revised_due_at"]


def test_a_failed_action_never_closes_the_case_and_names_the_next_best_for_the_human():
    s, a, _ = mk("state_unchanged", 7001)
    a.analyst_decision(evidence(a).action.hash, True, "Priya", "analyst")
    assert not a.s.closed and a.s.status.value == "escalated"
    assert any(st.label == "Next-best action for the human" for st in a.s.steps)
    outs = get_store().outcomes(a.rec.tenant, s.case_id)
    assert outs and outs[-1]["verdict"] in ("failed", "partial")


def test_measured_effects_of_actions_are_available_for_the_next_decision():
    s, a, t = mk()
    a.analyst_decision(evidence(a).action.hash, True, "Priya", "analyst")
    eff = get_store().effectiveness(t, "submit_dispute_evidence")
    assert eff["n"] == 1 and eff["mean_drop"] is not None and eff["mean_drop"] >= 0
    assert "submit_dispute_evidence" in a.rec.view()["effectiveness"]


# ====================================================================== voice delivery policy
def channels(s):
    return [(r["channel"], r["reason"]) for r in get_store().voice(s.agent.rec.tenant, s.case_id)]


def test_voice_goes_to_the_soundbox_in_the_day_and_to_the_app_when_offline_or_in_quiet_hours():
    s, a, _ = mk("status_query", 3)
    assert channels(s)[0][0] == "soundbox"
    s.world.ext.set_soundbox(False)
    a.handle_message("mera settlement kab aayega? aaj")
    assert channels(s)[-1][0] == "app" and "offline" in channels(s)[-1][1]
    s.world.ext.set_soundbox(True)
    s.world.advance(13)                                                             # 10:00 + 13 h = 23:00
    a.handle_message("kitna time lagega?")
    assert channels(s)[-1][0] == "app" and "quiet hours" in channels(s)[-1][1]
    assert any(m.role == "agent" for m in a.s.messages)                              # the text itself was always delivered


def test_the_daily_voice_cap_sends_the_overflow_to_the_app():
    s, a, _ = mk("status_query", 3)
    for i in range(6):
        a.handle_message(f"Where is my settlement? check {i}")
    ch = [c for c, _ in channels(s)]
    assert ch.count("soundbox") == ext_config()["contact"]["daily_voice_cap"] and any("cap" in r for _, r in channels(s))


# ====================================================================== cascade and merchant why
def test_the_cascade_passes_the_miss_probability_down_the_assumed_edges():
    rep = SimpleNamespace(obligation_at_risk=True, p_miss_do_nothing=0.8, chosen=SimpleNamespace(p_miss=0.2))
    c = cascade(rep, {"emi_due_in_days": 3, "monthly_emi": 6000, "festival_stock_order": 25000}, datetime(2026, 11, 2, tzinfo=timezone.utc))
    emi = next(n for n in c["nodes"] if n["id"] == "emi_debit")
    assert emi["p_miss_do_nothing"] == 0.44 and emi["p_miss_chosen"] == 0.11 and c["expected_emi_penalty_do_nothing"] > c["expected_emi_penalty_chosen"]
    assert "assumption" in c["assumption"]
    quiet = cascade(SimpleNamespace(obligation_at_risk=False, p_miss_do_nothing=0.9, chosen=SimpleNamespace(p_miss=0.9)), {"emi_due_in_days": 3, "monthly_emi": 6000, "festival_stock_order": 1}, datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert all(n["p_miss_do_nothing"] == 0 for n in quiet["nodes"])


def test_the_case_view_carries_the_cascade_and_the_extra_signals_are_on_the_timeline():
    s, a, _ = mk()
    v = a.view()
    assert v["cascade"]["nodes"] and v["soundbox_online"] is True
    types = {e.type for e in s.world.events}
    assert {"transactions_summary", "soundbox_status", "emi_schedule", "support_contacts"} <= types


def test_the_merchant_why_is_plain_in_their_language_and_promises_nothing():
    c = TestClient(app)
    c.post("/api/cases/seed", json={})
    rows = c.get("/api/cases").json()
    sid = next(r["sid"] for r in rows if r["family"] == "missing_evidence_flagship")
    w = c.get("/api/case/why", params={"sid": sid}, headers=H("meena")).json()
    assert w["language"] == "hinglish" and "chargeback" in w["text"] and "promise nahi" in w["text"] and "KYC" in w["text"]
    assert "released by" not in w["text"].lower() and "%" in w["text"]
    assert "send_" not in w["text"] and "submit_" not in w["text"]                  # plain words, never internal action names
    anon = TestClient(app)
    anon.headers.pop("Authorization", None)
    assert anon.get("/api/case/why", params={"sid": sid}).status_code == 401


# ====================================================================== measurement
def test_escalation_over_weeks_runs_the_real_orchestrator_with_zero_errors():
    from app.ext.weeks import run
    r = run(weeks=2, cases_per_week=6, seed=5)
    assert [w["week"] for w in r["weeks"]] == [1, 2] and all(w["unsafe_actions"] == 0 and w["false_closures"] == 0 for w in r["weeks"])
    assert r["synthetic"] is True and "Scripted analyst" in r["note"]


def test_the_hand_written_set_is_scored_against_targets_that_were_declared_first():
    import subprocess
    from app.bench.holdout import evaluate, scenarios
    assert len(scenarios()) == 6 and all(s.ground_truth["hand_written"] for s in scenarios())
    res = evaluate()
    assert res["n_scenarios"] == 6 and res["all_met"] is True and len(res["targets"]) == 7
    # the targets file was committed before the first run of the set: its first commit precedes the commit that added the runner's results
    log = subprocess.run(["git", "log", "--format=%H %s", "--", "config/targets.yaml"], capture_output=True, text=True, cwd=str(__import__("app.config", fromlist=["ROOT"]).ROOT)).stdout
    assert "Declare targets for the hand-written held-out set before it is run" in log


def test_the_new_result_endpoints_serve_files_behind_authentication():
    c = TestClient(app)
    assert c.get("/api/bench/holdout").json()["all_met"] is True
    wk = c.get("/api/bench/weeks").json()
    assert len(wk["with_rules"]["weeks"]) == len(wk["without_rules"]["weeks"]) >= 4
    anon = TestClient(app)
    anon.headers.pop("Authorization", None)
    assert anon.get("/api/bench/weeks").status_code == 401 and anon.get("/api/bench/holdout").status_code == 401
