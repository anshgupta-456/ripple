from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agent.orchestrator import Orchestrator
from app.analysis import analyze_world
from app.audit.log import AuditLog
from app.baselines.common import LoggedWorld
from app.bench.adversarial import run_suite
from app.bench.audit_policy import audit_calls
from app.bench.lab import ATTACK_TEXTS, PRESETS, run_lab
from app.bench.templates import CANONICAL_SEED, REGISTRY, InboundMessage, Scenario, generate, write_ground_truth
from app.config import ROOT, load_config
from app.executor.executor import Executor
from app.firewall.firewall import Firewall, FirewallContext
from app.firewall.stores import ApprovalStore, ConsentStore
from app.llm.client import LLM
from app.mock_sources.world import FAULT_MODES
from app.models.contracts import Action, CaseStatus
from app.security.audit_security import get_security_log, security_event
from app.security.auth import PERMS, PERSONAS, AuthError, User, current_user, describe_env, issue_dev_token, require
from app.security.governance import KILL, LIMITER
from app.security.redact import redact_obj
from app.security.backends import configure_from_env
from app.security.secret_store import install_log_scrubber
from app.security.settings import data_dir, is_dev, retention_days

app = FastAPI(title="RIPPLE", version="0.7.0", docs_url=None if not is_dev() else "/docs", redoc_url=None)
# Same-origin in production; the Vite dev server origin is allowed only in dev. Credentials are bearer tokens, not cookies.
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"] if is_dev() else [], allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["Authorization", "Content-Type"])
install_log_scrubber()
configure_from_env()      # RIPPLE_SECRETS_BACKEND=vault|aws; a misconfigured backend stops the app from starting (fail closed)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Cache-Control", "no-store")            # case data must not sit in shared caches
    return resp


# ------------------------------------------------------------------ sessions (one case each), scoped by tenant
class Session:
    def __init__(self, scenario: Scenario, tenant: str = "demo", creator: str = "system"):
        self.id = uuid.uuid4().hex[:8]
        self.scenario, self.tenant, self.creator = scenario, tenant, creator
        self.created = time.time()
        self.world = LoggedWorld(scenario.world_spec)      # tool-layer call log feeds the live unsafe-action audit
        for tool, mode in scenario.ground_truth.get("harness", {}).get("faults", []):   # environment faults (demo harness)
            self.world.inject_fault(tool, mode)
        self.last_graph = None
        self.case_id = f"C-{self.id}"
        self.audit_path = data_dir() / f"audit_{self.id}.jsonl"
        self.audit = AuditLog(self.audit_path)
        self.approvals = ApprovalStore(load_config()["policy"]["approval_expiry_minutes"])
        self.consents = ConsentStore()
        self.firewall = Firewall(self.approvals, self.consents, self.audit)
        self.executor = Executor(self.world, self.firewall, self.audit, self.approvals, self.consents)
        self.agent = Orchestrator(self.world, self.executor, self.firewall, self.approvals, self.consents, self.audit,
                                  self.case_id, scenario.seed, LLM())

    @property
    def merchant_id(self) -> str:
        return self.world.spec.merchant_id

    def unsafe(self) -> list[dict[str, str]]:
        return audit_calls(self.world.calls, self.case_id,
                           {h for h, a in self.approvals._by_hash.items() if a.used},
                           {h for h, c in self.consents._by_hash.items() if c.used},
                           self.world.kyc.spec.opted_in_reminders)

    def delete_files(self) -> None:
        self.audit_path.unlink(missing_ok=True)


SESSIONS: dict[str, Session] = {}
ACTIVE: dict[str, str] = {}            # tenant -> most recent session id (convenience default)


def _add(s: Session) -> Session:
    SESSIONS[s.id] = s
    ACTIVE[s.tenant] = s.id
    return s


def _visible(user: User, s: Session) -> bool:
    if s.tenant != user.tenant:                       # tenant isolation: other tenants' cases do not exist for you
        return False
    if user.roles <= {"merchant"}:                    # a merchant-only user sees only their own merchant's case
        return bool(user.merchant_id) and user.merchant_id == s.merchant_id
    return True


def _lookup(user: User, sid: str | None) -> Session:
    sid = sid or ACTIVE.get(user.tenant)
    s = SESSIONS.get(sid) if sid else None
    if s is None or not _visible(user, s):
        raise HTTPException(404, "no such case")
    return s


def case_dep(perm: str, *, limit: tuple[str, int, int] | None = None):
    base = require(perm, limit=limit)

    async def dep(user: User = Depends(base), sid: str | None = Query(default=None)) -> tuple[User, Session]:
        return user, _lookup(user, sid)

    return dep


def agent_dep(perm: str, *, limit: tuple[str, int, int] | None = None):
    """Case dependency for anything that makes the agent act: refused while the kill switch is on for this user/globally."""
    inner = case_dep(perm, limit=limit)

    async def dep(pair: tuple[User, Session] = Depends(inner)) -> tuple[User, Session]:
        reason = KILL.blocked(pair[0].sub)
        if reason:
            security_event("kill_switch_block", pair[0], outcome="deny", code=reason)
            raise HTTPException(503, f"the agent is disabled ({reason})")
        return pair

    return dep


def demo_dep(perm: str = "demo.control", *, limit: tuple[str, int, int] | None = None):
    """Simulation controls (seed, advance time, reset, fault injection) exist only in demo mode: 404 in production."""
    base = require(perm, limit=limit)

    async def dep(user: User = Depends(base)) -> User:
        if not is_dev():
            raise HTTPException(404, "not found")
        return user

    return dep


def _public_state(s: Session) -> dict[str, Any]:
    return {"session_id": s.id, "scenario": s.scenario.public(), "world": s.world.snapshot(),
            "banner": load_config()["synthetic_data_banner"]}


def _case_view(s: Session, user: User) -> dict[str, Any]:
    v = s.agent.view() | {"sid": s.id, "sim_now": s.world.now.isoformat(), "scenario_family": s.scenario.family,
                          "merchant_name": s.world.spec.merchant_name, "unsafe": s.unsafe()}
    if not user.can("case.pii"):                      # viewers get masked conversation text
        v["messages"] = redact_obj(v["messages"])
        if v.get("memo"):
            v["memo"]["merchant_ask"] = redact_obj(v["memo"]["merchant_ask"])
            v["memo"]["summary"] = redact_obj(v["memo"]["summary"])
    return v


# ------------------------------------------------------------------ request bodies (no role / approver fields: identity comes from the token)
class ResetBody(BaseModel):
    family: str = "missing_evidence_flagship"
    seed: int = CANONICAL_SEED
    canonical: bool = True


class AnalyzeBody(BaseModel):
    seed: int = CANONICAL_SEED


class AdvanceBody(BaseModel):
    hours: float


class MessageBody(BaseModel):
    text: str


class DecideBody(BaseModel):
    action_hash: str
    approve: bool


class EditBody(BaseModel):
    action_hash: str
    docs: list[str]


class ConsentBody(BaseModel):
    action_hash: str
    accept: bool


class FaultBody(BaseModel):
    tool: str
    mode: str


class SeedBody(BaseModel):
    families: list[str] | None = None
    send_messages: bool = True


class NewCaseBody(BaseModel):
    family: str = "missing_evidence_flagship"
    seed: int = CANONICAL_SEED
    canonical: bool = False
    text: str | None = None


class LabBody(BaseModel):
    preset: str = "flagship"
    message: str | None = None
    replay: bool = False


class DevLoginBody(BaseModel):
    persona: str


class KillBody(BaseModel):
    scope: str            # global | user
    target: str = ""      # user sub when scope == user
    on: bool


# ------------------------------------------------------------------ public + auth
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/config/public")
def public_config() -> dict[str, Any]:
    c = load_config()
    return {"banner": c["synthetic_data_banner"], "auth": describe_env()}


@app.get("/api/auth/oidc-config")
def oidc_config() -> dict[str, Any]:
    """Public, non-secret settings for the SPA's authorization-code + PKCE flow. Endpoints come from the IdP's discovery
    document, validated server-side (issuer match, https, allowed hosts). There is no client secret to return."""
    from app.security import oidc as _oidc
    try:
        st = _oidc.oidc_settings()
        if st is None:
            raise HTTPException(404, "OIDC sign-in is not configured")
        return _oidc.browser_config(st)
    except _oidc.OidcError as e:
        raise HTTPException(e.status, e.detail)


@app.post("/api/auth/dev-login")
def dev_login(body: DevLoginBody, request: Request) -> dict[str, Any]:
    """Demo-only persona login (RIPPLE_ENV=dev). Mints a 15-minute token; in production this route does not exist."""
    if not is_dev():
        raise HTTPException(404, "not found")
    ip = request.client.host if request.client else "?"
    if not LIMITER.allow("dev-login", ip, 20, 60):
        raise HTTPException(429, "rate limit exceeded")
    try:
        tok, user = issue_dev_token(body.persona)
    except AuthError as e:
        raise HTTPException(e.status, e.detail)
    security_event("login", user, outcome="ok", code="dev_persona")
    return {"token": tok, "expires_at": user.exp, "user": {"sub": user.sub, "name": user.name, "roles": sorted(user.roles)}}


@app.get("/api/auth/personas")
def personas() -> list[dict[str, Any]]:
    if not is_dev():
        raise HTTPException(404, "not found")
    return [{"id": k, "name": v[0], "roles": sorted(v[1])} for k, v in PERSONAS.items()]


@app.get("/api/auth/me")
async def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {"sub": user.sub, "name": user.name, "roles": sorted(user.roles), "tenant": user.tenant, "expires_at": user.exp,
            "perms": sorted(p for p in PERMS if user.can(p)), "kill_switch": KILL.state()}


@app.get("/api/config/assumptions")
def assumptions(user: User = Depends(require("bench.read"))) -> dict[str, Any]:
    c = load_config()
    return {"version": c["version"], "banner": c["synthetic_data_banner"], "cost_model": c["cost_model"],
            "harm_weights": c["harm_weights"], "policy": c["policy"], "held_out_families": c["benchmark"]["held_out_families"],
            "engine": {k: c["engine"][k] for k in ("review_hours", "release_delay_hours", "approval_latency_hours",
                                                   "supplier_notice", "at_risk_p_miss", "unreachable_p_miss")}}


@app.get("/api/scenarios")
def scenarios(user: User = Depends(require("console.read"))) -> list[dict[str, Any]]:
    return [{"family": f, "split": (s := generate(f, CANONICAL_SEED, canonical=True)).split, "description": s.description}
            for f in REGISTRY]


# ------------------------------------------------------------------ one case
@app.get("/api/case")
def case_view(ctx: tuple[User, Session] = Depends(case_dep("case.read"))) -> dict[str, Any]:
    user, s = ctx
    return _case_view(s, user)


@app.get("/api/session/state")
def state(ctx: tuple[User, Session] = Depends(case_dep("case.read"))) -> dict[str, Any]:
    return _public_state(ctx[1])


@app.get("/api/session/events")
def events(ctx: tuple[User, Session] = Depends(case_dep("case.read"))) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in ctx[1].world.events]


@app.post("/api/session/analyze")
def analyze(body: AnalyzeBody, ctx: tuple[User, Session] = Depends(case_dep("case.read"))) -> dict[str, Any]:
    """Run the deterministic decision engine on the current state. Read-only: it takes no action."""
    s = ctx[1]
    report = analyze_world(s.world, body.seed, s.last_graph)
    s.last_graph = report.graph
    return report.model_dump(mode="json")


@app.post("/api/case/message")
def case_message(body: MessageBody, ctx: tuple[User, Session] = Depends(agent_dep("case.message", limit=("message", 30, 60)))) -> dict[str, Any]:
    user, s = ctx
    if len(body.text) > 2000:
        raise HTTPException(413, "message too long")
    if "merchant" not in user.roles:
        security_event("impersonation", user, outcome="ok", code="message", case=s.case_id)
        s.audit.add(s.world.now, "impersonation", case_id=s.case_id, outcome="ok", acting_as="merchant")
    s.agent.handle_message(body.text)
    return _case_view(s, user)


@app.post("/api/case/decide")
def case_decide(body: DecideBody, ctx: tuple[User, Session] = Depends(agent_dep("approval.decide", limit=("decide", 30, 60)))) -> dict[str, Any]:
    """Only an authenticated analyst can approve. The approver identity is the token's subject, never the request body."""
    user, s = ctx
    try:
        s.agent.analyst_decision(body.action_hash, body.approve, user.name, "analyst")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return _case_view(s, user)


@app.post("/api/case/edit")
def case_edit(body: EditBody, ctx: tuple[User, Session] = Depends(agent_dep("approval.decide", limit=("decide", 30, 60)))) -> dict[str, Any]:
    """Analyst edits the evidence bundle. Editing changes the action hash, so it needs a fresh approval."""
    user, s = ctx
    try:
        s.agent.edit_plan_action(body.action_hash, body.docs)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _case_view(s, user)


@app.post("/api/case/consent")
def case_consent(body: ConsentBody, ctx: tuple[User, Session] = Depends(agent_dep("case.consent", limit=("message", 30, 60)))) -> dict[str, Any]:
    user, s = ctx
    if "merchant" not in user.roles:
        security_event("impersonation", user, outcome="ok", code="consent", case=s.case_id)
        s.audit.add(s.world.now, "impersonation", case_id=s.case_id, outcome="ok", acting_as="merchant")
    try:
        s.agent.merchant_consent(body.action_hash, body.accept)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return _case_view(s, user)


@app.get("/api/audit")
def audit(ctx: tuple[User, Session] = Depends(case_dep("audit.read"))) -> list[dict[str, Any]]:
    return ctx[1].audit.dump()


@app.get("/api/audit/verify")
def audit_verify(ctx: tuple[User, Session] = Depends(case_dep("audit.verify"))) -> dict[str, Any]:
    s = ctx[1]
    return {"memory": s.audit.verify(), "file": s.audit.verify_file()}


# ------------------------------------------------------------------ demo controls (dev only)
@app.post("/api/session/reset")
def reset(body: ResetBody, user: User = Depends(demo_dep())) -> dict[str, Any]:
    try:
        scn = generate(body.family, body.seed, body.canonical)
    except KeyError as e:
        raise HTTPException(404, str(e))
    write_ground_truth(scn)            # evaluator-only file; never returned by the API
    old = ACTIVE.get(user.tenant)
    if old and old in SESSIONS and SESSIONS[old].tenant == user.tenant:
        SESSIONS.pop(old).delete_files()
    return _public_state(_add(Session(scn, user.tenant, user.sub)))


@app.post("/api/session/advance")
def advance(body: AdvanceBody, ctx: tuple[User, Session] = Depends(agent_dep("demo.control"))) -> dict[str, Any]:
    if not is_dev():
        raise HTTPException(404, "not found")
    s = ctx[1]
    s.world.advance(max(0.0, min(body.hours, 24 * 14)))
    s.agent.tick()
    return _public_state(s)


@app.post("/api/dev/fault")
def dev_fault(body: FaultBody, ctx: tuple[User, Session] = Depends(case_dep("demo.control"))) -> dict[str, Any]:
    if not is_dev():
        raise HTTPException(404, "not found")
    try:
        ctx[1].world.inject_fault(body.tool, body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "modes": list(FAULT_MODES)}


# ------------------------------------------------------------------ ops console (portfolio of cases, this tenant only)
def _mine(user: User) -> list[Session]:
    return [s for s in SESSIONS.values() if _visible(user, s)]


def _summary(s: Session) -> dict[str, Any]:
    c = s.agent.s
    plan = c.plan
    waiting = ("analyst" if any(p.state == "awaiting_approval" for p in plan)
               else "merchant" if any(p.state == "awaiting_consent" for p in plan)
               else "source" if (not c.closed and c.status == CaseStatus.OPEN and c.messages) else None)
    rep = c.report
    due_h = None
    sp = s.world.spec.supplier
    if sp and s.world.call("read_settlement")["status"] != "released":
        due_h = round((sp.due_at - s.world.now).total_seconds() / 3600, 1)
    last = redact_obj(next((m.text for m in reversed(c.messages) if m.role == "agent"), ""))
    return {
        "sid": s.id, "case_id": s.case_id, "merchant": s.world.spec.merchant_name, "family": s.scenario.family,
        "intent": c.intent, "language": c.language, "status": c.status.value, "closed": c.closed, "waiting_on": waiting,
        "tier_max": max([p.tier for p in plan], default=1), "hours_to_deadline": due_h,
        "at_risk": bool(rep and rep.obligation_at_risk), "p_miss": rep.chosen.p_miss if rep and rep.obligation_at_risk else None,
        "last_agent_message": last[:160], "n_messages": len(c.messages), "sim_now": s.world.now.isoformat(),
        "unsafe": len(s.unsafe()), "description": s.scenario.description,
    }


_ORDER = {"analyst": 0, "merchant": 1, "source": 2, None: 3}


def _list(user: User) -> list[dict[str, Any]]:
    rows = [_summary(s) for s in _mine(user)]
    rows.sort(key=lambda r: (_ORDER.get(r["waiting_on"], 3), r["status"] == "verified", r["hours_to_deadline"] is None,
                             r["hours_to_deadline"] or 0))
    return rows


@app.get("/api/cases")
def list_cases(user: User = Depends(require("console.read"))) -> list[dict[str, Any]]:
    return _list(user)


def _converse(s: Session, text: str | None = None) -> None:
    s.agent.handle_message(text or s.scenario.inbound.text)
    if text is None:
        for f in s.scenario.followups:
            s.agent.handle_message(f)


def _kill_gate(user: User) -> None:
    reason = KILL.blocked(user.sub)
    if reason:
        raise HTTPException(503, f"the agent is disabled ({reason})")


@app.post("/api/cases/seed")
def seed_portfolio(body: SeedBody, user: User = Depends(demo_dep(limit=("seed", 10, 60)))) -> list[dict[str, Any]]:
    """Replace this tenant's portfolio with one case per scenario family, each already handled by the agent."""
    _kill_gate(user)
    for s in _mine(user):
        SESSIONS.pop(s.id).delete_files()
    for i, fam in enumerate(body.families or list(REGISTRY)):
        if fam not in REGISTRY:
            raise HTTPException(404, f"unknown family {fam}")
        scn = generate(fam, CANONICAL_SEED if fam == "missing_evidence_flagship" else CANONICAL_SEED + i,
                       canonical=(fam == "missing_evidence_flagship"))
        s = _add(Session(scn, user.tenant, user.sub))
        if body.send_messages:
            _converse(s)
    return _list(user)


@app.post("/api/cases/new")
def new_case(body: NewCaseBody, user: User = Depends(demo_dep(limit=("seed", 10, 60)))) -> dict[str, Any]:
    _kill_gate(user)
    if body.text and len(body.text) > 2000:
        raise HTTPException(413, "message too long")
    try:
        scn = generate(body.family, body.seed, body.canonical)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if body.text:
        scn = scn.model_copy(update={"inbound": InboundMessage(text=body.text, language=scn.inbound.language), "followups": []})
    s = _add(Session(scn, user.tenant, user.sub))
    _converse(s, body.text or None)
    return _summary(s)


@app.post("/api/cases/advance")
def advance_all(body: AdvanceBody, user: User = Depends(demo_dep())) -> list[dict[str, Any]]:
    _kill_gate(user)
    for s in _mine(user):
        s.world.advance(max(0.0, min(body.hours, 24 * 14)))
        s.agent.tick()
    return _list(user)


@app.delete("/api/cases/{sid}")
def delete_case(sid: str, user: User = Depends(require("data.delete"))) -> dict[str, Any]:
    """Deletion path: removes the case from memory and deletes its audit file from disk."""
    s = SESSIONS.get(sid)
    if s is None or not _visible(user, s):
        raise HTTPException(404, "no such case")
    SESSIONS.pop(sid).delete_files()
    if ACTIVE.get(user.tenant) == sid:
        ACTIVE.pop(user.tenant, None)
    security_event("data_deleted", user, outcome="ok", case=s.case_id)
    return {"deleted": sid}


@app.post("/api/admin/purge")
def purge(user: User = Depends(require("data.delete"))) -> dict[str, Any]:
    n = purge_expired()
    security_event("retention_purge", user, outcome="ok", deleted=n)
    return {"deleted_files": n, "retention_days": retention_days()}


def purge_expired() -> int:
    """Retention: audit files older than RIPPLE_RETENTION_DAYS are deleted (security audit and kill-switch state are kept)."""
    cutoff = time.time() - retention_days() * 86400
    n = 0
    for p in data_dir().glob("audit_*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


@app.get("/api/console/kpis")
def kpis(user: User = Depends(require("console.read"))) -> dict[str, Any]:
    sess = _mine(user)
    rows = [_summary(s) for s in sess]
    auto = sum(1 for r in rows if r["status"] == "auto_resolved")
    needed_human = sum(1 for s, r in zip(sess, rows) if r["waiting_on"] in ("analyst", "merchant")
                       or r["status"] in ("escalated", "verified") or any(p.tier >= 2 for p in s.agent.s.plan))
    mins = sum(s.agent.s.memo.minutes_saved for s in sess if s.agent.s.memo and any(p.tier == 3 for p in s.agent.s.plan))
    return {
        "total": len(rows), "auto_resolved": auto, "verified": sum(1 for r in rows if r["status"] == "verified"),
        "awaiting_analyst": sum(1 for r in rows if r["waiting_on"] == "analyst"),
        "awaiting_merchant": sum(1 for r in rows if r["waiting_on"] == "merchant"),
        "escalated": sum(1 for r in rows if r["status"] == "escalated"),
        "open": sum(1 for r in rows if not r["closed"] and r["status"] not in ("escalated",)),
        "autonomous_rate": {"k": auto, "n": auto + needed_human, "label": "auto-resolved ÷ (auto-resolved + cases that needed a person)"},
        "unsafe_actions": sum(r["unsafe"] for r in rows), "modeled_minutes_saved": mins,
        "at_risk": sum(1 for r in rows if r["at_risk"] and not r["closed"]),
    }


@app.get("/api/approvals/pending")
def pending_approvals(user: User = Depends(require("approval.read"))) -> list[dict[str, Any]]:
    out = []
    for s in _mine(user):
        c = s.agent.s
        for p in c.plan:
            if p.state == "awaiting_approval":
                memo = c.memo.model_dump(mode="json") if c.memo else None
                if memo and not user.can("case.pii"):
                    memo["merchant_ask"] = redact_obj(memo["merchant_ask"])
                out.append({"sid": s.id, "case_id": s.case_id, "merchant": s.world.spec.merchant_name, "family": s.scenario.family,
                            "action": p.action.model_dump(mode="json"), "label": p.label, "tier": p.tier, "memo": memo,
                            "bundle": c.bundle.model_dump(mode="json") if c.bundle else None, "sim_now": s.world.now.isoformat(),
                            "hours_to_deadline": _summary(s)["hours_to_deadline"], "can_decide": user.can("approval.decide")})
    out.sort(key=lambda r: (r["hours_to_deadline"] is None, r["hours_to_deadline"] or 0))
    return out


# ------------------------------------------------------------------ benchmark, safety suite, lab (synthetic sandboxes)
@app.get("/api/bench/results")
def bench_results(user: User = Depends(require("bench.read"))) -> dict[str, Any]:
    """Latest benchmark summary (frozen-v1 if present, else the dev run). Records are omitted to keep it small."""
    for name in ("bench_frozen-v1.json", "bench_dev.json"):
        path = ROOT / "data" / "results" / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("records", None)
            return data
    raise HTTPException(404, "no benchmark results yet: python -m app.bench.run --label dev")


@app.post("/api/adversarial/run")
def adversarial(user: User = Depends(require("lab.run", limit=("lab", 6, 60)))) -> dict[str, Any]:
    out = run_suite()
    return {k: out[k] for k in ("action_cases", "unsafe_actions", "screening_cases", "screening_misses",
                                 "screening_false_positives", "failed")} | {"rows": out["rows"]}


@app.get("/api/lab/presets")
def lab_presets(user: User = Depends(require("lab.run"))) -> dict[str, Any]:
    return {"presets": [{"id": k, "family": v[0], "description": v[1]} for k, v in PRESETS.items()], "attack_texts": ATTACK_TEXTS}


@app.post("/api/lab/run")
def lab_run(body: LabBody, user: User = Depends(require("lab.run", limit=("lab", 10, 60)))) -> dict[str, Any]:
    if body.preset not in PRESETS:
        raise HTTPException(404, f"unknown preset {body.preset}")
    if body.message and len(body.message) > 2000:
        raise HTTPException(413, "message too long")
    return run_lab(body.preset, body.message, body.replay)


# ------------------------------------------------------------------ governance
@app.get("/api/admin/killswitch")
def killswitch_state(user: User = Depends(require("killswitch.set"))) -> dict[str, Any]:
    return KILL.state()


@app.post("/api/admin/killswitch")
def killswitch_set(body: KillBody, user: User = Depends(require("killswitch.set"))) -> dict[str, Any]:
    if body.scope == "global":
        KILL.set_global(body.on)
    elif body.scope == "user" and body.target:
        KILL.set_user(body.target, body.on)
    else:
        raise HTTPException(400, "scope must be 'global' or 'user' (with target)")
    security_event("killswitch", user, outcome="on" if body.on else "off", code=body.scope, target=body.target)
    return KILL.state()


@app.get("/api/admin/security-log")
def security_log(user: User = Depends(require("audit.verify"))) -> dict[str, Any]:
    log = get_security_log()
    return {"verify": log.verify(), "entries": log.dump()[-100:]}


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Never leak stack traces or internals to callers; details go to the server log only."""
    import logging
    logging.getLogger("ripple").exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "internal error"}, status_code=500)
