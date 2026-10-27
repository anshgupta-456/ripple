"""Mock state for the v3 Tier 1 action "schedule internal recheck": a follow-up that comes due after simulated time passes.
Registered on a World only when extensions are enabled, so the benchmark's worlds never have this tool.
"""
from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.mock_sources.world import ToolDef
from app.models.contracts import Source

if TYPE_CHECKING:  # pragma: no cover
    from app.mock_sources.world import World


class ExtSources:
    def __init__(self, world: "World"):
        self.w = world
        self.followups: list[dict[str, Any]] = []

    def read(self) -> dict[str, Any]:
        return {"followups": [dict(x) for x in self.followups]}

    def schedule_follow_up(self, hours: int, reason: str) -> dict[str, Any]:
        due = self.w.now + timedelta(hours=hours)
        f = {"followup_id": f"fu_{len(self.followups) + 1:03d}", "reason": reason, "due_at": due.isoformat(), "status": "open", "hours": hours}
        self.followups.append(f)
        self.w.schedule(due, "followup_due", {"followup_id": f["followup_id"]})
        self.w.emit(Source.SYSTEM, "followup_scheduled", {"followup_id": f["followup_id"], "due_at": f["due_at"]}, f"followup:{f['followup_id']}")
        return {"ok": True, "followup_id": f["followup_id"], "due_at": f["due_at"]}

    def _followup_due(self, payload: dict[str, Any]) -> None:
        for f in self.followups:
            if f["followup_id"] == payload.get("followup_id") and f["status"] == "open":
                f["status"] = "due"
                self.w.emit(Source.SYSTEM, "followup_due", {"followup_id": f["followup_id"], "reason": f["reason"]}, f"followup:{f['followup_id']}")

    def attach(self) -> "ExtSources":
        self.w.tools["read_ext_state"] = ToolDef("read_ext_state", lambda: self.read(), False)
        self.w.tools["schedule_follow_up"] = ToolDef("schedule_follow_up", lambda hours, reason: self.schedule_follow_up(hours, reason), True)
        self.w._handlers["followup_due"] = self._followup_due
        self.w.ext = self
        return self


def attach_ext_sources(world: "World") -> ExtSources:
    return ExtSources(world).attach()
