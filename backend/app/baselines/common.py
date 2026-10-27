"""Shared plumbing for all five systems: a tool-layer call log and the system interface.

Every system acts on the SAME LoggedWorld through the SAME tool API. The log is taken at the tool
layer (below any firewall), so "unsafe action" is measured identically for every system.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.audit.trace import next_seq
from app.mock_sources.world import MissingIdempotencyKey, ToolTimeout, UnknownTool, World


class LoggedWorld(World):
    def __init__(self, spec):
        super().__init__(spec)
        self.calls: list[dict[str, Any]] = []
        self.event_gseq: dict[str, int] = {}

    def emit(self, source, type_, payload, source_ref):
        ev = super().emit(source, type_, payload, source_ref)
        self.event_gseq[ev.id] = next_seq()
        return ev

    def call(self, tool: str, params: dict[str, Any] | None = None, idem_key: str | None = None) -> Any:
        rec = {"gseq": next_seq(), "tool": tool, "params": dict(params or {}), "key": idem_key, "at": self.now.isoformat(), "outcome": "ok",
               "write": bool(self.tools.get(tool) and self.tools[tool].write)}
        self.calls.append(rec)
        try:
            out = super().call(tool, params, idem_key)
            if isinstance(out, dict) and out.get("ok") is False:
                rec["outcome"] = "source_error"
                rec["error"] = out.get("error")
            return out
        except UnknownTool:
            rec["outcome"] = "unknown_tool"
            raise
        except ToolTimeout:
            rec["outcome"] = "timeout"
            raise
        except MissingIdempotencyKey:
            rec["outcome"] = "missing_key"
            raise


class SysState(BaseModel):
    name: str
    closed: bool = False
    claimed_resolved: bool = False
    status: str = ""
    handoff: bool = False                 # handed to a human (approval request or escalation)
    replies: list[str] = Field(default_factory=list)
    memo_built: bool = False
    claimed_causes: list[str] = Field(default_factory=list)
    granted_approvals: list[str] = Field(default_factory=list)   # action hashes with a real approval
    granted_consents: list[str] = Field(default_factory=list)
    stability: float | None = None
    blind_retries: int = 0
    attempted_forbidden: bool = False
    closed_at: str | None = None
    case_id: str = "C-bench"
    notes: list[str] = Field(default_factory=list)


class BaseSystem:
    name = "?"
    label = "?"

    def __init__(self, world: LoggedWorld, scenario, seed: int):
        self.w, self.scn, self.seed = world, scenario, seed
        self.st = SysState(name=self.name)

    def handle(self, text: str) -> None:
        raise NotImplementedError

    def tick(self) -> None:
        pass

    def pending_human(self) -> list[tuple[str, str]]:
        """[(kind 'approval'|'consent', action_hash)] awaiting a person."""
        return []

    def human_decide(self, kind: str, action_hash: str, accept: bool) -> None:
        pass

    def state(self) -> SysState:
        return self.st

    def _close(self, claimed: bool = True) -> None:
        self.st.closed, self.st.claimed_resolved = True, claimed
        self.st.closed_at = self.w.now.isoformat()
