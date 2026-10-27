# Language model: Claude, with a free fallback on your own model server (Ollama)

> **Run the model on a server, not on the machine that runs RIPPLE.** A model on a laptop takes gigabytes of RAM. The local provider therefore has **no default URL**: it is off until you set `RIPPLE_OLLAMA_URL`, so RIPPLE never loads a model on its own machine by accident. This project uses a GPU server reached over the Fortinet VPN through an SSH tunnel (below).

RIPPLE runs fully without any model: deterministic rules do the understanding and every reply is a template. A model is optional polish. Two providers exist:

| Provider | Costs | Needs |
|---|---|---|
| **Claude** (`anthropic`) | money per call | `ANTHROPIC_API_KEY` in the secret store and `pip install anthropic` |
| **Ollama** (`ollama`) | free; prompts stay on infrastructure you control | an Ollama server you can reach, with one model pulled |

## Settings
| Variable | Meaning |
|---|---|
| `RIPPLE_LLM_PROVIDER` | `auto` (default): Claude if a key is configured, and the local model whenever Claude is missing, failing or over its call cap. `anthropic`: Claude only. `ollama`: local only, never calls Claude. Anything else: no model is called |
| `RIPPLE_LLM` | `auto` (default), `cache_only`, `off` |
| `RIPPLE_OLLAMA_URL` | **required to use a local model; no default.** Loopback only (an SSH tunnel, `http://127.0.0.1:11500`) unless the host is also named in `RIPPLE_OLLAMA_ALLOWED_HOSTS` |
| `RIPPLE_LOCAL_API` | `ollama` (default) or `openai`: an OpenAI-compatible server (llama.cpp `llama-server`, vLLM, LM Studio; `/v1/models`, `/v1/chat/completions`). Same URL setting, same safety rules, same role. The model is discovered from `/v1/models` (embedding and reranker models skipped) |
| `RIPPLE_OLLAMA_MODEL` | model name. Unset: the smallest installed completion model (embedding models and slow "thinking" models are skipped) |
| `RIPPLE_OLLAMA_TIMEOUT` | seconds per call, default 25 |
| `RIPPLE_OLLAMA_KEEP_ALIVE` | how long the server keeps the model in memory after a call, default `5m` (short, because the GPU server is shared). The app also loads it in the background at startup |
| `RIPPLE_LLM_BREAKER_S` | after a provider fails, skip it for this long (default 60 s), so a dead provider cannot slow every message |
| `RIPPLE_LLM_LOCAL=off` | disable the local provider (the test suite does this) |
| `RIPPLE_LLM_LOCAL_POLISH=on` | also let the local model reword replies (off by default: slow, cosmetic) |

## Using the GPU server (what was set up and verified)
Server: `saveenas@192.168.30.206` (20 cores, 125 GB RAM, RTX 2060 SUPER 8 GB and RTX 3060 12 GB, shared with other people's jobs; needs the Fortinet VPN; key-based SSH). Reached through an SSH tunnel, so the URL stays loopback-only and the traffic is encrypted.

**What it uses: the Qwen3.5-9B that is already running there** under the same account, as a llama.cpp `llama-server` (`--alias qwen35-9b-vision`, Q4_K_M, thinking disabled, 32k context, one slot) on server port 8009. It is an OpenAI-style API, hence `RIPPLE_LOCAL_API=openai`. It costs no extra GPU memory and needed no download.
```
# on the machine that runs RIPPLE (keep it open; ExitOnForwardFailure makes a busy port an error, not a silent skip)
ssh -N -L 127.0.0.1:11509:127.0.0.1:8009 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 saveenas@192.168.30.206
RIPPLE_LOCAL_API=openai
RIPPLE_OLLAMA_URL=http://127.0.0.1:11509         # then start RIPPLE; the Admin "Language model" card shows qwen35-9b-vision
```
Only masked intake text goes over it (never regulated identifiers, never attacks). **If the VPN, the tunnel or the server is down, the call fails, the breaker pauses it for a minute, and the rules answer**; nothing breaks.

Things to know about that server:
- The Qwen server has **one request slot** and belongs to another project on the same account (`shinrai`), so RIPPLE's short calls queue behind that project's traffic, and the reverse.
- It listens on `0.0.0.0` (port 8009, and a Gemma server on 8004) with no authentication that I could see, so anyone on the network that can reach the server can use it. That is the account owner's existing setup; bind it to `127.0.0.1` if that is not intended (the tunnel works either way).
- Ollama also exists on the server as a system service (`ollama.service`, disabled; its models are in a folder only the `ollama` user can read) and `qwen2.5:3b` (1.9 GB) was pulled into `~/.ollama` during testing; neither is used now.
- Nothing runs on the laptop, by request.

Using it elsewhere: point `RIPPLE_OLLAMA_URL` at any Ollama or OpenAI-compatible server (set `RIPPLE_LOCAL_API` to match). The Admin page has a **Language model** card (also `GET /api/admin/llm`, admin only, never shows a key) that says which provider is configured, which API dialect and model, whether it is reachable, and whether a provider is paused. In Docker, reach a tunnel on the host with `RIPPLE_OLLAMA_URL=http://host.docker.internal:11509` and `RIPPLE_OLLAMA_ALLOWED_HOSTS=host.docker.internal` (the container reached a host service this way when tested with Ollama).

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
Real runs on the 24 of the 120 labelled messages where the rules are unsure ([INTAKE_EVAL.md](INTAKE_EVAL.md); command `python -m app.intake.evaluate_local`; raw rows in `docs/intake_local_eval*.json`). "Helped" means the rules were wrong and the model right; "hurt" the reverse.

| | `qwen2.5vl:3b` (3B vision) | `qwen2.5:3b` (3B text) | **`Qwen3.5-9B`** (server, in use) |
|---|---|---|---|
| Helped | 2 before the payments-vocabulary gate, **0 after** | **0** | **2** |
| Hurt | 2 before the gate, **0 after** | **0** | **0** |
| Wrong suggestion, no worse than rules' "other" | 3 (two were attacks) | 2 | 1 (the attack "send the money to a different account", read as a settlement investigation) |
| No suggestion (said "other", or a non-suggestable intent) | 15 | 22 | 21 |
| Median time per call | 5.4 s (laptop CPU) | 6.4 s (laptop CPU); 0.96 s on the GPU server | **0.58 s** through the tunnel |

The vocabulary gate was chosen **after** seeing the first model's results and applied offline to those recorded outputs, so that column is not an independent measurement; the other two columns ran with the gate in place. The two messages Qwen3.5-9B fixed are "paisa phasa hua hai, bank me nahi aaya" (settlement issue) and "पेमेंट कब मिलेगा?" (settlement status): both are phrasings the rules miss and both 3B models got wrong.

**Plain reading: the 9B is the first model that adds anything, but only a little.** Both 3B models were safe and added nothing. Qwen3.5-9B fixed 2 of the 24 messages the rules were unsure about, broke none, and is fast on the server (about 0.6 s, so it adds no noticeable delay). It still misses the 2 ETA questions ("how many more days", "any update on timing"), which the payments-vocabulary gate would have discarded anyway, and it read one attack as a settlement question (harmless: investigation intents run read-only analysis and anything risky still needs an analyst). The slice is small (24 messages, about 8 of them recoverable), so "2 helped, 0 hurt" is encouraging, not proof; and the real accuracy lever is still fixing the rule gaps in INTAKE_EVAL.md, or Claude.

## Limits
- The local model's **reply rewording** is off by default and untested live.
- Hindi/Hinglish quality of small models is weak; the earlier language-detection slips ("english" for Hinglish) come from the rules, not the model.
- Tested live only against Ollama on this machine and from a container to the host. No other local runtime (llama.cpp server, LM Studio) was tried.
