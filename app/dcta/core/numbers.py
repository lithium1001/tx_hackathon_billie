"""Re-extract every amount a piece of text states, independently of the model.

Used twice: resolve checks an `absolute` amount the model reported is one the
customer actually said, and the validator's code side checks it again.

    extract_amounts("Transfer 500 dollars to mom")  -> {Decimal("500")}
    extract_amounts("move a grand, then 5k")        -> {Decimal("1000"), Decimal("5000")}
    extract_amounts("send fifty five to John")      -> {Decimal("55")}
"""

import re
from decimal import Decimal

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_THOUSAND = {"thousand", "grand", "k"}

_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|[a-z]+")


def extract_amounts(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    total = current = Decimal(0)
    active = False                       # inside a number right now

    def flush():
        nonlocal total, current, active
        if active:
            out.add(total + current)
        total = current = Decimal(0)
        active = False

    tokens = _TOKEN.findall((text or "").lower())
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok[0].isdigit():
            flush()
            current, active = Decimal(tok.replace(",", "")), True
        elif tok in _UNITS or tok in _TENS:
            # The slot this word fills is taken ("five six", "twenty thirty"):
            # that is a second number, not one.
            if active and current % 100 and (current % 10 or tok in _TENS):
                flush()
            current += _UNITS[tok] if tok in _UNITS else _TENS[tok]
            active = True
        elif tok == "a" and nxt in {"hundred", "thousand", "grand"}:
            flush()
            current, active = Decimal(1), True
        elif tok == "hundred" and active:
            current = (current or 1) * 100
        elif tok in _THOUSAND and active:
            total += (current or 1) * 1000
            current = Decimal(0)
        elif tok == "and" and active and (nxt in _UNITS or nxt in _TENS):
            continue                     # "five hundred and fifty"
        else:
            flush()
    flush()
    return out
