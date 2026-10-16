"""Input shield (DESIGN §4.5). Deterministic, pure code.

clean()            NFKC; drop invisible / bidi / control characters; strip chat-
                   template role markers and our own slot tags; collapse
                   whitespace; cap the length.
scan()             three rule groups — instruction override and claimed
                   authority / skipping confirmation BLOCK; prompt extraction and
                   weak pressure signals FLAG (the payment then needs step-up).
neutralise_field() for free-text bank data (payee notes, bill memos) shown to a
                   model: a field that reads like an instruction is withheld.

This layer cannot catch everything. What guarantees no unauthorised payment is
the confirm screen plus the gateway's signature check; this only narrows the
funnel, and the eval reports the two separately.
"""

import re
import unicodedata

MAX_INPUT_CHARS = 500
FIELD_MAX_CHARS = 120
WITHHELD = "[withheld: looked like an instruction]"

# Zero-width, directional marks / embeddings / isolates, word joiners, BOM, soft hyphen.
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_ROLE_LINE = re.compile(r"(?im)^\s*(system|assistant|user|developer|human|ai)\s*:\s*")
_TEMPLATE = re.compile(
    r"<\|[^|>]{0,40}\|>|\[/?INST\]|<</?SYS>>|"
    r"</?\s*(system|assistant|user|developer|customer_utterance|bank_question|"
    r"customer_context|authorisation_transcript|draft)\b[^>]*>", re.I)

RULES = {
    "override": [
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|"
        r"your|the|any)\b.{0,20}\b(instructions?|rules?|prompts?|guidelines|directions)\b",
        r"\byou are now\b",
        r"\bnew (system )?instructions?\b",
        r"\bfrom now on,? you\b",
        r"\bact as (an? )?(admin|administrator|developer|system|root)\b",
    ],
    "authority": [
        r"\b(i am|i'm|this is) (the |a |an )?(bank'?s? )?(admin|administrator|manager|officer|"
        r"staff|employee|developer|security team|compliance)\b",
        r"\b(skip|bypass|disable|without|no need for|don'?t need|do not need|no)\b.{0,15}"
        r"\b(confirmation|confirming|verification|verify|otp|2fa|signature|approval)\b",
        r"\b(pre-?approved|already approved|authori[sz]ed by the bank)\b",
        r"免确认|跳过确认|不用确认|无需确认|我是(银行)?(管理员|经理|工作人员)",
    ],
    "exfiltration": [
        r"\b(reveal|show|print|repeat|output|tell me|what (is|are))\b.{0,20}"
        r"\b(system prompt|your (instructions|prompt|rules)|hidden prompt)\b",
    ],
    "pressure": [
        r"\burgent(ly)?\b.{0,20}\b(now|immediately)\b",
        r"\bdon'?t (tell|ask|check)\b",
        r"\btrust me\b",
    ],
}
BLOCKING = ("override", "authority")
_COMPILED = {g: [re.compile(p, re.I) for p in ps] for g, ps in RULES.items()}


def clean(text: str, limit: int = MAX_INPUT_CHARS) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text)
    text = _TEMPLATE.sub(" ", _ROLE_LINE.sub("", text))
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)
    return " ".join(text.split())[:limit]


def scan(text: str) -> dict:
    """{"verdict": "pass" | "flag" | "block", "reasons": [rule groups that matched]}"""
    reasons = [g for g, pats in _COMPILED.items() if any(p.search(text) for p in pats)]
    verdict = ("block" if any(g in BLOCKING for g in reasons)
               else "flag" if reasons else "pass")
    return {"verdict": verdict, "reasons": reasons}


def neutralise_field(text: str) -> str:
    """Make a free-text data field (payee note, bill memo) safe to show a model."""
    cleaned = clean(text, limit=10 * FIELD_MAX_CHARS)
    if scan(cleaned)["verdict"] != "pass":
        return WITHHELD
    return cleaned if len(cleaned) <= FIELD_MAX_CHARS else cleaned[:FIELD_MAX_CHARS] + "…"
