import pytest

from app.audit.log import AuditLog
from app.bench.templates import CANONICAL_SEED, generate
from app.executor.executor import Executor
from app.firewall.firewall import Firewall, FirewallContext
from app.firewall.stores import ApprovalStore, ConsentStore
from app.mock_sources.world import World
from app.models.contracts import Action, VerificationState
from app.verifier.verify import closure_allowed, verify_resolution

DOCS = ["invoice", "delivery_proof", "customer_comm"]


@pytest.fixture
def env():
    scn = generate("missing_evidence_flagship", CANONICAL_SEED, canonical=True)
    w = World(scn.world_spec)
    audit = AuditLog()
    apps, cons = ApprovalStore(60), ConsentStore()
    fw = Firewall(apps, cons, audit)
    did = scn.world_spec.dispute.dispute_id
    ctx = FirewallContext(case_id="C1", merchant_id=scn.world_spec.merchant_id, now=w.now,
                          allowed_ids={"dispute_id": {did}})
    ex = Executor(w, fw, audit, apps, cons)
    ev = Action(case_id="C1", type="submit_dispute_evidence", params={"dispute_id": did, "docs": DOCS})
    return w, ex, ctx, apps, cons, audit, ev


def ctx_at(ctx, w):
    return ctx.model_copy(update={"now": w.now})


def test_denied_without_approval_touches_nothing(env):
    w, ex, ctx, _, _, audit, ev = env
    r = ex.execute(ev, ctx)
    assert r.outcome == "denied" and r.firewall.code == "approval_missing"
    assert w.call("read_dispute")["status"] == "evidence_required"
    assert not audit.find("tool_call")


def test_approved_flow_ladder_and_verification(env):
    w, ex, ctx, apps, _, audit, ev = env
    apps.issue(ev, "Priya", "analyst", w.now)
    r = ex.execute(ev, ctx)
    assert r.outcome == "executed" and r.verified
    assert r.ladder == ["attempted", "submitted", "approved", "executed", "verified"]
    assert audit.find("verification")[0].outcome == "verified"
    # approval is single use
    assert ex.execute(ev, ctx_at(ctx, w)).firewall.code == "approval_replay"


def test_submitted_evidence_is_not_a_resolution(env):
    w, ex, ctx, apps, _, _, ev = env
    apps.issue(ev, "Priya", "analyst", w.now)
    ex.execute(ev, ctx)
    read = lambda t: w.call(t)
    v = verify_resolution(read, set(w.tools))
    assert v.state == VerificationState.PARTIAL and not closure_allowed(v)
    w.advance(26)
    v = verify_resolution(read, set(w.tools))
    assert v.state == VerificationState.VERIFIED and closure_allowed(v)


def test_timeout_after_write_reconciles_without_blind_retry(env):
    w, ex, ctx, apps, _, audit, ev = env
    apps.issue(ev, "Priya", "analyst", w.now)
    w.inject_fault("submit_dispute_evidence", "timeout_after_write")
    r = ex.execute(ev, ctx)
    assert r.outcome == "executed" and r.reconciled and not r.retried and r.verified
    writes = [e for e in audit.find("tool_call") if e.data["action_type"] == "submit_dispute_evidence"]
    assert [e.outcome for e in writes] == ["timeout"]          # exactly one write attempt
    assert audit.find("reconcile")[0].outcome == "landed"


def test_action_succeeds_but_state_unchanged_is_not_verified(env):
    w, ex, ctx, apps, _, audit, ev = env
    apps.issue(ev, "Priya", "analyst", w.now)
    w.inject_fault("submit_dispute_evidence", "state_unchanged")
    r = ex.execute(ev, ctx)
    assert r.outcome == "executed" and not r.verified
    assert r.verification.state == VerificationState.FAILED
    assert "verified" not in r.ladder
    assert not closure_allowed(verify_resolution(lambda t: w.call(t), set(w.tools)))


def test_source_rejection_counts_as_failure_then_escalates(env):
    w, ex, ctx, apps, _, _, ev = env
    # a bad-but-schema-valid bundle (missing required docs) is rejected by the source
    bad = Action(case_id="C1", type="submit_dispute_evidence",
                 params={"dispute_id": ev.params["dispute_id"], "docs": ["invoice"]})
    apps.issue(bad, "Priya", "analyst", w.now)
    r = ex.execute(bad, ctx)
    assert r.outcome == "failed" and r.result["error"] == "incomplete"
    bad2 = Action(case_id="C1", type="submit_dispute_evidence",
                  params={"dispute_id": ev.params["dispute_id"], "docs": ["invoice", "courier_receipt"]})
    apps.issue(bad2, "Priya", "analyst", w.now)
    assert ex.execute(bad2, ctx).outcome == "failed"
    bad3 = Action(case_id="C1", type="submit_dispute_evidence",
                  params={"dispute_id": ev.params["dispute_id"], "docs": ["invoice", "bank_statement"]})
    apps.issue(bad3, "Priya", "analyst", w.now)
    assert ex.execute(bad3, ctx).firewall.code == "repeated_failure"


def test_tier2_consent_then_supplier_notice_verified(env):
    w, ex, ctx, _, cons, _, _ = env
    n = Action(case_id="C1", type="send_supplier_notice", params={"text": "Payment may arrive a day late. Sorry."})
    assert ex.execute(n, ctx).firewall.needs == "consent"
    cons.issue(n, ctx.merchant_id, w.now)
    r = ex.execute(n, ctx)
    assert r.verified and r.ladder == ["attempted", "approved", "executed", "verified"]


def test_tier1_merchant_message_verified_and_reads_go_through_firewall(env):
    w, ex, ctx, _, _, audit, _ = env
    m = Action(case_id="C1", type="send_merchant_message", params={"text": "Evidence is with the risk team."})
    r = ex.execute(m, ctx)
    assert r.verified and r.ladder == ["attempted", "executed", "verified"]
    assert ex.read("read_settlement", ctx)["status"] == "held_risk"
    with pytest.raises(PermissionError):
        ex.read("release_risk_hold", ctx)
