"""A9: every way around the signature is refused, and the ledger does not move."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import SEED_DIR
from dcta.core import audit
from dcta.core.schema import canonical
from dcta.eval.device import SimulatedDevice
from gateway.app import create_app

MOM = {"kind": "payee", "id": "PAY-0003", "name": "TAN MEI LING", "bank": "Eastgate Bank",
       "account_last4": "4821"}


def group(amount="50.00", debtor="ACC-1002", cid="C001", **txn):
    return {"v": 1, "group_id": "g_test", "customer_id": cid, "on_failure": "abort_all",
            "transactions": [{"seq": 1, "type": "transfer", "debtor_account": debtor,
                              "creditor": MOM, "amount": amount, "currency": "SGD",
                              "basis": {"kind": "absolute"}, "flags": [],
                              "end_to_end_id": "g_test-1", **txn}]}


class Gateway:
    """A fresh gateway with a controllable clock and one registered device."""

    def __init__(self, tmp_path):
        self.now = datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)
        self.app = create_app(SEED_DIR, tmp_path, clock=lambda: self.now)
        self.http = TestClient(self.app)
        self.device = SimulatedDevice()
        self.key_id = self.register("C001", self.device)

    def register(self, cid, device):
        return self.http.post("/v1/devices", json={"customer_id": cid,
                                                   "jwk": device.jwk()}).json()["key_id"]

    def challenge(self, g, key_id=None):
        return self.http.post("/v1/challenges", json={"group": canonical(g),
                                                      "key_id": key_id or self.key_id})

    def submit(self, ch, signing_input=None, device=None, otp=None, signature=...):
        s = signing_input or ch["signing_input"]
        sig = (device or self.device).sign(s) if signature is ... else signature
        return self.http.post("/v1/transactions", json={
            "challenge_id": ch["challenge_id"], "signing_input": s, "signature": sig, "otp": otp})

    def balances(self):
        return dict(self.app.state.ledger.balances)

    def rejects(self):
        return [e["payload"]["error"] for e in audit.read(self.app.state.audit_path)
                if e["kind"] == "reject"]


@pytest.fixture
def gw(tmp_path):
    return Gateway(tmp_path)


def test_signed_payment_posts(gw):
    ch = gw.challenge(group()).json()
    r = gw.submit(ch)
    assert r.status_code == 200, r.json()
    assert gw.app.state.ledger.balances["ACC-1002"] == 5150
    assert gw.http.get(f"/v1/transactions/{ch['challenge_id']}").json()["status"] == "posted"
    assert r.json()["group_hash"] == ch["signing_input"].split('"group_hash":"')[1][:64]
    assert audit.verify(gw.app.state.audit_path) is None


@pytest.mark.parametrize("attack,status,code", [
    ("no_signature", 401, "SIGNATURE_MISSING"),
    ("amount_changed_after_signing", 422, "PAYLOAD_TAMPERED"),
    ("expired", 410, "CHALLENGE_EXPIRED"),
    ("someone_elses_key", 403, "SIGNATURE_INVALID"),
    ("unknown_challenge", 404, "CHALLENGE_UNKNOWN"),
])
def test_execution_attacks_are_refused(gw, attack, status, code):
    ch = gw.challenge(group()).json()
    before = gw.balances()
    if attack == "no_signature":
        r = gw.submit(ch, signature=None)
    elif attack == "amount_changed_after_signing":
        forged = ch["signing_input"].replace('"amount":"50.00"', '"amount":"5000.00"')
        r = gw.submit(ch, signing_input=forged)          # validly signed, but not what was issued
    elif attack == "expired":
        gw.now += timedelta(seconds=121)
        r = gw.submit(ch)
    elif attack == "someone_elses_key":
        r = gw.submit(ch, device=SimulatedDevice())
    else:
        r = gw.submit({**ch, "challenge_id": "ch_nope"})
    assert (r.status_code, r.json()["error"]) == (status, code)
    assert gw.balances() == before
    assert gw.rejects() == [code]                        # every refusal is on the record


def test_replay_is_refused(gw):
    ch = gw.challenge(group()).json()
    first = gw.submit(ch).json()
    after_first = gw.balances()
    r = gw.submit(ch)
    assert (r.status_code, r.json()["error"]) == (409, "NONCE_REPLAYED")
    assert r.json()["receipt_id"] == first["receipt_id"]
    assert gw.balances() == after_first


def test_a_rejected_challenge_cannot_be_retried(gw):
    ch = gw.challenge(group()).json()
    gw.submit(ch, device=SimulatedDevice())              # wrong key: spends the challenge
    assert gw.submit(ch).json()["error"] == "NONCE_REPLAYED"


@pytest.mark.parametrize("make,status,code", [
    (lambda gw: (group(debtor="ACC-2001"), None), 403, "NOT_OWNED"),          # not C001's account
    (lambda gw: (group(creditor={**MOM, "name": "ALEX WONG"}), None), 403, "NOT_OWNED"),
    (lambda gw: (group(), gw.register("C002", SimulatedDevice())), 403, "KEY_UNKNOWN"),
    (lambda gw: (group(cid="C002", debtor="ACC-2001",
                       creditor={"kind": "payee", "id": "PAY-0101", "name": "NG AH HUAT",
                                 "bank": "Harbour Bank", "account_last4": "6602"}),
                 gw.register("C002", SimulatedDevice())), 403, "RISK_DENIED"),   # KYC failed
])
def test_challenge_is_refused(gw, make, status, code):
    g, key_id = make(gw)
    r = gw.challenge(g, key_id)
    assert (r.status_code, r.json()["error"]) == (status, code)
    assert not gw.app.state.verifier.challenges


def test_non_canonical_draft_is_refused(gw):
    r = gw.http.post("/v1/challenges", json={"group": canonical(group()).replace(",", ", "),
                                             "key_id": gw.key_id})
    assert r.json()["error"] == "NON_CANONICAL"


def test_step_up_needs_the_out_of_band_code(gw):
    g = group(amount="61500.00", debtor="ACC-1001", type="buy_security", creditor=None,
              instrument={"symbol": "AAPL", "name": "Apple Inc.", "market": "NASDAQ",
                          "currency": "USD"})
    del g["transactions"][0]["creditor"]
    ch = gw.challenge(g).json()
    assert ch["step_up"] is True
    r = gw.submit(ch)
    assert (r.status_code, r.json()["error"], r.json()["attempts_left"]) == \
        (403, "STEP_UP_REQUIRED", 2)
    otp = gw.http.get("/v1/oob/pending").json()[0]["otp"]
    r = gw.submit(ch, otp=otp)
    assert r.status_code == 200
    assert float(r.json()["transactions"][0]["quantity"]) > 200      # ~209 AAPL


def test_step_up_code_has_three_tries(gw):
    g = group(creditor={"kind": "payee", "id": "PAY-0002", "name": "JOHN LEE KOK HUA",
                        "bank": "Northpoint Bank", "account_last4": "5678"})   # first-time payee
    ch = gw.challenge(g).json()
    for _ in range(3):
        r = gw.submit(ch, otp="000000")
    assert r.json()["attempts_left"] == 0
    assert gw.submit(ch, otp=gw.app.state.verifier.challenges[ch["challenge_id"]].otp) \
        .json()["error"] == "NONCE_REPLAYED"
