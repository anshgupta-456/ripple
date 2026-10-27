import pytest

from app.bench.templates import REGISTRY, generate
from app.config import load_config
from app.mock_sources.world import World

NEW = ["duplicate_message", "refund_status", "kyc_reminder", "supplier_promise_unsaveable", "timeout_after_write",
       "state_unchanged", "conflicting_source_states", "weak_causal_edge", "prompt_injection"]


def test_twelve_families_registered():
    assert len(REGISTRY) == 12 and set(NEW) <= set(REGISTRY)


@pytest.mark.parametrize("family", list(REGISTRY))
@pytest.mark.parametrize("seed", [1, 2, 3, 7000, 7011])
def test_every_scenario_is_complete_and_deterministic(family, seed):
    a, b = generate(family, seed), generate(family, seed)
    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    ex, h = a.ground_truth["expected"], a.ground_truth["harness"]
    assert {"disposition", "required_human", "autonomy_allowed", "must_close_verified", "forbidden_tools"} <= set(ex)
    assert h["horizon_hours"] >= 48 and a.scenario_id == f"{family}_{seed}"
    World(a.world_spec)                                     # builds
    assert "ground_truth" not in a.public()


def test_held_out_families_come_from_frozen_config():
    held = set(load_config()["benchmark"]["held_out_families"])
    for f in REGISTRY:
        assert (generate(f, 1).split == "held_out") == (f in held)
    assert held <= set(REGISTRY)


def test_refund_world_completes_on_schedule():
    for seed in range(1, 12):
        scn = generate("refund_status", seed)
        w = World(scn.world_spec)
        r = w.call("read_refund")
        assert r["amount"] > 0
        if r["status"] == "processing":
            w.advance(40)
            assert w.call("read_refund")["status"] == "completed"
            assert any(e.type == "refund_completed" for e in w.events)


def test_duplicate_has_followup_with_same_text():
    s = generate("duplicate_message", 4)
    assert s.followups == [s.inbound.text]


def test_unsaveable_really_cannot_make_the_deadline():
    for seed in range(1, 8):
        s = generate("supplier_promise_unsaveable", seed)
        sp, ds = s.world_spec.supplier, s.world_spec.dispute
        due_h = (sp.due_at - s.world_spec.start).total_seconds() / 3600
        assert ds.review_hours + ds.release_delay_hours > due_h
        assert s.ground_truth["expected"]["unavoidable_miss"] is True


def test_conflict_and_weak_edge_world_setup():
    c = World(generate("conflicting_source_states", 1).world_spec)
    assert c.call("read_dispute")["status"] == "lost" and c.call("read_settlement")["hold_reason_code"] == "DISPUTE_EVIDENCE_MISSING"
    wk = World(generate("weak_causal_edge", 1).world_spec)
    assert "read_dispute" not in wk.tools and wk.call("read_kyc")["status"] == "expiring"


def test_injection_messages_cycle_and_are_flagged():
    from app.intake.extract import extract_rules
    for seed in range(0, 8):
        assert extract_rules(generate("prompt_injection", seed).inbound.text).intent == "forbidden_request"


def test_faults_declared_for_fault_families():
    assert generate("timeout_after_write", 1).ground_truth["harness"]["faults"] == [["submit_dispute_evidence", "timeout_after_write"]]
    assert generate("state_unchanged", 1).ground_truth["harness"]["faults"] == [["submit_dispute_evidence", "state_unchanged"]]
