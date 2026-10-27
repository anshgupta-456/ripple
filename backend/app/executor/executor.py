"""Executor: the only path from a proposed Action to a tool call.

firewall -> idempotent call -> (timeout => reconcile by key, never blind retry) -> re-read & verify.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.audit.log import AuditLog
from app.firewall.firewall import Firewall, FirewallContext, FwDecision
from app.firewall.stores import ApprovalStore, ConsentStore
from app.mock_sources.world import ToolTimeout, UnknownTool, World
from app.models.contracts import Action, ActionState, Verification
from app.verifier.verify import verify_action


class ExecResult(BaseModel):
    action_hash: str
    action_type: str
    outcome: str                          # denied | executed | failed | unknown
    firewall: FwDecision
    ladder: list[str] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    reconciled: bool = False
    retried: bool = False
    verification: Verification | None = None

    @property
    def verified(self) -> bool:
        return bool(self.verification and self.verification.state.value == "verified")


class Executor:
    def __init__(self, world: World, fw: Firewall, audit: AuditLog, approvals: ApprovalStore, consents: ConsentStore):
        self.w, self.fw, self.audit = world, fw, audit
        self.approvals, self.consents = approvals, consents

    def read(self, tool: str, ctx: FirewallContext) -> Any:
        d = self.fw.check(Action(case_id=ctx.case_id, type=tool), ctx)
        if not d.allowed:
            raise PermissionError(d.reason)
        return self.w.call(tool)

    def execute(self, action: Action, ctx: FirewallContext, approval=None, consent=None) -> ExecResult:
        d = self.fw.check(action, ctx, approval, consent)
        res = ExecResult(action_hash=action.hash, action_type=action.type, outcome="denied", firewall=d)
        if not d.allowed:
            return res

        tier = d.tier or 1
        res.ladder = [ActionState.ATTEMPTED.value]
        if tier == 3:
            res.ladder += [ActionState.SUBMITTED.value, ActionState.APPROVED.value]
            self.approvals.consume(action.hash)
        elif tier == 2:
            res.ladder.append(ActionState.APPROVED.value)
            self.consents.consume(action.hash)

        pre = self.w.snapshot()
        result = self._call(action, res)
        if result is None:
            return res
        res.result = result
        if not result.get("ok", True):
            res.outcome = "failed"
            self.fw.record_failure(action.case_id, action.type)
            self.audit.add(self.w.now, "tool_call", case_id=action.case_id, action_hash=action.hash,
                           outcome="failed", code=str(result.get("error", "")), action_type=action.type)
            return res

        res.outcome = "executed"
        res.ladder.append(ActionState.EXECUTED.value)
        self.fw.record_success(action.case_id, action.type)

        v = verify_action(action, pre, lambda t: self.w.call(t))
        res.verification = v
        self.audit.add(self.w.now, "verification", case_id=action.case_id, action_hash=action.hash,
                       outcome=v.state.value, code="action", reason=v.reason)
        if v.state.value == "verified":
            res.ladder.append(ActionState.VERIFIED.value)
        else:
            self.fw.record_failure(action.case_id, action.type)   # acknowledged but not confirmed
        return res

    # ---- tool call with timeout reconciliation ----
    def _call(self, a: Action, res: ExecResult) -> dict[str, Any] | None:
        attempts = 0
        while True:
            attempts += 1
            try:
                r = self.w.call(a.type, a.params, a.idempotency_key)
                self.audit.add(self.w.now, "tool_call", case_id=a.case_id, action_hash=a.hash, outcome="ok",
                               action_type=a.type, attempt=attempts, replayed=bool(r.get("replayed")))
                return r
            except UnknownTool:
                self.audit.add(self.w.now, "tool_call", case_id=a.case_id, action_hash=a.hash,
                               outcome="deny", code="no_such_tool", action_type=a.type)
                res.outcome = "failed"
                return None
            except ToolTimeout:
                self.audit.add(self.w.now, "tool_call", case_id=a.case_id, action_hash=a.hash,
                               outcome="timeout", action_type=a.type, attempt=attempts)
                landed = self.w.lookup_by_key(a.idempotency_key)      # query BEFORE any retry
                self.audit.add(self.w.now, "reconcile", case_id=a.case_id, action_hash=a.hash,
                               outcome="landed" if landed else "not_landed", action_type=a.type)
                res.reconciled = True
                if landed:
                    return landed["result"]
                self.fw.record_failure(a.case_id, a.type)
                if attempts > self.fw.max_retries:
                    res.outcome = "unknown"
                    return None
                res.retried = True      # same idempotency key => safe even if the first write lands late
