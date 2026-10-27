"""Thin LLM boundary: JSON-in/JSON-out, disk-cached, optional, with a free local fallback.

The LLM may extract and draft. It never scores, gates, executes or declares status; every output is validated by code before it is
used (intake: schema + cross-check against the rules, replies: facts/promises/guards). If no model is reachable, callers fall back to
deterministic rules, so the demo never depends on any API being up.

Providers
  anthropic   the Claude API. Costs money; needs ANTHROPIC_API_KEY (secret store) and `pip install anthropic`.
  ollama      a model served by Ollama on THIS machine (free, offline). Needs `ollama serve` and one pulled model.
RIPPLE_LLM_PROVIDER = auto (default: Claude if a key is configured, and Ollama when Claude is missing, failing or out of budget)
                      | anthropic (Claude only) | ollama (local only, never calls Claude)
RIPPLE_LLM = auto (default: cache, then live) | cache_only | off
RIPPLE_LLM_MODEL = Claude model id (default claude-haiku-5-5)
RIPPLE_OLLAMA_URL = where the model server is. REQUIRED to use a local model: there is deliberately no default, so RIPPLE never starts
                    or loads a model on the machine it runs on by accident. Loopback only (e.g. an SSH tunnel to a GPU server,
                    http://127.0.0.1:11500); any other host must also be named in RIPPLE_OLLAMA_ALLOWED_HOSTS
RIPPLE_LOCAL_API = ollama (default: Ollama's /api) | openai (an OpenAI-compatible server: llama.cpp `llama-server`, vLLM, LM Studio; /v1/models and
                   /v1/chat/completions). Same URL setting, same safety rules, same role.
RIPPLE_OLLAMA_MODEL = model name; unset = the smallest installed completion model (embedding models are skipped)
RIPPLE_OLLAMA_TIMEOUT = seconds per call (default 25; small models on a laptop CPU are slow)
RIPPLE_OLLAMA_KEEP_ALIVE = how long Ollama keeps the model in memory after a call (default 5m; a cold load of a 3B model on a laptop CPU takes
                           minutes, a warm call seconds). The app also warms the model in the background at startup (warm_up()).
RIPPLE_LLM_LOCAL = off disables the local provider (the test suite sets this)
RIPPLE_LLM_BREAKER_S = after a provider fails, skip it for this many seconds (default 60) so a dead provider does not slow every message
RIPPLE_LLM_MAX_CALLS / RIPPLE_LLM_GLOBAL_MAX = live-call caps per client / paid calls per process (spend cap; over cap => next provider)
RIPPLE_LLM_HOSTS = extra allowed Claude API hosts (default only api.anthropic.com)

Data handling: every prompt is masked first (phones, emails, ...); if it contains REGULATED identifiers (bank/card/PAN/Aadhaar/UPI)
it is not sent to ANY provider, local included, and the caller falls back to deterministic rules (secure default; the local model
would keep the data on the machine, but one rule for every provider is easier to reason about). The API key lives only inside the
SDK client; it is never in a prompt or a log. The cache key includes the tenant, the provider and the model, so one provider's or
tenant's output is never served as another's. Data-retention/"do not train" terms for Claude are a provider contract matter; a local
model sends nothing off the machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.security.redact import llm_safe_text
from app.security.secret_store import get_secret

DEFAULT_MODEL = "claude-haiku-5-5"
ALLOWED_HOSTS = {"api.anthropic.com"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_GLOBAL_LIVE_CALLS = 0
CACHE_PATH = ROOT / "data" / "llm_cache.json"
MAX_REPLY_BYTES = 1_000_000

# process-wide: every case creates its own LLM object, but a dead provider should be skipped by all of them
_BREAKER: dict[str, float] = {}
TOTALS: dict[str, int] = {"anthropic": 0, "ollama": 0, "fallbacks": 0, "failures": 0}
_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, tuple[float, str | None, list[str]]] = {}      # url -> (expires, chosen model, all models)


def _breaker_open(name: str) -> bool:
    return _BREAKER.get(name, 0.0) > time.time()


def _trip(name: str) -> None:
    secs = float(os.environ.get("RIPPLE_LLM_BREAKER_S", "60"))
    with _LOCK:
        _BREAKER[name] = time.time() + secs
        TOTALS["failures"] += 1


def reset_state() -> None:
    """Clear the process-wide breaker, counters and model-list cache (tests, admin 'retry now')."""
    with _LOCK:
        _BREAKER.clear()
        _MODEL_CACHE.clear()
        for k in TOTALS:
            TOTALS[k] = 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http_json(url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 method="POST" if payload is not None else "GET", headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
        raw = resp.read(MAX_REPLY_BYTES + 1)
    if len(raw) > MAX_REPLY_BYTES:
        raise ValueError("response too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("unexpected response")
    return data


# --------------------------------------------------------------------------- local provider (Ollama)
# Smaller is better here: this runs on a laptop CPU, and the job is a few short fields. Order = preference when several are installed.
_PREFERRED = ("qwen2.5:3b", "qwen2.5:1.5b", "llama3.2:3b", "llama3.2:1b", "gemma2:2b", "phi3:mini", "qwen2.5:7b", "llama3.1:8b")


class Ollama:
    def __init__(self, url: str | None = None, model: str | None = None, timeout: float | None = None):
        self.url = (url or os.environ.get("RIPPLE_OLLAMA_URL") or "").strip().rstrip("/")
        self.configured_model = model or os.environ.get("RIPPLE_OLLAMA_MODEL", "").strip() or None
        self.timeout = timeout if timeout is not None else float(os.environ.get("RIPPLE_OLLAMA_TIMEOUT", "25"))
        self.api = (os.environ.get("RIPPLE_LOCAL_API", "") or "ollama").strip().lower()

    @property
    def provider_label(self) -> str:
        """Part of the cache key: an Ollama answer and an OpenAI-style server's answer are different providers."""
        return "ollama" if self.api == "ollama" else "local-openai"

    def host_ok(self) -> bool:
        """Local only by default. Pointing the 'local' model at another machine is an explicit decision, named host by host."""
        if not self.url or self.api not in ("ollama", "openai"):
            return False                       # not configured (or an unknown API dialect): the local provider is off
        u = urllib.parse.urlparse(self.url)
        extra = {h.strip().lower() for h in os.environ.get("RIPPLE_OLLAMA_ALLOWED_HOSTS", "").split(",") if h.strip()}
        return u.scheme in ("http", "https") and (u.hostname or "").lower() in (LOOPBACK | extra)

    def models(self) -> tuple[str | None, list[str]]:
        """(chosen model, installed completion models). Cached briefly. (None, []) when the server is not reachable."""
        ck = f"{self.api}|{self.url}"
        hit = _MODEL_CACHE.get(ck)
        if hit and hit[0] > time.time():
            return (self.configured_model or hit[1]), hit[2]
        try:
            if self.api == "openai":
                listed = _http_json(self.url + "/v1/models", None, min(self.timeout, 3.0)).get("data", [])
                names = [str(m["id"]) for m in listed if isinstance(m, dict) and m.get("id")
                         and "embed" not in str(m["id"]).lower() and "rerank" not in str(m["id"]).lower()]
                chosen = names[0] if names else None                       # these servers normally host one model: it is the one to use
                _MODEL_CACHE[ck] = (time.time() + 300, chosen, names)
                return (self.configured_model or chosen), names
            tags = _http_json(self.url + "/api/tags", None, min(self.timeout, 3.0)).get("models", [])
        except Exception:
            _MODEL_CACHE[ck] = (time.time() + 10, None, [])      # do not re-probe a dead server on every message
            return self.configured_model, []
        usable = [m for m in tags if isinstance(m, dict) and "embedding" not in (m.get("capabilities") or [])
                  and "embed" not in str(m.get("name", "")).lower() and "bge" not in str(m.get("name", "")).lower()]
        names = [str(m["name"]) for m in usable]
        chosen = next((p for p in _PREFERRED if p in names), None)
        if chosen is None and usable:
            small = [m for m in usable if "thinking" not in (m.get("capabilities") or [])] or usable      # thinking models are slow and wordy
            chosen = str(min(small, key=lambda m: int(m.get("size") or 1 << 60))["name"])
        _MODEL_CACHE[ck] = (time.time() + 300, chosen, names)
        return (self.configured_model or chosen), names

    def complete(self, model: str, system: str, user: str) -> str:
        if self.api == "openai":
            data = _http_json(self.url + "/v1/chat/completions", {
                "model": model, "stream": False, "temperature": 0, "max_tokens": 400, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }, self.timeout)
            choices = data.get("choices") or [{}]
            return str(((choices[0] or {}).get("message") or {}).get("content") or "")
        data = _http_json(self.url + "/api/chat", {
            "model": model, "stream": False, "format": "json", "think": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "keep_alive": os.environ.get("RIPPLE_OLLAMA_KEEP_ALIVE", "5m"),
            "options": {"temperature": 0, "num_predict": 400, "num_ctx": 2048},
        }, self.timeout)
        return str((data.get("message") or {}).get("content") or "")

    def warm(self, timeout: float = 300.0) -> bool:
        """Load the model into memory with a one-token request. Blocking; run it in a thread."""
        model, _ = self.models()
        if not model:
            return False
        if self.api == "openai":
            return True                        # these servers load their model at start: listing it above is the whole check
        try:
            _http_json(self.url + "/api/chat", {"model": model, "stream": False, "think": False,
                                                "messages": [{"role": "user", "content": "hi"}], "keep_alive": os.environ.get("RIPPLE_OLLAMA_KEEP_ALIVE", "5m"),
                                                "options": {"num_predict": 1, "num_ctx": 2048}}, timeout)
            return True
        except Exception:
            return False


def warm_up() -> threading.Thread | None:
    """Fire-and-forget: get the local model into memory so the first real message is not the one that pays for the load. Does nothing
    unless the local provider is wanted, allowed (loopback) and running; never raises."""
    pref = (os.environ.get("RIPPLE_LLM_PROVIDER", "auto") or "auto").lower()
    if os.environ.get("RIPPLE_LLM", "auto").lower() in ("off", "cache_only") or pref not in ("auto", "ollama")             or os.environ.get("RIPPLE_LLM_LOCAL", "").lower() == "off":
        return None
    ol = Ollama()
    if not ol.host_ok():
        return None
    t = threading.Thread(target=ol.warm, name="ollama-warmup", daemon=True)
    t.start()
    return t


def llm_status() -> dict[str, Any]:
    """Non-secret status for the admin page: what would be used, in what order, and what is currently failing."""
    pref = (os.environ.get("RIPPLE_LLM_PROVIDER", "auto") or "auto").lower()
    ol = Ollama()
    local_on = os.environ.get("RIPPLE_LLM_LOCAL", "").lower() != "off" and pref in ("auto", "ollama") and bool(ol.url)
    chosen, names = ol.models() if local_on and ol.host_ok() else (None, [])
    return {
        "mode": os.environ.get("RIPPLE_LLM", "auto"), "provider": pref,
        "anthropic": {"key_configured": bool(get_secret("ANTHROPIC_API_KEY", required=False)) if pref in ("auto", "anthropic") else False,
                      "model": os.environ.get("RIPPLE_LLM_MODEL", DEFAULT_MODEL), "paused": _breaker_open("anthropic")},
        "ollama": {"enabled": local_on, "api": ol.api, "url": ol.url, "local_only": ol.host_ok(), "reachable": bool(names), "model": chosen,
                   "installed": names, "paused": _breaker_open("ollama")},
        "calls": dict(TOTALS),
    }


# --------------------------------------------------------------------------- the boundary
class LLM:
    def __init__(self, mode: str | None = None, cache_path: Path | None = None, client: Any = None,
                 model: str | None = None, tenant: str = "", provider: str | None = None, ollama: Ollama | None = None):
        self.tenant = tenant                   # part of every cache key: one tenant's cached output never answers another tenant
        self.mode = (mode or os.environ.get("RIPPLE_LLM", "auto")).lower()
        self.model = model or os.environ.get("RIPPLE_LLM_MODEL", DEFAULT_MODEL)
        self.provider = (provider or os.environ.get("RIPPLE_LLM_PROVIDER", "auto") or "auto").lower()
        self.cache_path = cache_path or CACHE_PATH
        self._client = client
        self._cache: dict[str, Any] | None = None
        self.local = ollama or Ollama()
        self.stats = {"cache": 0, "live": 0, "local": 0, "fallback": 0, "miss": 0, "blocked_regulated": 0, "budget": 0, "host_refused": 0}
        self.max_calls = int(os.environ.get("RIPPLE_LLM_MAX_CALLS", "50"))

    # ---- which providers, in what order ----
    def _order(self) -> list[str]:
        local_ok = os.environ.get("RIPPLE_LLM_LOCAL", "").lower() != "off" and bool(self.local.url)     # no URL, no local provider
        return {"auto": ["anthropic"] + (["ollama"] if local_ok else []), "anthropic": ["anthropic"],
                "ollama": ["ollama"] if local_ok else []}.get(self.provider, [])     # an unknown setting calls nothing

    # ---- cache ----
    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}
        return self._cache

    def _key(self, task: str, system: str, user: str, provider: str = "anthropic", model: str | None = None) -> str:
        return hashlib.sha256(json.dumps([self.tenant, provider, model or self.model, task, system, user]).encode()).hexdigest()

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ---- Claude ----
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
        allowed = ALLOWED_HOSTS | {h.strip() for h in os.environ.get("RIPPLE_LLM_HOSTS", "").split(",") if h.strip()}
        # what the client will actually call wins; the env var only matters when the client does not expose its URL
        url = str(getattr(client, "base_url", "") or "") or os.environ.get("ANTHROPIC_BASE_URL", "")
        if not url:
            return True            # SDK default (api.anthropic.com), or a test double with no network
        return (urllib.parse.urlparse(url).hostname or "") in allowed

    def _call_claude(self, system: str, user: str) -> str:
        client = self._client if self._client is not None else self._get_client()
        resp = client.messages.create(model=self.model, max_tokens=600, temperature=0, system=system,
                                      messages=[{"role": "user", "content": user}])
        return "".join(getattr(b, "text", "") for b in resp.content)

    @staticmethod
    def _parse(text: str) -> dict[str, Any] | None:
        m = re.search(r"\{.*\}", text, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def complete_json(self, task: str, system: str, user: str, *, local_ok: bool = True,
                      local_system: str | None = None) -> tuple[dict[str, Any] | None, str]:
        """-> (parsed JSON or None, source in {'cache','cache_local','live','local','none'}).

        local_ok=False keeps this call off the local model (callers use it where a small model adds latency and risk but little value).
        local_system is a shorter prompt for the local model: on a laptop CPU the cost is the number of tokens it has to WRITE."""
        global _GLOBAL_LIVE_CALLS
        if self.mode == "off":
            return None, "none"
        safe = llm_safe_text(user)
        if safe is None:                       # regulated identifiers present: never leaves the process, whichever provider
            self.stats["blocked_regulated"] += 1
            return None, "none"
        user = safe                            # masked text is what is hashed, cached and sent
        order = self._order()
        if not local_ok:
            order = [p for p in order if p != "ollama"]
        cache = self._load()
        local_model: str | None = None
        if "ollama" in order:
            if self.local.host_ok():
                local_model = self.local.models()[0]
            else:
                self.stats["host_refused"] += 1
                order = [p for p in order if p != "ollama"]
        local_sys = local_system or system
        keys = {"anthropic": self._key(task, system, user, "anthropic", self.model)}
        if local_model:
            keys["ollama"] = self._key(task, local_sys, user, self.local.provider_label, local_model)
        for p in order:
            if p in keys and keys[p] in cache:
                self.stats["cache"] += 1
                return cache[keys[p]], ("cache_local" if p == "ollama" else "cache")
        if self.mode == "cache_only":
            self.stats["miss"] += 1
            return None, "none"

        tried_failed = False
        for p in order:
            if p == "anthropic":
                client = self._get_client()
                if client is None or _breaker_open("anthropic"):
                    tried_failed = True                # Claude was wanted but is not available: a local answer counts as the fallback
                    continue
                if self.stats["live"] >= self.max_calls or _GLOBAL_LIVE_CALLS >= int(os.environ.get("RIPPLE_LLM_GLOBAL_MAX", "500")):
                    self.stats["budget"] += 1          # spend cap reached: next provider, else deterministic rules
                    tried_failed = True
                    continue
                if not self._host_ok(client):
                    self.stats["host_refused"] += 1
                    continue
                try:
                    data = self._parse(self._call_claude(system, user))
                except Exception:
                    data = None
                if data is None:
                    _trip("anthropic")                 # out of credit, bad key, outage, junk: do not retry on every message
                    tried_failed = True
                    continue
                cache[keys["anthropic"]] = data
                self._save()
                self.stats["live"] += 1
                with _LOCK:
                    _GLOBAL_LIVE_CALLS += 1
                    TOTALS["anthropic"] += 1
                return data, "live"
            if p == "ollama":
                if not local_model or _breaker_open("ollama"):
                    continue
                if self.stats["local"] >= self.max_calls:
                    self.stats["budget"] += 1
                    continue
                try:
                    data = self._parse(self.local.complete(local_model, local_sys, user))
                except Exception:
                    data = None
                if data is None:
                    _trip("ollama")
                    continue
                cache[keys["ollama"]] = data
                self._save()
                self.stats["local"] += 1
                if tried_failed:
                    self.stats["fallback"] += 1
                with _LOCK:
                    TOTALS["ollama"] += 1
                    if tried_failed:
                        TOTALS["fallbacks"] += 1
                return data, "local"
        self.stats["miss"] += 1
        return None, "none"
