"""Approval (Tier 3) and consent (Tier 2) stores.

Approvals can be issued only by a principal with role "analyst". The agent has no role and no
method that creates approvals. Each approval binds one exact action hash + scope + expiry and is
single-use.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from app.models.contracts import Action, Approval


class Consent(BaseModel):
    action_hash: str
    merchant_id: str
    issued_at: datetime
    expires_at: datetime
    used: bool = False


def scope_of(action: Action) -> str:
    return f"{action.type}:{action.case_id}"


class ApprovalStore:
    def __init__(self, ttl_minutes: int):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._by_hash: dict[str, Approval] = {}

    def issue(self, action: Action, approver: str, role: str, now: datetime) -> Approval:
        if role != "analyst":
            raise PermissionError(f"role '{role}' cannot issue approvals")
        if not approver or approver.lower() in {"agent", "ripple", "system", "ai", "assistant"}:
            raise PermissionError("approver must be a named human analyst")
        a = Approval(action_hash=action.hash, scope=scope_of(action), approver=approver,
                     issued_at=now, expires_at=now + self.ttl)
        self._by_hash[action.hash] = a
        return a

    def get(self, action_hash: str) -> Approval | None:
        return self._by_hash.get(action_hash)

    def consume(self, action_hash: str) -> None:
        self._by_hash[action_hash].used = True


class ConsentStore:
    def __init__(self, ttl_minutes: int = 240):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._by_hash: dict[str, Consent] = {}

    def issue(self, action: Action, merchant_id: str, now: datetime) -> Consent:
        c = Consent(action_hash=action.hash, merchant_id=merchant_id, issued_at=now, expires_at=now + self.ttl)
        self._by_hash[action.hash] = c
        return c

    def get(self, action_hash: str) -> Consent | None:
        return self._by_hash.get(action_hash)

    def consume(self, action_hash: str) -> None:
        self._by_hash[action_hash].used = True
