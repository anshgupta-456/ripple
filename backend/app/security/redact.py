"""PII / sensitive-data redaction and data classification.

Classes: public < internal < confidential < regulated. Regulated data (bank/card/government identifiers) is never sent
to a model and never written to logs. Confidential data (merchant text, amounts) may go to the model only after
masking. Redaction runs before any prompt or log write.
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Any


class DataClass(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    REGULATED = 3


# Order matters: most specific first. Each match is replaced by a placeholder, so later (more generic) patterns can
# never re-match the same characters, and classification reflects what each span really is.
_PATTERNS: list[tuple[str, re.Pattern[str], DataClass]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), DataClass.CONFIDENTIAL),
    ("UPI", re.compile(r"\b[\w.\-]{2,}@[a-z]{2,}\b", re.I), DataClass.REGULATED),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), DataClass.REGULATED),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), DataClass.REGULATED),
    ("CARD", re.compile(r"(?<![\d,])(?:\d[ -]?){13,19}(?![\d,])"), DataClass.REGULATED),
    ("AADHAAR", re.compile(r"(?<![\d,])\d{4}[ -]?\d{4}[ -]?\d{4}(?![\d,])"), DataClass.REGULATED),
    ("PHONE", re.compile(r"(?<![\d,])(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\d,])"), DataClass.CONFIDENTIAL),
    ("ACCOUNT", re.compile(r"(?<![\d,])\d{9,18}(?![\d,])"), DataClass.REGULATED),
]


def _scan(text: str) -> tuple[str, DataClass]:
    worst = DataClass.CONFIDENTIAL            # merchant text is at least confidential
    for label, pat, cls in _PATTERNS:
        text, n = pat.subn(f"[{label}]", text)
        if n and cls > worst:
            worst = cls
    return text, worst


def classify_text(text: str) -> DataClass:
    """Highest sensitivity class found in the text."""
    return _scan(text)[1]


def redact(text: str) -> str:
    return _scan(text)[0]


def redact_obj(obj: Any) -> Any:
    """Deep-redact strings inside dicts/lists/tuples. Non-strings pass through."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def llm_safe_text(text: str) -> str | None:
    """Gate for anything leaving for a model: masked text, or None when it contains regulated identifiers
    (the caller then falls back to deterministic rules and sends nothing)."""
    if classify_text(text) >= DataClass.REGULATED:
        return None
    return redact(text)
