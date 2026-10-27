"""Merchant replies. Deterministic, fact-bound templates in Hindi / English / Hinglish.

An optional LLM may rephrase a draft, but only if the rewrite keeps every required fact token and
trips no guard. Otherwise the template is used. Replies never promise what cannot be verified.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.firewall.content import outbound_text_violations
from app.llm.client import LLM

T: dict[str, dict[str, str]] = {
    "status_processing": {
        "english": "Your {amount} settlement is processing and should reach you around {eta}. Nothing is wrong. {extra}",
        "hinglish": "Aapka {amount} ka settlement process ho raha hai aur lagbhag {eta} tak aapko milna chahiye. Koi problem nahi hai. {extra}",
        "hindi": "आपका {amount} का सेटलमेंट प्रोसेस हो रहा है और लगभग {eta} तक मिलना चाहिए। कोई समस्या नहीं है। {extra}",
    },
    "status_released": {
        "english": "Your {amount} settlement has been released (confirmed at the source).",
        "hinglish": "Aapka {amount} ka settlement release ho chuka hai (source system me confirm hai).",
        "hindi": "आपका {amount} का सेटलमेंट रिलीज़ हो चुका है (सोर्स सिस्टम में पुष्टि हुई)।",
    },
    "hold_explained": {
        "english": "Your {held} is on hold because a chargeback dispute is missing its evidence. I have prepared the evidence and sent it to our risk analyst for approval. {notice_line}I will confirm here once the source system shows a change. I cannot promise a release time yet.",
        "hinglish": "Aapke {held} ek chargeback dispute ke evidence na hone ki wajah se hold par hain. Maine evidence taiyaar karke risk analyst ko approval ke liye bhej diya hai. {notice_line}Source system me change dikhte hi yahin confirm kiya jayega. Abhi release ka time promise nahi kar sakte.",
        "hindi": "आपके {held} एक चार्जबैक विवाद के सबूत न होने की वजह से होल्ड पर हैं। मैंने सबूत तैयार करके रिस्क एनालिस्ट को मंज़ूरी के लिए भेज दिया है। {notice_line}सोर्स सिस्टम में बदलाव दिखते ही यहीं पुष्टि की जाएगी। अभी रिलीज़ का समय तय नहीं बता सकते।",
    },
    "notice_line": {
        "english": "Your supplier payment of {due_amt} is due {due_day}; if you like, I can send {supplier} a short delay notice. ",
        "hinglish": "Aapka {supplier} ko {due_amt} ka payment {due_day} ko due hai; chahein to main unhe chhota delay notice bhej sakta hoon. ",
        "hindi": "आपका {supplier} को {due_amt} का भुगतान {due_day} को देय है; चाहें तो मैं उन्हें देरी की सूचना भेज सकता हूँ। ",
    },
    "evidence_waiting": {
        "english": "Your evidence was submitted and is with risk review. Estimate, not a promise: usually about {eta_h} hours from now. {notice_line}",
        "hinglish": "Aapka evidence submit ho gaya hai aur risk review me hai. Andaza (promise nahi): abhi se lagbhag {eta_h} ghante. {notice_line}",
        "hindi": "आपका सबूत जमा हो गया है और रिस्क रिव्यू में है। अनुमान (वादा नहीं): अभी से लगभग {eta_h} घंटे। {notice_line}",
    },
    "refund_status": {
        "english": "Your refund of {amount} is {status_word}{eta_part}.",
        "hinglish": "Aapka {amount} ka refund {status_word}{eta_part}.",
        "hindi": "आपका {amount} का रिफंड {status_word}{eta_part}।",
    },
    "eta_pending": {
        "english": "Your evidence is waiting for analyst approval. If approved now, release usually takes about {eta_h} hours after submission (an estimate, not a promise).",
        "hinglish": "Aapka evidence analyst ki approval ka intezaar kar raha hai. Abhi approve ho to submission ke baad release me aam taur par lagbhag {eta_h} ghante lagte hain (andaza, promise nahi).",
        "hindi": "आपका सबूत एनालिस्ट की मंज़ूरी का इंतज़ार कर रहा है। अभी मंज़ूर हो तो जमा होने के बाद रिलीज़ में आमतौर पर लगभग {eta_h} घंटे लगते हैं (अनुमान, वादा नहीं)।",
    },
    "supplier_notice_ask": {
        "english": "Shall I send {supplier} this note? \"{text}\" Reply yes or no.",
        "hinglish": "Kya main {supplier} ko ye note bhej doon? \"{text}\" Haan ya nahi batayein.",
        "hindi": "क्या मैं {supplier} को यह संदेश भेजूँ? \"{text}\" हाँ या नहीं बताएं।",
    },
    "supplier_notice_sent": {
        "english": "Done. The note to {supplier} was sent (confirmed in the supplier system).",
        "hinglish": "Ho gaya. {supplier} ko note bhej diya gaya (supplier system me confirm hai).",
        "hindi": "हो गया। {supplier} को संदेश भेज दिया गया (सप्लायर सिस्टम में पुष्टि है)।",
    },
    "supplier_notice_declined": {
        "english": "Okay, I will not contact {supplier}.",
        "hinglish": "Theek hai, main {supplier} ko contact nahi karunga.",
        "hindi": "ठीक है, मैं {supplier} से संपर्क नहीं करूँगा।",
    },
    "resolved_released": {
        "english": "Good news: your {amount} has been released, confirmed at the source. Closing this case.",
        "hinglish": "Achhi khabar: aapke {amount} release ho gaye hain, source system me confirm hai. Case close kar rahe hain.",
        "hindi": "अच्छी खबर: आपके {amount} रिलीज़ हो गए हैं, सोर्स सिस्टम में पुष्टि है। केस बंद कर रहे हैं।",
    },
    "escalated": {
        "english": "I have passed this to our risk team with everything prepared. I have not changed anything yet, and I will update you here.",
        "hinglish": "Maine ye risk team ko poori taiyari ke saath de diya hai. Abhi kuch change nahi kiya gaya hai, yahin update dunga.",
        "hindi": "मैंने यह पूरी तैयारी के साथ रिस्क टीम को दे दिया है। अभी कुछ बदला नहीं गया है, यहीं अपडेट दूँगा।",
    },
    "refuse_forbidden": {
        "english": "I cannot release holds, change bank details or skip checks; only our risk team decides that. If your settlement is on hold, I can prepare the evidence for them.",
        "hinglish": "Main hold release, bank details change ya checks skip nahi kar sakta; ye sirf risk team decide karti hai. Agar settlement hold par hai to main unke liye evidence taiyaar kar sakta hoon.",
        "hindi": "मैं होल्ड रिलीज़, बैंक विवरण बदलना या जाँच छोड़ना नहीं कर सकता; यह सिर्फ़ रिस्क टीम तय करती है। सेटलमेंट होल्ड पर है तो मैं उनके लिए सबूत तैयार कर सकता हूँ।",
    },
    "clarify": {
        "english": "Sorry, I did not fully understand. Is this about your settlement status, a hold, or KYC?",
        "hinglish": "Maaf kijiye, poori baat samajh nahi aayi. Kya ye settlement status, hold, ya KYC ke baare me hai?",
        "hindi": "क्षमा करें, पूरी बात समझ नहीं आई। क्या यह सेटलमेंट स्टेटस, होल्ड या केवाईसी के बारे में है?",
    },
    "kyc_status": {
        "english": "Your KYC is {kyc_status}{kyc_exp}. {kyc_extra}",
        "hinglish": "Aapka KYC {kyc_status} hai{kyc_exp}. {kyc_extra}",
        "hindi": "आपका केवाईसी {kyc_status} है{kyc_exp}। {kyc_extra}",
    },
    "nothing_pending": {
        "english": "There is nothing waiting to be sent right now.",
        "hinglish": "Abhi bhejne ke liye kuch pending nahi hai.",
        "hindi": "अभी भेजने के लिए कुछ लंबित नहीं है।",
    },
    "approval_rejected": {
        "english": "Our analyst did not approve submitting this evidence, so nothing was submitted. A human will follow up with you here.",
        "hinglish": "Hamare analyst ne ye evidence submit karne ki approval nahi di, isliye kuch submit nahi hua. Ek insaan yahin aapse sampark karega.",
        "hindi": "हमारे एनालिस्ट ने यह सबूत जमा करने की मंज़ूरी नहीं दी, इसलिए कुछ जमा नहीं हुआ। कोई व्यक्ति यहीं आपसे संपर्क करेगा।",
    },
    "action_failed": {
        "english": "The action did not complete or could not be confirmed at the source, so I am not treating it as done. I have flagged it for a human.",
        "hinglish": "Action complete nahi hua ya source par confirm nahi hua, isliye use done nahi maana ja raha. Maine ise human ke liye flag kar diya hai.",
        "hindi": "कार्रवाई पूरी नहीं हुई या सोर्स पर पुष्टि नहीं हुई, इसलिए इसे पूरा नहीं माना जा रहा। मैंने इसे इंसान के लिए चिह्नित कर दिया है।",
    },
}

BANNED_PROMISES = ["guarantee", "guaranteed", "100%", "pakka", "definitely", "for sure", "zaroor milega", "pakka milega"]
CLAIMS_DONE = ["has been released", "release ho chuka", "release ho gaye", "रिलीज़ हो"]
LANGS = ("english", "hinglish", "hindi")


class ReplyDraft(BaseModel):
    kind: str
    language: str
    text: str
    must_include: list[str] = Field(default_factory=list)   # fact tokens a rewrite must keep
    polished: bool = False


def inr(n: int | None) -> str:
    return f"Rs {n:,}" if n is not None else "-"


def fmt_dt(iso: str | datetime | None) -> str:
    if not iso:
        return "soon"
    dt = datetime.fromisoformat(iso) if isinstance(iso, str) else iso
    return dt.strftime("%a %d %b, %H:%M")


def compose(kind: str, language: str, **f: Any) -> ReplyDraft:
    lang = language if language in LANGS else "english"
    f.setdefault("extra", "")
    f.setdefault("notice_line", "")
    text = T[kind][lang].format(**f).strip()
    keep = [str(f[k]) for k in ("amount", "held", "due_amt", "supplier", "eta", "eta_h", "due_day") if f.get(k) not in (None, "")]
    return ReplyDraft(kind=kind, language=lang, text=" ".join(text.split()), must_include=keep)


def notice_line(language: str, due_amt: int, due_day: str, supplier: str) -> str:
    lang = language if language in LANGS else "english"
    return T["notice_line"][lang].format(due_amt=inr(due_amt), due_day=due_day, supplier=supplier)


def validate_reply(text: str, must_include: list[str], source_released: bool, max_len: int = 900) -> list[str]:
    problems = []
    low = text.lower()
    problems += [f"missing fact: {t}" for t in must_include if t not in text]
    problems += [f"banned promise: {b}" for b in BANNED_PROMISES if b in low]
    if not source_released:
        problems += [f"claims done without proof: {c}" for c in CLAIMS_DONE if c in low]
    problems += [f"guard: {v}" for v in outbound_text_violations(text)]
    if len(text) > max_len:
        problems.append("too long")
    return problems


def polish(draft: ReplyDraft, llm: LLM | None, source_released: bool) -> ReplyDraft:
    """Optional LLM rephrase; accepted only when it passes validation. Never required."""
    if llm is None:
        return draft
    sys = ("Rewrite this customer-support reply to sound warm and natural in the SAME language. Keep every number, "
           "name and date exactly. Do not add promises, links, bank details or new facts. Reply as JSON: {\"text\": \"...\"}")
    data, _ = llm.complete_json("reply_polish", sys, json.dumps({"reply": draft.text, "language": draft.language}))
    if not data or not isinstance(data.get("text"), str):
        return draft
    new = " ".join(data["text"].split())
    if validate_reply(new, draft.must_include, source_released, max_len=int(len(draft.text) * 1.4) + 40):
        return draft
    return draft.model_copy(update={"text": new, "polished": True})
