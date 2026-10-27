from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.config import ROOT

POLICY_PATH = ROOT / "config" / "policy.yaml"


class ActionPolicy(BaseModel):
    tier: int
    max_per_case: int = 1
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    requires: str | None = None


class Policy(BaseModel):
    allowed_docs: list[str]
    reads: list[str]
    actions: dict[str, ActionPolicy]
    forbidden: list[str]


@lru_cache(maxsize=1)
def load_policy() -> Policy:
    with POLICY_PATH.open(encoding="utf-8") as fh:
        return Policy(**yaml.safe_load(fh))
