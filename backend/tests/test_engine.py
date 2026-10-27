import pytest

from app.analysis import analyze_world
from app.bench.templates import CANONICAL_SEED, generate
from app.engine.decide import Disposition
from app.engine.reverse_trace import TraceStatus
from app.engine.scorer import DO_NOTHING
from app.mock_sources.world import World
from app.models.contracts import EdgeClass

DOCS = ["invoice", "delivery_proof", "customer_comm"]
BOTH = "SUBMIT_DISPUTE_EVIDENCE+SEND_SUPPLIER_NOTICE"


def flagship(canonical=True, seed=CANONICAL_SEED):
    scn = generate("missing_evidence_flagship", seed, canonical=canonical)
    return scn, World(scn.world_spec)


def test_flagship_traces_to_evidence_gap_and_ignores_kyc_decoy():
    _, w = flagship()
    r = analyze_world(w, 1)
    assert r.trace.status == TraceStatus.TRACED
    assert r.trace.root_cause.startswith("evidence_gap:")
    assert r.trace.fix_actions == ["submit_dispute_evidence"]
    assert any(e["src"].startswith("kyc:") and e["edge_class"] == "soft" for e in r.trace.rejected_edges)
    assert not any(n.startswith("kyc:") for n in r.trace.path)
    kyc = next(a for a in r.ranked if a.id == "SEND_KYC_REMINDER")
    assert kyc.decoy and kyc.score <= 0 and not kyc.permitted


def test_flagship_ranking_changes_with_simulation():
    _, w = flagship()
    r = analyze_world(w, 1)
    by = {a.id: a for a in r.ranked}
    assert r.obligation_at_risk and by[DO_NOTHING].p_miss > 0.75
    assert by["SUBMIT_DISPUTE_EVIDENCE"].p_miss < 0.4          # submitting cuts P(miss)
    assert by[BOTH].expected_harm < by["SUBMIT_DISPUTE_EVIDENCE"].expected_harm  # notice mitigates further
    assert r.chosen.id == BOTH
    assert r.disposition == Disposition.NEEDS_HUMAN_APPROVAL
    assert "tier3_action_needed" in r.triggers
    assert r.stability >= 0.8


def test_same_seed_same_report_and_seed_is_logged():
    _, w = flagship()
    a = analyze_world(w, 42).model_dump(mode="json")
    b = analyze_world(w, 42).model_dump(mode="json")
    assert a == b and a["seed"] == 42


def test_after_submission_trace_flips_to_waiting_and_miss_risk_drops():
    scn, w = flagship()
    before = analyze_world(w, 1)
    w.call("submit_dispute_evidence", {"dispute_id": scn.world_spec.dispute.dispute_id, "docs": DOCS}, "k")
    w.advance(2)
    after = analyze_world(w, 1, prev_graph=before.graph)
    assert after.trace.status == TraceStatus.TRACED_NOT_CONTROLLABLE
    assert after.trace.root_kind == "review_pending"
    assert after.graph.version > before.graph.version
    assert after.p_miss_do_nothing < 0.3 < before.p_miss_do_nothing
    ids = [a.id for a in after.ranked]
    assert "SUBMIT_DISPUTE_EVIDENCE" not in ids


def test_late_flagship_escalates_when_deadline_unreachable():
    _, w = flagship()
    w.advance(22)  # too late: review + release cannot fit before Tuesday 17:00
    r = analyze_world(w, 1)
    assert r.chosen.p_miss >= 0.95
    assert "deadline_unreachable" in r.triggers
    assert r.disposition == Disposition.ESCALATE


def test_no_action_family_chooses_do_nothing():
    scn = generate("no_action", 9)
    w = World(scn.world_spec)
    r = analyze_world(w, 1)
    assert not r.obligation_at_risk
    assert r.chosen.id == DO_NOTHING and r.disposition == Disposition.NO_ACTION
    assert r.trace.status == TraceStatus.NO_BLOCKER


def test_status_query_has_no_obligation_and_does_nothing():
    scn = generate("status_query", 3)
    r = analyze_world(World(scn.world_spec), 1)
    assert r.chosen.id == DO_NOTHING and r.disposition == Disposition.NO_ACTION


def test_soft_edge_only_forces_escalation_never_action():
    """Hold with an unrecognised reason and a correlated KYC expiry: no confirmed cause."""
    scn, w = flagship()
    w.settlement.s.hold_reason_code = "MANUAL_REVIEW"
    w.settlement.s.linked_dispute_id = None
    r = analyze_world(w, 1)
    assert r.trace.status == TraceStatus.NEEDS_EVIDENCE_OR_ESCALATE
    assert r.trace.fix_actions == []
    assert not any(a.steps and "submit_dispute_evidence" in a.steps for a in r.ranked)
    assert "soft_edge_dependency" in r.triggers and r.disposition == Disposition.ESCALATE
    classes = {e["edge_class"] for e in r.graph.edges}
    assert classes & {EdgeClass.SOFT.value, EdgeClass.UNKNOWN.value}


def test_conflicting_sources_escalate():
    _, w = flagship()
    w.dispute.status = "lost"          # settlement still says DISPUTE_EVIDENCE_MISSING
    r = analyze_world(w, 1)
    assert r.conflicts and "conflicting_sources" in r.triggers
    assert r.disposition == Disposition.ESCALATE


def test_kyc_reminder_permitted_only_when_opted_in():
    _, w = flagship()
    w.kyc.spec.opted_in_reminders = True
    r = analyze_world(w, 1)
    kyc = next(a for a in r.ranked if a.id == "SEND_KYC_REMINDER")
    assert kyc.permitted and kyc.tier == 1 and kyc.score <= 0   # still loses: no causal benefit
    assert r.chosen.id == BOTH


def test_no_tier4_option_is_ever_generated():
    _, w = flagship()
    r = analyze_world(w, 1)
    assert all(a.tier < 4 for a in r.ranked)
    assert all("release" not in s for a in r.ranked for s in a.steps)


@pytest.mark.parametrize("seed", range(1, 9))
def test_jittered_flagships_pick_an_evidence_path(seed):
    scn, w = flagship(canonical=False, seed=seed)
    r = analyze_world(w, 7)
    if scn.ground_truth["feasible_if_evidence_submitted_now"]:
        assert r.trace.root_cause.startswith("evidence_gap:")
        assert "SUBMIT_DISPUTE_EVIDENCE" in r.chosen.id or r.disposition == Disposition.ESCALATE
    assert not any(n.startswith("kyc:") for n in r.trace.path)
