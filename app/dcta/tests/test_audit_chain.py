"""A10: two hash chains — agent and gateway — linked by trace_id and group_hash."""
import json

from dcta.core import audit
from dcta.core.schema import canonical
from dcta.eval.device import SimulatedDevice
from dcta.server import stream

EX1 = "Transfer 500 dollars to my mom and use the rest to buy Apple shares"
EX1_PARSED = {"category": "transaction", "transactions": [
    {"intent": "transfer", "evidence": "Transfer 500 dollars to my mom", "target_mention": "my mom",
     "amount": {"kind": "absolute", "value": "500", "said_as": "500 dollars"}},
    {"intent": "buy_security", "evidence": "use the rest to buy Apple shares",
     "target_mention": "Apple", "amount": {"kind": "remainder", "after": [0]}}], "uncertain": []}
REPORT = {"transactions": [{"seq": s, "recipient": "match", "amount": "match", "asset": "match",
                            "source": "not_stated"} for s in (1, 2)], "extra": [], "missing": []}


def _tamper(path, seq):
    lines = path.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[seq - 1])
    e["payload"]["tampered"] = True
    lines[seq - 1] = json.dumps(e)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_chain_detects_the_first_broken_entry(tmp_path):
    path = tmp_path / "chain.jsonl"
    for i in range(5):
        audit.append(path, trace_id="t", actor="test", kind="k", payload={"i": i})
    assert audit.verify(path) is None
    _tamper(path, 3)
    assert audit.verify(path) == 3


def test_dropped_entry_is_detected(tmp_path):
    path = tmp_path / "chain.jsonl"
    for i in range(4):
        audit.append(path, trace_id="t", actor="test", kind="k", payload={"i": i})
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    assert audit.verify(path) == 2


def test_full_transaction_leaves_two_linked_intact_chains(bank, stub_llm):
    # Agent side: one turn through the real graph via the stream driver.
    stub_llm(json.dumps(EX1_PARSED), json.dumps(REPORT))
    stream.SESSIONS["s-audit"] = {"customer_id": "C001", "turn": 0}
    list(stream.stream_chat("s-audit", EX1))
    agent_path = stream.agent_chain_path()
    agent = audit.read(agent_path)
    resolved = next(e for e in agent if e["kind"] == "draft.resolved")["payload"]
    trace_id = agent[0]["trace_id"]

    # Customer side: the device signs what the gateway issued; the OOB code clears step-up.
    device = SimulatedDevice()
    key_id = bank.post("/v1/devices", json={"customer_id": "C001",
                                            "jwk": device.jwk()}).json()["key_id"]
    ch = bank.post("/v1/challenges", json={"group": canonical(resolved["group"]),
                                           "key_id": key_id, "trace_id": trace_id}).json()
    otp = bank.get("/v1/oob/pending").json()[0]["otp"]
    receipt = bank.post("/v1/transactions", json={
        "challenge_id": ch["challenge_id"], "signing_input": ch["signing_input"],
        "signature": device.sign(ch["signing_input"]), "otp": otp}).json()

    gateway_path = bank.app.state.audit_path
    gw = audit.read(gateway_path)
    assert audit.verify(agent_path) is None and audit.verify(gateway_path) is None

    # The five records the brief names are all there.
    assert {"input.raw", "draft.parsed", "draft.resolved", "validation"} <= {e["kind"] for e in agent}
    assert any(e["kind"] == "signature" and e["payload"]["verified"] for e in gw)
    # Drafted, signed and posted are the same draft.
    post = next(e for e in gw if e["kind"] == "ledger.post")
    assert resolved["group_hash"] == receipt["group_hash"] == post["payload"]["group_hash"]
    assert receipt["audit_hash"] == post["hash"] and post["trace_id"] == trace_id

    _tamper(agent_path, 4)
    assert audit.verify(agent_path) == 4
