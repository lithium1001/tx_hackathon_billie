"""The ledger. `post_group()` is the ONLY function in the repository that
changes a balance.

Double entry: every payment is one debit and one credit, so all entries sum to
zero. Money leaving the bank lands in a clearing account per kind of recipient.
"""

import json
import threading
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from gateway.data import DataStore

CLEARING = {"payee": "CLR-PAYMENTS", "biller": "CLR-BILLERS", "instrument": "CLR-BROKER"}
SHARE_STEP = Decimal("0.0001")      # fractional shares, rounded down (mock rule)


class InsufficientFunds(Exception):
    pass


class Ledger:

    def __init__(self, store: DataStore, state_dir: Path):
        self._store = store
        self._lock = threading.Lock()
        self._journal = Path(state_dir) / "ledger" / "journal.jsonl"
        self.balances = defaultdict(Decimal, {a["account_id"]: Decimal(a["balance"])
                                              for a in store.rows("accounts")})
        self.holdings = defaultdict(Decimal, {(h["customer_id"], h["symbol"]): Decimal(h["quantity"])
                                              for h in store.rows("holdings")})
        self.history = store.rows("history")
        self.entries: list[dict] = []

    # ── live views for the read API and the risk snapshot ──────────────────
    def accounts(self, cid: str) -> list[dict]:
        return [{**a, "balance": str(self.balances[a["account_id"]])}
                for a in self._store.accounts(cid)]

    def holdings_of(self, cid: str) -> list[dict]:
        return [{"customer_id": c, "symbol": s, "quantity": str(q)}
                for (c, s), q in self.holdings.items() if c == cid]

    def activity(self, cid: str) -> list[dict]:
        return sorted((h for h in self.history if h["customer_id"] == cid),
                      key=lambda h: h["ts"], reverse=True)

    # ── the one write ──────────────────────────────────────────────────────
    def post_group(self, group: dict, posted_at: datetime) -> list[dict]:
        """Trial the whole group, then post all of it — or nothing (abort_all)."""
        prices = {i["symbol"]: Decimal(i["price"]) for i in self._store.instruments()}
        fx = {pair: Decimal(r) for pair, r in self._store.fx().items()}
        txns = group["transactions"]
        with self._lock:
            need = defaultdict(Decimal)
            for t in txns:
                need[t["debtor_account"]] += Decimal(t["amount"])
            for acct, amount in need.items():
                if self.balances[acct] < amount:
                    raise InsufficientFunds(acct)

            posted = []
            for t in txns:
                amount = Decimal(t["amount"])
                if "instrument" in t:
                    inst = t["instrument"]
                    rate = fx.get(f"{inst['currency']}SGD", Decimal(1))
                    qty = (amount / rate / prices[inst["symbol"]]).quantize(SHARE_STEP, ROUND_DOWN)
                    self.holdings[(group["customer_id"], inst["symbol"])] += qty
                    credit, target = CLEARING["instrument"], inst["symbol"]
                else:
                    c = t["creditor"]
                    credit = c["id"] if c["kind"] == "own_account" else CLEARING[c["kind"]]
                    target, qty = c["id"], None
                self.balances[t["debtor_account"]] -= amount
                self.balances[credit] += amount
                pair = [{"account": t["debtor_account"], "amount": str(-amount)},
                        {"account": credit, "amount": str(amount)}]
                self.history.append({
                    "txn_id": t["end_to_end_id"], "customer_id": group["customer_id"],
                    "ts": posted_at.isoformat(), "type": t["type"],
                    "debtor_account": t["debtor_account"], "creditor_id": target,
                    "amount": t["amount"], "currency": t["currency"]})
                posted.append({"seq": t["seq"], "end_to_end_id": t["end_to_end_id"],
                               "entries": pair, **({"quantity": str(qty)} if qty is not None else {})})

            self.entries += [e for p in posted for e in p["entries"]]
            self._journal.parent.mkdir(parents=True, exist_ok=True)
            with self._journal.open("a", encoding="utf-8") as f:
                for p in posted:
                    f.write(json.dumps({"group_id": group["group_id"], "posted_at":
                                        posted_at.isoformat(), **p}) + "\n")
        return posted
