"""The stream driver: graph events in, SSE frames out, one metrics row per stream.

Feeds the driver hand-built event iterators (so every branch is reachable without
a model) and, once, the real skeleton graph end to end. No LLM / network.
"""
import csv
import json

from langchain_core.messages import AIMessage, ToolMessage

from dcta.server import stream
from dcta.utils.telemetry import metrics


def _events(frames):
    return [json.loads(f[len("data: "):]) for f in frames]


def _names(frames):
    return [e["event"] for e in _events(frames)]


_SMALLTALK = '{"category": "smalltalk", "transactions": [], "uncertain": []}'


def _login(sid):
    stream.SESSIONS[sid] = {"customer_id": "C001", "turn": 0}


def test_no_customer_means_no_turn():
    events = _events(stream.stream_chat("s-anon", "hello"))
    assert [e["event"] for e in events] == ["error", "done"]


def _sess():
    return {"session_id": "s1", "customer_id": "C001", "turn": 1,
            "thread_id": "t1", "pending": False}


def _rows():
    return list(csv.DictReader(metrics.RUNS_CSV.open(encoding="utf-8")))


class _Interrupt:
    def __init__(self, value):
        self.value = value


def test_chat_turn_runs_the_graph_end_to_end(stub_llm):
    from dcta.core import copy

    stub_llm(_SMALLTALK)
    _login("s-e2e")
    frames = list(stream.stream_chat("s-e2e", "Hello there"))
    events = _events(frames)

    assert events[0] == {"event": "turn", "num": 1, "user_msg": "Hello there"}
    assert events[-1] == {"event": "done"}
    answer = next(e for e in events if e["event"] == "answer")
    assert answer["text"] == copy.REPLIES["smalltalk"]

    # Every thinking block that opens is closed.
    names = [e["event"] for e in events]
    assert names.count("thinking_open") == names.count("thinking_close") == 3

    row = _rows()[-1]
    assert row["source"] == "server"
    assert row["session_id"] == "s-e2e"
    assert row["turn"] == "1"
    assert row["audit_complete"] == "1"
    assert row["nodes_total"] == "5"      # intake, shield, parser, parser_llm, parse_check
    assert row["error"] == ""


def test_empty_message_is_a_noop():
    assert _names(stream.stream_chat("s-empty", "   ")) == ["done"]
    assert not metrics.RUNS_CSV.exists()      # nothing ran, nothing recorded


def test_turn_counter_advances_per_session(stub_llm):
    stub_llm(*[_SMALLTALK] * 2)
    _login("s-turns")
    list(stream.stream_chat("s-turns", "one"))
    events = _events(stream.stream_chat("s-turns", "two"))
    assert events[0]["num"] == 2


def test_tool_traffic_reaches_the_activity_log():
    events_in = [
        {"parser": {"audit": [
            {"kind": "a2a", "payload": {"role": "routed", "to": "Parser", "agent": "parser"}}]}},
        {"parser_llm": {
            "audit": [{"kind": "tool_call",
                       "payload": {"name": "list_payees", "args": {"query": "John"}, "call_id": "c1"}}],
            "messages": [AIMessage(content="", tool_calls=[
                {"name": "list_payees", "args": {"query": "John"}, "id": "c1"}])]}},
        {"tools": {"messages": [ToolMessage(content='[{"id": "PAY-1"}]', name="list_payees",
                                            tool_call_id="c1")]}},
        {"parser_llm": {"audit": [{"kind": "a2a", "payload": {
            "role": "assistant", "from": "Parser", "to": "Pipeline",
            "content": "{}", "is_final": True}}]}},
    ]
    sess = _sess()
    events = _events(stream._drive(iter(events_in), sess))

    call = next(e for e in events if e["event"] == "tool_call")
    result = next(e for e in events if e["event"] == "tool_result")
    assert call["name"] == "list_payees" and call["call_id"] == "c1"
    assert result["call_id"] == "c1" and result["result"] == '[{"id": "PAY-1"}]'
    assert any(e["event"] == "routed" for e in events)

    row = _rows()[-1]
    assert row["tool_calls"] == "1"
    assert row["tools_called"] == "list_payees"
    assert row["loop_count"] == "1"           # parser_llm re-entered after the tool
    assert row["path"] == "parser"
    # The built-in tools node is exempt, so the trail still reads as complete.
    assert row["audit_complete"] == "1"


def test_model_text_is_never_promoted_to_the_answer():
    """An agent's raw AIMessage is model text. Only an `answer` entry a node wrote
    on purpose may reach the customer's bubble."""
    events_in = [{"parser_llm": {
        "audit": [{"kind": "a2a", "payload": {"role": "assistant", "from": "Parser",
                                              "to": "Pipeline", "content": "RAW MODEL TEXT",
                                              "is_final": True}}],
        "messages": [AIMessage(content="RAW MODEL TEXT")]}}]
    events = _events(stream._drive(iter(events_in), _sess()))

    answer = next(e for e in events if e["event"] == "answer")
    assert "RAW MODEL TEXT" not in answer["text"]


def test_interrupt_emits_the_gates_own_event_and_marks_pending():
    events_in = [
        {"intake": {"audit": [{"kind": "input.raw", "payload": {"text": "hi"}}]}},
        {"__interrupt__": [_Interrupt({"type": "clarify", "question": "Which John?",
                                       "options": [{"id": "PAY-1"}, {"id": "PAY-2"}]})]},
    ]
    sess = _sess()
    events = _events(stream._drive(iter(events_in), sess, think_agent="intake"))

    gate = events[-1]
    assert gate["event"] == "clarify"
    assert gate["question"] == "Which John?"
    assert len(gate["options"]) == 2
    assert "done" not in [e["event"] for e in events]   # the turn is paused, not over
    assert sess["pending"] is True
    assert _rows()[-1]["interrupted"] == "1"


def test_graph_exception_becomes_a_clean_error_and_still_writes_a_row():
    def boom():
        yield {"intake": {"audit": [{"kind": "input.raw", "payload": {"text": "hi"}}]}}
        raise KeyError("payee")

    sess = _sess()
    sess["pending"] = True
    events = _events(stream._drive(boom(), sess, think_agent="intake"))
    names = [e["event"] for e in events]

    assert names[-2:] == ["error", "done"]
    assert "thinking_close" in names                   # the open block was sealed
    error = events[-2]["text"]
    assert "internal error" in error                   # not blamed on the endpoint
    assert "no money has moved" in error.lower()
    assert sess["pending"] is False and sess["thread_id"] is None
    assert "KeyError" in _rows()[-1]["error"]


def test_row_is_written_when_the_browser_disconnects_mid_stream():
    def slow():
        yield {"intake": {"audit": [{"kind": "input.raw", "payload": {"text": "hi"}}]}}
        yield {"echo": {"audit": [{"kind": "answer", "payload": {"text": "x"}}]}}

    gen = stream._drive(slow(), _sess(), think_agent="intake")
    next(gen)
    gen.close()                                        # GeneratorExit, as on a dropped SSE

    assert len(_rows()) == 1


def test_resume_without_a_pending_gate_is_a_noop():
    assert _names(stream.stream_resume("s-none", {"choice_id": "PAY-1"})) == ["done"]


def test_reset_drops_the_session(stub_llm):
    stub_llm(_SMALLTALK)
    _login("s-reset")
    list(stream.stream_chat("s-reset", "hello"))
    out = stream.reset_session("s-reset")
    assert out["cleared"] is True and out["turns_dropped"] == 1
    left = stream.SESSIONS["s-reset"]
    assert not left["customer_id"] and not left["pending"] and left["thread_id"] is None
    assert left["turn"] == 1        # kept, so the next turn's trace id is new


def test_recursion_error_reads_as_what_happened():
    class GraphRecursionError(Exception):
        pass

    text = stream._explain_error(GraphRecursionError("Recursion limit of 24 reached"))
    assert "GraphRecursionError" not in text
    assert "no money has moved" in text.lower()
