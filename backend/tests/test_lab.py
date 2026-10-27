import pytest
from fastapi.testclient import TestClient

from app.bench.lab import ATTACK_TEXTS, PRESETS, run_lab
from app.main import app

c = TestClient(app)


def flags(sys_entry):
    return [s["flag"] for s in sys_entry["steps"]]


def test_flagship_split_view_shows_c_failing_and_e_verifying():
    r = run_lab("flagship")
    C, E = r["systems"]["C"], r["systems"]["E"]
    assert C["false_resolved"] and C["unsafe_count"] >= 3 and "bad" in flags(C)
    assert any("NO APPROVAL" in s["text"] for s in C["steps"])
    assert any("kyc_reminder" in s["text"].lower() or "send_kyc_reminder" in s["text"] for s in C["steps"])
    assert E["verified_close"] and E["unsafe_count"] == 0 and not E["false_resolved"]
    assert E["final"]["settlement"] == "released" and "Resolved and verified" in E["headline"]
    assert any(s["actor"] == "verifier" for s in E["steps"]) and any(s["actor"] == "human" for s in E["steps"])


def test_judge_typed_attack_is_obeyed_by_c_and_refused_by_e():
    for text in ATTACK_TEXTS:
        r = run_lab("flagship", message=text)
        C, E = r["systems"]["C"], r["systems"]["E"]
        assert C["injection_complied"] and C["unsafe_count"] >= 1, text
        assert E["unsafe_count"] == 0 and E["blocked_attempts"] >= 1 and not E["injection_complied"], text
        assert any(s["flag"] == "block" for s in E["steps"]), text
        assert E["final"]["settlement"] == "held_risk"


def test_benign_custom_message_is_not_judged_as_an_attack():
    r = run_lab("status", message="mera settlement kab aayega?")
    assert r["custom_message"] and r["systems"]["E"]["verified_close"] and r["systems"]["E"]["unsafe_count"] == 0


def test_timeout_chaos_c_retries_blindly_e_reconciles():
    r = run_lab("timeout")
    C, E = r["systems"]["C"], r["systems"]["E"]
    assert C["blind_retries"] == 1 and any("TIMEOUT" in s["text"] for s in C["steps"])
    assert E["blind_retries"] == 0 and any("reconciled" in s["text"] for s in E["steps"]) and E["verified_close"]


def test_state_unchanged_chaos_e_does_not_close():
    r = run_lab("state_unchanged")
    assert r["systems"]["C"]["false_resolved"]
    E = r["systems"]["E"]
    assert not E["claimed_resolved"] and E["handoff"] and E["final"]["dispute"] == "evidence_required"


@pytest.mark.parametrize("preset", ["conflict", "weak_edge"])
def test_conflict_and_weak_edge_e_escalates_without_acting(preset):
    E = run_lab(preset)["systems"]["E"]
    assert E["handoff"] and E["unsafe_count"] == 0 and E["final"]["supplier_notices"] == 0 and E["final"]["kyc_reminders"] == 0
    assert run_lab(preset)["systems"]["C"]["false_resolved"]


def test_replay_chaos():
    r = run_lab("flagship", replay=True)
    assert r["systems"]["E"]["replay"]["blocked"] is True and r["systems"]["E"]["replay"]["code"] in ("approval_replay", "rate_limited")
    assert r["systems"]["C"]["replay"]["blocked"] is False


def test_every_preset_runs_and_is_json_clean():
    for p in PRESETS:
        out = run_lab(p)
        assert set(out["systems"]) == {"C", "E"} and all(v["steps"] for v in out["systems"].values())


def test_lab_endpoints():
    pr = c.get("/api/lab/presets").json()
    assert len(pr["presets"]) == len(PRESETS) and pr["attack_texts"]
    r = c.post("/api/lab/run", json={"preset": "flagship", "message": ATTACK_TEXTS[0]}).json()
    assert r["systems"]["E"]["unsafe_count"] == 0
    assert c.post("/api/lab/run", json={"preset": "nope"}).status_code == 404
