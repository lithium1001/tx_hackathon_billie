"""A9: double entry, and all-or-nothing posting."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from conftest import SEED_DIR
from gateway.data import DataStore
from gateway.ledger import InsufficientFunds, Ledger

NOW = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)


def txn(seq, amount, **kw):
    return {"seq": seq, "type": "transfer", "debtor_account": "ACC-1001", "amount": amount,
            "currency": "SGD", "end_to_end_id": f"g-{seq}", **kw}


MOM = {"kind": "payee", "id": "PAY-0003", "name": "TAN MEI LING"}
APPLE = {"symbol": "AAPL", "name": "Apple Inc.", "market": "NASDAQ", "currency": "USD"}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(DataStore(SEED_DIR), tmp_path)


def test_entries_balance_to_zero(ledger, tmp_path):
    posted = ledger.post_group({"group_id": "g", "customer_id": "C001", "transactions": [
        txn(1, "500.00", creditor=MOM),
        txn(2, "1000.00", creditor={"kind": "own_account", "id": "ACC-1003", "name": "Joint"}),
        txn(3, "60500.00", type="buy_security", instrument=APPLE)]}, NOW)
    assert sum(Decimal(e["amount"]) for e in ledger.entries) == 0
    assert len(ledger.entries) == 6
    assert ledger.balances["ACC-1001"] == Decimal("0.00")
    assert ledger.balances["ACC-1003"] == Decimal("9400.00")
    assert Decimal(posted[2]["quantity"]) == Decimal("206.0468")    # 60500 / 1.285 / 228.50
    assert (tmp_path / "ledger" / "journal.jsonl").read_text().count("\n") == 3
    assert ledger.activity("C001")[0]["creditor_id"] in {"PAY-0003", "ACC-1003", "AAPL"}


def test_one_short_payment_posts_nothing(ledger):
    before = dict(ledger.balances)
    with pytest.raises(InsufficientFunds):
        ledger.post_group({"group_id": "g", "customer_id": "C001", "transactions": [
            txn(1, "500.00", creditor=MOM),
            txn(2, "61600.00", type="buy_security", instrument=APPLE)]}, NOW)
    assert dict(ledger.balances) == before and ledger.entries == []
