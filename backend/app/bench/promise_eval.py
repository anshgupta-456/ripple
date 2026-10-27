"""Small hand-labeled promise-extraction set (written before looking at extractor output)."""
from __future__ import annotations

from app.intake.extract import extract_rules

# (text, has_supplier_promise, weekday or None)
LABELED: list[tuple[str, bool, str | None]] = [
    ("mera settlement kyun ruka hai? Tuesday ko supplier ko paisa dena hai.", True, "tuesday"),
    ("Why is my settlement on hold? I have to pay my supplier on Tuesday.", True, "tuesday"),
    ("मेरा सेटलमेंट क्यों रुका है? मंगलवार को सप्लायर को पैसा देना है।", True, "tuesday"),
    ("settlement ruka hua hai, Tuesday ko supplier ka payment hai, kya karun?", True, "tuesday"),
    ("settlement ka status batao, supplier payment Friday ko hai", True, "friday"),
    ("Need to pay my vendor on Monday, settlement stuck", True, "monday"),
    ("shukravar ko supplier ko Rs 40,000 dena hai, settlement kab aayega", True, "friday"),
    ("budhvar ko vendor payment hai, hold hata do please", True, "wednesday"),
    ("Supplier payment due Thursday, where is my payout?", True, "thursday"),
    ("guruvar ko supplier ka payment dena hai", True, "thursday"),
    ("saturday ko supplier ko pay karna hai, settlement atka hai", True, "saturday"),
    ("sunday tak supplier ko dena hai paisa", True, "sunday"),
    ("mera settlement kab aayega?", False, None),
    ("Where is my settlement?", False, None),
    ("what is my KYC status", False, None),
    ("my refund has not arrived", False, None),
    ("kitna time lagega?", False, None),
    ("aaj ka settlement abhi tak nahi aaya", False, None),
    ("mera refund kab aayega?", False, None),
    ("hello", False, None),
]


def evaluate() -> dict:
    ok_promise = ok_day = n_day = 0
    wrong = []
    for text, has, day in LABELED:
        r = extract_rules(text)
        got = bool(r.promises)
        ok_promise += got == has
        if has:
            n_day += 1
            ok_day += r.weekday == day
        if got != has or (has and r.weekday != day):
            wrong.append(text)
    n = len(LABELED)
    return {"n": n, "promise_detection_accuracy": round(ok_promise / n, 3), "weekday_accuracy": round(ok_day / n_day, 3),
            "n_with_promise": n_day, "misses": wrong, "extractor": "rules (LLM off for benchmark)"}
