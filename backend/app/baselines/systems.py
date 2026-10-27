"""Baselines A, B, C.

A  Reactive chatbot      replies to the message, no tools.
B  Deadline tracker      flags late items and sends fixed reminders; never fixes anything.
C  Plain tool-calling    SCRIPTED PROXY of a competent tool-calling LLM agent: same understanding (rules intake),
   agent                 same tools, same reads; no graph, no firewall, no verification loop, no idempotency
                         discipline, no escalation tool (the tool list has none). Its documented habits:
                         acts on every plausible signal, trusts a tool's "ok", closes after acting, retries on
                         timeout with a fresh call, and obeys instructions found in the message.
                         It is a deterministic stand-in, not a live LLM, and the scoreboard says so.
"""
from __future__ import annotations

import itertools
from datetime import datetime

from app.agent.reply import compose, fmt_dt, inr
from app.baselines.common import BaseSystem
from app.intake.extract import extract_rules
from app.mock_sources.world import ToolTimeout, UnknownTool

GENERIC = {
    "english": "Thanks for reaching out. Please check the Settlements section of your dashboard for the latest status. Our team will look into your request.",
    "hinglish": "Aapke sampark ka shukriya. Latest status ke liye dashboard ke Settlements section ko dekhein. Hamari team aapki request dekhegi.",
    "hindi": "संपर्क करने के लिए धन्यवाद। ताज़ा स्थिति के लिए डैशबोर्ड का सेटलमेंट सेक्शन देखें। हमारी टीम आपका अनुरोध देखेगी।",
}
OVERPROMISE = {
    "english": "I have submitted your evidence and informed your supplier. This is now resolved and your money will arrive soon.",
    "hinglish": "Maine aapka evidence submit kar diya aur supplier ko bata diya. Ab ye resolve ho gaya hai aur paisa jaldi aa jayega.",
    "hindi": "मैंने आपका सबूत जमा कर दिया और सप्लायर को सूचित कर दिया। अब यह हल हो गया है और पैसा जल्दी आ जाएगा।",
}


class ChatbotSystem(BaseSystem):
    name, label = "A", "Reactive chatbot"

    def handle(self, text: str) -> None:
        intake = extract_rules(text)
        self.st.replies.append(GENERIC[intake.language])
        self.st.status = "replied"


class TrackerSystem(BaseSystem):
    name, label = "B", "Deadline tracker"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._n = itertools.count(1)

    def handle(self, text: str) -> None:
        intake = extract_rules(text)
        if intake.intent in ("consent_yes", "consent_no", "forbidden_request", "other"):
            self.st.status = "ignored"
            return
        settle = self.w.call("read_settlement")
        sup = self.w.call("read_supplier_obligation") if "read_supplier_obligation" in self.w.tools else None
        if sup and settle["status"] != "released":
            hours = (datetime.fromisoformat(sup["due_at"]) - self.w.now).total_seconds() / 3600
            if hours <= 48:
                msg = (f"Reminder: your payment of {inr(sup['amount'])} to {sup['name']} is due {fmt_dt(sup['due_at'])}. "
                       f"Your settlement is still pending.")
                self.w.call("send_merchant_message", {"text": msg}, f"b-{next(self._n)}")
                self.st.replies.append(msg)
                self.st.handoff = True            # flags the item to ops
                self.st.status = "flagged"
                return
        self.st.status = "no_flag"


class PlainAgentSystem(BaseSystem):
    name, label = "C", "Plain tool-calling agent (scripted proxy)"

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._n = itertools.count(1)

    # ---- tool use without discipline ----
    def _call(self, tool: str, params: dict | None = None) -> dict:
        for attempt in range(2):
            try:
                return self.w.call(tool, params or {}, f"c-{next(self._n)}")
            except ToolTimeout:
                if attempt == 0:
                    self.st.blind_retries += 1       # retries with a fresh call, no reconciliation
                    continue
                return {"ok": False, "error": "timeout"}
            except UnknownTool:
                return {"ok": False, "error": "unknown_tool"}
        return {"ok": False, "error": "unknown"}

    def _say(self, text: str) -> None:
        self._call("send_merchant_message", {"text": text})
        self.st.replies.append(text)

    @staticmethod
    def _worked(r: dict) -> bool:
        return bool(r.get("ok")) or "invalid_state" in str(r.get("error", ""))

    def handle(self, text: str) -> None:
        intake = extract_rules(text)
        lang, st = intake.language, self.st
        if intake.intent == "forbidden_request":
            tool = "change_bank_details" if "bank_change" in intake.injection_flags else "release_risk_hold"
            st.attempted_forbidden = True
            self._call(tool, {})                         # obeys the instruction; the tool layer decides
            self._say({"english": "Sorry, I could not do that right now.",
                       "hinglish": "Maaf kijiye, abhi ye nahi ho paya.", "hindi": "क्षमा करें, अभी यह नहीं हो पाया।"}[lang])
            return
        if intake.intent in ("consent_yes", "consent_no", "other", "followup_eta", "followup_supplier"):
            self._say(compose("clarify", lang).text)
            return
        if intake.intent == "refund_status" and "read_refund" in self.w.tools:
            r = self.w.call("read_refund")
            eta = f", expected by {fmt_dt(r['expected_by'])}" if r["status"] == "processing" and r["expected_by"] else ""
            self._say(f"Your refund of {inr(r['amount'])} is {r['status']}{eta}.")
            self._close()
            return
        if intake.intent == "kyc_status":
            k = self.w.call("read_kyc")
            if k["status"] in ("expiring", "expired"):
                rem = self._call("send_kyc_reminder")
                st.claimed_causes.append(f"kyc:{self.scn.world_spec.merchant_id}")
                if not self._worked(rem):
                    st.notes.append(f"kyc reminder failed: {rem.get('error')}")
            self._say(f"Your KYC is {k['status']}.")
            self._close()
            return

        settle = self.w.call("read_settlement")
        if settle["status"] in ("processing", "released"):
            kind = "status_released" if settle["status"] == "released" else "status_processing"
            self._say(compose(kind, lang, amount=inr(settle["amount_total"]), eta=fmt_dt(settle["expected_release"])).text)
            self._close()
            return

        # held settlement: act on every plausible signal
        acted = False
        if "read_dispute" in self.w.tools:
            d = self.w.call("read_dispute")
            if d["status"] == "evidence_required":
                r = self._call("submit_dispute_evidence", {"dispute_id": d["dispute_id"], "docs": d["required_docs"]})
                acted = acted or self._worked(r)
                st.claimed_causes.append(f"evidence_gap:{d['dispute_id']}")
        k = self.w.call("read_kyc")
        if k["status"] in ("expiring", "expired"):
            self._call("send_kyc_reminder")
            st.claimed_causes.append(f"kyc:{self.scn.world_spec.merchant_id}")
        sup = self.w.call("read_supplier_obligation")
        if sup and intake.promises:
            note = (f"Dear {sup['name']}, payment of {inr(sup['amount'])} may be delayed by about a day due to a "
                    f"pending settlement review. Thank you for your patience.")
            self._call("send_supplier_notice", {"text": note})
            acted = True
        if acted:
            self._say(OVERPROMISE[lang])
        else:
            self._say(compose("status_processing", lang, amount=inr(settle["amount_total"]), eta="soon").text)
        self._close()          # no verification loop: it believes it is done
