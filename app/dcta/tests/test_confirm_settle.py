"""A11: the confirm gate, settle, and the server endpoints around them."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from dcta.core import audit, copy
from dcta.eval.device import SimulatedDevice
from dcta.graph import graph, new_state
from dcta.server import stream
from dcta.server.app import app

TEXT = "Send 50 to my mom"
PARSED = {"category": "transaction", "transactions": [
    {"intent": "transfer", "evidence": TEXT, "target_mention": "my mom",
     "amount": {"kind": "absolute", "value": "50", "said_as": "50"}}], "uncertain": []}
REPORT = {"transactions": [{"seq": 1, "recipient": "match", "amount": "match", "asset": "match",
                            "source": "not_stated"}], "extra": [], "missing": []}


def to_confirm_gate(stub_llm):
    stub_llm(json.dumps(PARSED), json.dumps(REPORT))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    out = graph.invoke(new_state(customer_id="C001", session_id="s", user_text=TEXT, turn=1), cfg)
    return out["__interrupt__"][0].value, cfg


def device_pays(bank, draft):
    """What the browser does: register, get the bytes to sign, sign, submit."""
    dev = SimulatedDevice()
    key_id = bank.post("/v1/devices", json={"customer_id": "C001", "jwk": dev.jwk()}).json()["key_id"]
    ch = bank.post("/v1/challenges", json={"group": draft["group_canonical"], "key_id": key_id,
                                           "trace_id": draft["trace_id"]}).json()
    r = bank.post("/v1/transactions", json={"challenge_id": ch["challenge_id"],
                                            "signing_input": ch["signing_input"],
                                            "signature": dev.sign(ch["signing_input"])})
    assert r.status_code == 200, r.json()
    return ch["challenge_id"]


def test_signed_and_posted_settles_with_the_receipt(bank, stub_llm):
    draft, cfg = to_confirm_gate(stub_llm)
    assert draft["type"] == "draft" and draft["trace_id"] == "s:1"
    challenge_id = device_pays(bank, draft)
    out = graph.invoke(Command(resume={"status": "submitted", "challenge_id": challenge_id}), cfg)
    assert out["outcome"] == "posted"
    settle = next(a for a in out["audit"] if a["kind"] == "settle")["payload"]
    gw_post = next(e for e in audit.read(bank.app.state.audit_path) if e["kind"] == "ledger.post")
    assert settle["ok"] and settle["audit_hash"] == gw_post["hash"]     # the chains cross-reference
    assert settle["group_hash"] == draft["group_hash"]


def test_claimed_but_never_posted_is_not_believed(bank, stub_llm):
    draft, cfg = to_confirm_gate(stub_llm)
    out = graph.invoke(Command(resume={"status": "submitted", "challenge_id": "ch_made_up"}), cfg)
    assert out["outcome"] == "error"
    assert [a["payload"]["text"] for a in out["audit"] if a["kind"] == "answer"] == \
        [copy.SETTLE_MISMATCH]


@pytest.mark.parametrize("value,outcome,text", [
    ({"status": "cancelled"}, "cancelled", copy.CANCELLED),
    ({"status": "failed", "code": "PAYLOAD_TAMPERED"}, "denied",
     copy.gateway_refused("PAYLOAD_TAMPERED")),
])
def test_cancel_and_refusal_end_the_turn(bank, stub_llm, value, outcome, text):
    _, cfg = to_confirm_gate(stub_llm)
    out = graph.invoke(Command(resume=value), cfg)
    assert out["outcome"] == outcome
    assert [a["payload"]["text"] for a in out["audit"] if a["kind"] == "answer"][-1] == text


# ── server endpoints ────────────────────────────────────────────────────────
def _events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def web(bank):
    client = TestClient(app, base_url="http://dcta.test")   # a dotted host keeps the cookie
    yield client
    stream.SESSIONS.clear()


def test_login_scopes_the_session(web):
    assert web.get("/api/me").json() is None
    me = web.post("/api/login", json={"customer_id": "C001"}).json()
    assert me["name"] == "LIM WEI JIE" and any(a["is_default"] for a in me["accounts"])
    assert web.get("/api/me").json()["customer_id"] == "C001"
    assert web.post("/api/login", json={"customer_id": "C999"}).status_code == 400


def test_full_turn_over_http(web, bank, stub_llm):
    stub_llm(json.dumps(PARSED), json.dumps(REPORT))
    web.post("/api/login", json={"customer_id": "C001"})
    draft = next(e for e in _events(web.post("/api/chat", json={"message": TEXT}))
                 if e["event"] == "draft")
    challenge_id = device_pays(bank, draft)
    events = _events(web.post("/api/confirm", json={"challenge_id": challenge_id,
                                                    "status": "submitted"}))
    receipt = next(e for e in events if e["event"] == "receipt")
    assert receipt["ok"] and receipt["balances"][0] == {"account_id": "ACC-1001", "balance": "61950.00"}
    assert events[-1]["event"] == "done"

    chains = web.get("/api/audit").json()
    assert chains["agent"]["broken_at"] is None and chains["gateway"]["broken_at"] is None
    kinds = {e["kind"] for e in chains["agent"]["entries"]}
    assert {"input.raw", "draft.resolved", "validation", "confirm.submitted", "settle"} <= kinds
    assert any(e["kind"] == "ledger.post" for e in chains["gateway"]["entries"])


def test_cancel_over_http(web, stub_llm):
    stub_llm(json.dumps(PARSED), json.dumps(REPORT))
    web.post("/api/login", json={"customer_id": "C001"})
    web.post("/api/chat", json={"message": TEXT})
    events = _events(web.post("/api/cancel"))
    assert [e for e in events if e["event"] == "answer"][0]["text"] == copy.CANCELLED


def test_tamper_switch_is_off_by_default(web):
    assert web.post("/api/demo/tamper").status_code in (404, 405)
