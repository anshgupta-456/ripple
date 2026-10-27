"""Action firewall: deterministic, code-level gate in front of every tool call.

No LLM output can widen what this allows. Anything not named in config/policy.yaml has no tool.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.audit.log import AuditLog
from app.config import load_config
from app.firewall.content import outbound_text_violations
from app.firewall.policy import Policy, load_policy
from app.firewall.stores import ApprovalStore, ConsentStore, Consent, scope_of
from app.models.contracts import Action, Approval, Tier, action_hash
from app.security.governance import agent_blocked

ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,40}$")


class FirewallContext(BaseModel):
    case_id: str
    merchant_id: str
    now: datetime
    allowed_ids: dict[str, set[str]] = Field(default_factory=dict)   # param name -> permitted values
    kyc_opted_in: bool = False


class FwDecision(BaseModel):
    allowed: bool
    code: str
    reason: str
    tier: int | None = None
    needs: str | None = None   # "consent" | "approval" when the action is held pending a human/merchant


class Firewall:
    def __init__(self, approvals: ApprovalStore, consents: ConsentStore, audit: AuditLog,
                 policy: Policy | None = None):
        self.policy = policy or load_policy()
        self.approvals, self.consents, self.audit = approvals, consents, audit
        self.max_retries = load_config()["policy"]["max_failed_action_retries"]
        self.executed: dict[tuple[str, str], int] = {}
        self.failures: dict[tuple[str, str], int] = {}

    # ---- bookkeeping used by the executor ----
    def record_success(self, case_id: str, action_type: str) -> None:
        k = (case_id, action_type)
        self.executed[k] = self.executed.get(k, 0) + 1

    def record_failure(self, case_id: str, action_type: str) -> None:
        k = (case_id, action_type)
        self.failures[k] = self.failures.get(k, 0) + 1

    # ---- the gate ----
    def check(self, action: Action, ctx: FirewallContext, approval: Approval | None = None,
              consent: Consent | None = None) -> FwDecision:
        reason = agent_blocked()
        d = (FwDecision(allowed=False, code=reason, reason="the agent is disabled by the kill switch", tier=None)
             if reason else self._check(action, ctx, approval, consent))
        self.audit.add(ctx.now, "firewall", case_id=ctx.case_id, action_hash=action.hash,
                       outcome="allow" if d.allowed else "deny", code=d.code,
                       action_type=action.type, tier=d.tier, reason=d.reason, needs=d.needs)
        return d

    def _deny(self, code: str, reason: str, tier: int | None = None, needs: str | None = None) -> FwDecision:
        return FwDecision(allowed=False, code=code, reason=reason, tier=tier, needs=needs)

    def _check(self, a: Action, ctx: FirewallContext, approval: Approval | None, consent: Consent | None) -> FwDecision:
        pol = self.policy
        if a.type in pol.reads:
            if a.params:
                return self._deny("bad_params", "read tools take no parameters", 1)
            return FwDecision(allowed=True, code="allowed", reason="read-only", tier=1)
        if a.type in pol.forbidden:
            return self._deny("forbidden_action", f"{a.type} is Tier 4: no tool exists", int(Tier.NEVER))
        ap = pol.actions.get(a.type)
        if ap is None:
            return self._deny("unknown_action", f"{a.type} is not a permitted action", int(Tier.NEVER))
        tier = ap.tier

        if a.hash != action_hash(a.case_id, a.type, a.params):
            return self._deny("hash_tamper", "action hash does not match its content", tier)
        if a.idempotency_key != "idem_" + a.hash[:16]:
            return self._deny("bad_idempotency_key", "idempotency key must derive from the action hash", tier)
        if a.case_id != ctx.case_id:
            return self._deny("wrong_case", "action belongs to a different case", tier)

        bad = self._validate_params(a.params, ap.params)
        if bad:
            return self._deny("bad_params", bad, tier)
        for name, val in a.params.items():
            allowed = ctx.allowed_ids.get(name)
            if allowed is not None and val not in allowed:
                return self._deny("out_of_scope", f"{name}={val!r} is not part of this case", tier)
        for name, spec in ap.params.items():
            if spec["type"] == "text":
                v = outbound_text_violations(a.params[name])
                if v:
                    return self._deny("content_blocked", f"outbound text contains {', '.join(v)}", tier)

        if ap.requires == "kyc_opt_in" and not ctx.kyc_opted_in:
            return self._deny("precondition_failed", "merchant has not opted in to KYC reminders", tier)

        k = (a.case_id, a.type)
        if self.executed.get(k, 0) >= ap.max_per_case:
            return self._deny("rate_limited", f"{a.type} already used {ap.max_per_case}x on this case", tier)
        if self.failures.get(k, 0) > self.max_retries:
            return self._deny("repeated_failure", "action failed repeatedly; escalate to a human", tier)

        if tier == int(Tier.MERCHANT_CONSENT):
            c = consent or self.consents.get(a.hash)
            code = self._valid(c, a, ctx, kind="consent")
            if code:
                return self._deny(*code, tier, "consent")
        elif tier == int(Tier.HUMAN_APPROVAL):
            ap_obj = approval or self.approvals.get(a.hash)
            code = self._valid(ap_obj, a, ctx, kind="approval")
            if code:
                return self._deny(*code, tier, "approval")
        return FwDecision(allowed=True, code="allowed", reason="policy satisfied", tier=tier)

    # ---- helpers ----
    def _valid(self, obj: Approval | Consent | None, a: Action, ctx: FirewallContext, kind: str):
        if obj is None:
            return f"{kind}_missing", f"{kind} required before execution"
        store = self.approvals if kind == "approval" else self.consents
        stored = store.get(a.hash)
        if obj.action_hash != a.hash:
            return f"{kind}_hash_mismatch", f"{kind} was granted for a different action"
        if stored is None or stored != obj:
            return f"{kind}_unknown", f"{kind} was not issued by the trusted channel"
        if kind == "approval" and obj.scope != scope_of(a):
            return "approval_scope_mismatch", "approval scope does not match this action"
        if obj.used:
            return f"{kind}_replay", f"{kind} already used"
        if ctx.now > obj.expires_at:
            return f"{kind}_expired", f"{kind} expired"
        return None

    def _validate_params(self, params: dict[str, Any], spec: dict[str, dict[str, Any]]) -> str | None:
        extra = set(params) - set(spec)
        if extra:
            return f"unexpected parameters: {sorted(extra)}"
        for name, s in spec.items():
            if name not in params:
                return f"missing parameter: {name}"
            v, t = params[name], s["type"]
            if t == "id" and not (isinstance(v, str) and ID_RE.match(v)):
                return f"{name} must be a plain identifier"
            if t == "doc_list":
                if not (isinstance(v, list) and v and len(set(v)) == len(v)
                        and all(isinstance(x, str) and x in self.policy.allowed_docs for x in v)):
                    return f"{name} must be a non-empty list of known document types"
            if t == "text":
                if not (isinstance(v, str) and 1 <= len(v) <= s.get("max_len", 800)):
                    return f"{name} must be 1..{s.get('max_len', 800)} characters"
            if t == "int_range":
                if not (isinstance(v, int) and not isinstance(v, bool) and s["min"] <= v <= s["max"]):
                    return f"{name} must be a whole number from {s['min']} to {s['max']}"
            if t == "enum":
                if v not in s["values"]:
                    return f"{name} must be one of {s['values']}"
        return None
