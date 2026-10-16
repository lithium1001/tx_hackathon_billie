"""Every sentence the customer reads (P5). The model never writes customer copy:
a node picks a template here and fills it with structured fields."""

from decimal import Decimal

REPLIES = {
    "inquiry": ("Your accounts and balances are shown at the top of the screen. "
                "I can move money for you, e.g. \"Pay my credit card bill\"."),
    "advice": ("I can't recommend investments. If you tell me which shares and how "
               "much, I'll prepare the order for you to review."),
    "smalltalk": ("Hi! Tell me what you'd like to do, e.g. \"Transfer 500 dollars to "
                  "my mom\"."),
    "out_of_scope": "Sorry, I can only help with transfers, bill payments and buying shares.",
}

PARSE_FAILED = ("Sorry, I couldn't make sense of that instruction. Nothing has been "
                "prepared and no money has moved. Please rephrase it, or give one "
                "instruction at a time.")

NO_MONEY_MOVED = "No money has moved."


def money(amount, currency: str = "SGD") -> str:
    return f"{'S$' if currency == 'SGD' else currency + ' '}{Decimal(amount):,.2f}"


# ── resolve: questions and refusals ─────────────────────────────────────────
def ask_which(what: str, mention: str | None) -> str:
    return f'Which {what} do you mean by "{mention}"?' if mention else f"Which {what} do you mean?"


def ask_confirm_hint(what: str, mention: str | None) -> str:
    return f'I couldn\'t find "{mention}" exactly. Did you mean this {what}?'


def ask_not_found(what: str, mention: str | None) -> str:
    return f'I couldn\'t find "{mention}" among your {what}s. Is it one of these?'


ASK_SOURCE = "Which account should this come from?"


def ask_heard(amounts: list[str]) -> str:
    return f"Sorry, did you say {' or '.join(amounts)}?"


def ask_amount(target: str) -> str:
    return f"How much for {target}?"


def insufficient(account: str) -> str:
    return (f"Your {account} account doesn't have enough for these payments as "
            f"described, so I haven't prepared a draft. {NO_MONEY_MOVED}")


SGD_ONLY = f"I can only make payments in Singapore dollars. {NO_MONEY_MOVED}"


# ── clarify ─────────────────────────────────────────────────────────────────
CLARIFY_GAVE_UP = (f"I still couldn't pin down the details, so I've stopped here. "
                   f"{NO_MONEY_MOVED} Please try again with the full instruction.")


# ── validator / risk ────────────────────────────────────────────────────────
def frozen(fields: list[str]) -> str:
    return (f"I stopped before showing you a draft, because it didn't match what you "
            f"said ({', '.join(fields)}). {NO_MONEY_MOVED} Please try again.")


def denied(rules: list[dict]) -> str:
    reasons = "; ".join(r["detail"] for r in rules if r["result"] == "deny")
    return f"I can't make this payment: {reasons}. {NO_MONEY_MOVED}"


def _target(t: dict) -> str:
    if "instrument" in t:
        return f"buy {t['instrument']['name']} ({t['instrument']['symbol']})"
    c = t["creditor"]
    return {"payee": f"to {c['name']}", "own_account": f"to your {c['name']} account",
            "biller": f"pay {c['name']}"}[c["kind"]]


def _lines(group: dict) -> list[str]:
    return [f"{t['seq']}. {money(t['amount'])} {_target(t)}" for t in group["transactions"]]


# ── confirm / settle ────────────────────────────────────────────────────────
CANCELLED = f"Cancelled. {NO_MONEY_MOVED}"

SETTLE_MISMATCH = ("I couldn't confirm this payment with the bank, so please check "
                   "your account activity before trying again.")

# What the gateway's refusal codes mean to a customer.
GATEWAY_CODES = {
    "SIGNATURE_MISSING": "the payment wasn't signed on your device",
    "SIGNATURE_INVALID": "the signature from your device didn't check out",
    "PAYLOAD_TAMPERED": "the payment details changed after you approved them",
    "NONCE_REPLAYED": "this approval had already been used",
    "CHALLENGE_EXPIRED": "the approval timed out",
    "CHALLENGE_UNKNOWN": "the bank didn't recognise this approval",
    "KEY_UNKNOWN": "this device isn't registered for your account",
    "NOT_OWNED": "one of the accounts or payees isn't yours",
    "RISK_DENIED": "the bank's safety checks declined it",
    "STEP_UP_REQUIRED": "the extra verification code was missing or wrong",
    "INSUFFICIENT_FUNDS": "there isn't enough money in the account",
}


def gateway_refused(code: str) -> str:
    why = GATEWAY_CODES.get(code, "the bank declined it")
    return f"The payment didn't go through: {why}. {NO_MONEY_MOVED}"


def receipt(group: dict, rec: dict) -> str:
    return ("Done. The bank has made these payments:\n" + "\n".join(_lines(group)) +
            f"\nReceipt {rec['receipt_id']}.")


BLOCKED = ("I can't act on that message: it contains instructions aimed at the system "
           f"rather than a payment request. {NO_MONEY_MOVED} Please send just the payment "
           "you want to make — you will always review and approve it yourself.")

NO_CUSTOMER = "Please sign in first."


# ── sign-in (mock) ──────────────────────────────────────────────────────────
def sms_new_device(code: str) -> str:
    return (f"Bank: {code} is your code to confirm sign-in on a new device. "
            f"It expires in 5 minutes. Never share it - the bank will never ask for it.")


# ── review screen edits ─────────────────────────────────────────────────────
def describe_target(t: dict) -> str:
    """'the transfer to TAN MEI LING' — names only, no symbols or numbers."""
    if "instrument" in t:
        return f"the {t['instrument']['name']} purchase"
    c = t["creditor"]
    return {"payee": f"the transfer to {c['name']}",
            "own_account": f"the transfer to my {c['name']} account",
            "biller": f"the {c['name']} bill"}[c["kind"]]


def review_source_change(target: str, account: str) -> str:
    """The transcript line for a source account chosen on the review screen.
    No digits on purpose: the transcript is re-scanned for amounts."""
    return f"On the review screen I chose to pay {target} from my {account} account."


REVIEW_EDITS_EXHAUSTED = (f"That's too many changes for one approval, so I've stopped here. "
                          f"{NO_MONEY_MOVED} Please send the instruction again.")
