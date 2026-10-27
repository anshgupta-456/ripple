"""Core data contracts. Frozen early: the frontend types are generated from these."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Source(str, Enum):
    SETTLEMENT = "settlement"
    DISPUTE = "dispute"
    KYC = "kyc"
    SUPPLIER = "supplier"
    NOTIFICATION = "notification"
    MERCHANT = "merchant"
    SYSTEM = "system"


class Tier(IntEnum):
    AGENT_ALONE = 1
    MERCHANT_CONSENT = 2
    HUMAN_APPROVAL = 3
    NEVER = 4


class EdgeClass(str, Enum):
    CONFIRMED_CAUSAL = "confirmed_causal"
    OBLIGATION = "obligation"
    SOFT = "soft"
    UNKNOWN = "unknown"


class ObligationKind(str, Enum):
    SUPPLIER_PAYMENT = "supplier_payment"
    SETTLEMENT = "settlement"
    REFUND = "refund"
    KYC = "kyc"


class ActionState(str, Enum):
    """State ladder. Only VERIFIED may close a case."""

    ATTEMPTED = "attempted"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    EXECUTED = "executed"
    VERIFIED = "verified"


class VerificationState(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CaseStatus(str, Enum):
    OPEN = "open"
    AUTO_RESOLVED = "auto_resolved"
    WAITING_MERCHANT = "waiting_merchant"
    WAITING_ANALYST = "waiting_analyst"
    ESCALATED = "escalated"
    VERIFIED = "verified"


class Event(BaseModel):
    """Source-tagged, freshness-marked fact emitted by a mock source."""

    id: str
    ts: datetime
    source: Source
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_ref: str
    freshness: str = "fresh"  # fresh | stale | unknown


class Obligation(BaseModel):
    id: str
    kind: ObligationKind
    amount: int
    due_at: datetime
    owner: str
    description: str = ""


class Edge(BaseModel):
    src: str
    dst: str
    edge_class: EdgeClass
    provenance: list[str] = Field(default_factory=list)  # event source_refs
    note: str = ""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def action_hash(case_id: str, action_type: str, params: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json([case_id, action_type, params]).encode()).hexdigest()


class Action(BaseModel):
    case_id: str
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    tier: Tier = Tier.AGENT_ALONE
    idempotency_key: str = ""
    expires_at: datetime | None = None
    hash: str = ""

    @model_validator(mode="after")
    def _fill_derived(self) -> "Action":
        if not self.hash:
            self.hash = action_hash(self.case_id, self.type, self.params)
        if not self.idempotency_key:
            self.idempotency_key = "idem_" + self.hash[:16]
        return self


class Approval(BaseModel):
    """Bound to one exact action hash, scope and expiry. Single use."""

    action_hash: str
    scope: str
    approver: str
    issued_at: datetime
    expires_at: datetime
    used: bool = False


class Verification(BaseModel):
    state: VerificationState
    reason: str = ""
    source_snapshot: dict[str, Any] = Field(default_factory=dict)


class Case(BaseModel):
    id: str
    merchant_id: str
    language: str = "hinglish"
    intent: str = ""
    status: CaseStatus = CaseStatus.OPEN
    graph_version: int = 0
    seed: int = 0
    event_ids: list[str] = Field(default_factory=list)
