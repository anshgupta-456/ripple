from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.analysis import analyze_world
from app.bench.templates import CANONICAL_SEED, REGISTRY, Scenario, generate, write_ground_truth
from app.config import load_config
from app.mock_sources.world import (
    FAULT_MODES,
    MissingIdempotencyKey,
    ToolTimeout,
    UnknownTool,
    World,
)

app = FastAPI(title="RIPPLE", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"],
                   allow_headers=["*"])


class Session:
    def __init__(self, scenario: Scenario):
        self.id = uuid.uuid4().hex[:8]
        self.scenario = scenario
        self.world = World(scenario.world_spec)
        self.last_graph = None


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


class ToolBody(BaseModel):
    tool: str
    params: dict[str, Any] = {}
    idem_key: str | None = None


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


@app.post("/api/dev/tool")
def dev_tool(body: ToolBody) -> dict[str, Any]:
    w = _session().world
    try:
        return {"result": w.call(body.tool, body.params, body.idem_key)}
    except UnknownTool:
        raise HTTPException(404, f"no such tool: {body.tool}")
    except MissingIdempotencyKey:
        raise HTTPException(400, "write tools require an idempotency key")
    except ToolTimeout as e:
        raise HTTPException(504, {"error": "timeout", "idem_key": e.idem_key, "outcome": "unknown"})


@app.post("/api/dev/fault")
def dev_fault(body: FaultBody) -> dict[str, Any]:
    try:
        _session().world.inject_fault(body.tool, body.mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
