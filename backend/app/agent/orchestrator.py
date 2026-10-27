"""Case orchestrator: message -> context -> decide -> gate -> act -> re-read -> verify -> reply -> close/keep open.

The LLM (optional) only extracts and rephrases. Every decision comes from the deterministic engine,
every action goes through the firewall + executor, and a case closes only on a VERIFIED source state.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.agent.evidence import EvidenceBundle, compile_bundle
from app.agent.memo import AnalystMemo, build_memo
from app.agent.reply import ReplyDraft, compose, fmt_dt, inr, notice_line, polish
from app.audit.log import AuditLog
from app.audit.trace import next_seq
from app.security.governance import agent_blocked
from app.config import load_config
from app.engine.decide import DecisionReport, Disposition, decide
from app.engine.scorer import DO_NOTHING
from app.executor.executor import ExecResult, Executor
from app.firewall.firewall import Firewall, FirewallContext
from app.firewall.stores import ApprovalStore, ConsentStore
from app.graph.builder import GraphSnapshot
from app.intake.extract import IntakeResult, extract
from app.llm.client import LLM
from app.mock_sources.world import World
from app.models.contracts import Action, CaseStatus, Verification, VerificationState
from app.timeline.normalizer import Timeline, collect
from app.verifier.verify import closure_allowed, verify_resolution

WEEKDAY_FMT = "%a %d %b"


class Message(BaseModel):
    role: str            # merchant | agent | system
    text: str
    ts: str
    kind: str = ""
    gseq: int = 0


class Step(BaseModel):
    ts: str
    label: str
    detail: str = ""
    gseq: int = 0


class PlannedAction(BaseModel):
    action: Action
    label: str
    tier: int
    state: str           # awaiting_approval | awaiting_consent | verified | rejected | failed | denied
    note: str = ""
    notice_text: str | None = None
    ladder: list[str] = Field(default_factory=list)


class CaseState(BaseModel):
    case_id: str
    status: CaseStatus = CaseStatus.OPEN
    language: str = "hinglish"
    intent: str = ""
    messages: list[Message] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    plan: list[PlannedAction] = Field(default_factory=list)
    memo: AnalystMemo | None = None
    bundle: EvidenceBundle | None = None
    report: DecisionReport | None = None
    resolution: Verification | None = None
    closed: bool = False
    clarify_count: int = 0
    last_intake: IntakeResult | None = None


class Orchestrator:
    def __init__(self, world: World, executor: Executor, firewall: Firewall, approvals: ApprovalStore,
                 consents: ConsentStore, audit: AuditLog, case_id: str, seed: int, llm: LLM | None = None,
                 trace_mode: str = "reverse"):
        self.w, self.ex, self.fw = world, executor, firewall
        self.approvals, self.consents, self.audit = approvals, consents, audit
        self.seed, self.llm = seed, llm
        self.trace_mode = trace_mode
        self.cfg = load_config()
        self.s = CaseState(case_id=case_id, language=world.spec.language)
        self._prev_graph: GraphSnapshot | None = None
        self._tl: Timeline | None = None

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return self.w.now.isoformat()

    def _step(self, label: str, detail: str = "") -> None:
        self.s.steps.append(Step(ts=self._now(), label=label, detail=detail, gseq=next_seq()))

    def _ctx(self) -> FirewallContext:
        w = self.w
        ids = {"dispute_id": {w.spec.dispute.dispute_id}} if w.spec.dispute else {}
        return FirewallContext(case_id=self.s.case_id, merchant_id=w.spec.merchant_id, now=w.now, allowed_ids=ids,
                               kyc_opted_in=w.kyc.spec.opted_in_reminders)

    def _read(self, key_tool: str) -> Any:
        return self.ex.read(key_tool, self._ctx())

    def _timeline(self, claims: dict[str, Any] | None = None) -> Timeline:
        tools = set(self.w.tools)
        tl = collect(lambda t: self.ex.read(t, self._ctx()), tools, self.w.now, self.w.spec.merchant_id,
                     self.w.events, claims if claims is not None else {"supplier_paid_from_settlement": self._claim})
        self._tl = tl
        return tl

    _claim = False

    def _decide(self, tl: Timeline) -> DecisionReport:
        """Run the deterministic engine and record WHY in the audit trail: seed, graph version, chosen action, triggers."""
        r = decide(tl, set(self.w.tools), self.seed, self._prev_graph, trace_mode=self.trace_mode)
        self.audit.add(self.w.now, "decision", case_id=self.s.case_id, outcome=r.disposition.value, code=r.chosen.id,
                       seed=r.seed, graph_version=r.graph.version, graph_digest=r.graph.digest, stability=r.stability,
                       p_miss_do_nothing=r.p_miss_do_nothing, triggers=r.triggers, trace_status=r.trace.status.value,
                       root_cause=r.trace.root_cause, rejected_edges=[e["src"] for e in r.trace.rejected_edges],
                       trace_mode=self.trace_mode)
        return r

    def _say(self, kind: str, **facts: Any) -> ExecResult | None:
        draft: ReplyDraft = compose(kind, self.s.language, **facts)
        released = self._read("read_settlement")["status"] == "released"
        draft = polish(draft, self.llm, released)
        a = Action(case_id=self.s.case_id, type="send_merchant_message", params={"text": draft.text})
        res = self.ex.execute(a, self._ctx())
        if res.outcome == "executed" and res.verified:
            self.s.messages.append(Message(role="agent", text=draft.text, ts=self._now(), kind=kind, gseq=next_seq()))
        else:
            self._step("Reply blocked or unverified", f"{kind}: {res.firewall.code} / {res.outcome}")
        return res

    def _set_status(self, st: CaseStatus) -> None:
        self.s.status = st

    # ------------------------------------------------------------------ entry points
    def handle_message(self, text: str) -> CaseState:
        s = self.s
        reason = agent_blocked()
        if reason:
            self._step("Agent disabled", f"{reason}: message recorded, no action taken")
            s.messages.append(Message(role="merchant", text=text, ts=self._now(), gseq=next_seq()))
            return s
        prev = next((m for m in reversed(s.messages) if m.role == "merchant"), None)
        s.messages.append(Message(role="merchant", text=text, ts=self._now(), gseq=next_seq()))
        if (prev and " ".join(prev.text.lower().split()) == " ".join(text.lower().split()) and s.last_intake
                and s.last_intake.intent in ("settlement_status", "settlement_issue", "refund_status", "kyc_status")):
            self._step("Duplicate message ignored", "identical to the previous message; no new action")
            return s
        intake = extract(text, self.llm, self.cfg["policy"]["extraction_confidence_min"])
        s.last_intake, s.intent, s.language = intake, intake.intent, intake.language
        self._step("Understood message", f"intent={intake.intent} language={intake.language} confidence={intake.confidence} via {intake.source}")
        if intake.injection_flags:
            self.audit.add(self.w.now, "injection_flag", case_id=s.case_id, outcome="flagged",
                           code=",".join(intake.injection_flags), text=text[:200])
            self._step("Flagged untrusted text", ", ".join(intake.injection_flags))

        if intake.intent == "forbidden_request":
            return self._forbidden(intake, text)
        if intake.confidence < self.cfg["policy"]["extraction_confidence_min"] or intake.intent == "other":
            return self._clarify()
        s.clarify_count = 0

        if intake.intent in ("consent_yes", "consent_no"):
            return self._chat_consent(intake.intent == "consent_yes")
        if intake.intent == "followup_eta":
            return self._followup_eta()
        if intake.intent == "followup_supplier":
            return self._followup_supplier()
        if intake.intent == "kyc_status":
            return self._kyc()
        if intake.intent == "refund_status":
            return self._refund()
        self._claim = any(p.confidence >= 0.5 for p in intake.promises) or self._claim
        return self._settlement_case(intake, text)

    def tick(self) -> CaseState:
        """Call after simulated time advances: re-read the source and verify, never assume."""
        s = self.s
        if agent_blocked():
            return s
        if s.closed or not any(p.state == "verified" and p.action.type == "submit_dispute_evidence" for p in s.plan):
            return s
        v = verify_resolution(lambda t: self.ex.read(t, self._ctx()), set(self.w.tools))
        s.resolution = v
        if closure_allowed(v):
            self._step("Resolution verified at source", v.reason)
            amt = self.w.call("read_settlement")["amount_total"]
            self._say("resolved_released", amount=inr(amt))
            s.closed = True
            self._set_status(CaseStatus.VERIFIED)
        elif v.state == VerificationState.FAILED:
            self._escalate("Evidence was submitted but the hold is still active without a pending review", "")
        else:
            self._step("Re-checked source", f"{v.state.value}: {v.reason}")
        return s

    # ------------------------------------------------------------------ branches
    def _clarify(self) -> CaseState:
        self.s.clarify_count += 1
        if self.s.clarify_count >= 2:
            return self._escalate("Could not understand the merchant after two tries", "")
        self._say("clarify")
        return self.s

    def _forbidden(self, intake: IntakeResult, text: str) -> CaseState:
        flags = set(intake.injection_flags)
        tool = "change_bank_details" if "bank_change" in flags else "release_risk_hold"
        # Tripwire: exercise the real gate so the denial is code-enforced and audited, not just claimed.
        r = self.ex.execute(Action(case_id=self.s.case_id, type=tool, params={}), self._ctx())
        self._step("Forbidden request refused by firewall", f"{tool}: {r.firewall.code}")
        self._say("refuse_forbidden")
        self._set_status(CaseStatus.ESCALATED)
        return self.s

    def _escalate(self, reason: str, text: str) -> CaseState:
        self._step("Escalated to human", reason)
        self._say("escalated")
        self._set_status(CaseStatus.ESCALATED)
        return self.s

    def _kyc(self) -> CaseState:
        k = self._read("read_kyc")
        exp = f" (valid till {fmt_dt(k['expires_at'])})" if k.get("expires_at") else ""
        extra = ""
        if k["status"] in ("expiring", "expired"):
            if k["opted_in_reminders"] and k["reminders_sent"] == 0:
                r = self.ex.execute(Action(case_id=self.s.case_id, type="send_kyc_reminder"), self._ctx())
                self._step("KYC reminder", f"{r.outcome}, verified={r.verified}")
                extra = {"english": "I have sent you a KYC reminder as you asked for.",
                         "hinglish": "Aapki request ke mutabik KYC reminder bhej diya hai.",
                         "hindi": "आपके अनुरोध के अनुसार केवाईसी रिमाइंडर भेज दिया है।"}[self.s.language] if r.verified else ""
            else:
                extra = {"english": "Please renew it before it expires; this is separate from any settlement hold.",
                         "hinglish": "Expire hone se pehle renew kar lein; ye kisi settlement hold se alag baat hai.",
                         "hindi": "समाप्त होने से पहले नवीनीकरण करा लें; यह किसी सेटलमेंट होल्ड से अलग है।"}[self.s.language]
        status_word = {"valid": "valid", "expiring": "expiring soon", "expired": "expired"}[k["status"]]
        res = self._say("kyc_status", kyc_status=status_word, kyc_exp=exp, kyc_extra=extra)
        if res and res.verified:
            self._set_status(CaseStatus.AUTO_RESOLVED)
            self.s.closed = True
        return self.s

    def _refund(self) -> CaseState:
        if "read_refund" not in self.w.tools:
            return self._escalate("no refund on record for this merchant", "")
        r = self._read("read_refund")
        words = {"processing": {"english": "still processing", "hinglish": "abhi process ho raha hai", "hindi": "अभी प्रोसेस हो रहा है"},
                 "completed": {"english": "completed", "hinglish": "complete ho chuka hai", "hindi": "पूरा हो चुका है"}}
        eta_part = ""
        if r["status"] == "processing" and r.get("expected_by"):
            eta_part = {"english": f", expected by {fmt_dt(r['expected_by'])}",
                        "hinglish": f", {fmt_dt(r['expected_by'])} tak aane ki ummeed hai",
                        "hindi": f", {fmt_dt(r['expected_by'])} तक आने की उम्मीद है"}[self.s.language]
        res = self._say("refund_status", amount=inr(r["amount"]), status_word=words[r["status"]][self.s.language],
                        eta_part=eta_part)
        if res and res.verified:
            self._step("Closed without human", f"refund reply matches source status {r['status']}")
            self._set_status(CaseStatus.AUTO_RESOLVED)
            self.s.closed = True
        return self.s

    def _settlement_case(self, intake: IntakeResult, text: str) -> CaseState:
        s = self.s
        tl = self._timeline()
        report = self._decide(tl)
        self._prev_graph, s.report = report.graph, report
        self._step("Traced cause", " ← ".join(report.trace.path) or "no blocker")
        for e in report.trace.rejected_edges:
            self._step("Rejected non-causal link", f"{e['src']} → {e['dst']} ({e['edge_class']}): {e['note']}")
        self._step("Ranked actions", "; ".join(f"{r.id}={r.score:.3f}" for r in report.ranked[:3]))
        status = tl.val("settlement.status")

        only_unreachable = report.disposition == Disposition.ESCALATE and set(report.triggers) <= {"deadline_unreachable", "tier3_action_needed"} \
            and bool(report.chosen.steps)
        if only_unreachable:
            self._step("Deadline cannot be met", "recommending an honest supplier notice and fixing the cause; no promise made")
            return self._propose_plan(intake, text, tl, report, list(report.chosen.steps))
        if report.disposition == Disposition.ESCALATE:
            s.memo = build_memo(intake, text, tl, report, compile_bundle(tl), [])
            return self._escalate("; ".join(report.triggers) or "engine escalation", text)

        if report.disposition == Disposition.NO_ACTION:
            hold_fixable = status == "held_risk" and any(n["id"].startswith("evidence_gap:") for n in report.graph.nodes)
            if hold_fixable:      # service rule: merchant asked about a hold we can fix, even with no dated obligation
                self._step("Service action", "no dated obligation at risk, but the hold is fixable: proposing evidence")
                return self._propose_plan(intake, text, tl, report, ["submit_dispute_evidence"])
            return self._status_reply(tl, report)

        steps = list(report.chosen.steps)
        return self._propose_plan(intake, text, tl, report, steps)

    def _status_reply(self, tl: Timeline, report: DecisionReport) -> CaseState:
        s = self.s
        status = tl.val("settlement.status")
        amount = inr(tl.val("settlement.amount_total"))
        if status == "released":
            res = self._say("status_released", amount=amount)
        elif status == "processing":
            extra = ""
            if tl.val("supplier.due_at") and not report.obligation_at_risk and tl.val("merchant.supplier_paid_from_settlement"):
                extra = {"english": f"That is ahead of your supplier payment due {fmt_dt(tl.val('supplier.due_at'))}.",
                         "hinglish": f"Ye aapke supplier payment ({fmt_dt(tl.val('supplier.due_at'))}) se pehle hai.",
                         "hindi": f"यह आपके सप्लायर भुगतान ({fmt_dt(tl.val('supplier.due_at'))}) से पहले है।"}[s.language]
            res = self._say("status_processing", amount=amount, eta=fmt_dt(tl.val("settlement.expected_release")), extra=extra)
        elif status == "held_risk" and tl.val("dispute.status") in ("submitted", "under_review"):
            self._eta_reply(waiting=True)     # evidence already with risk review: report honestly, keep case open
            return s
        else:
            return self._escalate(f"unrecognised settlement status {status}", "")
        if res and res.verified:
            self._step("Closed without human", f"chosen={DO_NOTHING}; reply matches source status {status}")
            self._set_status(CaseStatus.AUTO_RESOLVED)
            s.closed = True
        return s

    def _notice_text(self, tl: Timeline) -> str:
        return (f"Dear {tl.val('supplier.name')}, payment of {inr(tl.val('supplier.amount'))} from "
                f"{self.w.spec.merchant_name} may be delayed by about a day due to a pending settlement review. "
                f"Thank you for your patience.")

    def _plan_notice(self, tl: Timeline) -> PlannedAction | None:
        if not tl.val("supplier.supplier_id"):
            return None
        a = Action(case_id=self.s.case_id, type="send_supplier_notice", params={"text": self._notice_text(tl)})
        return PlannedAction(action=a, label="Send delay notice to supplier", tier=2, state="awaiting_consent",
                             notice_text=a.params["text"])

    def _propose_plan(self, intake: IntakeResult, text: str, tl: Timeline, report: DecisionReport, steps: list[str]) -> CaseState:
        s = self.s
        bundle = compile_bundle(tl)
        s.bundle = bundle
        new: list[PlannedAction] = []
        for step in steps:
            if step == "submit_dispute_evidence":
                if bundle is None or not bundle.complete:
                    s.memo = build_memo(intake, text, tl, report, bundle, [])
                    return self._escalate("evidence bundle incomplete or dispute not awaiting evidence", text)
                a = Action(case_id=s.case_id, type="submit_dispute_evidence",
                           params={"dispute_id": bundle.dispute_id, "docs": bundle.docs})
                new.append(PlannedAction(action=a, label="Submit dispute evidence", tier=3, state="awaiting_approval"))
            elif step == "send_supplier_notice":
                n = self._plan_notice(tl)
                if n:
                    new.append(n)
        existing = {(p.action.type, p.state) for p in s.plan}
        for p in new:
            if (p.action.type, p.state) not in existing and not any(q.action.hash == p.action.hash for q in s.plan):
                s.plan.append(p)
        proposed = [{"type": p.action.type, "tier": p.tier, "hash": p.action.hash, "label": p.label} for p in s.plan]
        s.memo = build_memo(intake, text, tl, report, bundle, proposed)
        self._step("Prepared analyst card", f"{len(new)} action(s) pending; memo saves ~{s.memo.minutes_saved} min")

        nl = ""
        notice = next((p for p in s.plan if p.action.type == "send_supplier_notice" and p.state == "awaiting_consent"), None)
        if notice and tl.val("supplier.due_at"):
            nl = notice_line(s.language, tl.val("supplier.amount"), fmt_dt(tl.val("supplier.due_at")), tl.val("supplier.name"))
        if any(p.state == "awaiting_approval" for p in s.plan):
            self._say("hold_explained", held=inr(tl.val("settlement.held_amount")), notice_line=nl)
            self._set_status(CaseStatus.WAITING_ANALYST)
        if notice:
            self._say("supplier_notice_ask", supplier=tl.val("supplier.name"), text=notice.notice_text)
            if not any(p.state == "awaiting_approval" for p in s.plan):
                self._set_status(CaseStatus.WAITING_MERCHANT)
        return s

    # ------------------------------------------------------------------ human decisions
    def _find(self, h: str, state: str) -> PlannedAction:
        for p in self.s.plan:
            if p.action.hash == h and p.state == state:
                return p
        raise KeyError(f"no plan action {h[:8]} in state {state}")

    def analyst_decision(self, action_hash: str, approve: bool, approver: str, role: str) -> CaseState:
        p = self._find(action_hash, "awaiting_approval")
        if not approve:
            p.state = "rejected"
            self.audit.add(self.w.now, "approval", case_id=self.s.case_id, action_hash=p.action.hash, outcome="rejected",
                           approver=approver)
            self._step("Analyst rejected", p.label)
            self._say("approval_rejected")
            self._set_status(CaseStatus.ESCALATED)
            return self.s
        try:
            ap = self.approvals.issue(p.action, approver, role, self.w.now)
        except PermissionError:
            self.audit.add(self.w.now, "approval", case_id=self.s.case_id, action_hash=p.action.hash, outcome="deny",
                           code="issuer_not_authorised", role=role, approver=approver)
            raise
        self.audit.add(self.w.now, "approval", case_id=self.s.case_id, action_hash=p.action.hash, outcome="issued",
                       approver=approver, expires_at=ap.expires_at.isoformat())
        self._step("Analyst approved", f"{approver}: {p.label}")
        return self._run(p)

    def edit_plan_action(self, action_hash: str, docs: list[str]) -> CaseState:
        """Analyst edits the evidence bundle. A different bundle is a different action hash, so any earlier
        approval no longer applies and a fresh approval is required."""
        p = self._find(action_hash, "awaiting_approval")
        if p.action.type != "submit_dispute_evidence":
            raise ValueError("only evidence bundles can be edited")
        new = Action(case_id=self.s.case_id, type=p.action.type, params={**p.action.params, "docs": list(docs)})
        d = self.fw.check(new, self._ctx())
        if d.code in ("bad_params", "out_of_scope", "content_blocked", "hash_tamper"):
            raise ValueError(d.reason)
        old = p.action.hash
        p.action, p.label = new, "Submit dispute evidence (edited)"
        self.audit.add(self.w.now, "approval", case_id=self.s.case_id, action_hash=new.hash, outcome="edited",
                       old_hash=old, docs=list(docs))
        self._step("Analyst edited the evidence bundle", f"{len(docs)} document(s); action hash changed, fresh approval needed")
        if self.s.memo:
            for a in self.s.memo.proposed_actions:
                if a.get("hash") == old:
                    a["hash"], a["label"] = new.hash, p.label
        if self.s.bundle:
            self.s.bundle = self.s.bundle.model_copy(update={"docs": list(docs)})
        return self.s

    def merchant_consent(self, action_hash: str, accept: bool) -> CaseState:
        p = self._find(action_hash, "awaiting_consent")
        if not accept:
            p.state = "rejected"
            self.audit.add(self.w.now, "consent", case_id=self.s.case_id, action_hash=p.action.hash, outcome="declined")
            self._say("supplier_notice_declined", supplier=self._tl.val("supplier.name") if self._tl else "supplier")
            self._after_plan_change()
            return self.s
        self.consents.issue(p.action, self.w.spec.merchant_id, self.w.now)
        self.audit.add(self.w.now, "consent", case_id=self.s.case_id, action_hash=p.action.hash, outcome="issued")
        self._step("Merchant consented", p.label)
        return self._run(p)

    def _chat_consent(self, yes: bool) -> CaseState:
        pend = [p for p in self.s.plan if p.state == "awaiting_consent"]
        if not pend:
            self._say("nothing_pending")
            return self.s
        return self.merchant_consent(pend[-1].action.hash, yes)

    def _run(self, p: PlannedAction) -> CaseState:
        r = self.ex.execute(p.action, self._ctx())
        p.ladder = r.ladder
        self._step("Executed", f"{p.action.type}: {r.outcome}; ladder {' → '.join(r.ladder) or '-'}; "
                   f"verification {r.verification.state.value if r.verification else 'n/a'}")
        if r.outcome == "denied":
            p.state, p.note = "denied", r.firewall.code
            self._say("action_failed")
            self._set_status(CaseStatus.ESCALATED)
        elif r.verified:
            p.state = "verified"
            if p.action.type == "send_supplier_notice":
                self._say("supplier_notice_sent", supplier=self._tl.val("supplier.name") if self._tl else "supplier")
            elif p.action.type == "submit_dispute_evidence":
                self._eta_reply(waiting=True)
        else:
            p.state, p.note = "failed", (r.verification.reason if r.verification else r.outcome)
            self._say("action_failed")
            self._set_status(CaseStatus.ESCALATED)
        self._after_plan_change()
        return self.s

    def _after_plan_change(self) -> None:
        s = self.s
        if s.status == CaseStatus.ESCALATED or s.closed:
            return
        if any(p.state == "awaiting_approval" for p in s.plan):
            self._set_status(CaseStatus.WAITING_ANALYST)
        elif any(p.state == "awaiting_consent" for p in s.plan):
            self._set_status(CaseStatus.WAITING_MERCHANT)
        else:
            self._set_status(CaseStatus.OPEN)   # waiting on the source system, not on a person

    # ------------------------------------------------------------------ follow-ups
    def _eta_reply(self, waiting: bool) -> None:
        tl = self._timeline()
        report = self._decide(tl)
        self._prev_graph, self.s.report = report.graph, report
        nl = ""
        pend = next((p for p in self.s.plan if p.state == "awaiting_consent"), None)
        if pend and tl.val("supplier.due_at"):
            nl = notice_line(self.s.language, tl.val("supplier.amount"), fmt_dt(tl.val("supplier.due_at")), tl.val("supplier.name"))
        if waiting:
            p50 = next((r.release_p50_h for r in report.ranked if r.id == DO_NOTHING), None)
            if p50 is not None:
                self._say("evidence_waiting", eta_h=round(p50), notice_line=nl)
                return
        self._say("hold_explained", held=inr(tl.val("settlement.held_amount")), notice_line=nl)

    def _followup_eta(self) -> CaseState:
        s = self.s
        status = self.w.call("read_settlement")["status"]
        if status == "released":
            self._say("status_released", amount=inr(self.w.call("read_settlement")["amount_total"]))
            return s
        if any(p.state == "awaiting_approval" for p in s.plan):
            tl = self._timeline()
            report = self._decide(tl)
            best = next((r for r in report.ranked if r.id.startswith("SUBMIT_DISPUTE_EVIDENCE")), None)
            h = best.release_p50_h if best and best.release_p50_h else None
            if h is not None:
                self._say("eta_pending", eta_h=round(h))
                return s
        if any(p.state == "verified" and p.action.type == "submit_dispute_evidence" for p in s.plan):
            self._eta_reply(waiting=True)
            return s
        return self._settlement_case(s.last_intake, "follow-up: eta")  # type: ignore[arg-type]

    def _followup_supplier(self) -> CaseState:
        s = self.s
        tl = self._timeline()
        existing = [p for p in s.plan if p.action.type == "send_supplier_notice"]
        live = [p for p in existing if p.state in ("awaiting_consent", "verified")]
        if live:
            p = live[-1]
            if p.state == "verified":
                self._say("supplier_notice_sent", supplier=tl.val("supplier.name"))
            else:
                self._say("supplier_notice_ask", supplier=tl.val("supplier.name"), text=p.notice_text)
            return s
        n = self._plan_notice(tl)
        if n is None or self.w.call("read_settlement")["status"] == "released":
            self._say("nothing_pending")
            return s
        s.plan.append(n)
        self._step("Prepared supplier notice", "waiting for merchant consent")
        self._say("supplier_notice_ask", supplier=tl.val("supplier.name"), text=n.notice_text)
        self._after_plan_change()
        return s

    # ------------------------------------------------------------------ view
    def view(self) -> dict[str, Any]:
        s = self.s
        pending = [p for p in s.plan if p.state in ("awaiting_approval", "awaiting_consent")]
        return s.model_dump(mode="json") | {"pending": [p.action.hash for p in pending]}
