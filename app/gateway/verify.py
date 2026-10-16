"""Challenges and execution (DESIGN §5.2, §5.3).

issue()   — draft in, signing bytes out: schema + canonical form, ownership,
            risk trial (deny → no challenge), nonce and expiry by the gateway's
            clock, a 6-digit out-of-band code when step-up is needed.
execute() — the ten checks in a fixed order; each failure has its own code.
            Steps 4–10 hold one lock, so a challenge can be spent only once.
"""

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from pydantic import ValidationError

from dcta.core.risk import evaluate
from dcta.core.schema import TransactionGroup, canonical, group_hash
from gateway.data import DataStore
from gateway.devices import Devices
from gateway.ledger import InsufficientFunds, Ledger

OTP_MAX_ATTEMPTS = 3


class Reject(Exception):
    def __init__(self, status: int, code: str, detail: str = "", **extra):
        super().__init__(code)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


@dataclass
class Challenge:
    id: str
    customer_id: str
    key_id: str
    trace_id: str
    group: dict
    group_hash: str
    signing_sha256: str
    expires_at: datetime
    risk: dict
    otp: str | None
    otp_attempts: int = 0
    used: bool = False
    receipt: dict | None = None
    summary: list = field(default_factory=list)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Verifier:

    def __init__(self, store: DataStore, ledger: Ledger, devices: Devices,
                 audit: Callable[..., dict], clock: Callable[[], datetime], ttl_s: int):
        self.store, self.ledger, self.devices = store, ledger, devices
        self.audit, self.clock, self.ttl = audit, clock, timedelta(seconds=ttl_s)
        self.challenges: dict[str, Challenge] = {}
        self._lock = threading.Lock()

    def _risk(self, group: dict) -> dict:
        cid = group["customer_id"]
        return evaluate(group, {
            "customer": self.store.customer(cid) or {}, "limits": self.store.limits(cid),
            "accounts": self.ledger.accounts(cid), "activity": self.ledger.activity(cid),
            "screening": self.store.screening_list(), "now": self.clock()})

    def _check_ownership(self, g: TransactionGroup) -> None:
        cid = g.customer_id
        accounts = {a["account_id"] for a in self.store.accounts(cid)}
        payees = {p["payee_id"]: p for p in self.store.payees(cid)}
        bills = {b["bill_id"]: b for b in self.store.bills(cid)}
        symbols = {i["symbol"] for i in self.store.instruments()}
        for t in g.transactions:
            if t.debtor_account not in accounts:
                raise Reject(403, "NOT_OWNED", f"payment {t.seq}: account {t.debtor_account} "
                                               f"does not belong to {cid}")
            c = t.creditor
            if t.type == "buy_security":
                ok = t.instrument is not None and t.instrument.symbol in symbols
            elif c is None:
                ok = False
            elif c.kind == "payee":
                p = payees.get(c.id)
                ok = (t.type == "transfer" and p is not None and p["name"] == c.name
                      and p["account_last4"] == c.account_last4)
            elif c.kind == "own_account":
                ok = t.type == "transfer" and c.id in accounts and c.id != t.debtor_account
            else:
                b = bills.get(c.id)
                ok = t.type == "bill_payment" and b is not None and b["biller"] == c.name
            if not ok:
                raise Reject(403, "NOT_OWNED", f"payment {t.seq}: recipient is not one of "
                                               f"{cid}'s records")

    # ── POST /v1/challenges ────────────────────────────────────────────────
    def issue(self, group_canonical: str, key_id: str, trace_id: str = "") -> dict:
        try:
            g = TransactionGroup.model_validate_json(group_canonical)
        except ValidationError as e:
            raise Reject(422, "SCHEMA_INVALID", str(e.errors()[0]["msg"]))
        if canonical(g) != group_canonical:
            raise Reject(422, "NON_CANONICAL", "the draft is not in canonical form")
        if self.devices.owner(key_id) != g.customer_id:
            raise Reject(403, "KEY_UNKNOWN", "device key is not registered to this customer")
        self._check_ownership(g)

        group = json.loads(group_canonical)
        risk = self._risk(group)
        if risk["disposition"] == "deny":
            raise Reject(403, "RISK_DENIED", "", rules=risk["rules"])

        now = self.clock()
        envelope = {"aud": "dcta-gateway", "challenge_id": f"ch_{secrets.token_hex(8)}",
                    "customer_id": g.customer_id, "expires_at": _iso(now + self.ttl),
                    "group": group, "group_hash": group_hash(g), "issued_at": _iso(now),
                    "key_id": key_id, "nonce": secrets.token_urlsafe(32), "v": 1}
        signing_input = canonical(envelope)
        step_up = risk["disposition"] == "step_up"
        ch = Challenge(
            id=envelope["challenge_id"], customer_id=g.customer_id, key_id=key_id,
            trace_id=trace_id, group=group, group_hash=envelope["group_hash"],
            signing_sha256=_sha256(signing_input), expires_at=now + self.ttl, risk=risk,
            otp=f"{secrets.randbelow(10 ** 6):06d}" if step_up else None,
            summary=[f"{t.seq}. S${t.amount} {t.type}" for t in g.transactions])
        self.challenges[ch.id] = ch
        self.audit("challenge", {"challenge_id": ch.id, "group_hash": ch.group_hash,
                                 "signing_input_sha256": ch.signing_sha256,
                                 "expires_at": envelope["expires_at"], "risk": risk,
                                 "step_up": step_up}, trace_id)
        return {"challenge_id": ch.id, "signing_input": signing_input,
                "expires_at": envelope["expires_at"], "risk": risk, "step_up": step_up}

    # ── POST /v1/transactions ──────────────────────────────────────────────
    def execute(self, body: dict) -> dict:
        sig = body.get("signature") or {}
        if not sig.get("value"):                                            # 1
            raise Reject(401, "SIGNATURE_MISSING")
        with self._lock:
            ch = self.challenges.get(body.get("challenge_id", ""))
            if ch is None:                                                  # 2
                raise Reject(404, "CHALLENGE_UNKNOWN")
            if self.clock() > ch.expires_at:                                # 3
                raise Reject(410, "CHALLENGE_EXPIRED")
            if ch.used:                                                     # 4
                raise Reject(409, "NONCE_REPLAYED", "",
                             receipt_id=(ch.receipt or {}).get("receipt_id"))
            ch.used = True     # spent from here on, whatever happens next
            signing_input = body.get("signing_input", "")
            if _sha256(signing_input) != ch.signing_sha256:                 # 5
                raise Reject(422, "PAYLOAD_TAMPERED")
            if self.devices.owner(ch.key_id) != ch.customer_id:             # 6
                raise Reject(403, "KEY_UNKNOWN")
            valid = self.devices.verify(ch.key_id, signing_input.encode("utf-8"), sig["value"])
            self.audit("signature", {"challenge_id": ch.id, "key_id": ch.key_id,
                                     "alg": sig.get("alg"), "format": sig.get("format"),
                                     "value": sig["value"], "signing_input_sha256":
                                     ch.signing_sha256, "verified": valid}, ch.trace_id)
            if not valid:                                                   # 7
                raise Reject(403, "SIGNATURE_INVALID")

            risk = self._risk(ch.group)                                     # 8
            self.audit("risk.decision", {"challenge_id": ch.id, **risk}, ch.trace_id)
            if risk["disposition"] == "deny":
                raise Reject(403, "RISK_DENIED", "", rules=risk["rules"])
            if risk["disposition"] == "step_up":
                if ch.otp is None:                 # risk rose after the challenge was issued
                    ch.otp = f"{secrets.randbelow(10 ** 6):06d}"
                ok = body.get("otp") == ch.otp
                ch.otp_attempts += 0 if ok else 1
                self.audit("stepup", {"challenge_id": ch.id, "ok": ok,
                                      "attempts": ch.otp_attempts}, ch.trace_id)
                if not ok:
                    left = OTP_MAX_ATTEMPTS - ch.otp_attempts
                    ch.used = left <= 0            # a wrong code may be retried, 3 tries
                    raise Reject(403, "STEP_UP_REQUIRED", "", attempts_left=max(left, 0))

            now = self.clock()
            try:                                                            # 9 + 10
                posted = self.ledger.post_group(ch.group, now)
            except InsufficientFunds as e:
                raise Reject(409, "INSUFFICIENT_FUNDS", f"account {e}")
            post = self.audit("ledger.post", {"challenge_id": ch.id, "group_hash": ch.group_hash,
                                              "posted": posted}, ch.trace_id)
            ch.receipt = {
                "receipt_id": f"r_{secrets.token_hex(8)}", "challenge_id": ch.id,
                "group_hash": ch.group_hash, "posted_at": _iso(now),
                "transactions": [{"seq": p["seq"], "status": "posted",
                                  **({"quantity": p["quantity"]} if "quantity" in p else {})}
                                 for p in posted],
                "balances": [{"account_id": a["account_id"], "balance": a["balance"]}
                             for a in self.ledger.accounts(ch.customer_id)],
                "audit_hash": post["hash"]}
            return ch.receipt
