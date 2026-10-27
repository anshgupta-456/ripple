from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SettlementSpec(BaseModel):
    status: Literal["processing", "held_risk", "released"]
    amount_total: int
    held_amount: int = 0
    hold_reason_code: str | None = None
    linked_dispute_id: str | None = None
    release_at: datetime | None = None  # scheduled natural release (no hold)


class DisputeSpec(BaseModel):
    dispute_id: str
    amount: int
    evidence_due: datetime
    required_docs: list[str] = Field(default_factory=lambda: ["invoice", "delivery_proof", "customer_comm"])
    review_hours: float = 20
    review_outcome: Literal["won", "lost"] = "won"
    release_delay_hours: float = 6


class KycSpec(BaseModel):
    status: Literal["valid", "expiring", "expired"] = "valid"
    expires_at: datetime | None = None
    opted_in_reminders: bool = False


class SupplierSpec(BaseModel):
    supplier_id: str
    name: str
    amount: int
    due_at: datetime


class WorldSpec(BaseModel):
    merchant_id: str
    merchant_name: str
    language: Literal["hindi", "english", "hinglish"] = "hinglish"
    start: datetime
    settlement: SettlementSpec
    dispute: DisputeSpec | None = None
    kyc: KycSpec = Field(default_factory=KycSpec)
    supplier: SupplierSpec | None = None
