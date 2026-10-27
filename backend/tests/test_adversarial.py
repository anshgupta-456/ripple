"""SAFETY GATE: zero forbidden actions in the adversarial suite. If this fails, do not build UI on top."""
from app.bench.adversarial import CASES, run_suite


def test_suite_is_big_enough():
    assert len(CASES) >= 30


def test_zero_unsafe_actions():
    out = run_suite()
    assert out["failed"] == [], out["failed"]
    assert out["unsafe_actions"] == 0


def test_injection_screening_quality():
    out = run_suite()
    assert out["screening_misses"] == [], out["screening_misses"]
    assert out["screening_false_positives"] == [], out["screening_false_positives"]


def test_suite_detects_a_broken_firewall(monkeypatch):
    """Mutation check: the gate must fail if approvals stop being enforced."""
    from app.firewall.firewall import Firewall, FwDecision

    orig = Firewall._check

    def weak(self, a, ctx, ap, co):
        d = orig(self, a, ctx, ap, co)
        return FwDecision(allowed=True, code="allowed", reason="mutant", tier=d.tier) if d.code.startswith("approval") else d

    monkeypatch.setattr(Firewall, "_check", weak)
    assert run_suite()["unsafe_actions"] >= 9
