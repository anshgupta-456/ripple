# Language model: Claude, with a free local fallback (Ollama)

RIPPLE runs fully without any model: deterministic rules do the understanding and every reply is a template. A model is optional polish. Two providers exist:

| Provider | Costs | Needs |
|---|---|---|
| **Claude** (`anthropic`) | money per call | `ANTHROPIC_API_KEY` in the secret store and `pip install anthropic` |
| **Ollama** (`ollama`) | free, offline, nothing leaves the machine | `ollama serve` running and one model pulled |

## Settings
| Variable | Meaning |
|---|---|
| `RIPPLE_LLM_PROVIDER` | `auto` (default): Claude if a key is configured, and the local model whenever Claude is missing, failing or over its call cap. `anthropic`: Claude only. `ollama`: local only, never calls Claude. Anything else: no model is called |
| `RIPPLE_LLM` | `auto` (default), `cache_only`, `off` |
| `RIPPLE_OLLAMA_URL` | default `http://127.0.0.1:11434`. **Loopback only** unless the host is named in `RIPPLE_OLLAMA_ALLOWED_HOSTS` |
| `RIPPLE_OLLAMA_MODEL` | model name. Unset: the smallest installed completion model (embedding models and slow "thinking" models are skipped) |
| `RIPPLE_OLLAMA_TIMEOUT` | seconds per call, default 25 |
| `RIPPLE_OLLAMA_KEEP_ALIVE` | how long Ollama keeps the model in memory, default `30m`. The app also loads it in the background at startup |
| `RIPPLE_LLM_BREAKER_S` | after a provider fails, skip it for this long (default 60 s), so a dead provider cannot slow every message |
| `RIPPLE_LLM_LOCAL=off` | disable the local provider (the test suite does this) |
| `RIPPLE_LLM_LOCAL_POLISH=on` | also let the local model reword replies (off by default: slow, cosmetic) |

Using it: run `ollama serve`, pull a model, start RIPPLE. The Admin page has a **Language model** card (also `GET /api/admin/llm`, admin only, never shows a key) that says which provider is configured, whether Ollama is reachable, which model it picked, and whether a provider is paused. In Docker, point at the host with `RIPPLE_OLLAMA_URL=http://host.docker.internal:11434` and `RIPPLE_OLLAMA_ALLOWED_HOSTS=host.docker.internal` (checked: the container reached the host's Ollama this way).

## What the local model is allowed to do
A small CPU model is a fallback, not a peer of Claude, so it is boxed in:
- It is asked **only when the rules are unsure** (intent "other" or low confidence). A clear message never waits on it (0.2 s through the rules).
- It writes **two short fields** (intent and confidence), which keeps a call to seconds on a laptop CPU.
- It can only suggest a **read-only information or investigation intent** (settlement status/issue, KYC status, refund status, ETA). It can never produce consent, a supplier message, or anything forbidden, and it never touches language, weekday, amount or the "owes a supplier" flag.
- It is ignored unless the message contains **payments vocabulary** (settlement, payment, paisa, KYC, refund, bank, ...), so "app crash ho raha hai" is not turned into a settlement question.
- Its confidence is **capped at 0.75** (Claude's path is unchanged: it can only lower confidence on disagreement). What happens next is governed by the same firewall: nothing risky runs without an analyst's approval.
- Attacks (forbidden requests) and messages with bank, card, PAN, Aadhaar or UPI identifiers are never sent to it, same as for Claude. Other text is masked first.
- Redirects are not followed, replies over 1 MB are refused, cache entries are keyed by tenant, provider and model.
- A model that is not running, slow, or returns junk means the rules answer. Nothing breaks.

## Measured, honestly
Real runs on this laptop (7 GB RAM, no usable GPU, CPU only) on the 24 of the 120 labelled messages where the rules are unsure ([INTAKE_EVAL.md](INTAKE_EVAL.md); command `python -m app.intake.evaluate_local`; raw rows in `docs/intake_local_eval.json` and `docs/intake_local_eval_qwen2.5-3b.json`). "Helped" means the rules were wrong and the model right; "hurt" the reverse.

| | `qwen2.5vl:3b` (3B vision model) | `qwen2.5:3b` (3B text model, 1.9 GB) |
|---|---|---|
| Helped | 2 before the payments-vocabulary gate, **0 after** | **0** |
| Hurt | 2 before the gate, **0 after** | **0** |
| Wrong suggestion, no worse than rules' "other" | 3 (two were attacks, answered as a status question) | 2 (a refund/ETA guess on two payment questions) |
| No suggestion (said "other", or a non-suggestable intent) | 15 | 22 (including all 3 attacks and every genuinely unrelated message) |
| Median time per call | 5.4 s | 6.4 s (one cold load took 77 s) |

The vocabulary gate was chosen **after** seeing the first model's results and applied offline to those recorded outputs, so that column is not an independent measurement; the `qwen2.5:3b` column ran with the gate in place.

**Plain reading: neither 3B model helps, and the text model is not better.** Both are safe and free and never make things worse. The text model is more conservative (it says "other" about everything it cannot place, so it makes fewer wrong guesses, and it correctly stays out of the attack messages), but it also recovers none of the messages the rules miss: it answered "other" even for "how many more days". The rules miss exactly the awkward phrasings that a model this small also cannot place. The slice is small (24 messages, about 8 of them recoverable), so this cannot separate the two models; it does say a 3B model on a laptop CPU is not the way to improve intake. A 7B-class model does not fit comfortably in this machine's memory (`qwen3-vl:8b` is installed but skipped by default as a slow thinking model). Better routes for accuracy: fix the rule gaps listed in INTAKE_EVAL.md, or use Claude.

With `qwen2.5:3b` installed, the auto-pick now chooses it (it is first in the preference list). Its cold start (up to about 77 s) is longer than the default 25 s timeout, so the first call after the model has been unloaded is skipped and the breaker pauses the provider for a minute; the background warm-up at startup and `keep_alive` avoid this in normal use.

## Limits
- The local model's **reply rewording** is off by default and untested live.
- Hindi/Hinglish quality of small models is weak; the earlier language-detection slips ("english" for Hinglish) come from the rules, not the model.
- Tested live only against Ollama on this machine and from a container to the host. No other local runtime (llama.cpp server, LM Studio) was tried.
