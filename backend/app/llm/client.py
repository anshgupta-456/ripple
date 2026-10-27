"""Thin LLM boundary: JSON-in/JSON-out, disk-cached, optional.

The LLM may extract and draft. It never scores, gates, executes or declares status; every output
is validated by code before it is used. If the SDK/key is missing or a call fails, callers fall
back to deterministic rules, so the demo never depends on an API being up.

RIPPLE_LLM = auto (default: cache, then live if possible) | cache_only | off
RIPPLE_LLM_MODEL = model id (default claude-haiku-5-5)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from app.config import ROOT

DEFAULT_MODEL = "claude-haiku-5-5"
CACHE_PATH = ROOT / "data" / "llm_cache.json"


class LLM:
    def __init__(self, mode: str | None = None, cache_path: Path | None = None, client: Any = None,
                 model: str | None = None):
        self.mode = (mode or os.environ.get("RIPPLE_LLM", "auto")).lower()
        self.model = model or os.environ.get("RIPPLE_LLM_MODEL", DEFAULT_MODEL)
        self.cache_path = cache_path or CACHE_PATH
        self._client = client
        self._cache: dict[str, Any] | None = None
        self.stats = {"cache": 0, "live": 0, "miss": 0}

    # ---- cache ----
    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}
        return self._cache

    def _key(self, task: str, system: str, user: str) -> str:
        return hashlib.sha256(json.dumps([self.model, task, system, user]).encode()).hexdigest()

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ---- client ----
    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        try:
            import anthropic  # optional dependency
        except ImportError:
            return None
        self._client = anthropic.Anthropic()
        return self._client

    def complete_json(self, task: str, system: str, user: str) -> tuple[dict[str, Any] | None, str]:
        """-> (parsed JSON or None, source in {'cache','live','none'})"""
        if self.mode == "off":
            return None, "none"
        key = self._key(task, system, user)
        cache = self._load()
        if key in cache:
            self.stats["cache"] += 1
            return cache[key], "cache"
        if self.mode == "cache_only":
            self.stats["miss"] += 1
            return None, "none"
        client = self._get_client()
        if client is None:
            self.stats["miss"] += 1
            return None, "none"
        try:
            resp = client.messages.create(model=self.model, max_tokens=600, temperature=0, system=system,
                                          messages=[{"role": "user", "content": user}])
            text = "".join(getattr(b, "text", "") for b in resp.content)
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else None
        except Exception:
            data = None
        if data is None:
            self.stats["miss"] += 1
            return None, "none"
        cache[key] = data
        self._save()
        self.stats["live"] += 1
        return data, "live"
