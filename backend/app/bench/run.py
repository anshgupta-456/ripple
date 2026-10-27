"""CLI: python -m app.bench.run [--runs N] [--out path] [--label name] [--scored]

Writes a versioned results file: records, summary, promise-extraction eval, adversarial suite, and run metadata
(config hash, policy hash, git commit, dirty flag) so any number on the scoreboard can be traced to a state of the repo.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from app.bench.adversarial import run_suite
from app.bench.metrics import summarize
from app.bench.promise_eval import evaluate
from app.bench.runner import SYSTEM_KEYS, SYSTEM_LABELS, run_benchmark, seeds_for
from app.bench.templates import REGISTRY
from app.config import CONFIG_PATH, ROOT, load_config
from app.firewall.policy import POLICY_PATH


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def build_results(runs: int | None, label: str, scored: bool) -> dict:
    t0 = time.time()
    cfg = load_config()
    n = runs or cfg["benchmark"]["runs_per_family"]
    recs = run_benchmark(runs_per_family=n)
    adv = run_suite()
    return {
        "meta": {
            "label": label, "scored": scored, "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config_version": cfg["version"], "config_hash": _sha(CONFIG_PATH), "policy_hash": _sha(POLICY_PATH),
            "git_commit": _git("rev-parse", "HEAD")[:12], "git_dirty": bool(_git("status", "--porcelain")),
            "git_tags_at_head": _git("tag", "--points-at", "HEAD").split(),
            "families": list(REGISTRY), "held_out_families": cfg["benchmark"]["held_out_families"],
            "seeds": seeds_for(n), "n_scenarios": len(REGISTRY) * n, "n_runs": len(recs),
            "systems": {k: SYSTEM_LABELS[k] for k in SYSTEM_KEYS}, "llm": "off (deterministic rules)",
            "synthetic_data": True, "runtime_s": round(time.time() - t0, 1),
            "notes": ["System C is a scripted proxy of a tool-calling LLM agent, not a live model.",
                      "Cost, time and harm values are modeled assumptions from config/frozen.yaml.",
                      "Missed obligations are only scored on cases a system could resolve (must_close_verified)."],
        },
        "summary": summarize(recs),
        "promise_eval": evaluate(),
        "adversarial": {k: v for k, v in adv.items() if k not in ("rows",)},
        "records": [r.model_dump(mode="json") for r in recs],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--label", default="dev")
    ap.add_argument("--out", default=None)
    ap.add_argument("--scored", action="store_true")
    a = ap.parse_args()
    res = build_results(a.runs, a.label, a.scored)
    out = Path(a.out) if a.out else ROOT / "data" / "results" / f"bench_{a.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out}  runs={res['meta']['n_runs']}  {res['meta']['runtime_s']}s")
    for k, v in res["summary"]["all"].items():
        print(f"{k} {v['label'][:34]:34} auto={v['autonomous_resolution_rate']['rate']} verified={v['verified_resolution_rate']['rate']} "
              f"unsafe={v['unsafe_actions']['total']} falseRes={v['false_resolved']['k']} "
              f"escP={v['escalation_precision']['rate']} escR={v['escalation_recall']['rate']}")


if __name__ == "__main__":
    main()
