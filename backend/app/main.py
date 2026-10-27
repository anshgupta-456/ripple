from __future__ import annotations

import asyncio
import contextvars
import json
import uuid
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.orchestrator import Orchestrator
from app.analysis import analyze_world
from app.audit.log import AuditLog
from app.baselines.common import LoggedWorld
from app.bench.adversarial import run_suite
from app.bench.audit_policy import audit_calls
from app.bench.templates import CANONICAL_SEED, REGISTRY, InboundMessage, Scenario, generate, write_ground_truth
from app.config import ROOT, load_config
from app.executor.executor import Executor
from app.firewall.firewall import Firewall, FirewallContext
from app.firewall.stores import ApprovalStore, ConsentStore
from app.llm.client import LLM
from app.mock_sources.world import FAULT_MODES
from app.models.contracts import Action, CaseStatus

app = FastAPI(title="RIPPLE", version="0.6.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

_SID: contextvars.ContextVar[str | None] = contextvars.ContextVar("sid", default=None)


class SidMiddleware:
    """Pure ASGI middleware: `?sid=<id>` selects which case/session every endpoint operates on."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            qs = parse_qs(scope.get("query_string", b"").decode())
            tok = _SID.set((qs.get("sid") or [None])[0])
            try:
                await self.app(scope, receive, send)
            finally:
                _SID.reset(tok)
        else:
            await self.app(scope, receive, send)


app.add_middleware(SidMiddleware)


class Session:
    def __init__(self, scenario: Scenario):
        self.id = uuid.uuid4().hex[:8]
        self.scenario = scenario
        self.world = LoggedWorld(scenario.world_spec)      # tool-layer call log feeds the live unsafe-action audit
        for tool, mode in scenario.ground_truth.get("harness", {}).get("faults", []):   # environment faults (demo harness)
            self.world.inject_fault(tool, mode)
        self.last_graph = None
        self.case_id = f"C-{self.id}"
        self.audit = AuditLog(ROOT / "data" / f"audit_{self.id}.jsonl")
        self.approvals = ApprovalStore(load_config()["policy"]["approval_expiry_minutes"])
        self.consents = ConsentStore()
        self.firewall = Firewall(self.approvals, self.consents, self.audit)
        self.executor = Executor(self.world, self.firewall, self.audit, self.approvals, self.consents)
        self.proposals: dict[str, Action] = {}
        self.agent = Orchestrator(self.world, self.executor, self.firewall, self.approvals, self.consents, self.audit,
                                  self.case_id, scenario.seed, LLM())

    def ctx(self) -> FirewallContext:
        w = self.world
        ids = {"dispute_id": {w.spec.dispute.dispute_id}} if w.spec.dispute else {}
        return FirewallContext(case_id=self.case_id, merchant_id=w.spec.merchant_id, now=w.now,
                               allowed_ids=ids, kyc_opted_in=w.kyc.spec.opted_in_reminders)

    def unsafe(self) -> list[dict[str, str]]:
        return audit_calls(self.world.calls, self.case_id,
                           {h for h, a in self.approvals._by_hash.items() if a.used},
                           {h for h, c in self.consents._by_hash.items() if c.used},
                           self.world.kyc.spec.opted_in_reminders)


SESSIONS: dict[str, Session] = {}
ACTIVE: str | None = None


def _add(s: Session) -> Session:
    global ACTIVE
    SESSIONS[s.id] = s
    ACTIVE = s.id
    return s


def _session() -> Session:
    sid = _SID.get() or ACTIVE
    if sid is None:
        return _add(Session(generate("missing_evidence_flagship", CANONICAL_SEED, canonical=True)))
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, f"unknown session {sid}")
    return s


def _public_state(s: Session) -> dict[str, Any]:
    return {"session_id": s.id, "scenario": s.scenario.public(), "world": s.world.snapshot(),
            "banner": load_config()["synthetic_data_banner"]}


# ------------------------------------------------------------------ bodies
class ResetBody(BaseModel):
    family: str = "missing_evidence_flagship"
    seed: int = CANONICAL_SEED
    canonical: bool = True


class AnalyzeBody(BaseModel):
    seed: int = CANONICAL_SEED


class AdvanceBody(BaseModel):
    hours: float


class ProposeBody(BaseModel):
    type: str
    params: dict[str, Any] = {}


class GrantBody(BaseModel):
    action_hash: str
    approver: str = ""
    role: str = ""


class MessageBody(BaseModel):
    text: str


class DecideBody(BaseModel):
    action_hash: str
    approve: bool
    approver: str = ""
    role: str = ""


class EditBody(BaseModel):
    action_hash: str
    docs: list[str]


class ConsentBody(BaseModel):
    action_hash: str
    accept: bool


class ExecuteBody(BaseModel):
    action_hash: str


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


# ------------------------------------------------------------------ basics
@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "config_version": load_config()["version"]}


@app.get("/api/config/public")
def public_config() -> dict[str, Any]:
    c = load_config()
    return {"version": c["version"], "banner": c["synthetic_data_banner"], "cost_model": c["cost_model"],
            "harm_weights": c["harm_weights"], "policy": c["policy"], "held_out_families": c["benchmark"]["held_out_families"],
            "engine": {k: c["engine"][k] for k in ("review_hours", "release_delay_hours", "approval_latency_hours",
                                                   "supplier_notice", "at_risk_p_miss", "unreachable_p_miss")}}


@app.get("/api/scenarios")
def scenarios() -> list[dict[str, Any]]:
    out = []
    for family in REGISTRY:
        scn = generate(family, CANONICAL_SEED, canonical=True)
        out.append({"family": family, "split": scn.split, "description": scn.description})
    return out


@app.post("/api/session/reset")
def reset(body: ResetBody) -> dict[str, Any]:
    try:
        scn = generate(body.family, body.seed, body.canonical)
    except KeyError as e:
        raise HTTPException(404, str(e))
    write_ground_truth(scn)  # evaluator-only file; never returned by the API
    old = _SID.get() or ACTIVE
    if old:
        SESSIONS.pop(old, None)
    return _public_state(_add(Session(scn)))


@app.get("/api/session/state")
def state() -> dict[str, Any]:
    return _public_state(_session())


@app.post("/api/session/advance")
def advance(body: AdvanceBody) -> dict[str, Any]:
    s = _session()
    s.world.advance(body.hours)
    s.agent.tick()
    return _public_state(s)


@app.post("/api/session/analyze")
def analyze(body: AnalyzeBody) -> dict[str, Any]:
    """Run the deterministic decision engine on the current world state (no actions taken)."""
    s = _session()
    report = analyze_world(s.world, body.seed, s.last_graph)
    s.last_graph = report.graph
    return report.model_dump(mode="json")


@app.get("/api/session/events")
def events() -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in _session().world.events]


@app.get("/api/session/stream")
async def stream(request: Request) -> StreamingResponse:
    sess = _session()

    async def gen():
        idx = ticks = 0
        yield f"event: hello\ndata: {json.dumps({'session_id': sess.id})}\n\n"
        while True:
            if await request.is_disconnected():
                return
            evs = sess.world.events
            while idx < len(evs):
                yield f"event: world_event\ndata: {evs[idx].model_dump_json()}\n\n"
                idx += 1
            ticks += 1
            if ticks % 60 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/dev/tools")
def dev_tools() -> dict[str, Any]:
    w = _session().world
    return {"tools": {n: {"write": t.write} for n, t in w.tools.items()}, "fault_modes": list(FAULT_MODES)}


# ------------------------------------------------------------------ action pipeline (every step audited)
@app.post("/api/actions/propose")
def propose(body: ProposeBody) -> dict[str, Any]:
    s = _session()
    a = Action(case_id=s.case_id, type=body.type, params=body.params)
    s.proposals[a.hash] = a
    d = s.firewall.check(a, s.ctx())
    return {"action": a.model_dump(mode="json"), "decision": d.model_dump()}


def _proposal(s: Session, h: str) -> Action:
    a = s.proposals.get(h)
    if a is None:
        raise HTTPException(404, "unknown action_hash: propose it first")
    return a


@app.post("/api/approvals/issue")
def issue_approval(body: GrantBody) -> dict[str, Any]:
    s = _session()
    a = _proposal(s, body.action_hash)
    try:
        ap = s.approvals.issue(a, body.approver, body.role, s.world.now)
    except PermissionError as e:
        s.audit.add(s.world.now, "approval", case_id=s.case_id, action_hash=a.hash, outcome="deny",
                    code="issuer_not_authorised", role=body.role, approver=body.approver)
        raise HTTPException(403, str(e))
    s.audit.add(s.world.now, "approval", case_id=s.case_id, action_hash=a.hash, outcome="issued",
                approver=body.approver, expires_at=ap.expires_at.isoformat())
    return ap.model_dump(mode="json")


@app.post("/api/consents/issue")
def issue_consent(body: GrantBody) -> dict[str, Any]:
    s = _session()
    a = _proposal(s, body.action_hash)
    c = s.consents.issue(a, s.world.spec.merchant_id, s.world.now)
    s.audit.add(s.world.now, "consent", case_id=s.case_id, action_hash=a.hash, outcome="issued")
    return c.model_dump(mode="json")


@app.post("/api/actions/execute")
def execute(body: ExecuteBody) -> dict[str, Any]:
    s = _session()
    r = s.executor.execute(_proposal(s, body.action_hash), s.ctx())
    return r.model_dump(mode="json") | {"verified": r.verified}


# ------------------------------------------------------------------ the agent (one case per session)
@app.post("/api/case/message")
def case_message(body: MessageBody) -> dict[str, Any]:
    s = _session()
    s.agent.handle_message(body.text)
    return _case_view(s)


def _case_view(s: Session) -> dict[str, Any]:
    return s.agent.view() | {"sid": s.id, "sim_now": s.world.now.isoformat(), "scenario_family": s.scenario.family,
                             "merchant_name": s.world.spec.merchant_name, "unsafe": s.unsafe()}


@app.get("/api/case")
def case_view() -> dict[str, Any]:
    return _case_view(_session())


@app.post("/api/case/decide")
def case_decide(body: DecideBody) -> dict[str, Any]:
    s = _session()
    try:
        s.agent.analyst_decision(body.action_hash, body.approve, body.approver, body.role)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return _case_view(s)


@app.post("/api/case/edit")
def case_edit(body: EditBody) -> dict[str, Any]:
    """Analyst edits the evidence bundle. Editing changes the action hash, so it needs a fresh approval."""
    s = _session()
    try:
        s.agent.edit_plan_action(body.action_hash, body.docs)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _case_view(s)


@app.post("/api/case/consent")
def case_consent(body: ConsentBody) -> dict[str, Any]:
    s = _session()
    try:
        s.agent.merchant_consent(body.action_hash, body.accept)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return _case_view(s)


@app.get("/api/audit")
def audit() -> list[dict[str, Any]]:
    return _session().audit.dump()


@app.post("/api/adversarial/run")
def adversarial() -> dict[str, Any]:
    out = run_suite()
    return {k: out[k] for k in ("action_cases", "unsafe_actions", "screening_cases", "screening_misses",
                                 "screening_false_positives", "failed")} | {"rows": out["rows"]}


@app.post("/api/dev/fault")
def dev_fault(body: FaultBody) -> dict[str, Any]:
    try:
        _session().world.inject_fault(body.tool, body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ------------------------------------------------------------------ ops console (portfolio of cases)
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
    last = next((m.text for m in reversed(c.messages) if m.role == "agent"), "")
    return {
        "sid": s.id, "case_id": s.case_id, "merchant": s.world.spec.merchant_name, "family": s.scenario.family,
        "intent": c.intent, "language": c.language, "status": c.status.value, "closed": c.closed, "waiting_on": waiting,
        "tier_max": max([p.tier for p in plan], default=1), "hours_to_deadline": due_h,
        "at_risk": bool(rep and rep.obligation_at_risk), "p_miss": rep.chosen.p_miss if rep and rep.obligation_at_risk else None,
        "last_agent_message": last[:160], "n_messages": len(c.messages), "sim_now": s.world.now.isoformat(),
        "unsafe": len(s.unsafe()), "description": s.scenario.description,
    }


_ORDER = {"analyst": 0, "merchant": 1, "source": 2, None: 3}


@app.get("/api/cases")
def list_cases() -> list[dict[str, Any]]:
    rows = [_summary(s) for s in SESSIONS.values()]
    rows.sort(key=lambda r: (_ORDER.get(r["waiting_on"], 3), r["status"] == "verified", r["hours_to_deadline"] is None,
                             r["hours_to_deadline"] or 0))
    return rows


def _converse(s: Session, text: str | None = None) -> None:
    s.agent.handle_message(text or s.scenario.inbound.text)
    if text is None:
        for f in s.scenario.followups:
            s.agent.handle_message(f)


@app.post("/api/cases/seed")
def seed_portfolio(body: SeedBody) -> list[dict[str, Any]]:
    """Replace the portfolio with one case per scenario family (canonical flagship), each already handled by the agent."""
    SESSIONS.clear()
    for fam in body.families or list(REGISTRY):
        scn = generate(fam, CANONICAL_SEED, canonical=(fam == "missing_evidence_flagship"))
        s = _add(Session(scn))
        if body.send_messages:
            _converse(s)
    return list_cases()


@app.post("/api/cases/new")
def new_case(body: NewCaseBody) -> dict[str, Any]:
    try:
        scn = generate(body.family, body.seed, body.canonical)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if body.text:
        scn = scn.model_copy(update={"inbound": InboundMessage(text=body.text, language=scn.inbound.language),
                                     "followups": []})
    s = _add(Session(scn))
    _converse(s, None if not body.text else body.text)
    return _summary(s)


@app.post("/api/cases/advance")
def advance_all(body: AdvanceBody) -> list[dict[str, Any]]:
    for s in SESSIONS.values():
        s.world.advance(body.hours)
        s.agent.tick()
    return list_cases()


@app.get("/api/console/kpis")
def kpis() -> dict[str, Any]:
    sess = list(SESSIONS.values())
    rows = [_summary(s) for s in sess]
    auto = sum(1 for r in rows if r["status"] == "auto_resolved")
    needed_human = sum(1 for r in rows if r["waiting_on"] in ("analyst", "merchant") or r["status"] in ("escalated", "verified")
                       or any(p.tier >= 2 for p in sess[rows.index(r)].agent.s.plan))
    verified = sum(1 for r in rows if r["status"] == "verified")
    mins = sum(s.agent.s.memo.minutes_saved for s in sess if s.agent.s.memo and any(p.tier == 3 for p in s.agent.s.plan))
    return {
        "total": len(rows), "auto_resolved": auto, "verified": verified,
        "awaiting_analyst": sum(1 for r in rows if r["waiting_on"] == "analyst"),
        "awaiting_merchant": sum(1 for r in rows if r["waiting_on"] == "merchant"),
        "escalated": sum(1 for r in rows if r["status"] == "escalated"),
        "open": sum(1 for r in rows if not r["closed"] and r["status"] not in ("escalated",)),
        "autonomous_rate": {"k": auto, "n": auto + needed_human, "label": "auto-resolved ÷ (auto-resolved + cases that needed a person)"},
        "unsafe_actions": sum(r["unsafe"] for r in rows),
        "modeled_minutes_saved": mins,
        "at_risk": sum(1 for r in rows if r["at_risk"] and not r["closed"]),
    }


@app.get("/api/approvals/pending")
def pending_approvals() -> list[dict[str, Any]]:
    out = []
    for s in SESSIONS.values():
        c = s.agent.s
        for p in c.plan:
            if p.state == "awaiting_approval":
                out.append({"sid": s.id, "case_id": s.case_id, "merchant": s.world.spec.merchant_name,
                            "family": s.scenario.family, "action": p.action.model_dump(mode="json"), "label": p.label,
                            "tier": p.tier, "memo": c.memo.model_dump(mode="json") if c.memo else None,
                            "bundle": c.bundle.model_dump(mode="json") if c.bundle else None,
                            "sim_now": s.world.now.isoformat(),
                            "hours_to_deadline": _summary(s)["hours_to_deadline"]})
    out.sort(key=lambda r: (r["hours_to_deadline"] is None, r["hours_to_deadline"] or 0))
    return out


# ------------------------------------------------------------------ benchmark results
@app.get("/api/bench/results")
def bench_results() -> dict[str, Any]:
    """Latest benchmark summary (frozen-v1 if present, else the dev run). Records are omitted to keep it small."""
    for name in ("bench_frozen-v1.json", "bench_dev.json"):
        path = ROOT / "data" / "results" / name
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("records", None)
            return data
    raise HTTPException(404, "no benchmark results yet: python -m app.bench.run --label dev")
