"""Stream layer — drive the LangGraph graph and emit Server-Sent Events.

Consumes `graph.stream(...)` events and translates each into a small JSON SSE
event the browser renders:

    {"event": "turn",           "num": 3, "user_msg": "..."}       # activity-log group
    {"event": "thinking_open",  "agent": "...", "title": "..."}
    {"event": "thinking_close", "agent": "...", "title": "...", "duration": 1.2, "summary": "..."}
    {"event": "routed",         "agent": "...", "label": "...", "color": "..."}
    {"event": "a2a",            "role": "...", "from": "...", "to": "...", "content": "..."}
    {"event": "tool_call",      "name": "...", "args": {...}, "call_id": "..."}
    {"event": "tool_result",    "name": "...", "result": "...", "call_id": "..."}
    {"event": "<gate type>",    ...interrupt payload...}           # a gate is waiting
    {"event": "answer",         "text": "..."}                     # final reply bubble
    {"event": "error",          "text": "..."}
    {"event": "done"}                                              # stream finished

Per-session state (turn counter + the thread_id of a paused gate) is kept
server-side in SESSIONS so a resume hits the same thread. The graph itself knows
nothing about HTTP or SSE.
"""

from __future__ import annotations

import json
import time
import uuid

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from dcta.core import audit as audit_chain
from dcta.graph import AGENTS, MAX_TOOL_ROUNDS, graph, new_state
from dcta.utils import config
from dcta.utils.telemetry import RunMetrics, token_counter

# Business-language copy for the per-node thinking blocks: (pending title, done title).
_THINK_COPY = {
    "intake": ("Receiving your instruction…", "Instruction received"),
    "shield": ("Screening your message…", "Message screened"),
    "parser": ("Understanding your instruction…", "Instruction broken down"),
    "resolve": ("Looking up your payees and accounts…", "Draft prepared"),
    "validator": ("Checking the draft against what you said…", "Check complete"),
    "risk": ("Running safety checks…", "Safety checks done"),
    "settle": ("Confirming with the bank…", "Payment confirmed"),
}
_THINK_MAX_CHARS = 700

# Audit kinds that also reach the browser as their own SSE event (card / receipt).
_CARD_EVENTS = {"frozen": "frozen", "denied": "denied", "blocked": "blocked",
                "shield": "shield", "settle": "receipt"}

# The node a fresh turn enters at. Its thinking block is opened up front, because
# graph events only fire when a node COMPLETES.
_ENTRY_NODE = "intake"


# ── server-side session store ───────────────────────────────────────────────
# Keyed by session id. Holds the turn counter and the active gate's thread_id so
# a paused turn can be resumed on a later request.
SESSIONS: dict[str, dict] = {}


def _session(session_id: str) -> dict:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = {"thread_id": None, "pending": False,
                                "turn": 0, "customer_id": ""}
    SESSIONS[session_id]["session_id"] = session_id
    return SESSIONS[session_id]


def reset_session(session_id: str) -> dict:
    """Drop this session's conversation state, returning what was discarded.

    Clearing `thread_id`/`pending` is what abandons any half-finished gate, so the
    next turn starts clean rather than trying to resume a dead interrupt. The
    graph's own checkpointer is untouched — a new thread_id is minted per turn
    anyway (see _config).
    """
    sess = SESSIONS.pop(session_id, None) or {}
    # The turn counter survives: trace ids are "session:turn", and reusing a
    # number would file two different turns under one trace in the audit chain.
    if sess.get("turn"):
        _session(session_id)["turn"] = sess["turn"]
    return {"cleared": True, "turns_dropped": sess.get("turn", 0),
            "had_pending_gate": bool(sess.get("pending"))}


def agent_chain_path():
    return config.ARTIFACTS_DIR / "audit" / "agent.jsonl"


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


def _summary(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _THINK_MAX_CHARS else text[:_THINK_MAX_CHARS].rstrip() + " …"


def _recursion_limit() -> int:
    return MAX_TOOL_ROUNDS * max(len(AGENTS), 1) + 16


def _config():
    return {
        "configurable": {"thread_id": uuid.uuid4().hex},
        "recursion_limit": _recursion_limit(),
    }


def _explain_error(exc: Exception) -> str:
    """Human-readable text for a graph-killing exception.

    The raw repr of an SDK error is close to useless: a bare "InternalServerError:
    Internal Server Error" names neither the endpoint that failed nor anything you
    can act on. So we name the configured endpoint and say what class of failure
    it is. metrics.error keeps the raw string — this only changes what the human
    reads. Every message says the same thing about money: nothing moved.
    """
    raw = f"{type(exc).__name__}: {exc}"

    # Tool-round exhaustion is not an endpoint failure and must not read like one.
    # It means the agent kept calling tools without converging.
    if type(exc).__name__ == "GraphRecursionError" or "recursion limit" in str(exc).lower():
        return ("The assistant kept looking things up without settling on an answer, "
                "so this turn was stopped. No money has moved. Try rephrasing the "
                "instruction, or give one instruction at a time.")

    try:
        from dcta.utils import config
        endpoint = config.BASE_URL
        retries = config.MAX_RETRIES
    except Exception:  # noqa: BLE001 — never fail while reporting a failure
        return raw

    status = None
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            status = val
            break

    if status in (500, 502, 503, 504):
        return (f"{raw} — the LLM endpoint ({endpoint}) returned HTTP {status} and "
                f"was still failing after {retries} retries. No money has moved; "
                f"retry the turn.")
    if status in (401, 403):
        return (f"{raw} — the LLM endpoint ({endpoint}) rejected the credential. "
                f"Check DCTA_LLM_API_KEY in .env. No money has moved.")
    if isinstance(exc, (ConnectionError, OSError)) or "name or service not known" in str(exc).lower():
        return (f"{raw} — could not reach the LLM endpoint ({endpoint}). Check "
                f"DCTA_LLM_BASE_URL. No money has moved.")
    # A local programming error never touched the network, so do NOT blame the
    # endpoint — that sends the diagnosis at the provider when the cause is ours.
    if isinstance(exc, (KeyError, AttributeError, IndexError, TypeError, ValueError)):
        return (f"{raw} — this is an internal error in the assistant, not the LLM "
                f"endpoint. The turn was stopped and no money has moved.")
    return f"{raw} — while calling the LLM endpoint ({endpoint}). No money has moved."


def _drive(stream_iter, sess, think_agent="", is_resume=False):
    """Core event loop shared by a fresh chat turn and a gate resume.

    Yields SSE strings. Tracks the currently-open thinking block so it can be
    sealed (thinking_close) when the next node takes over / the run ends.
    The actual loop is in _drive_events; this wrapper turns any graph exception
    (e.g. an LLM auth/gateway error) into a clean SSE `error` event so the
    browser never just sees a dropped connection.

    Also records one operational-metrics row per stream: the token counter is
    reset here so it sums only this stream's LLM calls, and the row is written in
    `finally`, i.e. on EVERY exit path — including the browser disconnecting
    mid-stream, which throws GeneratorExit into this generator. Monitoring must not
    be the thing that loses data when the feature under it breaks.
    """
    # The eval harness drives this same function; its sessions carry golden-case
    # labels so its rows land in the same runs.csv, tagged.
    labels = sess.get("eval") or {}
    metrics = RunMetrics(
        source="eval" if labels else "server",
        session_id=sess.get("session_id", ""),
        customer_id=sess.get("customer_id", ""),
        turn=sess.get("turn", 0),
        is_resume=is_resume,
        case_id=labels.get("case_id", ""),
        trial=labels.get("trial", 1),
    )
    token_counter.reset()   # count only this stream's LLM calls
    think_t0 = time.time() if think_agent else None
    # Shared, mutable view of the currently-open thinking block. _drive_events
    # advances this as nodes take over; on a graph exception the except clause
    # below reads it to seal the block that was ACTUALLY open — not the stale
    # initial `think_agent`, which would mislabel the failing node's block.
    cur = {"agent": think_agent, "t0": think_t0}

    def close_block(agent, t0):
        if not agent:
            return None
        ev = {"event": "thinking_close", "agent": agent,
              "title": _THINK_COPY.get(agent, ("", agent))[1]}
        if t0 is not None:
            ev["duration"] = round(time.time() - t0, 1)
        return ev

    try:
        yield from _drive_events(stream_iter, sess, think_agent, think_t0,
                                 close_block, cur, metrics)
    except Exception as e:  # surface a graceful error instead of dropping the SSE
        ev = close_block(cur["agent"], cur["t0"])
        if ev:
            yield _sse(ev)
        sess["pending"] = False
        sess["thread_id"] = None
        metrics.error = f"{type(e).__name__}: {e}"
        yield _sse({"event": "error", "text": _explain_error(e)})
        yield _sse({"event": "done"})
    finally:
        metrics.write()   # one row per stream, on every exit path


def _drive_events(stream_iter, sess, think_agent, think_t0, close_block, cur, metrics):
    """Inner stream loop (separated so _drive can wrap it in one try/except).

    `cur` is the shared {agent, t0} view of the open thinking block; it is kept in
    sync with the local think_agent/think_t0 so _drive can seal the right block if
    the graph raises mid-run. `metrics` is the RunMetrics row being tallied.
    """
    trace_id = f"{sess.get('session_id', '')}:{sess.get('turn', 0)}"
    final_answer = ""
    interrupted = False
    gate: dict = {}
    agent_summary = ""   # rolling "conclusion" for the current node's thinking block

    def close_with_summary(agent, t0, summ):
        ev = close_block(agent, t0)
        if ev and summ:
            ev["summary"] = _summary(summ)
        return ev

    for event in stream_iter:
        # LangGraph surfaces an interrupt under the special '__interrupt__' key.
        if "__interrupt__" in event:
            interrupted = True
            metrics.interrupted = True
            intr = event["__interrupt__"]
            payload = getattr(intr[0], "value", {}) if intr else {}
            gate = payload if isinstance(payload, dict) else {"value": payload}
            break

        for node_name, node_output in event.items():
            if not isinstance(node_output, dict):
                continue

            base = node_name[:-4] if node_name.endswith("_llm") else node_name

            # Metrics: an *_llm node output is one agent-node visit. Re-entering
            # the same llm node (after a tool loop) is what loop_count counts.
            if node_name.endswith("_llm"):
                metrics.visit_node(base)

            # Audit completeness: every node that runs is bound by the audit
            # convention, so tally the node here and how many entries it logged. A
            # node that appears in nodes_ran but never in nodes_logged is a gap in
            # the trail — that is the KPI, not the raw entry count.
            metrics.saw_node(node_name)
            metrics.add_audit_entries(node_name, len(node_output.get("audit", []) or []))

            # Open a new thinking block when a new node appears.
            if base in _THINK_COPY and base != think_agent:
                ev = close_with_summary(think_agent, think_t0, agent_summary)
                if ev:
                    yield _sse(ev)
                agent_summary = ""
                think_agent, think_t0 = base, time.time()
                cur["agent"], cur["t0"] = think_agent, think_t0
                yield _sse({"event": "thinking_open", "agent": base,
                            "title": _THINK_COPY[base][0]})

            for entry in node_output.get("audit", []):
                kind = entry.get("kind", "")
                payload = entry.get("payload", {})
                # The ONLY place the agent's audit chain is written (P6). Nodes
                # just return entries, so a gate re-running a node on resume
                # cannot write anything twice.
                audit_chain.append(agent_chain_path(), trace_id=trace_id, actor=node_name,
                                   kind=kind, payload=payload)
                if kind == "tool_call":
                    metrics.add_tool_call(payload.get("name", ""), payload.get("args", {}))
                    yield _sse({"event": "tool_call", "name": payload.get("name", ""),
                                "args": payload.get("args", {}),
                                "call_id": payload.get("call_id", "")})
                elif kind == "a2a":
                    role = payload.get("role", "")
                    if role == "routed":
                        # Routing-chain hop for the activity log (agent badge).
                        ag = AGENTS.get(payload.get("agent", ""))
                        yield _sse({"event": "routed",
                                    "agent": payload.get("agent", ""),
                                    "label": ag.label if ag else payload.get("to", ""),
                                    "color": ag.color if ag else "#6b7280"})
                    else:
                        # system / user / assistant message card.
                        yield _sse({"event": "a2a",
                                    "role": role,
                                    "from": payload.get("from", ""),
                                    "to": payload.get("to", ""),
                                    "content": payload.get("content", ""),
                                    "is_final": bool(payload.get("is_final"))})
                    if payload.get("is_final") and payload.get("content"):
                        agent_summary = payload["content"]
                elif kind in _CARD_EVENTS:
                    yield _sse({"event": _CARD_EVENTS[kind], **payload})
                elif kind == "answer":
                    # The only source of the customer-facing reply: an `answer`
                    # entry a node wrote deliberately. An agent's raw AIMessage is
                    # model text and is never promoted to the bubble.
                    final_answer = payload.get("text", "")
                    agent_summary = payload.get("text", "")

            for m in node_output.get("messages", []):
                if isinstance(m, ToolMessage):
                    result_str = (m.content if isinstance(m.content, str)
                                  else json.dumps(m.content, ensure_ascii=False, default=str))
                    yield _sse({"event": "tool_result", "name": getattr(m, "name", ""),
                                "result": result_str,
                                "call_id": getattr(m, "tool_call_id", "")})
                elif isinstance(m, AIMessage) and m.content and not getattr(m, "tool_calls", None):
                    if base in AGENTS:
                        agent_summary = m.content

    if interrupted:
        ev = close_with_summary(think_agent, think_t0, agent_summary)
        if ev:
            yield _sse(ev)
        sess["pending"] = True
        # The gate's own `type` names the SSE event, so the front-end can tell
        # which kind of gate is waiting without the driver knowing each one.
        body = {k: v for k, v in gate.items() if k != "type"}
        yield _sse({"event": gate.get("type", "interrupt"), **body})
        return

    # Ran to END.
    ev = close_with_summary(think_agent, think_t0, agent_summary or final_answer)
    if ev:
        yield _sse(ev)
    sess["pending"] = False
    sess["thread_id"] = None
    yield _sse({"event": "answer", "text": final_answer or "(no response)"})
    yield _sse({"event": "done"})


def stream_chat(session_id: str, message: str, channel: str = "text",
                asr: dict | None = None):
    """SSE generator for a fresh customer message."""
    if not message.strip():
        yield _sse({"event": "done"})
        return

    sess = _session(session_id)
    if not sess.get("customer_id"):
        from dcta.core.copy import NO_CUSTOMER
        yield _sse({"event": "error", "text": NO_CUSTOMER})
        yield _sse({"event": "done"})
        return
    run_cfg = _config()
    # A new message abandons any gate still waiting: the new thread_id below means
    # the old interrupt can no longer be resumed.
    sess["pending"] = False
    sess["thread_id"] = run_cfg["configurable"]["thread_id"]
    sess["turn"] = sess.get("turn", 0) + 1

    state = new_state(customer_id=sess.get("customer_id", ""), session_id=session_id,
                      user_text=message, channel=channel, asr=asr, turn=sess["turn"])

    # Open a new activity-log turn group.
    yield _sse({"event": "turn", "num": sess["turn"], "user_msg": message})
    # Pre-open the entry node's block (events only fire on node completion).
    yield _sse({"event": "thinking_open", "agent": _ENTRY_NODE,
                "title": _THINK_COPY[_ENTRY_NODE][0]})

    stream = graph.stream(state, config=run_cfg)
    yield from _drive(stream, sess, think_agent=_ENTRY_NODE, is_resume=False)


def stream_resume(session_id: str, value: dict):
    """SSE generator for resuming a paused gate with `value`."""
    sess = _session(session_id)
    if not sess.get("thread_id") or not sess.get("pending"):
        # No pending interrupt — nothing to resume.
        yield _sse({"event": "done"})
        return

    run_cfg = {"configurable": {"thread_id": sess["thread_id"]},
              "recursion_limit": _recursion_limit()}
    stream = graph.stream(Command(resume=value), config=run_cfg)
    yield from _drive(stream, sess, is_resume=True)
