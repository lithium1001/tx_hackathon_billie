"""A5: deterministic resolution and the amount re-extractor."""
from decimal import Decimal

import pytest

from conftest import SEED_DIR
from dcta.core import resolve as R
from dcta.core.numbers import extract_amounts
from dcta.core.schema import ParsedIntent
from gateway.data import DataStore

store = DataStore(SEED_DIR)


def ctx(cid="C001"):
    return {"customer": store.customer(cid), "accounts": store.accounts(cid),
            "payees": store.payees(cid), "bills": store.bills(cid),
            "instruments": store.instruments(), "activity": store.activity(cid)}


def txn(intent, evidence, target, amount, hint=None, source=None):
    return {"intent": intent, "evidence": evidence, "target_mention": target,
            "target_hint": hint, "source_mention": source, "amount": amount}


def run(said, *txns, pins=None, cid="C001"):
    parsed = ParsedIntent.model_validate({"category": "transaction", "transactions": list(txns)})
    return R.resolve(parsed, said, ctx(cid), pins=pins)


def absolute(v, said_as=None):
    return {"kind": "absolute", "value": v, "said_as": said_as or v}


EX1 = "Transfer 500 dollars to my mom and use the rest to buy Apple shares"
EX2 = "Move a grand to the joint account, then use half of it to buy DBS shares"


def test_constants_are_pinned():
    assert (R.FUZZY_CUTOFF, R.NEAREST_N, R.CENT) == (0.8, 3, Decimal("0.01"))


@pytest.mark.parametrize("text,expected", [
    ("Transfer 500 dollars", {"500"}), ("a grand", {"1000"}), ("5k and 1.5k", {"5000", "1500"}),
    ("fifty five", {"55"}), ("five hundred and fifty", {"550"}), ("S$1,284.50", {"1284.50"}),
    ("two thousand five hundred", {"2500"}), ("five six", {"5", "6"}), ("no numbers", set()),
])
def test_extract_amounts(text, expected):
    assert extract_amounts(text) == {Decimal(x) for x in expected}


def test_example_1_remainder():
    r = run(EX1,
            txn("transfer", "Transfer 500 dollars to my mom", "my mom", absolute("500"), "PAY-0003"),
            txn("buy_security", "use the rest to buy Apple shares", "Apple",
                {"kind": "remainder", "after": [0]}, "AAPL"))
    assert r["status"] == "ok"
    t1, t2 = r["group"]["transactions"]
    assert (t1["creditor"]["name"], t1["amount"]) == ("TAN MEI LING", "500.00")
    assert (t2["instrument"]["symbol"], t2["amount"]) == ("AAPL", "61500.00")
    assert t2["basis"] == {"kind": "remainder", "balance_at_draft": "62000.00", "less": ["500.00"]}
    assert t1["flags"] == ["default_source"]                 # mom was paid before
    assert t2["flags"] == ["default_source", "fx_indicative"]
    assert len(r["group_hash"]) == 64


def test_example_2_fraction():
    r = run(EX2,
            txn("transfer", "Move a grand to the joint account", "the joint account",
                absolute("1000", "a grand"), "ACC-1003"),
            txn("buy_security", "use half of it to buy DBS shares", "DBS shares",
                {"kind": "fraction", "of": 0, "ratio": "0.5", "said_as": "half of it"}, "D05"))
    t1, t2 = r["group"]["transactions"]
    assert (t1["creditor"]["kind"], t1["creditor"]["id"], t1["amount"]) == \
        ("own_account", "ACC-1003", "1000.00")
    assert (t2["instrument"]["symbol"], t2["amount"]) == ("D05", "500.00")
    assert t2["basis"] == {"kind": "fraction", "of_seq": 1, "ratio": "0.5"}


def test_two_johns_ask_with_details():
    r = run("Send fifty to John", txn("transfer", "Send fifty to John", "John", absolute("50")))
    assert r["status"] == "ask" and r["slot"] == "0.target"
    assert {o["id"] for o in r["options"]} == {"PAY-0001", "PAY-0002"}
    assert all(o["detail"] for o in r["options"])            # bank + last 4 shown


def test_pinned_choice_resolves_the_john():
    r = run("Send fifty to John", txn("transfer", "Send fifty to John", "John", absolute("50")),
            pins={"0.target": "PAY-0002"})
    t = r["group"]["transactions"][0]
    assert t["creditor"]["name"] == "JOHN LEE KOK HUA" and "first_time_payee" in t["flags"]


def test_hint_conflicting_with_match_asks():
    r = run("Send 50 to my mom", txn("transfer", "Send 50 to my mom", "my mom", absolute("50"),
                                     hint="PAY-0004"))
    assert r["status"] == "ask" and {o["id"] for o in r["options"]} == {"PAY-0003", "PAY-0004"}


def test_hint_alone_becomes_a_question_not_a_decision():
    r = run("Pay Granny 50", txn("transfer", "Pay Granny 50", "Granny", absolute("50"),
                                 hint="PAY-0003"))
    assert r["status"] == "ask" and [o["id"] for o in r["options"]] == ["PAY-0003"]


def test_hint_for_another_customers_record_is_ignored():
    r = run("Pay Granny 50", txn("transfer", "Pay Granny 50", "Granny", absolute("50"),
                                 hint="PAY-0101"))
    assert r["status"] == "ask" and "PAY-0101" not in {o["id"] for o in r["options"]}
    assert len(r["options"]) == R.NEAREST_N


def test_amount_nobody_said_is_asked():
    r = run("Send fifty to my mom", txn("transfer", "Send fifty to my mom", "my mom", absolute("15")))
    assert r["status"] == "ask" and r["slot"] == "0.amount"
    assert [o["id"] for o in r["options"]] == ["50.00"]


def test_missing_amount_is_asked():
    r = run("Send money to my mom", txn("transfer", "Send money to my mom", "my mom", None))
    assert r["status"] == "ask" and r["slot"] == "0.amount" and r["options"] == []


def test_named_source_account_is_used():
    r = run("Pay my credit card from Everyday",
            txn("bill_payment", "Pay my credit card from Everyday", "credit card",
                {"kind": "bill_due"}, source="Everyday"))
    t = r["group"]["transactions"][0]
    assert (t["debtor_account"], t["amount"], t["flags"]) == ("ACC-1002", "1284.50", [])
    assert t["basis"] == {"kind": "bill_due", "bill_id": "BILL-0001"}


def test_overdrawn_remainder_produces_no_draft():
    r = run("Send 70000 to mom and the rest to John",
            txn("transfer", "Send 70000 to mom", "mom", absolute("70000")),
            txn("transfer", "the rest to John", "John Tan", {"kind": "remainder", "after": [0]}))
    assert r["status"] == "fail" and "No money has moved" in r["text"]


def test_foreign_currency_is_refused():
    r = run("Send 100 USD to mom", txn("transfer", "Send 100 USD to mom", "mom",
                                       {**absolute("100"), "currency": "USD"}))
    assert r["status"] == "fail"


def test_voice_alternatives_that_disagree_on_the_amount_ask():
    parsed = ParsedIntent.model_validate({"category": "transaction", "transactions": [
        txn("transfer", "Send fifty to my mom", "my mom", absolute("50", "fifty"))]})
    r = R.resolve(parsed, "Send fifty to my mom", ctx(), heard=["Send fifteen to my mom"])
    assert r["status"] == "ask" and r["slot"] == "0.amount"
    assert [o["id"] for o in r["options"]] == ["15.00", "50.00"] and "or" in r["question"]
    # once the customer picks, the pinned amount stands
    r = R.resolve(parsed, "Send fifty to my mom", ctx(), heard=["Send fifteen to my mom"],
                  pins={"0.amount": "15.00"})
    assert r["group"]["transactions"][0]["amount"] == "15.00"


def test_voice_alternatives_only_matter_for_the_amount_they_disagree_on():
    parsed = ParsedIntent.model_validate({"category": "transaction", "transactions": [
        txn("transfer", "Send 500 to my mom", "my mom", absolute("500")),
        txn("transfer", "and 50 to John Tan", "John Tan", absolute("50"))]})
    r = R.resolve(parsed, "Send 500 to my mom and 50 to John Tan", ctx(),
                  heard=["Send 500 to my mom and 15 to John Tan"])
    assert r["status"] == "ask" and r["slot"] == "1.amount"


def test_shield_flag_marks_every_payment():
    r = run("Send 50 to my mom", txn("transfer", "Send 50 to my mom", "my mom", absolute("50")))
    assert "shield_flag" not in r["group"]["transactions"][0]["flags"]
    parsed = ParsedIntent.model_validate({"category": "transaction", "transactions": [
        txn("transfer", "Send 50 to my mom", "my mom", absolute("50"))]})
    r = R.resolve(parsed, "Send 50 to my mom", ctx(), flags=["shield_flag"])
    assert "shield_flag" in r["group"]["transactions"][0]["flags"]


def test_a_near_spelling_is_asked_not_assumed():
    """'my other account' is 0.91 similar to the alias 'mother' — that must be a
    question, never a payment to mom."""
    r = run("Move 500 to my other account",
            txn("transfer", "Move 500 to my other account", "my other account", absolute("500")))
    assert r["status"] == "ask" and r["options"][0]["id"] == "PAY-0003"
    assert "Did you mean" in r["question"]
