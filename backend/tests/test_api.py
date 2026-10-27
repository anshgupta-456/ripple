from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)


def test_health_and_scenarios():
    assert c.get("/api/health").json()["ok"] is True
    fams = {s["family"] for s in c.get("/api/scenarios").json()}
    assert {"missing_evidence_flagship", "status_query", "no_action"} <= fams


def test_reset_hides_ground_truth_and_flagship_flow():
    st = c.post("/api/session/reset", json={}).json()
    assert "ground_truth" not in st["scenario"]
    assert st["scenario"]["inbound"]["text"].startswith("mera settlement kyun ruka hai")
    assert st["world"]["settlement"]["status"] == "held_risk"
    docs = ["invoice", "delivery_proof", "customer_comm"]
    did = st["world"]["dispute"]["dispute_id"]
    r = c.post("/api/dev/tool", json={"tool": "submit_dispute_evidence", "idem_key": "a",
                                      "params": {"dispute_id": did, "docs": docs}})
    assert r.json()["result"]["ok"] is True
    st = c.post("/api/session/advance", json={"hours": 26}).json()
    assert st["world"]["settlement"]["status"] == "released"
    assert len(c.get("/api/session/events").json()) >= 4


def test_analyze_endpoint_returns_ranked_actions_without_acting():
    c.post("/api/session/reset", json={})
    r = c.post("/api/session/analyze", json={}).json()
    assert r["chosen"]["id"] == "SUBMIT_DISPUTE_EVIDENCE+SEND_SUPPLIER_NOTICE"
    assert r["disposition"] == "needs_human_approval" and r["seed"] == 5120
    assert c.get("/api/session/events").json() == []     # analysis takes no actions


def test_unknown_tool_404_and_timeout_504():
    c.post("/api/session/reset", json={})
    assert c.post("/api/dev/tool", json={"tool": "release_risk_hold", "idem_key": "x"}).status_code == 404
    c.post("/api/dev/fault", json={"tool": "send_merchant_message", "mode": "timeout_after_write"})
    r = c.post("/api/dev/tool", json={"tool": "send_merchant_message", "idem_key": "z", "params": {"text": "hi"}})
    assert r.status_code == 504 and r.json()["detail"]["outcome"] == "unknown"
