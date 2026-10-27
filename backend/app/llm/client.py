"""Thin LLM boundary: JSON-in/JSON-out, disk-cached, optional.

The LLM may extract and draft. It never scores, gates, executes or declares status; every output
is validated by code before it is used. If the SDK/key is missing or a call fails, callers fall
back to deterministic rules, so the demo never depends on an API being up.

RIPPLE_LLM = auto (default: cache, then live if possible) | cache_only | off
RIPPLE_LLM_MODEL = model id (default claude-haiku-5-5)
RIPPLE_LLM_MAX_CALLS / RIPPLE_LLM_GLOBAL_MAX = live-call caps per client / per process (spend cap; over cap => rules)
RIPPLE_LLM_HOSTS = extra allowed API hosts (default only api.anthropic.com)

Data handling: every prompt is masked first (phones, emails, ...); if it contains REGULATED identifiers (bank/card/PAN/
Aadhaar/UPI) it is not sent at all and the caller falls back to deterministic rules. The API key lives only inside the
SDK client; it is never in a prompt or a log. Data-retention/"do not train" terms are a provider contract matter, to be
confirmed in your agreement; this code only minimizes what is sent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.security.redact import llm_safe_text
from app.security.secret_store import get_secret

DEFAULT_MODEL = "claude-haiku-5-5"
ALLOWED_HOSTS = {"api.anthropic.com"}
_GLOBAL_LIVE_CALLS = 0
CACHE_PATH = ROOT / "data" / "llm_cache.json"


class LLM:
    def __init__(self, mode: str | None = None, cache_path: Path | None = None, client: Any = None,
                 model: str | None = None, tenant: str = ""):
        self.tenant = tenant                   # part of every cache key: one tenant's cached output never answers another tenant
        self.mode = (mode or os.environ.get("RIPPLE_LLM", "auto")).lower()
        self.model = model or os.environ.get("RIPPLE_LLM_MODEL", DEFAULT_MODEL)
        self.cache_path = cache_path or CACHE_PATH
        self._client = client
        self._cache: dict[str, Any] | None = None
        self.stats = {"cache": 0, "live": 0, "miss": 0, "blocked_regulated": 0, "budget": 0, "host_refused": 0}
        self.max_calls = int(os.environ.get("RIPPLE_LLM_MAX_CALLS", "50"))

    # ---- cache ----
    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}
        return self._cache

    def _key(self, task: str, system: str, user: str) -> str:
        return hashlib.sha256(json.dumps([self.tenant, self.model, task, system, user]).encode()).hexdigest()

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
        key = get_secret("ANTHROPIC_API_KEY", required=False)
        if not key:
            return None
        try:
            import anthropic  # optional dependency
        except ImportError:
            return None
        self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def _host_ok(self, client: Any) -> bool:
        """Domain allowlist for outbound calls: refuse any endpoint that is not an approved model API host."""
        from urllib.parse import urlparse
        allowed = ALLOWED_HOSTS | {h.strip() for h in os.environ.get("RIPPLE_LLM_HOSTS", "").split(",") if h.strip()}
        # what the client will actually call wins; the env var only matters when the client does not expose its URL
        url = str(getattr(client, "base_url", "") or "") or os.environ.get("ANTHROPIC_BASE_URL", "")
        if not url:
            return True            # SDK default (api.anthropic.com), or a test double with no network
        return (urlparse(url).hostname or "") in allowed

    def complete_json(self, task: str, system: str, user: str) -> tuple[dict[str, Any] | None, str]:
        """-> (parsed JSON or None, source in {'cache','live','none'})"""
        if self.mode == "off":
            return None, "none"
        safe = llm_safe_text(user)
        if safe is None:                       # regulated identifiers present: never leaves the process
            self.stats["blocked_regulated"] += 1
            return None, "none"
        user = safe                            # masked text is what is hashed, cached and sent
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
        global _GLOBAL_LIVE_CALLS
        if self.stats["live"] >= self.max_calls or _GLOBAL_LIVE_CALLS >= int(os.environ.get("RIPPLE_LLM_GLOBAL_MAX", "500")):
            self.stats["budget"] += 1          # spend cap reached: degrade to deterministic rules
            return None, "none"
        if not self._host_ok(client):
            self.stats["host_refused"] += 1
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
        _GLOBAL_LIVE_CALLS += 1
        return data, "live"
