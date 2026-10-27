from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.orchestrator import Orchestrator
from app.analysis import analyze_world
from app.audit.log import AuditLog
from app.bench.adversarial import run_suite
from app.executor.executor import Executor
from app.firewall.firewall import Firewall, FirewallContext
from app.firewall.stores import ApprovalStore, ConsentStore
from app.models.contracts import Action
from app.bench.templates import CANONICAL_SEED, REGISTRY, Scenario, generate, write_ground_truth
from app.config import ROOT, load_config
from app.llm.client import LLM
from app.mock_sources.world import FAULT_MODES, World

app = FastAPI(title="RIPPLE", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"],
                   allow_headers=["*"])


class Session:
    def __init__(self, scenario: Scenario):
        self.id = uuid.uuid4().hex[:8]
        self.scenario = scenario
        self.world = World(scenario.world_spec)
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


SESSION: Session | None = None


def _session() -> Session:
    global SESSION
    if SESSION is None:
        SESSION = Session(generate("missing_evidence_flagship", CANONICAL_SEED, canonical=True))
    return SESSION


def _public_state(s: Session) -> dict[str, Any]:
    return {
        "session_id": s.id,
        "scenario": s.scenario.public(),
        "world": s.world.snapshot(),
        "banner": load_config()["synthetic_data_banner"],
    }


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


class ConsentBody(BaseModel):
    action_hash: str
    accept: bool


class ExecuteBody(BaseModel):
    action_hash: str


class FaultBody(BaseModel):
    tool: str
    mode: str


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "config_version": load_config()["version"]}


@app.get("/api/scenarios")
def scenarios() -> list[dict[str, Any]]:
    out = []
    for family in REGISTRY:
        scn = generate(family, CANONICAL_SEED, canonical=True)
        out.append({"family": family, "split": scn.split, "description": scn.description})
    return out


@app.post("/api/session/reset")
def reset(body: ResetBody) -> dict[str, Any]:
    global SESSION
    try:
        scn = generate(body.family, body.seed, body.canonical)
    except KeyError as e:
        raise HTTPException(404, str(e))
    write_ground_truth(scn)  # evaluator-only file; never returned by the API
    SESSION = Session(scn)
    return _public_state(SESSION)


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
    async def gen():
        sess = _session()
        idx = ticks = 0
        yield f"event: hello\ndata: {json.dumps({'session_id': sess.id})}\n\n"
        while True:
            if await request.is_disconnected():
                return
            if SESSION is not None and SESSION.id != sess.id:
                sess, idx = SESSION, 0
                yield f"event: reset\ndata: {json.dumps({'session_id': sess.id})}\n\n"
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


# ---- dev-only endpoints (Phase 1). The firewall will front tool calls in Phase 3. ----
@app.get("/api/dev/tools")
def dev_tools() -> dict[str, Any]:
    w = _session().world
    return {"tools": {n: {"write": t.write} for n, t in w.tools.items()}, "fault_modes": list(FAULT_MODES)}


# ---- action pipeline: propose -> (consent | approval) -> execute. Every step is audited. ----
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


# ---- the agent: one case per session ----
@app.post("/api/case/message")
def case_message(body: MessageBody) -> dict[str, Any]:
    s = _session()
    s.agent.handle_message(body.text)
    return s.agent.view()


@app.get("/api/case")
def case_view() -> dict[str, Any]:
    return _session().agent.view()


@app.post("/api/case/decide")
def case_decide(body: DecideBody) -> dict[str, Any]:
    s = _session()
    try:
        s.agent.analyst_decision(body.action_hash, body.approve, body.approver, body.role)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return s.agent.view()


@app.post("/api/case/consent")
def case_consent(body: ConsentBody) -> dict[str, Any]:
    s = _session()
    try:
        s.agent.merchant_consent(body.action_hash, body.accept)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return s.agent.view()


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
