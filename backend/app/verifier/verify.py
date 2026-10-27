"""Rule-based verifier. Re-reads authoritative source state; never trusts an action's own return value.

Two levels:
  verify_action      did THIS action change source state as intended?
  verify_resolution  is the merchant's underlying problem actually fixed?
Only a VERIFIED resolution may close a case. "Evidence submitted" is an action result, not a resolution.
"""
from __future__ import annotations

from typing import Any, Callable

from app.models.contracts import Action, Verification, VerificationState

V = VerificationState
EVIDENCE_LANDED = {"submitted", "under_review", "won"}


def _safe(read: Callable[[str], Any], tool: str) -> tuple[Any, str | None]:
    try:
        return read(tool), None
    except Exception as e:  # source unreachable => unknown, never assumed success
        return None, f"{tool} failed: {type(e).__name__}"


def verify_action(action: Action, pre: dict[str, Any], read: Callable[[str], Any]) -> Verification:
    t = action.type
    if t == "submit_dispute_evidence":
        d, err = _safe(read, "read_dispute")
        if err:
            return Verification(state=V.UNKNOWN, reason=err)
        st = d["status"]
        if st in EVIDENCE_LANDED:
            return Verification(state=V.VERIFIED, reason=f"dispute status is {st}", source_snapshot=d)
        return Verification(state=V.FAILED, reason=f"source acknowledged but dispute is still {st}", source_snapshot=d)
    if t == "send_supplier_notice":
        s, err = _safe(read, "read_supplier_obligation")
        if err:
            return Verification(state=V.UNKNOWN, reason=err)
        before = (pre.get("supplier") or {}).get("notices_sent", 0)
        ok = s and s["notices_sent"] > before
        return Verification(state=V.VERIFIED if ok else V.FAILED,
                            reason="supplier notice recorded" if ok else "no new supplier notice at source",
                            source_snapshot=s or {})
    if t == "send_merchant_message":
        n, err = _safe(read, "read_notifications")
        if err:
            return Verification(state=V.UNKNOWN, reason=err)
        ok = any(m["text"] == action.params.get("text") for m in n)
        return Verification(state=V.VERIFIED if ok else V.FAILED,
                            reason="message in notification log" if ok else "message not found in log")
    if t == "send_kyc_reminder":
        k, err = _safe(read, "read_kyc")
        if err:
            return Verification(state=V.UNKNOWN, reason=err)
        ok = k["reminders_sent"] > pre.get("kyc", {}).get("reminders_sent", 0)
        return Verification(state=V.VERIFIED if ok else V.FAILED,
                            reason="reminder recorded" if ok else "no new reminder at source", source_snapshot=k)
    if t in ("send_rekyc_link", "schedule_follow_up", "accept_bridge_offer", "send_debtor_reminder"):
        e, err = _safe(read, "read_ext_state")
        if err:
            return Verification(state=V.UNKNOWN, reason=err)
        before = pre.get("ext") or {}
        if t == "send_rekyc_link":
            ok, why = len(e["rekyc_links"]) > len(before.get("rekyc_links", [])), "re-KYC link recorded at source"
        elif t == "schedule_follow_up":
            ok, why = len(e["followups"]) > len(before.get("followups", [])), "follow-up scheduled at source"
        elif t == "accept_bridge_offer":
            ok = len(e["offers"]) > len(before.get("offers", [])) and e["offers"][-1]["status"] == "accepted"
            why = "offer accepted and disbursed at source"
        else:
            did = action.params.get("debtor_id")
            was = next((r["reminders"] for r in before.get("receivables", []) if r["debtor_id"] == did), 0)
            now = next((r["reminders"] for r in e["receivables"] if r["debtor_id"] == did), 0)
            ok, why = now > was, "reminder recorded for the debtor"
        return Verification(state=V.VERIFIED if ok else V.FAILED, reason=why if ok else f"not confirmed at source ({t})", source_snapshot=e)
    return Verification(state=V.UNKNOWN, reason=f"no verifier for {t}")


def verify_resolution(read: Callable[[str], Any], tools: set[str]) -> Verification:
    """Settlement-hold resolution: funds actually released at the source."""
    s, err = _safe(read, "read_settlement")
    if err:
        return Verification(state=V.UNKNOWN, reason=err)
    if s["status"] == "released":
        return Verification(state=V.VERIFIED, reason="settlement released at source", source_snapshot=s)
    if s["status"] == "processing" and not s.get("hold_reason_code"):
        return Verification(state=V.PARTIAL, reason="no hold; release still pending", source_snapshot=s)
    if s["status"] == "held_risk":
        if "read_dispute" in tools:
            d, _ = _safe(read, "read_dispute")
            if d and d["status"] in ("submitted", "under_review"):
                return Verification(state=V.PARTIAL, reason=f"evidence {d['status']}; hold not yet lifted",
                                    source_snapshot=s)
        return Verification(state=V.FAILED, reason="hold still active with no pending remediation", source_snapshot=s)
    return Verification(state=V.UNKNOWN, reason=f"unrecognised settlement status {s['status']}", source_snapshot=s)


def closure_allowed(v: Verification) -> bool:
    return v.state == V.VERIFIED
