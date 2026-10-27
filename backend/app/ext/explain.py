"""The merchant's plain-language answer to "why did you do that?" (feature 16). Built only from the decision record and the case's actions, in
the merchant's language; it states what is verified and what is still pending, and promises nothing that cannot be verified."""
from __future__ import annotations

from typing import Any

T = {
    "english": {
        "cause": "Your settlement is held because a chargeback dispute is missing its evidence. That is the earliest thing we can fix.",
        "decoy": "Your KYC expiry is a separate matter and is not the reason for the hold.",
        "did": "What I did: ", "pending": "Still waiting on: ", "none": "nothing yet",
        "closing": "I will tell you here once the source system shows the change. I cannot promise a release time.",
        "no_cause": "I could not find a cause that the records support, so I have asked a person to look at it instead of guessing.",
        "generic": "I looked at your settlement, your supplier payment and your open cases, and only acted where the records support it.",
        "deadline": "Your supplier payment is due {due}; if nothing were done there was about a {p0}% chance of missing it, and with this plan about {p1}%.",
    },
    "hinglish": {
        "cause": "Aapka settlement isliye hold par hai kyunki ek chargeback dispute ka evidence jama nahi hua. Yahi sabse pehli cheez hai jo hum theek kar sakte hain.",
        "decoy": "Aapka KYC expiry alag baat hai, hold ki wajah ye nahi hai.",
        "did": "Maine kya kiya: ", "pending": "Abhi intezaar: ", "none": "abhi kuch nahi",
        "closing": "Source system me change dikhte hi yahin bataunga. Release ka time promise nahi kar sakta.",
        "no_cause": "Records se koi pakka kaaran nahi mila, isliye andaaza lagane ke bajaye ek insaan ko dekhne ke liye kaha hai.",
        "generic": "Maine aapka settlement, supplier payment aur open cases dekhe, aur sirf wahi kiya jo records support karte hain.",
        "deadline": "Aapka supplier payment {due} ko due hai; kuch na karte to usse chook jaane ka lagbhag {p0}% khatra tha, is plan ke saath lagbhag {p1}%.",
    },
    "hindi": {
        "cause": "आपका सेटलमेंट इसलिए होल्ड पर है क्योंकि एक चार्जबैक विवाद का सबूत जमा नहीं हुआ। यही सबसे पहली चीज़ है जो हम ठीक कर सकते हैं।",
        "decoy": "आपका केवाईसी की समाप्ति अलग बात है, होल्ड की वजह यह नहीं है।",
        "did": "मैंने क्या किया: ", "pending": "अभी इंतज़ार: ", "none": "अभी कुछ नहीं",
        "closing": "सोर्स सिस्टम में बदलाव दिखते ही यहीं बताऊँगा। रिलीज़ का समय तय नहीं बता सकता।",
        "no_cause": "रिकॉर्ड से कोई पक्का कारण नहीं मिला, इसलिए अंदाज़ा लगाने के बजाय एक इंसान से देखने को कहा है।",
        "generic": "मैंने आपका सेटलमेंट, सप्लायर भुगतान और खुले मामले देखे, और सिर्फ़ वही किया जो रिकॉर्ड समर्थन करते हैं।",
        "deadline": "आपका सप्लायर भुगतान {due} को देय है; कुछ न करने पर चूकने का लगभग {p0}% ख़तरा था, इस योजना के साथ लगभग {p1}%।",
    },
}
DONE = {
    "submit_dispute_evidence": {"english": "submitted the dispute evidence", "hinglish": "dispute ka evidence jama kiya", "hindi": "विवाद का सबूत जमा किया"},
    "send_supplier_notice": {"english": "sent a short note to your supplier", "hinglish": "supplier ko chhota note bheja", "hindi": "सप्लायर को छोटा संदेश भेजा"},
    "accept_bridge_offer": {"english": "set up the short-term cover you accepted", "hinglish": "aapka manzoor kiya short-term cover lagaya", "hindi": "आपका मंज़ूर किया शॉर्ट-टर्म कवर लगाया"},
    "send_debtor_reminder": {"english": "sent the polite reminder to your customer", "hinglish": "aapke customer ko vinamr yaad bheji", "hindi": "आपके ग्राहक को विनम्र याद भेजी"},
}
WAIT = {
    "submit_dispute_evidence": {"english": "approval from our risk analyst for the dispute evidence", "hinglish": "dispute evidence par risk analyst ki manzoori", "hindi": "विवाद के सबूत पर रिस्क एनालिस्ट की मंज़ूरी"},
    "send_supplier_notice": {"english": "your yes to the supplier note", "hinglish": "supplier note par aapki haan", "hindi": "सप्लायर संदेश पर आपकी हाँ"},
    "accept_bridge_offer": {"english": "your tap on the short-term cover offer (nothing happens without it)", "hinglish": "short-term cover offer par aapka tap (iske bina kuch nahi hoga)", "hindi": "शॉर्ट-टर्म कवर ऑफर पर आपका टैप (इसके बिना कुछ नहीं होगा)"},
    "send_debtor_reminder": {"english": "your yes to the reminder for your customer", "hinglish": "customer ko yaad bhejne par aapki haan", "hindi": "ग्राहक को याद भेजने पर आपकी हाँ"},
}


def why_merchant(language: str, report: Any | None, plan: list[Any], due_text: str | None) -> dict[str, Any]:
    lang = language if language in T else "english"
    t = T[lang]
    parts: list[str] = []
    facts: dict[str, Any] = {}
    if report is None:
        parts.append(t["generic"])
    else:
        rc = report.trace.root_cause or ""
        if rc.startswith("evidence_gap"):
            parts.append(t["cause"])
            if any(e.get("edge_class") in ("soft",) or "kyc" in str(e.get("src", "")) for e in report.trace.rejected_edges):
                parts.append(t["decoy"])
        else:
            parts.append(t["no_cause"])
        done = [DONE.get(p.action.type, {}).get(lang, "an action") for p in plan if p.state == "verified"]
        wait = [WAIT.get(p.action.type, {}).get(lang, "a decision") for p in plan if p.state in ("awaiting_approval", "awaiting_consent")]
        parts.append(t["did"] + (", ".join(done) if done else t["none"]) + ".")
        if wait:
            parts.append(t["pending"] + ", ".join(wait) + ".")
        if report.obligation_at_risk and due_text:
            parts.append(t["deadline"].format(due=due_text, p0=round(report.p_miss_do_nothing * 100), p1=round(report.chosen.p_miss * 100)))
        facts = {"seed": report.seed, "graph_version": report.graph.version, "chosen": report.chosen.id}
    parts.append(t["closing"])
    return {"language": lang, "text": " ".join(parts), "facts": facts}
