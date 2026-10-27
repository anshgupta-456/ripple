"""Content guards.

`screen_untrusted_text` flags prompt-injection / fake-authority phrasing in merchant or
third-party text (for the audit trail; flags never grant anything). `outbound_text_violations`
blocks outbound message text that smuggles money-movement details or links.
"""
from __future__ import annotations

import re

INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_rules": re.compile(r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(rules?|instructions?|polic(y|ies)|guardrails?|restrictions?|safety)\b", re.I),
    "release_funds": re.compile(r"\b(release|unfreeze|unlock|lift|remove)\b.{0,25}\b(funds?|hold|settlement|paisa|money)\b", re.I),
    "fake_authority": re.compile(r"\b(i am|this is|main hoon|i'm)\b.{0,20}\b(manager|admin|ceo|director|risk head|owner|supervisor|rbi|paytm (team|official))\b", re.I),
    "claims_approval": re.compile(r"\b(already )?(approved|authori[sz]ed|sanctioned)\b.{0,30}\b(by|from)\b.{0,20}\b(manager|admin|risk|head|ceo)\b", re.I),
    "system_prompt": re.compile(r"\b(system prompt|developer message|you are now|new instructions?|act as)\b", re.I),
    "bank_change": re.compile(r"\b(change|update|replace)\b.{0,20}\b(bank|account|ifsc|upi)\b", re.I),
    "hinglish_release": re.compile(r"\b(hold|settlement|paisa)\b.{0,20}\b(hata|release|chhod|nikal)\w*", re.I),
    "skip_checks": re.compile(r"\b(skip|without|no need for)\b.{0,15}\b(approval|verification|check|review)\b", re.I),
}


def screen_untrusted_text(text: str) -> list[str]:
    return sorted(name for name, pat in INJECTION_PATTERNS.items() if pat.search(text))


_OUTBOUND = {
    "url": re.compile(r"(https?://|www\.)", re.I),
    "account_number": re.compile(r"\b\d{9,18}\b"),
    "ifsc": re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"),
    "upi_id": re.compile(r"\b[\w.\-]{2,}@[a-z]{2,}\b", re.I),
    "control_chars": re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]"),
}


def outbound_text_violations(text: str) -> list[str]:
    return sorted(name for name, pat in _OUTBOUND.items() if pat.search(text))
