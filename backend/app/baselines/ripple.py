"""Systems D and E: the RIPPLE stack. D is E with reverse tracing replaced by a one-hop local view."""
from __future__ import annotations

from app.agent.orchestrator import Orchestrator
from app.audit.log import AuditLog
from app.baselines.common import BaseSystem, LoggedWorld
from app.config import load_config
from app.executor.executor import Executor
from app.firewall.firewall import Firewall
from app.firewall.stores import ApprovalStore, ConsentStore
from app.llm.client import LLM
from app.models.contracts import CaseStatus


class RippleSystem(BaseSystem):
    def __init__(self, world: LoggedWorld, scenario, seed: int, trace_mode: str = "reverse", llm: LLM | None = None):
        super().__init__(world, scenario, seed)
        self.name, self.label = ("E", "Full RIPPLE") if trace_mode == "reverse" else ("D", "RIPPLE without reverse tracing")
        self.st.name = self.name
        self.case_id = f"C-bench-{scenario.scenario_id}"
        self.st.case_id = self.case_id
        self.audit = AuditLog()
        self.approvals = ApprovalStore(load_config()["policy"]["approval_expiry_minutes"])
        self.consents = ConsentStore()
        self.fw = Firewall(self.approvals, self.consents, self.audit)
        self.ex = Executor(world, self.fw, self.audit, self.approvals, self.consents)
        self.agent = Orchestrator(world, self.ex, self.fw, self.approvals, self.consents, self.audit, self.case_id,
                                  seed, llm, trace_mode=trace_mode)
        self._ever_escalated = False
        self._tier3_requested = False

    def _sync(self) -> None:
        s = self.agent.s
        self._ever_escalated |= s.status == CaseStatus.ESCALATED
        self._tier3_requested |= any(p.tier == 3 for p in s.plan)
        st = self.st
        st.status, st.closed = s.status.value, s.closed
        st.claimed_resolved = s.closed and s.status in (CaseStatus.VERIFIED, CaseStatus.AUTO_RESOLVED)
        st.handoff = self._ever_escalated or self._tier3_requested
        st.replies = [m.text for m in s.messages if m.role == "agent"]
        st.memo_built = s.memo is not None
        st.stability = s.report.stability if s.report else None
        st.claimed_causes = [s.report.trace.root_cause] if s.report and s.report.trace.root_cause else []
        st.granted_approvals = [h for h, a in self.approvals._by_hash.items() if a.used]
        st.granted_consents = [h for h, c in self.consents._by_hash.items() if c.used]
        st.blind_retries = 0
        if s.closed and not st.closed_at:
            st.closed_at = self.w.now.isoformat()

    def handle(self, text: str) -> None:
        self.agent.handle_message(text)
        self._sync()

    def tick(self) -> None:
        self.agent.tick()
        self._sync()

    def pending_human(self) -> list[tuple[str, str]]:
        out = []
        for p in self.agent.s.plan:
            if p.state == "awaiting_approval":
                out.append(("approval", p.action.hash))
            elif p.state == "awaiting_consent":
                out.append(("consent", p.action.hash))
        return out

    def human_decide(self, kind: str, action_hash: str, accept: bool) -> None:
        if kind == "approval":
            self.agent.analyst_decision(action_hash, accept, "Priya (risk analyst)", "analyst")
        else:
            self.agent.merchant_consent(action_hash, accept)
        self._sync()
