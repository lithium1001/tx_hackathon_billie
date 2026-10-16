"""A3: the data contracts and the canonical form that gets hashed and signed."""
import pytest
from pydantic import ValidationError

from dcta.core.schema import ParsedIntent, TransactionGroup, canonical, group_hash

GROUP = {
    "v": 1, "group_id": "g_TEST", "customer_id": "C001", "on_failure": "abort_all",
    "transactions": [
        {"seq": 1, "type": "transfer", "debtor_account": "ACC-1001",
         "creditor": {"kind": "payee", "id": "PAY-0003", "name": "TAN MEI LING",
                      "bank": "Eastgate Bank", "account_last4": "4821"},
         "amount": "500.00", "currency": "SGD", "basis": {"kind": "absolute"},
         "flags": ["default_source"], "end_to_end_id": "g_TEST-1"},
        {"seq": 2, "type": "buy_security", "debtor_account": "ACC-1001",
         "instrument": {"symbol": "AAPL", "name": "Apple Inc.", "market": "NASDAQ",
                        "currency": "USD"},
         "amount": "61500.00", "currency": "SGD",
         "basis": {"kind": "remainder", "balance_at_draft": "62000.00", "less": ["500.00"]},
         "flags": ["default_source", "fx_indicative"], "end_to_end_id": "g_TEST-2"},
    ],
}


def test_same_draft_same_hash():
    a = TransactionGroup.model_validate(GROUP)
    b = TransactionGroup.model_validate(GROUP)
    assert group_hash(a) == group_hash(b)
    # A model and its own dict form serialise to the same bytes.
    assert group_hash(a) == group_hash(a.model_dump(mode="json", exclude_none=True))


def test_canonical_is_compact_sorted_utf8():
    s = canonical({"b": 1, "a": "名字"})
    assert s == '{"a":"名字","b":1}'


def test_changing_one_field_changes_the_hash():
    base = group_hash(TransactionGroup.model_validate(GROUP))
    tampered = {**GROUP, "transactions": [
        {**GROUP["transactions"][0], "amount": "5000.00"}, GROUP["transactions"][1]]}
    assert group_hash(TransactionGroup.model_validate(tampered)) != base


def test_float_is_rejected():
    with pytest.raises(TypeError, match="float"):
        canonical({"amount": 500.0})


def test_group_amount_must_be_two_place_decimal_string():
    bad = {**GROUP, "transactions": [{**GROUP["transactions"][0], "amount": "500"}]}
    with pytest.raises(ValidationError):
        TransactionGroup.model_validate(bad)


def test_group_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        TransactionGroup.model_validate({**GROUP, "skip_confirmation": True})


def test_parsed_intent_example_sentence():
    p = ParsedIntent.model_validate({
        "category": "transaction",
        "transactions": [
            {"intent": "transfer", "evidence": "Transfer 500 dollars to my mom",
             "target_mention": "my mom", "target_hint": "PAY-0003",
             "amount": {"kind": "absolute", "value": 500, "said_as": "500 dollars"}},
            {"intent": "buy_security", "evidence": "use the rest to buy Apple shares",
             "target_mention": "Apple", "target_hint": "AAPL",
             "amount": {"kind": "remainder", "after": [0]}},
        ],
    })
    assert p.transactions[0].amount.value == "500"      # bare number coerced to string
    assert p.transactions[1].amount.kind == "remainder"


@pytest.mark.parametrize("amount", [
    {"kind": "absolute", "value": "five hundred", "said_as": "x"},   # not a decimal
    {"kind": "absolute", "value": "-5", "said_as": "x"},             # negative
    {"kind": "fraction", "of": 0, "ratio": "1.5", "said_as": "x"},   # > 1
    {"kind": "computed", "value": "61500"},                          # unknown kind
])
def test_parsed_intent_rejects_bad_amounts(amount):
    with pytest.raises(ValidationError):
        ParsedIntent.model_validate({"category": "transaction", "transactions": [
            {"intent": "transfer", "evidence": "e", "amount": amount}]})
