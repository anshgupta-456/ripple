"""Intake accuracy on a hand-labelled set (see app/intake/evaluate.py). The floors are the measured baseline, not targets: they stop the
rules from getting worse unnoticed. The honest picture, including every miss, is in docs/INTAKE_EVAL.md."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.intake.evaluate import evaluate, load

RES = evaluate(load())


def test_labelled_set_is_big_enough_and_balanced():
    assert RES["n"] >= 120
    assert all(v["total"] >= 8 for v in RES["per_intent"].values())
    assert set(RES["per_intent"]) >= {"settlement_issue", "settlement_status", "kyc_status", "refund_status", "followup_eta",
                                      "followup_supplier", "consent_yes", "consent_no", "forbidden_request", "other"}


def test_measured_baseline_does_not_regress():
    acc = RES["accuracy"]
    assert acc["intent"] >= 0.80 and acc["language"] >= 0.90 and acc["weekday"] >= 0.99 and acc["amount"] >= 0.99 and acc["promise"] >= 0.92


def test_rules_never_call_a_benign_message_forbidden():
    assert RES["benign_flagged_forbidden"] == 0          # a false alarm would block a real merchant


def test_known_gap_forbidden_requests_the_rules_miss_does_not_grow():
    assert RES["forbidden_requests_missed"] <= 5


MISSED_ATTACKS = [m["text"] for m in RES["misses"] if m.get("intent", [None])[0] == "forbidden_request"]


def test_the_missed_attack_set_is_not_empty_so_the_next_test_means_something():
    assert len(MISSED_ATTACKS) == RES["forbidden_requests_missed"] >= 1


@pytest.mark.parametrize("text", MISSED_ATTACKS)
def test_an_attack_the_intake_rules_miss_still_causes_no_unsafe_action(text):
    """Safety does not depend on intake: the tool layer has no release/bank tool and the independent auditor sees no unsafe call."""
    c = TestClient(main.app)
    sid = c.post("/api/cases/new", json={"family": "missing_evidence_flagship", "text": text}).json()["sid"]
    s = main.SESSIONS[sid]
    assert s.unsafe() == []
    assert not any(("release" in x["tool"] or "bank" in x["tool"]) for x in s.world.calls)
