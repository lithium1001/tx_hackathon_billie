"""Mock sign-in failures, the banking data behind /api/me, and changing the
source account on the review screen."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from dcta.core import copy
from dcta.graph import MAX_REVIEW_EDITS, graph, new_state
from dcta.server import auth, stream
from dcta.server.app import app

TEXT = "Send 50 to my mom"
PARSED = {"category": "transaction", "transactions": [
    {"intent": "transfer", "evidence": TEXT, "target_mention": "my mom",
     "amount": {"kind": "absolute", "value": "50", "said_as": "50"}}], "uncertain": []}
REPORT = {"transactions": [{"seq": 1, "recipient": "match", "amount": "match", "asset": "match",
                            "source": "match"}], "extra": [], "missing": []}


@pytest.fixture
def web(bank):
    client = TestClient(app, base_url="http://dcta.test")
    yield client
    stream.SESSIONS.clear()
    for cid in ("C001", "C003", "C004", "C005"):
        auth.reset(cid)


def login(web, cid, **kw):
    return web.post("/api/login", json={"customer_id": cid, **kw})


# ── sign-in ─────────────────────────────────────────────────────────────────
def test_three_wrong_pins_lock_the_account(web):
    r = login(web, "C001", method="pin", pin="000000")
    assert r.status_code == 401 and r.json() == {"error": "PIN_WRONG", "attempts_left": 2}
    login(web, "C001", method="pin", pin="111111")
    r = login(web, "C001", method="pin", pin="222222")
    assert r.status_code == 423 and r.json()["reason"] == "pin"
    # Locked means locked: even the right PIN and biometrics are refused now.
    assert login(web, "C001", method="pin", pin=auth.DEMO_PIN).status_code == 423
    assert login(web, "C001").status_code == 423
    assert web.get("/api/me").json() is None
    web.post("/api/mock/reset-login", json={"customer_id": "C001"})
    assert login(web, "C001", method="pin", pin=auth.DEMO_PIN).status_code == 200


def test_bank_locked_customer_cannot_sign_in(web):
    r = login(web, "C004")
    assert r.status_code == 423 and r.json() == {"error": "LOCKED", "reason": "bank"}
    assert web.get("/api/me").json() is None


def test_new_device_needs_the_sms_code(web):
    r = login(web, "C005")
    assert r.status_code == 428 and r.json()["sent_to"].endswith("2290")
    sms = web.get("/api/mock/sms").json()
    code = sms[0]["code"]
    assert code in sms[0]["text"]
    bad = "000000" if code != "000000" else "111111"
    assert login(web, "C005", otp=bad).json()["error"] == "OTP_WRONG"
    assert web.get("/api/me").json() is None
    me = login(web, "C005", otp=code).json()
    assert me["customer_id"] == "C005"
    assert any("New device confirmed" in e["text"] for e in me["security"])
    # Once confirmed, the device is trusted for the next sign-in.
    assert login(web, "C005").status_code == 200


def test_me_carries_the_banking_screens(web):
    me = login(web, "C001").json()
    assert me["session_id"] and me["member_since"] == "2016-03-14"
    assert {a["account_id"] for a in me["accounts"]} == {"ACC-1001", "ACC-1002", "ACC-1003"}
    alex = next(p for p in me["payees"] if p["payee_id"] == "PAY-0004")
    zenith = next(p for p in me["payees"] if p["payee_id"] == "PAY-0005")
    assert alex["paid_before"] is False and zenith["screened"] is True
    assert "note" not in alex                         # free-text notes stay out of the page
    salary = next(r for r in me["activity"] if "Salary" in r["counterparty"])
    assert salary["type"] == "incoming" and salary["amount"] == "6850.00"
    mom = next(r for r in me["activity"] if r["counterparty"] == "TAN MEI LING")
    assert mom["amount"] == "-500.00"
    held = {h["symbol"]: h for h in me["holdings"]}
    assert held["D05"]["value_sgd"] == "3820.00"
    assert me["limits"] == {"daily": "1000.00", "used_today": "0", "step_up_amount": "5000.00"}


def test_customer_without_default_account_is_asked_where_from(web, stub_llm):
    stub_llm(json.dumps({"category": "transaction", "transactions": [
        {"intent": "transfer", "evidence": "Send 100 to my sister", "target_mention": "my sister",
         "amount": {"kind": "absolute", "value": "100", "said_as": "100"}}], "uncertain": []}))
    login(web, "C003")
    events = [json.loads(line[6:]) for line in web.post(
        "/api/chat", json={"message": "Send 100 to my sister"}).text.splitlines()
        if line.startswith("data: ")]
    ask = next(e for e in events if e["event"] == "clarify")
    assert ask["slot"] == "0.source" and {o["id"] for o in ask["options"]} == {"ACC-3001", "ACC-3002"}


def test_sign_in_rotates_the_session_and_turns_never_repeat(web, stub_llm):
    stub_llm(*[json.dumps({"category": "smalltalk", "transactions": [], "uncertain": []})] * 3)
    first = login(web, "C001").json()["session_id"]
    web.post("/api/chat", json={"message": "hi"})
    web.post("/api/reset")
    web.post("/api/chat", json={"message": "hello"})
    traces = [e["trace_id"] for e in web.get("/api/audit").json()["agent"]["entries"]
              if e["kind"] == "input.raw"]
    assert len(traces) == 2 and len(set(traces)) == 2          # reset does not reuse turn 1
    second = login(web, "C003").json()["session_id"]
    assert second != first                                     # a new sign-in, a new session
    assert web.get("/api/audit").json()["agent"]["entries"] == []


# ── review screen: change the source account ────────────────────────────────
def to_confirm_gate(stub_llm, *extra_reports):
    stub_llm(json.dumps(PARSED), json.dumps(REPORT), *map(json.dumps, extra_reports))
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    out = graph.invoke(new_state(customer_id="C001", session_id="s", user_text=TEXT, turn=1), cfg)
    return out["__interrupt__"][0].value, cfg


def debtor(draft):
    return json.loads(draft["group_canonical"])["transactions"][0]["debtor_account"]


def test_changing_the_source_redrafts_and_revalidates(bank, stub_llm):
    draft, cfg = to_confirm_gate(stub_llm, REPORT)
    assert debtor(draft) == "ACC-1001"
    out = graph.invoke(Command(resume={"status": "edit", "seq": 1, "account_id": "ACC-1002"}), cfg)
    again = out["__interrupt__"][0].value
    assert again["type"] == "draft" and debtor(again) == "ACC-1002"
    assert again["group_hash"] != draft["group_hash"]
    said = out["transcript"][-1]
    assert said["choice_id"] == "ACC-1002" and "Everyday" in said["text"]
    assert not any(ch.isdigit() for ch in said["text"])      # never adds a "said" amount
    kinds = [a["kind"] for a in out["audit"]]
    assert kinds.count("validation") == 2 and "confirm.edited" in kinds
    group = json.loads(again["group_canonical"])["transactions"][0]
    assert "default_source" not in group["flags"]


@pytest.mark.parametrize("value", [
    {"status": "edit", "seq": 1, "account_id": "ACC-2001"},   # another customer's account
    {"status": "edit", "seq": 7, "account_id": "ACC-1002"},   # no such payment
])
def test_a_bad_edit_cancels_without_redrafting(bank, stub_llm, value):
    _, cfg = to_confirm_gate(stub_llm)
    out = graph.invoke(Command(resume=value), cfg)
    assert out["outcome"] == "cancelled" and "__interrupt__" not in out
    assert [a["payload"]["text"] for a in out["audit"] if a["kind"] == "answer"][-1] == \
        copy.REVIEW_EDITS_EXHAUSTED


def test_edits_are_capped(bank, stub_llm):
    _, cfg = to_confirm_gate(stub_llm, *[REPORT] * MAX_REVIEW_EDITS)
    for i in range(MAX_REVIEW_EDITS):
        acct = "ACC-1002" if i % 2 == 0 else "ACC-1001"
        out = graph.invoke(Command(resume={"status": "edit", "seq": 1, "account_id": acct}), cfg)
        assert "__interrupt__" in out
    out = graph.invoke(Command(resume={"status": "edit", "seq": 1, "account_id": "ACC-1003"}), cfg)
    assert out["outcome"] == "cancelled"
