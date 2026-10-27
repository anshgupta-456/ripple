from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

from app.config import ROOT

EXT_PATH = ROOT / "config" / "extensions.yaml"


@lru_cache(maxsize=1)
def ext_config() -> dict[str, Any]:
    with EXT_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
