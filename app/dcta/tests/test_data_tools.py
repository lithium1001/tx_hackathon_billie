"""A2: seed data, the gateway's read-only endpoints, and the agent's read tools."""
import json

from langchain_core.messages import AIMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from dcta.graph import DctaState

from dcta.utils import bank_client
from dcta.utils.tools import (ALL_TOOLS, find_instrument, get_holdings, list_accounts,
                              list_bills, list_payees)


def test_no_tool_exposes_customer_id_to_the_model():
    for t in ALL_TOOLS:
        props = t.tool_call_schema.model_json_schema().get("properties", {})
        assert "customer_id" not in props, t.name


def test_gateway_unknown_customer_is_404(bank):
    assert bank.get("/v1/customers/C999/accounts").status_code == 404
    assert bank.get("/v1/customers/C001/ledger").status_code == 404


def test_customer_record(bank):
    c = bank_client.customer("C001")
    assert c["kyc_status"] == "verified" and c["default_account"] == "ACC-1001"
    assert bank_client.customer("C002")["kyc_status"] == "failed"


def test_list_accounts(bank):
    accts = {a["account_id"]: a for a in list_accounts.invoke({"customer_id": "C001"})}
    assert accts["ACC-1001"]["balance"] == "62000.00"          # string, never float
    assert accts["ACC-1001"]["is_default"] is True
    assert accts["ACC-1003"]["joint_holder"] == "LIM SU YIN"
    assert "ACC-2001" not in accts                             # other customer's


def test_list_payees(bank):
    payees = list_payees.invoke({"customer_id": "C001"})
    johns = [p for p in payees if p["name"].startswith("JOHN ")]
    assert len(johns) == 2
    mom = next(p for p in payees if p["payee_id"] == "PAY-0003")
    assert "mom" in mom["aliases"]
    assert all(set(p) == {"payee_id", "name", "bank", "account_last4", "aliases", "note"}
               for p in payees)
    assert "PAY-0101" not in {p["payee_id"] for p in payees}


def test_list_bills(bank):
    bills = {b["bill_id"]: b for b in list_bills.invoke({"customer_id": "C001"})}
    assert bills["BILL-0001"]["amount_due"] == "1284.50"
    assert "BILL-0101" not in bills


def test_get_holdings(bank):
    held = {h["symbol"]: h["quantity"] for h in get_holdings.invoke({"customer_id": "C001"})}
    assert held == {"D05": "100", "ES3": "500"}


def test_find_instrument(bank):
    assert [i["symbol"] for i in find_instrument.invoke({"query": "Apple shares"})] == ["AAPL"]
    assert [i["symbol"] for i in find_instrument.invoke({"query": "dbs"})] == ["D05"]
    assert "error" in find_instrument.invoke({"query": "  "})   # bad input → readable error


def test_tool_node_injects_customer_from_state(bank):
    """The model's call carries no customer id; ToolNode fills it from state."""
    g = StateGraph(DctaState)
    g.add_node("tools", ToolNode(ALL_TOOLS))
    g.set_entry_point("tools")
    g.add_edge("tools", END)
    call = AIMessage(content="", tool_calls=[{"name": "list_accounts", "args": {}, "id": "c1"}])
    out = g.compile().invoke({"messages": [call], "customer_id": "C002"})
    ids = [a["account_id"] for a in json.loads(out["messages"][-1].content)]
    assert ids == ["ACC-2001"]


def test_seed_data_has_the_demo_fixtures(bank):
    payees = bank_client.payees("C001")
    assert any("ignore previous instructions" in p["note"] for p in payees)
    screened = {r["name"] for r in bank.app.state.store.screening_list()}
    assert screened & {p["name"] for p in payees}
