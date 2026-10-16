"""Data contracts and canonical hashing (DESIGN §3).

ParsedIntent      — what the parser model returns. Untrusted; validated strictly.
TransactionGroup  — the draft code resolves from it. This is what gets signed.
canonical()       — the one byte-exact serialisation; group_hash() hashes it.

Pure code: no LLM imports, no I/O. The gateway imports this module too.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Decimal strings, never float. The model may say "500" or "500.5"; a resolved
# amount is always exactly two places.
DecimalStr = Annotated[str, StringConstraints(pattern=r"^\d+(\.\d{1,2})?$")]
Money = Annotated[str, StringConstraints(pattern=r"^\d+\.\d{2}$")]
Ratio = Annotated[str, StringConstraints(pattern=r"^(0?\.\d+|1(\.0+)?)$")]

Intent = Literal["transfer", "bill_payment", "buy_security"]


class _Strict(BaseModel):
    # coerce_numbers_to_str: a bare JSON number (500) is accepted as "500" — the
    # model's most common slip — and the pattern still rejects anything odd.
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


# ── ParsedIntent (§3.1) ─────────────────────────────────────────────────────
class AbsoluteAmount(_Strict):
    kind: Literal["absolute"]
    value: DecimalStr
    said_as: str
    currency: str | None = None


class RemainderAmount(_Strict):
    kind: Literal["remainder"]
    after: list[int] = []          # indexes of earlier transactions in this list


class FractionAmount(_Strict):
    kind: Literal["fraction"]
    of: int                        # index of the transaction it is a share of
    ratio: Ratio
    said_as: str


class BillDueAmount(_Strict):
    kind: Literal["bill_due"]


Amount = Annotated[AbsoluteAmount | RemainderAmount | FractionAmount | BillDueAmount,
                   Field(discriminator="kind")]


class ParsedTxn(_Strict):
    intent: Intent
    evidence: str = Field(min_length=1)
    source_mention: str | None = None
    target_mention: str | None = None
    target_hint: str | None = None
    amount: Amount | None = None   # None = the customer did not say → ask


class ParsedIntent(_Strict):
    category: Literal["transaction", "inquiry", "advice", "smalltalk", "out_of_scope"]
    transactions: list[ParsedTxn] = []
    uncertain: list[str] = []


# ── ValidatorReport (§4.7): the validator model's reply, also untrusted ─────
Verdict = Literal["match", "mismatch", "not_stated"]


class FieldVerdicts(_Strict):
    seq: int
    recipient: Verdict
    amount: Verdict
    asset: Verdict
    source: Verdict


class ValidatorReport(_Strict):
    transactions: list[FieldVerdicts]
    extra: list[int] = []        # draft seqs the customer never asked for
    missing: list[str] = []      # things asked for that are not in the draft


# ── TransactionGroup (§3.2) ─────────────────────────────────────────────────
class Creditor(_Strict):
    kind: Literal["payee", "own_account", "biller"]
    id: str
    name: str
    bank: str | None = None
    account_last4: str | None = None


class Instrument(_Strict):
    symbol: str
    name: str
    market: str
    currency: str


class Basis(_Strict):
    kind: Literal["absolute", "remainder", "fraction", "bill_due"]
    balance_at_draft: Money | None = None    # remainder
    less: list[Money] | None = None          # remainder
    of_seq: int | None = None                # fraction
    ratio: Ratio | None = None               # fraction
    bill_id: str | None = None               # bill_due


class Txn(_Strict):
    seq: int = Field(ge=1)
    type: Intent
    debtor_account: str
    creditor: Creditor | None = None         # transfer / bill_payment
    instrument: Instrument | None = None     # buy_security
    amount: Money
    currency: str = "SGD"
    basis: Basis
    flags: list[str] = []
    end_to_end_id: str


class TransactionGroup(_Strict):
    v: Literal[1] = 1
    group_id: str
    customer_id: str
    on_failure: Literal["abort_all"] = "abort_all"
    transactions: list[Txn] = Field(min_length=1)


# ── canonical form & hash (§3.3) ────────────────────────────────────────────
def _no_floats(obj, path="$"):
    if isinstance(obj, float):
        raise TypeError(f"float at {path}: amounts must be decimal strings")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _no_floats(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_floats(v, f"{path}[{i}]")


def canonical(obj) -> str:
    """Sorted keys, no whitespace, UTF-8 as-is. Unset (None) model fields are
    dropped so an optional field cannot change the bytes. Raises on any float."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json", exclude_none=True)
    _no_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def group_hash(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()
