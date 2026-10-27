import pytest

from app.bench.templates import CANONICAL_SEED, REGISTRY, generate
from app.mock_sources.world import MissingIdempotencyKey, ToolTimeout, UnknownTool, World

DOCS = ["invoice", "delivery_proof", "customer_comm"]


def flagship(seed=CANONICAL_SEED, canonical=True):
    scn = generate("missing_evidence_flagship", seed, canonical=canonical)
    return scn, World(scn.world_spec)


def submit(w, scn, key="k1", docs=DOCS):
    did = scn.world_spec.dispute.dispute_id
    return w.call("submit_dispute_evidence", {"dispute_id": did, "docs": docs}, key)


def test_canonical_flagship_numbers():
    scn, w = flagship()
    s = w.call("read_settlement")
    assert s["status"] == "held_risk" and s["held_amount"] == 72_000
    assert s["hold_reason_code"] == "DISPUTE_EVIDENCE_MISSING"
    assert w.call("read_supplier_obligation")["amount"] == 60_000
    assert w.call("read_kyc")["status"] == "expiring"  # the decoy
    assert scn.ground_truth["feasible_if_evidence_submitted_now"] is True


def test_no_release_hold_tool_exists():
    _, w = flagship()
    for name in ("release_risk_hold", "change_bank_details", "approve_dispute"):
        assert name not in w.tools
        with pytest.raises(UnknownTool):
            w.call(name, {}, "x")


def test_full_lifecycle_releases_before_deadline():
    scn, w = flagship()
    assert submit(w, scn)["ok"]
    w.advance(26)  # 20h review + 6h release
    assert w.call("read_dispute")["status"] == "won"
    assert w.call("read_settlement")["status"] == "released"
    due = scn.world_spec.supplier.due_at
    released = [e for e in w.events if e.type == "released"][0]
    assert released.ts < due


def test_late_submission_misses_deadline():
    scn, w = flagship()
    w.advance(10)  # agent sat idle
    submit(w, scn)
    w.advance(40)
    released = [e for e in w.events if e.type == "released"][0]
    assert released.ts > scn.world_spec.supplier.due_at


def test_incomplete_evidence_is_rejected_and_state_unchanged():
    scn, w = flagship()
    r = submit(w, scn, docs=["invoice"])
    assert r["ok"] is False and "delivery_proof" in r["missing"]
    assert w.call("read_dispute")["status"] == "evidence_required"


def test_write_requires_idempotency_key():
    scn, w = flagship()
    with pytest.raises(MissingIdempotencyKey):
        w.call("send_merchant_message", {"text": "hi"}, None)


def test_idempotent_replay_does_not_double_apply():
    scn, w = flagship()
    w.call("send_merchant_message", {"text": "hi"}, "k")
    r = w.call("send_merchant_message", {"text": "hi"}, "k")
    assert r["replayed"] is True
    assert len(w.call("read_notifications")) == 1


def test_timeout_after_write_applies_then_reconciles_by_key():
    scn, w = flagship()
    w.inject_fault("submit_dispute_evidence", "timeout_after_write")
    with pytest.raises(ToolTimeout):
        submit(w, scn, key="kt")
    # outcome unknown to the caller; the write landed. Query by key, never blind retry.
    assert w.lookup_by_key("kt")["result"]["ok"] is True
    assert w.call("read_dispute")["status"] == "submitted"
    assert submit(w, scn, key="kt")["replayed"] is True


def test_state_unchanged_fault_acks_without_applying():
    scn, w = flagship()
    w.inject_fault("submit_dispute_evidence", "state_unchanged")
    assert submit(w, scn)["ok"] is True
    assert w.call("read_dispute")["status"] == "evidence_required"  # verifier must catch this


def test_kyc_reminder_requires_opt_in():
    _, w = flagship()
    assert w.call("send_kyc_reminder", {}, "k")["error"] == "not_opted_in"


def test_events_are_source_tagged_and_ordered():
    scn, w = flagship()
    submit(w, scn)
    w.advance(26)
    ids = [e.id for e in w.events]
    assert ids == sorted(ids)
    assert {e.source.value for e in w.events} == {"dispute", "settlement"}
    assert all(e.source_ref for e in w.events)


@pytest.mark.parametrize("family", list(REGISTRY))
def test_generation_is_deterministic(family):
    a = generate(family, 77).model_dump(mode="json")
    b = generate(family, 77).model_dump(mode="json")
    assert a == b


@pytest.mark.parametrize("family", list(REGISTRY))
def test_public_view_hides_ground_truth(family):
    pub = generate(family, 3).public()
    assert "ground_truth" not in pub


def test_jittered_flagship_varies_with_seed_and_has_ground_truth():
    a = generate("missing_evidence_flagship", 1)
    b = generate("missing_evidence_flagship", 2)
    assert a.world_spec != b.world_spec
    for s in (a, b):
        assert s.ground_truth["earliest_controllable_cause"].startswith("dispute:")
        assert s.ground_truth["decoys"] == ["kyc:expiring"]


def test_no_action_release_precedes_obligation():
    scn = generate("no_action", 9)
    w = World(scn.world_spec)
    w.advance(12)
    assert w.call("read_settlement")["status"] == "released"
    assert w.events[0].ts < scn.world_spec.supplier.due_at


def test_held_out_split_comes_from_frozen_config():
    assert generate("no_action", 1).split == "held_out"
    assert generate("status_query", 1).split == "tuning"
