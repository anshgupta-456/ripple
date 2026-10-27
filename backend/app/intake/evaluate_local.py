"""Measure the local-model fallback on the messages it is actually allowed to touch (rules unsure).

    python -m app.intake.evaluate_local [--limit N] [--json out.json]     (needs `ollama serve` and a pulled model)

For each labelled message where the rules say "other" or are below the act-on threshold, ask the real local model through the same
code path the app uses, and count: helped (rules wrong, local right), hurt (rules right, local suggests something else), no change.
Slow by design: a laptop CPU takes seconds per call.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from app.intake.evaluate import load
from app.intake.extract import extract, extract_rules
from app.llm.client import LLM, llm_status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    os.environ["RIPPLE_LLM_PROVIDER"] = "ollama"
    os.environ.setdefault("RIPPLE_OLLAMA_TIMEOUT", "90")
    st = llm_status()["ollama"]
    print("local model:", st["model"], "reachable:", st["reachable"])
    if not st["reachable"]:
        raise SystemExit("Ollama is not reachable")
    llm = LLM(cache_path=Path(tempfile.mkdtemp()) / "c.json")
    rows = [r for r in load() if (lambda x: x.intent == "other" or x.confidence < 0.7)(extract_rules(r["t"]))]
    if a.limit:
        rows = rows[: a.limit]
    out, tally, times = [], {"helped": 0, "hurt": 0, "same_right": 0, "same_wrong": 0, "unusable": 0}, []
    for r in rows:
        rules = extract_rules(r["t"])
        t0 = time.time()
        res = extract(r["t"], llm)
        dt = time.time() - t0
        times.append(dt)
        used = res.source == "llm_local"
        rules_ok, final_ok = rules.intent == r["i"], res.intent == r["i"]
        k = ("unusable" if not used else "helped" if final_ok and not rules_ok else "hurt" if rules_ok and not final_ok
             else "same_right" if final_ok else "same_wrong")
        tally[k] += 1
        out.append({"text": r["t"], "label": r["i"], "rules": rules.intent, "final": res.intent, "source": res.source, "conf": res.confidence, "s": round(dt, 1), "result": k})
        print(f"{dt:5.1f}s {k:10s} label={r['i']:18s} rules={rules.intent:18s} final={res.intent:18s} | {r['t'][:60]}", flush=True)
    summary = {"n": len(rows), "model": st["model"], **tally, "median_seconds": sorted(times)[len(times) // 2] if times else None}
    print(json.dumps(summary))
    if a.json:
        a.json.write_text(json.dumps({"summary": summary, "rows": out}, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
