"""Udhaar-recovery plug-in (feature 19): recovering money customers owe the merchant on credit, as one more intervention type.

Used only when a cash gap makes it valuable (the supplier payment is still likely to be missed after the main rescue). A reminder to a debtor is
a message to a third party, so it is Tier 2: the merchant sees the exact text and consents. Tone is polite and relationship-aware. The agent never
shames a debtor, and a human decides whenever a debtor reports hardship or disputes the debt: no message is proposed at all.
"""
from __future__ import annotations

from typing import Any

from app.ext.config import ext_config

TEXT = {
    "voice_reminder": {
        "english": "Hello, a gentle reminder from {merchant}: {amount} is pending from your account. Whenever it is convenient, please settle it. Thank you.",
        "hinglish": "Namaste, {merchant} ki taraf se ek vinamr yaad: aapka {amount} baaki hai. Jab sahuliyat ho, kripya kar dein. Dhanyavaad.",
        "hindi": "नमस्ते, {merchant} की ओर से एक विनम्र याद: आपका {amount} बाकी है। जब सुविधा हो, कृपया चुका दें। धन्यवाद।",
    },
    "payment_link": {
        "english": "Hello, a gentle reminder from {merchant}: {amount} is pending. You can pay with a payment request in the Paytm app. Thank you.",
        "hinglish": "Namaste, {merchant} ki taraf se ek vinamr yaad: {amount} baaki hai. Paytm app mein payment request se aasaani se de sakte hain. Dhanyavaad.",
        "hindi": "नमस्ते, {merchant} की ओर से एक विनम्र याद: {amount} बाकी है। आप पेटीएम ऐप में पेमेंट रिक्वेस्ट से आसानी से दे सकते हैं। धन्यवाद।",
    },
    "split_payment": {
        "english": "Hello, a gentle reminder from {merchant}: {amount} is pending. If it helps, you are welcome to pay it in two parts. Thank you.",
        "hinglish": "Namaste, {merchant} ki taraf se ek vinamr yaad: {amount} baaki hai. Chahein to do hisson mein de sakte hain. Dhanyavaad.",
        "hindi": "नमस्ते, {merchant} की ओर से एक विनम्र याद: {amount} बाकी है। चाहें तो दो हिस्सों में दे सकते हैं। धन्यवाद।",
    },
}


def tone_violations(text: str) -> list[str]:
    low = text.lower()
    return [w for w in ext_config()["udhaar"]["shame_words"] if w in low]


def plan_udhaar(receivables: list[dict[str, Any]], gap_p_miss: float, merchant: str, language: str) -> dict[str, Any]:
    cfg = ext_config()["udhaar"]
    if gap_p_miss < cfg["min_gap_p_miss"]:
        return {"skip": "no cash gap that recovery would help with"}
    rows, vetoes = [], []
    for r in receivables:
        if r["hardship"] or r["disputed"]:
            vetoes.append({"debtor_id": r["debtor_id"], "name": r["name"], "reason": "debtor reported hardship" if r["hardship"] else "debtor disputes the debt",
                           "decision": "a human decides; no message is proposed"})
            continue
        if r["relationship"] < cfg["min_relationship"]:
            vetoes.append({"debtor_id": r["debtor_id"], "name": r["name"], "reason": "relationship too fragile for an automatic reminder", "decision": "a human decides"})
            continue
        best = None
        for aid, a in cfg["actions"].items():
            score = r["amount"] * a["uplift"] * r["relationship"] - a["cost"] * 50
            if best is None or score > best["score"]:
                best = {"action": aid, "label": a["label"], "score": round(score, 1), "uplift": a["uplift"], "cost": a["cost"]}
        rows.append({"debtor_id": r["debtor_id"], "name": r["name"], "amount": r["amount"], "overdue_days": r["overdue_days"], "relationship": r["relationship"], **best})
    rows.sort(key=lambda x: -x["score"])
    return {"candidates": rows, "vetoes": vetoes, "language": language}


def message_for(action_id: str, merchant: str, amount: int, language: str) -> str:
    lang = language if language in ("english", "hinglish", "hindi") else "english"
    text = TEXT[action_id][lang].format(merchant=merchant, amount=f"Rs {amount:,}")
    assert not tone_violations(text)                      # the templates are fixed; this guards future edits
    return text
