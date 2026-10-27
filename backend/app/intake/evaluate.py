"""Score the rule-based intake extractor against a hand-labelled message set.

    python -m app.intake.evaluate [--file tests/data/intake_labeled.jsonl] [--json out.json]

Labels are one author's judgement (the same author as the rules), written as natural messages including deliberately awkward ones
(misspellings, word order, two intents at once). Conventions: `i` is the primary thing the merchant wants a support agent to handle;
`wd` is the weekday of the merchant's deadline (or the only weekday mentioned); `p` is true when the merchant states a payment
they owe to someone else. This measures the rules only; it says nothing about a live model.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.intake.extract import extract_rules

DEFAULT = ROOT / "backend" / "tests" / "data" / "intake_labeled.jsonl"
FIELDS = ("intent", "language", "weekday", "amount", "promise")


def load(path: Path = DEFAULT) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def predict(text: str) -> dict[str, Any]:
    r = extract_rules(text)
    return {"intent": r.intent, "language": r.language, "weekday": r.weekday, "amount": r.amount, "promise": bool(r.promises)}


def evaluate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok: dict[str, int] = defaultdict(int)
    misses: list[dict[str, Any]] = []
    per_intent: dict[str, list[int]] = defaultdict(lambda: [0, 0])          # intent -> [correct, total]
    confusion: dict[str, int] = defaultdict(int)
    forbidden_missed = false_forbidden = 0
    for row in rows:
        want = {"intent": row["i"], "language": row["l"], "weekday": row["wd"], "amount": row["amt"], "promise": row["p"]}
        got = predict(row["t"])
        bad = {f: [want[f], got[f]] for f in FIELDS if want[f] != got[f]}
        for f in FIELDS:
            ok[f] += f not in bad
        per_intent[want["intent"]][1] += 1
        per_intent[want["intent"]][0] += "intent" not in bad
        if "intent" in bad:
            confusion[f"{want['intent']} -> {got['intent']}"] += 1
        forbidden_missed += want["intent"] == "forbidden_request" and got["intent"] != "forbidden_request"
        false_forbidden += want["intent"] != "forbidden_request" and got["intent"] == "forbidden_request"
        if bad:
            misses.append({"text": row["t"], **bad})
    n = len(rows)
    return {
        "n": n,
        "accuracy": {f: round(ok[f] / n, 3) for f in FIELDS},
        "all_fields_correct": round((n - len(misses)) / n, 3),
        "per_intent": {k: {"correct": v[0], "total": v[1]} for k, v in sorted(per_intent.items())},
        "forbidden_requests_missed": forbidden_missed,
        "benign_flagged_forbidden": false_forbidden,
        "confusions": dict(sorted(confusion.items(), key=lambda kv: -kv[1])),
        "misses": misses,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=DEFAULT)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    res = evaluate(load(a.file))
    print(f"{res['n']} labelled messages")
    for f, v in res["accuracy"].items():
        print(f"  {f:9s} {v:.1%}")
    print(f"  every field right: {res['all_fields_correct']:.1%}")
    print(f"  forbidden requests missed: {res['forbidden_requests_missed']}   benign flagged forbidden: {res['benign_flagged_forbidden']}")
    for k, v in res["per_intent"].items():
        print(f"  {k:20s} {v['correct']}/{v['total']}")
    print("misses:")
    for m in res["misses"]:
        print("  -", json.dumps(m, ensure_ascii=False))
    if a.json:
        a.json.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
