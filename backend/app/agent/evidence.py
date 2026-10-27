"""Evidence bundle compiler (Tier 1: internal, read-only). Lists what would be submitted and why."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.timeline.normalizer import Timeline


class EvidenceBundle(BaseModel):
    dispute_id: str
    docs: list[str]
    missing: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    summary: list[str] = Field(default_factory=list)
    provenance_note: str = "Synthetic merchant records; no real documents exist in this build."

    @property
    def complete(self) -> bool:
        return not self.missing and bool(self.docs)


def compile_bundle(tl: Timeline, available_docs: set[str] | None = None) -> EvidenceBundle | None:
    did = tl.val("dispute.id")
    if not did or tl.val("dispute.status") != "evidence_required":
        return None
    required = list(tl.val("dispute.required_docs") or [])
    have = available_docs if available_docs is not None else set(required)   # synthetic: all retrievable
    docs = [d for d in required if d in have]
    missing = [d for d in required if d not in have]
    return EvidenceBundle(
        dispute_id=did, docs=docs, missing=missing,
        source_refs=[tl.ref("dispute.status"), tl.ref("settlement.hold_reason_code")],
        summary=[f"Dispute {did} for Rs {tl.val('dispute.amount'):,} requires: {', '.join(required)}.",
                 f"Compiled {len(docs)}/{len(required)} required documents."] + (
                 [f"Missing: {', '.join(missing)}. Cannot submit a complete bundle."] if missing else []),
    )
