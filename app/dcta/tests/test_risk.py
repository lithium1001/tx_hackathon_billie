"""A8: KYC and risk rules — one passing and one firing case per rule."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from conftest import SEED_DIR
from dcta.core import risk as R
from gateway.data import DataStore

store = DataStore(SEED_DIR)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=R.SGT)


def ctx(cid="C001", recent=0):
    """Seed snapshot; `recent` adds that many payments made minutes ago."""
    activity = store.activity(cid) + [
        {"txn_id": f"R{i}", "customer_id": cid, "ts": (NOW - timedelta(minutes=i + 1)).isoformat(),
         "type": "transfer", "debtor_account": "ACC-1002", "creditor_id": "PAY-0001",
         "amount": "10.00", "currency": "SGD"} for i in range(recent)]
    return {"customer": store.customer(cid), "limits": store.limits(cid),
            "accounts": store.accounts(cid), "activity": activity,
            "screening": store.screening_list(), "now": NOW}


def pay(amount, to="PAY-0003", name="TAN MEI LING", kind="payee", type="transfer",
        acct="ACC-1002", flags=(), seq=1):
    t = {"seq": seq, "type": type, "debtor_account": acct, "amount": amount, "flags": list(flags)}
    if type == "buy_security":
        t["instrument"] = {"symbol": "AAPL"}
    else:
        t["creditor"] = {"kind": kind, "id": to, "name": name}
    return t


def check(*txns, c=None):
    out = R.evaluate({"transactions": list(txns)}, c or ctx())
    return out["disposition"], {r["rule"] for r in out["rules"]}


def test_constants_are_pinned():
    assert (R.DEFAULT_DAILY_LIMIT, R.STEP_UP_AMOUNT, R.DRAIN_MIN_BALANCE, R.DRAIN_SHARE) == \
        (Decimal("1000.00"), Decimal("5000.00"), Decimal("50000.00"), Decimal("0.5"))
    assert (R.VELOCITY_WINDOW, R.VELOCITY_STEP_UP, R.VELOCITY_DENY, R.DRAIN_WINDOW) == \
        (timedelta(minutes=10), 3, 5, timedelta(hours=24))


def test_ordinary_payment_is_allowed():
    assert check(pay("100.00")) == ("allow", set())


def test_kyc_status():
    assert "KYC_STATUS" not in check(pay("100.00"))[1]
    assert check(pay("50.00", to="PAY-0101", name="NG AH HUAT", acct="ACC-2001"),
                 c=ctx("C002")) == ("deny", {"KYC_STATUS"})


def test_screening():
    disp, rules = check(pay("100.00", to="PAY-0005", name="ZENITH OFFSHORE LTD"))
    assert disp == "deny" and "SCREENING" in rules


def test_first_time_payee():
    assert check(pay("100.00", to="PAY-0002", name="JOHN LEE KOK HUA")) == \
        ("step_up", {"FIRST_TIME_PAYEE"})
    # own-account transfers are exempt
    assert check(pay("100.00", to="ACC-1003", name="Joint", kind="own_account")) == ("allow", set())


def test_daily_limit():
    assert check(pay("1000.00")) == ("allow", set())
    assert check(pay("600.00"), pay("500.00", seq=2)) == ("deny", {"DAILY_LIMIT"})
    # own-account transfers do not count toward it
    assert check(pay("900.00"), pay("3000.00", to="ACC-1003", name="Joint",
                                    kind="own_account", seq=2)) == ("allow", set())


def test_step_up_amount():
    assert check(pay("4999.99", type="buy_security")) == ("allow", set())
    assert check(pay("5000.00", type="buy_security")) == ("step_up", {"STEP_UP_AMOUNT"})


def test_velocity():
    assert check(pay("10.00"), c=ctx(recent=2)) == ("allow", set())
    assert check(pay("10.00"), c=ctx(recent=3)) == ("step_up", {"VELOCITY"})
    out = R.evaluate({"transactions": [pay("10.00")]}, ctx(recent=5))
    assert out["disposition"] == "deny" and "retry after 12:05" in out["rules"][-1]["detail"]


def test_drain_example_sentence_1():
    """'Transfer 500 to my mom and use the rest to buy Apple shares' empties savings."""
    disp, rules = check(pay("500.00", acct="ACC-1001"),
                        pay("61500.00", type="buy_security", acct="ACC-1001", seq=2))
    assert disp == "step_up" and "DRAIN" in rules


def test_drain_exemptions():
    assert "DRAIN" not in check(pay("30000.00", acct="ACC-1001", type="buy_security"))[1]
    assert "DRAIN" not in check(pay("40000.00", to="ACC-1003", name="Joint",
                                    kind="own_account", acct="ACC-1001"))[1]
    assert "DRAIN" not in check(pay("40000.00", to="BILL-0001", name="Visa", kind="biller",
                                    type="bill_payment", acct="ACC-1001"))[1]


@pytest.mark.parametrize("flags,expected", [((), "allow"), (("shield_flag",), "step_up")])
def test_shield_flag(flags, expected):
    assert check(pay("100.00", flags=flags))[0] == expected
