import json

import pytest

from app.agent.evidence import compile_bundle
from app.agent.memo import build_memo
from app.agent.reply import LANGS, T, compose, fmt_dt, inr, notice_line, polish, validate_reply
from app.analysis import analyze_world
from app.bench.templates import CANONICAL_SEED, generate
from app.intake.extract import extract_rules
from app.llm.client import LLM
from app.mock_sources.world import World
from app.timeline.normalizer import collect

MSG = "mera settlement kyun ruka hai? Tuesday ko supplier ko paisa dena hai."


def flagship_tl():
    w = World(generate("missing_evidence_flagship", CANONICAL_SEED, canonical=True).world_spec)
    tl = collect(lambda t: w.call(t), set(w.tools), w.now, w.spec.merchant_id, w.events,
                 {"supplier_paid_from_settlement": True})
    return w, tl


def test_every_template_exists_in_all_languages_and_formats():
    ctx = dict(amount="Rs 72,000", held="Rs 72,000", eta="Tue 03 Nov, 12:00", eta_h=27, extra="", notice_line="",
               due_amt="Rs 60,000", due_day="Tue 03 Nov", supplier="Anand Wholesale", text="Late by a day.",
               kyc_status="expiring", kyc_exp=" (Wed)", kyc_extra="", status_word="processing", eta_part=", soon",
               fee="Rs 1,440", kind_word="short-term cover", debtor="Verma Traders")
    for kind, langs in T.items():
        assert set(langs) == set(LANGS), kind
        for lang in LANGS:
            assert compose(kind, lang, **ctx).text, (kind, lang)


def test_formatting_helpers():
    assert inr(72000) == "Rs 72,000"
    assert fmt_dt("2026-11-03T12:00:00+05:30") == "Tue 03 Nov, 12:00"


def test_reply_validation_catches_promises_and_unproven_claims():
    assert validate_reply("Your Rs 72,000 will be released, guaranteed", ["Rs 72,000"], False)
    assert validate_reply("Your money has been released", [], False)
    assert not validate_reply("Your money has been released", [], True)
    assert validate_reply("Pay to 123456789012", [], False)
    assert validate_reply("hello", ["Rs 72,000"], False)


def test_hold_reply_is_honest_in_all_languages():
    nl = notice_line("english", 60000, "Tue 03 Nov", "Anand Wholesale")
    for lang in LANGS:
        d = compose("hold_explained", lang, held="Rs 72,000", notice_line=nl)
        assert "Rs 72,000" in d.text
        assert not validate_reply(d.text, d.must_include, source_released=False), lang


def test_polish_accepts_only_validated_rewrites(tmp_path):
    d = compose("status_processing", "english", amount="Rs 9,000", eta="Tue 03 Nov, 12:00")

    def client(text):
        class R: content = [type("B", (), {"text": json.dumps({"text": text})})()]
        class C:
            class messages:
                @staticmethod
                def create(**kw): return R()
        return C()

    good = "Good news! Your Rs 9,000 settlement is on its way and should land around Tue 03 Nov, 12:00."
    p = polish(d, LLM(mode="auto", cache_path=tmp_path / "a.json", client=client(good), model="m"), False)
    assert p.polished and p.text == good
    for bad in ("Your settlement is guaranteed by tomorrow.", "Pay at http://x.example", "Your money is on its way."):
        p = polish(d, LLM(mode="auto", cache_path=tmp_path / f"{abs(hash(bad))}.json", client=client(bad), model="m"), False)
        assert not p.polished and p.text == d.text
    assert polish(d, None, False) is d


def test_evidence_bundle_compiled_only_when_evidence_is_missing():
    w, tl = flagship_tl()
    b = compile_bundle(tl)
    assert b.complete and set(b.docs) == {"invoice", "delivery_proof", "customer_comm"}
    partial = compile_bundle(tl, available_docs={"invoice"})
    assert not partial.complete and "delivery_proof" in partial.missing
    w.call("submit_dispute_evidence", {"dispute_id": tl.val("dispute.id"), "docs": b.docs}, "k")
    _, tl2 = flagship_tl()
    w2 = World(generate("status_query", 1).world_spec)
    tl3 = collect(lambda t: w2.call(t), set(w2.tools), w2.now, w2.spec.merchant_id, [])
    assert compile_bundle(tl3) is None


def test_memo_carries_facts_trace_options_and_time_saved():
    w, tl = flagship_tl()
    rep = analyze_world(w, 5120)
    memo = build_memo(extract_rules(MSG), MSG, tl, rep, compile_bundle(tl), [{"type": "submit_dispute_evidence", "tier": 3}])
    assert memo.facts and all(f.source_ref for f in memo.facts)
    assert memo.cause_path[-1].startswith("evidence_gap:")
    assert any(e["src"].startswith("kyc:") for e in memo.rejected_edges)
    assert sum(o.chosen for o in memo.options) == 1 and memo.minutes_saved > 0
    assert memo.if_nothing_p_miss > 0.7 and "approval" in memo.disposition
    assert "Rs 60,000" in " ".join(f.value for f in memo.facts)
