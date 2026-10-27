from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "frozen.yaml"
GROUND_TRUTH_DIR = ROOT / "data" / "ground_truth"


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
