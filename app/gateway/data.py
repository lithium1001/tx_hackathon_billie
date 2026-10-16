"""DataStore — the bank's seed tables (data/*.csv), loaded once into memory.

One public method per read endpoint. Every value stays a string exactly as it is
in the CSV: amounts are decimal strings and never pass through float.
"""

import csv
from pathlib import Path

TABLES = ("customers", "accounts", "payees", "bills", "instruments", "fx",
          "holdings", "limits", "screening_list", "history")


class DataStore:

    def __init__(self, seed_dir: str | Path):
        self._t: dict[str, list[dict]] = {}
        for name in TABLES:
            with open(Path(seed_dir) / f"{name}.csv", newline="", encoding="utf-8") as f:
                self._t[name] = list(csv.DictReader(f))

    def rows(self, table: str) -> list[dict]:
        return [dict(r) for r in self._t[table]]

    def _of(self, table: str, customer_id: str) -> list[dict]:
        return [dict(r) for r in self._t[table] if r["customer_id"] == customer_id]

    def customer(self, customer_id: str) -> dict | None:
        rows = self._of("customers", customer_id)
        return rows[0] if rows else None

    def accounts(self, customer_id: str) -> list[dict]:
        return self._of("accounts", customer_id)

    def payees(self, customer_id: str) -> list[dict]:
        return self._of("payees", customer_id)

    def bills(self, customer_id: str) -> list[dict]:
        return self._of("bills", customer_id)

    def holdings(self, customer_id: str) -> list[dict]:
        return self._of("holdings", customer_id)

    def activity(self, customer_id: str) -> list[dict]:
        return sorted(self._of("history", customer_id), key=lambda r: r["ts"], reverse=True)

    def limits(self, customer_id: str) -> dict | None:
        rows = self._of("limits", customer_id)
        return rows[0] if rows else None

    def instruments(self, q: str = "") -> list[dict]:
        """Case-insensitive: q is part of a symbol / name / alias, or one word of q
        IS one of them ("apple shares" → AAPL). Empty q lists all."""
        q = q.strip().lower()
        words = set(q.split())
        out = []
        for r in self._t["instruments"]:
            keys = [r["symbol"].lower(), r["name"].lower(),
                    *filter(None, r["aliases"].lower().split("|"))]
            if not q or any(q in k or k in words for k in keys):
                out.append(dict(r))
        return out

    def fx(self) -> dict[str, str]:
        return {r["pair"]: r["rate"] for r in self._t["fx"]}

    def screening_list(self) -> list[dict]:
        return [dict(r) for r in self._t["screening_list"]]
