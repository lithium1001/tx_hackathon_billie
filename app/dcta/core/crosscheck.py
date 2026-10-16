"""The validator's code side (DESIGN §4.7): re-derive the draft from the
customer's own words and the bank's records, without trusting what the draft
says about itself or what either model said.

crosscheck() returns the problems found; an empty list means consistent.
"""

from decimal import ROUND_HALF_UP, Decimal

from dcta.core.numbers import extract_amounts
from dcta.core.schema import ParsedIntent

CENT = Decimal("0.01")


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def crosscheck(parsed: ParsedIntent, group: dict, said: str, ctx: dict) -> list[str]:
    """`said` is every customer turn of the authorisation transcript; `ctx` the
    customer's accounts and bills."""
    said_n = _norm(said)
    amounts = extract_amounts(said)
    balances = {a["account_id"]: Decimal(a["balance"]) for a in ctx["accounts"]}
    bills = {b["bill_id"]: Decimal(b["amount_due"]) for b in ctx["bills"]}
    txns = group["transactions"]
    problems = []

    grounded = [p for p in parsed.transactions if _norm(p.evidence) in said_n]
    if len(txns) > len(grounded):
        problems.append(f"transactions: the draft has {len(txns)}, the customer asked "
                        f"for {len(grounded)}")

    for i, t in enumerate(txns):
        seq, amt, basis = t["seq"], Decimal(t["amount"]), t["basis"]
        p = parsed.transactions[i] if i < len(parsed.transactions) else None
        if p is None or p.intent != t["type"] or seq != i + 1:
            problems.append(f"{seq}.type: not an instruction the customer gave")
            continue
        for field, mention in (("target", p.target_mention), ("source", p.source_mention)):
            if mention and _norm(mention) not in said_n:
                problems.append(f"{seq}.{field}: '{mention}' was never said")

        if basis["kind"] == "absolute":
            ok = amt in amounts
        elif basis["kind"] == "remainder":
            less = [Decimal(x["amount"]) for x in txns[:i]
                    if x["debtor_account"] == t["debtor_account"]]
            bal = balances.get(t["debtor_account"])
            ok = (bal is not None and Decimal(basis["balance_at_draft"]) == bal
                  and [Decimal(x) for x in basis["less"]] == less and amt == bal - sum(less))
        elif basis["kind"] == "fraction":
            of = basis["of_seq"]
            ok = (0 < of < seq and amt == (Decimal(txns[of - 1]["amount"]) *
                                           Decimal(basis["ratio"])).quantize(CENT, ROUND_HALF_UP))
        else:                                          # bill_due
            ok = bills.get(basis.get("bill_id")) == amt
        if not ok:
            problems.append(f"{seq}.amount: {t['amount']} does not follow from what was said")
    return problems
