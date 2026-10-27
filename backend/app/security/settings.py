"""Security-relevant runtime settings. Read at call time (never import time) so they can be changed per process/test.

Secure default: RIPPLE_ENV is "prod" unless explicitly set to "dev". Prod fails closed.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.config import ROOT


def env() -> str:
    return os.environ.get("RIPPLE_ENV", "prod").strip().lower()


def is_dev() -> bool:
    return env() == "dev"


def data_dir() -> Path:
    p = Path(os.environ.get("RIPPLE_DATA_DIR") or (ROOT / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def retention_days() -> int:
    try:
        return max(1, int(os.environ.get("RIPPLE_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


MAX_TOKEN_LIFETIME_S = 3600       # tokens valid for longer than an hour are rejected, whoever signed them
DEV_TOKEN_LIFETIME_S = 900
