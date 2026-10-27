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
Real run on this laptop (7 GB RAM, no usable GPU, CPU only) with the installed `qwen2.5vl:3b` (a 3B vision-language model, used for text), on the 24 of the 120 labelled messages where the rules are unsure ([INTAKE_EVAL.md](INTAKE_EVAL.md); raw rows in `docs/intake_local_eval.json`, command `python -m app.intake.evaluate_local`):

| | Messages |
|---|---|
| Local model helped (rules wrong, model right) | 2 before the vocabulary gate, **0 after** |
| Local model hurt (rules right, model wrong) | 2 before the gate, **0 after** |
| Still wrong, no worse than before (unsure rules, wrong read-only intent) | 3 (two of them attacks the rules also missed, answered as a status question) |
| No usable suggestion (it said "other", or a non-suggestable intent) | 15 |
| Time per call | median 5.4 s warm; a cold load of the model took over 100 s, which is why it is kept loaded and warmed at startup |

The vocabulary gate was chosen **after** seeing these results and its effect was computed offline from the same recorded model outputs, so it is not an independent measurement.

**Plain reading: with this particular model the local fallback is close to neutral. It is safe and free, it never makes things worse, but it adds almost no accuracy; the deterministic rules are doing the real work.** A 3B vision model is not the best choice for text classification. A small *text* model (for example `qwen2.5:3b` or `llama3.2:3b`, about 2 GB) would probably do better and is the first thing to try; it was not downloaded or measured here. A 7B or 8B model does not fit comfortably in this machine's memory (`qwen3-vl:8b` is installed but is skipped by default because it is a slow thinking model).

## Limits
- The local model's **reply rewording** is off by default and untested live.
- Hindi/Hinglish quality of small models is weak; the earlier language-detection slips ("english" for Hinglish) come from the rules, not the model.
- Tested live only against Ollama on this machine and from a container to the host. No other local runtime (llama.cpp server, LM Studio) was tried.
