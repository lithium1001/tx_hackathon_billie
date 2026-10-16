"""Deterministic resolution (DESIGN §4.6): ParsedIntent + the customer's own
records → a TransactionGroup, or exactly one question to ask.

Pure code (P3). The model's ids (`target_hint`) are clues only: a hint can turn
into a question, never into a decision. Every number is computed here.

`ctx` is the customer's data as the gateway's read API returns it:
    {customer, accounts, payees, bills, instruments, activity}
`pins` holds clarify answers, keyed by slot ("0.target", "1.amount", …).
"""

import difflib
import re
from decimal import ROUND_HALF_UP, Decimal

from dcta.core import copy
from dcta.core.numbers import extract_amounts
from dcta.core.schema import ParsedIntent, TransactionGroup, group_hash

FUZZY_CUTOFF = 0.8     # difflib similarity for the edit-distance rung
NEAREST_N = 3          # suggestions offered when nothing matched
CENT = Decimal("0.01")

_STOP = {"my", "the", "a", "an", "to", "of", "for", "from", "in", "s",
         "account", "acct", "shares", "share", "stock", "stocks", "bill", "bills"}


class _Ask(Exception):
    def __init__(self, slot, question, options):
        self.slot, self.question, self.options = slot, question, options


class _Fail(Exception):
    def __init__(self, text):
        self.text = text


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP]


def _key(s: str) -> str:
    return " ".join(_words(s))


def _fmt(d: Decimal) -> str:
    return str(d.quantize(CENT))


# ── candidates: one shape for every kind of record ──────────────────────────
def _cand(id, name, aliases, label, detail, **rec):
    return {"id": id, "name": name, "aliases": [a for a in aliases if a],
            "label": label, "detail": detail, **rec}


def _accounts(ctx):
    return [_cand(a["account_id"], a["nickname"], [a["last4"], a.get("joint_holder", "")],
                  f"{a['nickname']} account", f"·· {a['last4']} · {copy.money(a['balance'])}",
                  kind="own_account", last4=a["last4"], balance=Decimal(a["balance"]))
            for a in ctx["accounts"]]


def _payees(ctx):
    return [_cand(p["payee_id"], p["name"], p.get("alias", "").split("|"), p["name"],
                  f"{p['bank']} ·· {p['account_last4']}",
                  kind="payee", bank=p["bank"], last4=p["account_last4"])
            for p in ctx["payees"]]


def _bills(ctx):
    return [_cand(b["bill_id"], b["biller"], [b["reference"]], b["biller"],
                  f"{copy.money(b['amount_due'])} due {b['due_date']}",
                  kind="biller", last4=b["reference"][-4:], amount_due=Decimal(b["amount_due"]))
            for b in ctx["bills"]]


def _instruments(ctx):
    return [_cand(i["symbol"], i["name"], [i["symbol"], *i.get("aliases", "").split("|")],
                  f"{i['name']} ({i['symbol']})", f"{i['market']} · {i['currency']}",
                  kind="instrument", market=i["market"], currency=i["currency"])
            for i in ctx["instruments"]]


# ── the matching ladder ─────────────────────────────────────────────────────
def _match(mention: str | None, cands: list[dict]) -> tuple[list[dict], bool]:
    """Exact → alias → shared word → edit distance; the first rung with any hit
    wins. Returns (hits, sure). An edit-distance hit is never "sure": "other" is
    0.91 similar to "mother", so it can only become a question."""
    m = _key(mention)
    if not m:
        return [], True
    words = set(m.split())

    def keys(c):
        return [_key(c["name"]), *map(_key, c["aliases"])]

    def shared(c):
        return len(words & {w for k in keys(c) for w in k.split()})

    for rung in (lambda c: m == _key(c["name"]) or m == c["id"].lower(),
                 lambda c: m in map(_key, c["aliases"])):
        hits = [c for c in cands if rung(c)]
        if hits:
            return hits, True
    # Shared words: only the records sharing the MOST words ("John Tan" is not
    # also TAN MEI LING).
    best = max((shared(c) for c in cands), default=0)
    if best:
        return [c for c in cands if shared(c) == best], True
    return [c for c in cands
            if any(difflib.SequenceMatcher(None, m, k).ratio() >= FUZZY_CUTOFF for k in keys(c))], False


def _option(c: dict) -> dict:
    return {"id": c["id"], "label": c["label"], "detail": c["detail"]}


def _pick(slot, mention, hint, cands, what, pins):
    """Apply the §4.6 table. Returns one candidate, or raises _Ask."""
    by_id = {c["id"]: c for c in cands}
    if pins.get(slot) in by_id:
        return by_id[pins[slot]]
    hits, sure = _match(mention, cands)
    hinted = by_id.get(hint or "")
    if len(hits) == 1 and sure and hinted in (None, hits[0]):
        return hits[0]
    if hits and not sure:                      # only a near spelling: confirm, don't assume
        options = hits + ([hinted] if hinted and hinted not in hits else [])
        raise _Ask(slot, copy.ask_confirm_hint(what, mention), [_option(c) for c in options])
    if hits:                                   # several, or the hint disagrees
        options = hits + ([hinted] if hinted and hinted not in hits else [])
        raise _Ask(slot, copy.ask_which(what, mention), [_option(c) for c in options])
    if hinted:
        raise _Ask(slot, copy.ask_confirm_hint(what, mention), [_option(hinted)])
    m = _key(mention)
    nearest = sorted(cands, key=lambda c: -max(
        difflib.SequenceMatcher(None, m, k).ratio() for k in [_key(c["name"]), *map(_key, c["aliases"])]))
    raise _Ask(slot, copy.ask_not_found(what, mention), [_option(c) for c in nearest[:NEAREST_N]])


# ── resolve ─────────────────────────────────────────────────────────────────
def resolve(parsed: ParsedIntent, said: str, ctx: dict, pins: dict | None = None,
            group_id: str = "g_draft", heard: list[str] = (), flags: list[str] = ()) -> dict:
    """{"status": "ok", "group", "group_hash"} · {"status": "ask", "slot",
    "question", "options"} · {"status": "fail", "text"}

    `heard`: the speech recogniser's alternative transcripts (voice only).
    `flags`: added to every payment (e.g. "shield_flag" from the input shield)."""
    pins = pins or {}
    try:
        group = _build(parsed, said, ctx, pins, group_id, heard, flags)
    except _Ask as q:
        return {"status": "ask", "slot": q.slot, "question": q.question, "options": q.options}
    except _Fail as f:
        return {"status": "fail", "text": f.text}
    return {"status": "ok", "group": group.model_dump(mode="json", exclude_none=True),
            "group_hash": group_hash(group)}


def _build(parsed, said, ctx, pins, group_id, heard, extra_flags) -> TransactionGroup:
    accounts = _accounts(ctx)
    default = ctx["customer"].get("default_account", "")
    paid_before = {h["creditor_id"] for h in ctx["activity"]}
    said_amounts = extract_amounts(said)
    heard_amounts = [extract_amounts(h) for h in heard]
    txns = []

    for i, t in enumerate(parsed.transactions):
        flags = list(extra_flags)
        # source account
        if t.source_mention or f"{i}.source" in pins:
            src = _pick(f"{i}.source", t.source_mention, None, accounts, "account", pins)
        elif default in {a["id"] for a in accounts}:
            src = next(a for a in accounts if a["id"] == default)
            flags.append("default_source")
        else:
            raise _Ask(f"{i}.source", copy.ASK_SOURCE, [_option(a) for a in accounts])

        # target
        if t.intent == "transfer":
            cands, what = _payees(ctx) + [a for a in accounts if a is not src], "recipient"
        elif t.intent == "bill_payment":
            cands, what = _bills(ctx), "bill"
        else:
            cands, what = _instruments(ctx), "security"
        tgt = _pick(f"{i}.target", t.target_mention, t.target_hint, cands, what, pins)

        amount, basis = _amount(i, t, tgt, src, txns, said_amounts, heard_amounts, pins)
        if amount <= 0:
            raise _Fail(copy.insufficient(src["name"]))

        txn = {"seq": i + 1, "type": t.intent, "debtor_account": src["id"],
               "amount": _fmt(amount), "currency": "SGD", "basis": basis,
               "end_to_end_id": f"{group_id}-{i + 1}"}
        if tgt["kind"] == "instrument":
            txn["instrument"] = {"symbol": tgt["id"], "name": tgt["name"],
                                 "market": tgt["market"], "currency": tgt["currency"]}
            if tgt["currency"] != "SGD":
                flags.append("fx_indicative")
        else:
            txn["creditor"] = {"kind": tgt["kind"], "id": tgt["id"], "name": tgt["name"],
                               "bank": tgt.get("bank"), "account_last4": tgt["last4"]}
            if tgt["kind"] == "payee" and tgt["id"] not in paid_before:
                flags.append("first_time_payee")
        txn["flags"] = flags
        txns.append(txn)

    return TransactionGroup.model_validate({"group_id": group_id, "customer_id":
                                            ctx["customer"]["customer_id"],
                                            "transactions": txns})


def _money_options(values) -> list[dict]:
    return [{"id": _fmt(v), "label": copy.money(_fmt(v)), "detail": ""} for v in sorted(values)]


def _amount(i, t, tgt, src, prior, said_amounts, heard_amounts, pins) -> tuple[Decimal, dict]:
    slot = f"{i}.amount"
    a = t.amount
    if slot in pins:
        return Decimal(pins[slot]), {"kind": "absolute"}
    if a is None:
        raise _Ask(slot, copy.ask_amount(tgt["label"]), [])

    if a.kind == "absolute":
        if a.currency and a.currency.upper() not in {"SGD", "S$", "$"}:
            raise _Fail(copy.SGD_ONLY)
        value = Decimal(a.value)
        if value not in said_amounts:          # the model reported a number nobody said
            raise _Ask(slot, copy.ask_amount(tgt["label"]), _money_options(said_amounts))
        # An alternative transcript without this number heard another one instead
        # ("fifty" / "fifteen"): ask which, rather than pick.
        misheard = {x for h in heard_amounts if value not in h for x in h - said_amounts}
        if misheard:
            options = _money_options({value} | misheard)
            raise _Ask(slot, copy.ask_heard([o["label"] for o in options]), options)
        return value, {"kind": "absolute"}

    if a.kind == "remainder":
        less = [Decimal(p["amount"]) for p in prior if p["debtor_account"] == src["id"]]
        return src["balance"] - sum(less), {"kind": "remainder", "balance_at_draft":
                                            _fmt(src["balance"]), "less": [_fmt(x) for x in less]}

    if a.kind == "fraction":
        if not 0 <= a.of < i:
            raise _Fail(copy.PARSE_FAILED)
        base = Decimal(prior[a.of]["amount"])
        return ((base * Decimal(a.ratio)).quantize(CENT, ROUND_HALF_UP),
                {"kind": "fraction", "of_seq": a.of + 1, "ratio": a.ratio})

    # bill_due
    if tgt["kind"] != "biller":
        raise _Ask(slot, copy.ask_amount(tgt["label"]), [])
    return tgt["amount_due"], {"kind": "bill_due", "bill_id": tgt["id"]}
