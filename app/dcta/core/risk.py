"""KYC and risk rules (DESIGN §5.4).

evaluate() is a pure function. The agent's precheck and the gateway's
authoritative decision call the same one; only the gateway's result counts.
Constants live here and nowhere else; test_risk pins them, so changing a limit
means changing that test too.

`ctx` is a snapshot, in the gateway read API's shapes:
    {customer, limits, accounts, activity, screening, now}
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

SGT = timezone(timedelta(hours=8))

DEFAULT_DAILY_LIMIT = Decimal("1000.00")   # third-party transfers per SGT calendar day
STEP_UP_AMOUNT = Decimal("5000.00")        # one payment at or above this needs step-up
VELOCITY_WINDOW = timedelta(minutes=10)
VELOCITY_STEP_UP = 3                       # payments already made inside the window
VELOCITY_DENY = 5
DRAIN_MIN_BALANCE = Decimal("50000.00")
DRAIN_SHARE = Decimal("0.5")               # more than half the balance out within…
DRAIN_WINDOW = timedelta(hours=24)

_RANK = {"allow": 0, "step_up": 1, "deny": 2}


def _own(t: dict) -> bool:
    return (t.get("creditor") or {}).get("kind") == "own_account"


def daily_limit(limits: dict | None) -> Decimal:
    return Decimal((limits or {}).get("daily_transfer_limit") or DEFAULT_DAILY_LIMIT)


def sent_today(activity: list[dict], own_ids: set[str], now: datetime) -> Decimal:
    """Third-party transfers already made on `now`'s SGT calendar day."""
    today = now.astimezone(SGT).date()
    return sum((Decimal(h["amount"]) for h in activity if h["type"] == "transfer"
                and h["creditor_id"] not in own_ids
                and datetime.fromisoformat(h["ts"]).astimezone(SGT).date() == today), Decimal(0))


def evaluate(group: dict, ctx: dict) -> dict:
    """{"disposition": allow | step_up | deny, "rules": [{rule, result, detail}]}
    — the strictest rule that fired decides."""
    now: datetime = ctx["now"]
    own_ids = {a["account_id"] for a in ctx["accounts"]}
    balances = {a["account_id"]: Decimal(a["balance"]) for a in ctx["accounts"]}
    history = [{**h, "at": datetime.fromisoformat(h["ts"]), "amt": Decimal(h["amount"])}
               for h in ctx["activity"]]
    txns = group["transactions"]
    third_party = [t for t in txns if t["type"] == "transfer" and not _own(t)]
    rules = []

    def hit(rule, result, detail):
        rules.append({"rule": rule, "result": result, "detail": detail})

    kyc = ctx["customer"].get("kyc_status", "")
    if kyc != "verified":
        hit("KYC_STATUS", "deny", f"identity verification status is '{kyc}'")

    screened = {s["name"].strip().upper() for s in ctx["screening"]}
    for t in txns:
        name = (t.get("creditor") or {}).get("name", "")
        if name.strip().upper() in screened:
            hit("SCREENING", "deny", f"payment {t['seq']}: recipient is on a screening list")

    paid_before = {h["creditor_id"] for h in history}
    for t in third_party:
        if t["creditor"]["id"] not in paid_before:
            hit("FIRST_TIME_PAYEE", "step_up", f"payment {t['seq']}: first payment to this payee")

    if third_party:
        limit = daily_limit(ctx.get("limits"))
        total = sent_today(ctx["activity"], own_ids, now) + sum(Decimal(t["amount"])
                                                                for t in third_party)
        if total > limit:
            hit("DAILY_LIMIT", "deny", f"S${total:,.2f} to others today exceeds the daily "
                                       f"limit of S${limit:,.2f}")

    for t in txns:
        if not _own(t) and Decimal(t["amount"]) >= STEP_UP_AMOUNT:
            hit("STEP_UP_AMOUNT", "step_up", f"payment {t['seq']} is S${STEP_UP_AMOUNT:,.0f} or more")

    recent = sorted(h["at"] for h in history if now - h["at"] <= VELOCITY_WINDOW)
    if len(recent) >= VELOCITY_DENY:
        retry = (recent[-VELOCITY_DENY] + VELOCITY_WINDOW).astimezone(SGT)
        hit("VELOCITY", "deny", f"{len(recent)} payments in 10 minutes; retry after "
                                f"{retry:%H:%M}")
    elif len(recent) >= VELOCITY_STEP_UP:
        hit("VELOCITY", "step_up", f"{len(recent)} payments in the last 10 minutes")

    for acct in dict.fromkeys(t["debtor_account"] for t in txns):
        balance = balances.get(acct, Decimal(0))
        if balance < DRAIN_MIN_BALANCE:
            continue
        out = sum((Decimal(t["amount"]) for t in txns if t["debtor_account"] == acct
                   and not _own(t) and t["type"] != "bill_payment"), Decimal(0))
        out += sum((h["amt"] for h in history if h["debtor_account"] == acct
                    and h["creditor_id"] not in own_ids and h["type"] != "bill_payment"
                    and now - h["at"] <= DRAIN_WINDOW), Decimal(0))
        if out > balance * DRAIN_SHARE:
            hit("DRAIN", "step_up", f"more than half of the S${balance:,.2f} balance would "
                                    f"leave the account within 24 hours")

    if any("shield_flag" in t.get("flags", []) for t in txns):
        hit("SHIELD_FLAG", "step_up", "this instruction was flagged by the input shield")

    disposition = max((r["result"] for r in rules), key=_RANK.get, default="allow")
    return {"disposition": disposition, "rules": rules}
