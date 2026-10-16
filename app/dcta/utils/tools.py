"""Tools exposed to the agents via bind_tools().

Every tool here is READ-ONLY. Nothing that moves money is defined in, or
importable from, this process — the tool allowlist in graph.py is the boundary,
and the model cannot call what it cannot see.

TOOLS_BY_NAME maps name → tool object; graph.py filters it per agent.
"""

import functools
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from dcta.core.shield import neutralise_field
from dcta.utils import bank_client

# The customer id comes from graph state, injected by ToolNode. It is NOT part of
# the schema the model sees, so the model cannot ask for another customer's data.
CustomerId = Annotated[str, InjectedState("customer_id")]


def _tool_errors_as_result(fn):
    """Turn a bad-input exception into ``{"error": ...}`` the agent can read.

    The @tool wrappers are called by LangGraph's ToolNode, which does NOT catch: an
    exception raised inside one propagates out of the graph and ends the turn, and
    the customer sees a failure. That is the wrong failure mode for INPUT the model
    itself supplied — a missing or mistyped argument is something the agent can fix
    by re-calling with the field, but only if it is handed back as a result instead
    of thrown.

    Deliberately narrow: ValueError / KeyError / TypeError / IndexError are the
    malformed-argument family. Anything else still propagates, because a bug we
    have not characterised must stay loud rather than be reported to the model as
    if the customer had said something wrong.

    Apply it UNDER @tool:

        @tool
        @_tool_errors_as_result
        def some_tool(...): ...
    """

    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            detail = str(exc).strip("'\"") or type(exc).__name__
            # ASCII only, and guarded: on a GBK (CP936) console, printing a glyph
            # outside the codepage raises UnicodeEncodeError — which would escape
            # THIS handler and re-crash the turn we are here to rescue.
            try:
                print(f"  [tool] {fn.__name__} bad input: {detail}")
            except Exception:  # noqa: BLE001 — console encoding must not gate the result
                pass
            return {"error": f"{detail}. Check the arguments and call the tool again "
                             f"with the missing or corrected value."}
    return _wrapped


def _pick(row: dict, *keys: str) -> dict:
    return {k: row.get(k, "") for k in keys}


# Each tool returns only the fields resolution needs. Free-text fields (payee
# note, bill memo) are attacker-controllable, so they pass neutralise_field().

@tool
@_tool_errors_as_result
def list_accounts(customer_id: CustomerId) -> list[dict]:
    """List the customer's own accounts: account_id, nickname, type, last4,
    currency, available balance, joint_holder (if joint), and is_default (the
    account used when the customer does not say which one to pay from)."""
    default = bank_client.customer(customer_id).get("default_account", "")
    return [
        {**_pick(a, "account_id", "nickname", "type", "last4", "currency",
                 "balance", "joint_holder"),
         "is_default": a["account_id"] == default}
        for a in bank_client.accounts(customer_id)
    ]


@tool
@_tool_errors_as_result
def list_payees(customer_id: CustomerId) -> list[dict]:
    """List the customer's saved payees: payee_id, registered name, aliases
    (nicknames such as "mom"), bank, account_last4, and the customer's note."""
    return [
        {**_pick(p, "payee_id", "name", "bank", "account_last4"),
         "aliases": [a for a in p.get("alias", "").split("|") if a],
         "note": neutralise_field(p.get("note", ""))}
        for p in bank_client.payees(customer_id)
    ]


@tool
@_tool_errors_as_result
def list_bills(customer_id: CustomerId) -> list[dict]:
    """List the customer's outstanding bills (including credit-card statements):
    bill_id, biller, reference, amount_due, due_date, memo."""
    return [
        {**_pick(b, "bill_id", "biller", "reference", "amount_due", "due_date"),
         "memo": neutralise_field(b.get("memo", ""))}
        for b in bank_client.bills(customer_id)
    ]


@tool
@_tool_errors_as_result
def get_holdings(customer_id: CustomerId) -> list[dict]:
    """List the securities the customer already holds: symbol and quantity."""
    return [_pick(h, "symbol", "quantity") for h in bank_client.holdings(customer_id)]


@tool
@_tool_errors_as_result
def find_instrument(query: str) -> list[dict]:
    """Look up a tradable security by company name, ticker or common name (e.g.
    "Apple", "DBS"). Returns symbol, name, market, currency and an indicative
    price. Use it to find the symbol for target_hint; never to recommend."""
    if not query.strip():
        raise ValueError("query is required")
    return [_pick(i, "symbol", "name", "market", "currency", "price")
            for i in bank_client.instruments(query)]


ALL_TOOLS = [list_accounts, list_payees, list_bills, get_holdings, find_instrument]
TOOLS_BY_NAME: dict = {t.name: t for t in ALL_TOOLS}
