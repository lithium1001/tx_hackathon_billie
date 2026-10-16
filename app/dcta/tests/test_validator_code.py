"""A7 code side: crosscheck re-derives the draft from the customer's words."""
import copy

from conftest import SEED_DIR
from dcta.core.crosscheck import crosscheck
from dcta.core.resolve import resolve
from dcta.core.schema import ParsedIntent
from gateway.data import DataStore

store = DataStore(SEED_DIR)
CTX = {"customer": store.customer("C001"), "accounts": store.accounts("C001"),
       "payees": store.payees("C001"), "bills": store.bills("C001"),
       "instruments": store.instruments(), "activity": store.activity("C001")}

SAID = "Transfer 500 dollars to my mom and use the rest to buy Apple shares"
PARSED = ParsedIntent.model_validate({"category": "transaction", "transactions": [
    {"intent": "transfer", "evidence": "Transfer 500 dollars to my mom", "target_mention": "my mom",
     "amount": {"kind": "absolute", "value": "500", "said_as": "500 dollars"}},
    {"intent": "buy_security", "evidence": "use the rest to buy Apple shares",
     "target_mention": "Apple", "amount": {"kind": "remainder", "after": [0]}}]})
GROUP = resolve(PARSED, SAID, CTX)["group"]


def problems(group=GROUP, parsed=PARSED, said=SAID):
    return crosscheck(parsed, group, said, CTX)


def test_honest_draft_passes():
    assert problems() == []


def test_changed_amount_is_caught():
    g = copy.deepcopy(GROUP)
    g["transactions"][0]["amount"] = "5000.00"
    assert any(p.startswith("1.amount") for p in problems(g))
    # …and the dependent remainder no longer adds up either
    assert any(p.startswith("2.amount") for p in problems(g))


def test_tampered_remainder_is_caught():
    g = copy.deepcopy(GROUP)
    g["transactions"][1]["amount"] = "60000.00"
    assert [p.split(":")[0] for p in problems(g)] == ["2.amount"]


def test_extra_transaction_is_caught():
    g = copy.deepcopy(GROUP)
    extra = copy.deepcopy(g["transactions"][0])
    extra.update(seq=3, end_to_end_id="x-3")
    g["transactions"].append(extra)
    found = problems(g)
    assert any(p.startswith("transactions") for p in found)
    assert any(p.startswith("3.type") for p in found)


def test_mention_never_said_is_caught():
    p = PARSED.model_copy(deep=True)
    p.transactions[0].target_mention = "Alex"
    assert any(x.startswith("1.target") for x in problems(parsed=p))


def test_evidence_not_in_transcript_is_caught():
    assert any(p.startswith("transactions") for p in problems(said="Transfer 500 dollars"))
