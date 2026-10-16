"""Billie — the compiled LangGraph graph. The only graph definition in the project.

Public surface used by callers (FastAPI server, eval harness):
    graph            — the compiled StateGraph (entry point: graph.stream / graph.invoke)
    DctaState        — the graph state TypedDict
    AGENTS           — dict[str, AgentDef] of registered LLM agents
    chat             — text-only LLM helper for one-shot JSON calls
    new_state(...)   — build a fresh DctaState for a turn

Dependency direction (one-way): core, utils → graph → server. The graph imports
only from dcta.core / dcta.utils and langgraph/langchain; it never imports the
server, and nothing in this package imports the gateway.
"""

import html
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from operator import add
from pathlib import Path
from typing import Annotated, Callable

from typing_extensions import TypedDict

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import ValidationError

from dcta.core import copy, shield
from dcta.core.crosscheck import crosscheck
from dcta.core.resolve import resolve
from dcta.core.risk import evaluate
from dcta.core.schema import ParsedIntent, ValidatorReport, canonical
from dcta.utils import bank_client, config
from dcta.utils.llm import make_llm
from dcta.utils.tools import TOOLS_BY_NAME

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# Budget for one agent's tool loop. Not enforced per-agent directly: it scales the
# graph-wide `recursion_limit` (see server/stream.py), which is the only ceiling
# LangGraph actually applies. Hitting it does not degrade gracefully — LangGraph
# raises GraphRecursionError and the whole turn is lost — so leave headroom.
MAX_TOOL_ROUNDS = 4

# A parser reply that fails validation gets this many repair attempts (each one
# hands the model its own error), then the turn ends on a fixed message.
PARSE_RETRIES = 1

# Questions asked in one turn before giving up (DESIGN §4.2).
MAX_CLARIFY_ROUNDS = 2

# Source-account changes the customer may make on one review screen.
MAX_REVIEW_EDITS = 3


# ══════════════════════════════════════════════════════════════════════════
# LLM clients — built on first use, so importing the graph needs no credential.
#
# temperature=0 everywhere. The output feeds a flow that ends in moving money;
# determinism is worth more than phrasing variety.
# ══════════════════════════════════════════════════════════════════════════
@lru_cache(maxsize=None)
def get_llm(model: str | None = None):
    return make_llm(temperature=0, model=model)


def chat(messages_dicts: list[dict], model: str | None = None) -> str:
    """Text-only, tool-less LLM call. Returns the reply content ('' if none)."""
    lc_messages = []
    for m in messages_dicts:
        role, content = m["role"], m.get("content", "")
        if role == "system":
            lc_messages.append(SystemMessage(content=content))
        elif role == "user":
            lc_messages.append(HumanMessage(content=content))
        else:
            lc_messages.append(AIMessage(content=content))
    resp = get_llm(model).invoke(lc_messages)
    return resp.content or ""


# ══════════════════════════════════════════════════════════════════════════
# Agent definitions — loaded from skills/
#
# Each agent's system prompt lives in skills/<agent>/skill.md, with its display
# label and colour in config.json. Prompts are edited THERE, never in a .py file,
# and are loaded whole — nothing is interpolated into them.
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class AgentDef:
    name: str
    label: str
    color: str
    system: str
    tool_names: list[str]
    model: str | None = None

    @property
    def tools(self) -> list:
        return [TOOLS_BY_NAME[n] for n in self.tool_names if n in TOOLS_BY_NAME]

    @property
    def bound_llm(self):
        llm = get_llm(self.model)
        return llm.bind_tools(self.tools) if self.tools else llm

    def bound_llm_excluding(self, *exclude: str):
        """Same as bound_llm but with the named tools withheld from the LLM. Used to
        hard-remove a tool from a specific flow — the model can't call what it
        can't see."""
        drop = set(exclude)
        tools = [TOOLS_BY_NAME[n] for n in self.tool_names
                 if n in TOOLS_BY_NAME and n not in drop]
        llm = get_llm(self.model)
        return llm.bind_tools(tools) if tools else llm


def _load_skill(name: str) -> tuple[str, str, str]:
    cfg = json.loads((SKILLS_DIR / name / "config.json").read_text(encoding="utf-8"))
    system = (SKILLS_DIR / name / "skill.md").read_text(encoding="utf-8").strip()
    return cfg["label"], cfg["color"], system


# Tool access list per agent — controls what each agent can call.
#
# This allowlist IS the boundary, not the prompt. Every name here is a read-only
# lookup; nothing that writes to the ledger exists in this process, so no entry
# could grant it. The validator has no tools at all.
_AGENT_TOOLS: dict[str, list[str]] = {
    "parser": [
        "list_accounts", "list_payees", "list_bills", "get_holdings", "find_instrument",
    ],
    "validator": [],
}

# Per-agent model override; an agent not listed uses config.MODEL.
_AGENT_MODEL: dict[str, str] = {
    "validator": config.VALIDATOR_MODEL,
}

AGENTS: dict[str, AgentDef] = {}
for _name, _tool_names in _AGENT_TOOLS.items():
    try:
        _label, _color, _system = _load_skill(_name)
        AGENTS[_name] = AgentDef(
            name=_name, label=_label, color=_color,
            system=_system, tool_names=_tool_names,
            model=_AGENT_MODEL.get(_name),
        )
    except FileNotFoundError as e:
        print(f"  [SKIP] {_name} - missing file: {e}")


# ── LogEntry (one audit record) ───────────────────────────────────────────
# Audit convention: EVERY node appends at least one LogEntry to state["audit"].
# Nodes only RETURN entries — they never write the trail themselves, because a
# node paused by interrupt() re-runs from the top on resume and a write made
# before the pause would happen twice. Persisting is the stream driver's job.
@dataclass
class LogEntry:
    kind: str
    payload: dict


# ══════════════════════════════════════════════════════════════════════════
# DctaState
# ══════════════════════════════════════════════════════════════════════════
class DctaState(TypedDict):
    messages: Annotated[list, add_messages]
    customer_id: str     # session principal — set by the server, never by the model
    session_id: str
    turn: int
    channel: str         # 'text' | 'voice'
    raw_text: str        # the utterance / transcript exactly as received
    clean_text: str      # after input sanitising
    asr: dict            # alternative transcripts + confidences (voice only)
    shield: dict         # {verdict, reasons}
    parsed: dict         # latest ParsedIntent (model output, untrusted)
    parse_retries: int
    group: dict          # latest TransactionGroup (resolved by code)
    group_hash: str
    pins: dict           # clarify answers by slot ("0.target" → "PAY-0001"); read by resolve
    clarify: dict        # the question resolve wants asked: {question, slot, options}
    transcript: list     # authorisation transcript: utterance + each clarify Q/A
    clarify_rounds: int
    review_edits: int    # source accounts changed on the review screen this turn
    validation: dict
    risk: dict
    decision: dict       # value a gate was resumed with
    outcome: str         # posted | cancelled | frozen | denied | blocked | replied | error
    audit: Annotated[list, add]
    current_agent: str   # agent whose tool loop is active (tool-return routing)


def new_state(customer_id: str = "", session_id: str = "", user_text: str = "",
              channel: str = "text", asr: dict | None = None, turn: int = 0) -> DctaState:
    """Build a fresh DctaState for a single turn so callers don't hand-build the
    (large) initial dict."""
    return {
        "messages": [],
        "customer_id": customer_id,
        "session_id": session_id,
        "turn": turn,
        "channel": channel or "text",
        "raw_text": user_text,
        "clean_text": "",
        "asr": asr or {},
        "shield": {},
        "parsed": {},
        "parse_retries": 0,
        "group": {},
        "group_hash": "",
        "pins": {},
        "clarify": {},
        "transcript": [],
        "clarify_rounds": 0,
        "review_edits": 0,
        "validation": {},
        "risk": {},
        "decision": {},
        "outcome": "",
        "audit": [],
        "current_agent": "",
    }


# ══════════════════════════════════════════════════════════════════════════
# Tolerant JSON parsing for one-shot model replies
# ══════════════════════════════════════════════════════════════════════════
def _first_json_object(s: str) -> str:
    """Return the first brace-balanced {...} substring in s, or '' if none. Keeps
    us from tripping on leading/trailing junk the model occasionally adds."""
    start = s.find("{")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return ""


# Zero-width characters some endpoints prepend to a reply; invisible, and enough
# to break both json.loads and string comparison.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))


def parse_json_reply(raw: str) -> dict | None:
    """Parse a model reply that is supposed to be one JSON object.

    Tolerates a code fence, zero-width characters, and stray text around the
    object: tries a direct parse, then the first balanced {...} block. Returns
    None on failure — the caller must then fall back to a FIXED message. The raw
    model text is never something to show the customer.
    """
    text = (raw or "").translate(_ZERO_WIDTH).strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    for candidate in (text, _first_json_object(text)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ══════════════════════════════════════════════════════════════════════════
# Agent node factory
#
# Each LLM agent is two nodes: `<name>` seeds the system prompt + this turn's
# input and records itself as current_agent; `<name>_llm` calls the model and is
# what a tool loop re-enters.
# ══════════════════════════════════════════════════════════════════════════
def make_agent_node(agent_name: str, build_input: Callable[[dict], str] | None = None):
    """`build_input(state) -> str` produces the user message for this agent. The
    default hands over the sanitised utterance (falling back to the raw one)."""
    def agent_node(state: DctaState) -> dict:
        agent = AGENTS[agent_name]
        if build_input is not None:
            question = build_input(state)
        else:
            question = state.get("clean_text") or state.get("raw_text", "")

        audit_entries = [
            LogEntry("a2a", {"role": "system", "from": "System", "to": agent.label, "content": agent.system}),
            LogEntry("a2a", {"role": "user", "from": "Pipeline", "to": agent.label, "content": question}),
            LogEntry("a2a", {"role": "routed", "from": "Pipeline", "to": agent.label, "agent": agent_name}),
        ]
        return {
            "messages": [SystemMessage(content=agent.system), HumanMessage(content=question)],
            "audit": [vars(e) for e in audit_entries],
            "current_agent": agent_name,   # tool results route back to this agent's LLM node
        }
    agent_node.__name__ = agent_name
    return agent_node


def _agent_segment(messages: list) -> list:
    """The messages belonging to the CURRENTLY-RUNNING agent's own turn.

    `messages` uses the `add_messages` reducer, so it only ever APPENDS: every
    earlier agent's system prompt, tool traffic and answer stays in the list.
    Sending all of it would do two wrong things — bloat the request, and show one
    agent another agent's working. The second matters here: the validator is only
    independent if it never sees what the parser was given or what it said.

    Each agent node re-seeds its own SystemMessage + HumanMessage on entry (see
    make_agent_node), so that SystemMessage is a clean segment boundary: everything
    from the LAST one onward is this agent's own context. Slicing there is a
    SEMANTIC cut, not a positional one, which is what keeps every tool_call paired
    with its ToolMessage — an orphaned ToolMessage is a hard 400.

    Anything one agent must hand to the next travels through state fields, which
    the receiving agent's build_input() puts into its own question.
    """
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], SystemMessage):
            return messages[i:]
    return messages          # no boundary yet: send as-is


def make_llm_node(agent_name: str):
    def llm_node(state: DctaState) -> dict:
        agent = AGENTS[agent_name]
        # Send only this agent's own segment — see _agent_segment. The full list
        # stays in state for the audit trail.
        resp = agent.bound_llm.invoke(_agent_segment(state["messages"]))
        is_final = not bool(getattr(resp, "tool_calls", None))
        audit_entries = []
        if resp.content:
            audit_entries.append(LogEntry("a2a", {
                "role": "assistant", "from": agent.label,
                "to": "Pipeline" if is_final else "Tool",
                "content": resp.content, "is_final": is_final,
            }))
        for tc in getattr(resp, "tool_calls", []) or []:
            audit_entries.append(LogEntry("tool_call", {
                "name": tc["name"], "args": tc["args"], "call_id": tc["id"],
            }))
        if not audit_entries:
            # An empty reply still has to leave a trace, or this node would read as
            # a gap in the trail.
            audit_entries.append(LogEntry("a2a", {
                "role": "assistant", "from": agent.label, "to": "Pipeline",
                "content": "", "is_final": True,
            }))
        return {"messages": [resp], "audit": [vars(e) for e in audit_entries]}
    llm_node.__name__ = f"{agent_name}_llm"
    return llm_node


def _last_msg_has_tool_calls(state: DctaState) -> bool:
    for m in reversed(state["messages"]):
        if isinstance(m, AIMessage):
            return bool(getattr(m, "tool_calls", None))
    return False


# ── Tool → back to calling agent ─────────────────────────────────────────
def _route_tools_back(state: DctaState) -> str:
    # Each agent's setup node records itself in state['current_agent'], so tool
    # results return to that agent's LLM node explicitly — never by matching
    # prompt text, which breaks the moment two prompts collide or one is edited.
    cur = state.get("current_agent", "")
    return f"{cur}_llm" if cur in AGENTS else END


# ══════════════════════════════════════════════════════════════════════════
# Pipeline nodes
# ══════════════════════════════════════════════════════════════════════════
def _log(*entries: LogEntry) -> list[dict]:
    return [vars(e) for e in entries]


def _last_ai_text(state: DctaState) -> str:
    return next((m.content for m in reversed(state["messages"]) if isinstance(m, AIMessage)), "")


def intake_node(state: DctaState) -> dict:
    text = state.get("raw_text", "")
    audit = [LogEntry("input.raw", {"channel": state.get("channel", "text"), "text": text})]
    if state.get("asr"):
        audit.append(LogEntry("input.transcript", {"text": text, **state["asr"]}))
    return {
        "messages": [HumanMessage(content=text)],
        "transcript": [{"role": "customer", "text": text}],
        "audit": _log(*audit),
    }


def _screen(text: str, source: str) -> tuple[str, dict, list[LogEntry]]:
    """Clean and scan one piece of customer text. A block ends the turn here."""
    cleaned = shield.clean(text)
    verdict = shield.scan(cleaned)
    audit = [LogEntry("shield", {"source": source, **verdict, "altered": cleaned != text})]
    if verdict["verdict"] == "block":
        audit += [LogEntry("blocked", {"text": copy.BLOCKED}),
                  LogEntry("answer", {"text": copy.BLOCKED})]
    return cleaned, verdict, audit


def shield_node(state: DctaState) -> dict:
    cleaned, verdict, audit = _screen(state.get("raw_text", ""), "utterance")
    if verdict["verdict"] == "block":
        return {"shield": verdict, "outcome": "blocked", "audit": _log(*audit)}
    # From here on, the cleaned text is the customer's words for every node.
    return {"shield": verdict, "clean_text": cleaned,
            "transcript": [{"role": "customer", "text": cleaned}], "audit": _log(*audit)}


def _withheld_fields(state: DctaState) -> list[LogEntry]:
    """Bank data fields the tools withheld this turn (they cannot log themselves)."""
    seg = _agent_segment(state["messages"])
    return [LogEntry("shield", {"source": f"tool:{m.name}", "verdict": "withheld",
                                "fields": m.content.count(shield.WITHHELD)})
            for m in seg if isinstance(m, ToolMessage) and shield.WITHHELD in str(m.content)]


def _utterance(state: DctaState) -> str:
    return state.get("clean_text") or state.get("raw_text", "")


def _said(state: DctaState) -> str:
    """Every customer turn of the authorisation transcript: the whole basis for
    this turn's draft (each instruction is its own turn, DESIGN §4.2)."""
    turns = [t["text"] for t in state.get("transcript", []) if t["role"] == "customer"]
    return "\n".join(turns) or _utterance(state)


def _bank_ctx(customer_id: str) -> dict:
    """The customer's own records, read through the gateway's GET-only API."""
    return {"customer": bank_client.customer(customer_id),
            "accounts": bank_client.accounts(customer_id),
            "payees": bank_client.payees(customer_id),
            "bills": bank_client.bills(customer_id),
            "instruments": bank_client.instruments(),
            "activity": bank_client.activity(customer_id)}


_SLOTS = {"customer": "customer_utterance", "bank": "bank_question"}


def parser_input(state: DctaState) -> str:
    """Each turn goes in a fixed slot. Angle brackets are escaped first, so the
    customer's words cannot close the slot and talk to the model from outside it."""
    turns = state.get("transcript") or [{"role": "customer", "text": _utterance(state)}]
    return "\n".join(f"<{_SLOTS[t['role']]}>{html.escape(t['text'], quote=False)}"
                     f"</{_SLOTS[t['role']]}>" for t in turns)


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def _check_parsed(raw: str, said: str) -> tuple[ParsedIntent | None, str]:
    """(ParsedIntent, "") if the reply is acceptable, else (None, reason)."""
    obj = parse_json_reply(raw)
    if obj is None:
        return None, "The reply was not a JSON object."
    try:
        parsed = ParsedIntent.model_validate(obj)
    except ValidationError as e:
        return None, "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}"
                               for err in e.errors())
    # Evidence must be the customer's own words. One fabricated span voids the
    # whole draft: it means the model wrote something the customer did not say.
    said = _norm(said)
    for i, t in enumerate(parsed.transactions):
        if _norm(t.evidence) not in said:
            return None, (f"transactions.{i}.evidence is not an exact quote of the "
                          f"utterance: {t.evidence!r}")
    if parsed.category == "transaction" and not parsed.transactions:
        return None, "category is transaction but transactions is empty."
    return parsed, ""


def parse_check_node(state: DctaState) -> dict:
    """Exit of the parser: validate its reply, ask it to repair once, or give up
    on a fixed message. The model's raw text goes to the audit trail only."""
    raw = _last_ai_text(state)
    parsed, error = _check_parsed(raw, _said(state))
    withheld = _withheld_fields(state)

    if error:
        audit = withheld + [LogEntry("draft.parsed", {"ok": False, "error": error, "raw": raw})]
        if state.get("parse_retries", 0) < PARSE_RETRIES:
            fix = (f"Your reply was rejected: {error}\n"
                   "Reply again with ONLY the corrected JSON object.")
            return {"messages": [HumanMessage(content=fix)],
                    "parse_retries": state.get("parse_retries", 0) + 1,
                    "audit": _log(*audit)}
        audit.append(LogEntry("answer", {"text": copy.PARSE_FAILED}))
        return {"outcome": "error", "audit": _log(*audit)}

    data = parsed.model_dump(mode="json")
    audit = withheld + [LogEntry("draft.parsed", {"ok": True, "parsed": data})]
    if parsed.category != "transaction":
        audit.append(LogEntry("answer", {"text": copy.REPLIES[parsed.category]}))
        return {"parsed": data, "outcome": "replied", "audit": _log(*audit)}
    return {"parsed": data, "audit": _log(*audit)}


def resolve_node(state: DctaState) -> dict:
    """Code turns the parsed intent into numbers and records (core/resolve.py)."""
    heard = [a.get("text", "") for a in (state.get("asr") or {}).get("alternatives", [])[1:]]
    flagged = (state.get("shield") or {}).get("verdict") == "flag"
    res = resolve(ParsedIntent.model_validate(state["parsed"]), _said(state),
                  _bank_ctx(state["customer_id"]), state.get("pins"),
                  group_id=f"g_{uuid.uuid4().hex[:10]}", heard=heard,
                  flags=["shield_flag"] if flagged else [])
    audit = [LogEntry("draft.resolved", res)]
    if res["status"] == "ok":
        return {"group": res["group"], "group_hash": res["group_hash"], "audit": _log(*audit)}
    if res["status"] == "ask":
        return {"clarify": {k: res[k] for k in ("question", "slot", "options")},
                "audit": _log(*audit)}
    audit += [LogEntry("denied", {"text": res["text"]}), LogEntry("answer", {"text": res["text"]})]
    return {"outcome": "denied", "audit": _log(*audit)}


def clarify_node(state: DctaState) -> dict:
    """Gate: ask the question resolve raised, wait for the answer.

    interrupt() re-runs this node from the top on resume, so nothing before it
    may have a side effect — the audit entries are only returned, afterwards.
    """
    q = state["clarify"]
    rounds = state.get("clarify_rounds", 0)
    if rounds >= MAX_CLARIFY_ROUNDS:
        return {"outcome": "cancelled", "clarify": {},
                "audit": _log(LogEntry("clarify.ask", {**q, "asked": False}),
                              LogEntry("answer", {"text": copy.CLARIFY_GAVE_UP}))}

    answer = interrupt({"type": "clarify", **q})

    answer = answer if isinstance(answer, dict) else {"text": str(answer)}
    chosen = next((o for o in q["options"] if o["id"] == answer.get("choice_id")), None)
    transcript = [*state["transcript"], {"role": "bank", "text": q["question"]}]
    audit = _log(LogEntry("clarify.ask", q), LogEntry("clarify.answer", answer))
    update = {"clarify": {}, "clarify_rounds": rounds + 1, "audit": audit}

    if chosen:   # a structured choice goes straight back to resolve, not the model
        transcript.append({"role": "customer", "text": chosen["label"], "choice_id": chosen["id"]})
        return {**update, "transcript": transcript,
                "pins": {**state.get("pins", {}), q["slot"]: chosen["id"]}}
    text = str(answer.get("text", "")).strip()
    if not text:
        audit.append(vars(LogEntry("answer", {"text": copy.CLARIFY_GAVE_UP})))
        return {**update, "outcome": "cancelled"}
    # Free text is a new utterance: screened like the first, then the parser
    # reads the whole transcript again.
    text, verdict, screened = _screen(text, "clarify_answer")
    audit += _log(*screened)
    if verdict["verdict"] == "block":
        return {**update, "outcome": "blocked"}
    flagged = "flag" in (verdict["verdict"], (state.get("shield") or {}).get("verdict"))
    transcript.append({"role": "customer", "text": text})
    return {**update, "transcript": transcript, "pins": {}, "parsed": {}, "parse_retries": 0,
            "shield": {**(state.get("shield") or {}), "verdict": "flag" if flagged else "pass"}}


_INTENT_WORDS = {"transfer": "transfer", "bill_payment": "bill payment",
                 "buy_security": "buy shares"}


def _draft_line(t: dict, ctx: dict) -> str:
    """One readable line per payment for the validator — built from the resolved
    draft and the bank's records, never from the parser's output."""
    src = next((a for a in ctx["accounts"] if a["account_id"] == t["debtor_account"]), {})
    if "instrument" in t:
        to = f"{t['instrument']['name']} ({t['instrument']['symbol']})"
    else:
        c = t["creditor"]
        to = {"payee": f"payee {c['name']} ({c.get('bank')} ·· {c['account_last4']})",
              "own_account": f"the customer's own {c['name']} account ·· {c['account_last4']}",
              "biller": f"bill from {c['name']}"}[c["kind"]]
        nick = next((p["alias"] for p in ctx["payees"] if p["payee_id"] == c["id"]), "")
        if nick:
            to += f", saved nickname: {nick.replace('|', ', ')}"
    b = t["basis"]
    how = {"absolute": "",
           "remainder": f" = {src.get('nickname')} balance S${b.get('balance_at_draft')} minus "
                        f"earlier payments {', '.join('S$' + x for x in b.get('less') or []) or 'none'}",
           "fraction": f" = {b.get('ratio')} x payment {b.get('of_seq')}",
           "bill_due": " = full amount due on the bill"}[b["kind"]]
    return (f"{t['seq']}. {_INTENT_WORDS[t['type']]} · from {src.get('nickname')} account "
            f"·· {src.get('last4')} · to {to} · {copy.money(t['amount'])}{how}")


def validator_input(state: DctaState) -> str:
    """Only two things: the authorisation transcript and the draft (DESIGN §4.7)."""
    ctx = _bank_ctx(state["customer_id"])
    convo = "\n".join(f"{t['role']}: {t['text']}" for t in state["transcript"])
    lines = "\n".join(_draft_line(t, ctx) for t in state["group"]["transactions"])
    return (f"<authorisation_transcript>\n{html.escape(convo, quote=False)}\n"
            f"</authorisation_transcript>\n<draft>\n{html.escape(lines, quote=False)}\n</draft>")


def validate_check_node(state: DctaState) -> dict:
    """Two independent engines; either one finding a problem freezes the turn."""
    raw = _last_ai_text(state)
    seqs = {t["seq"] for t in state["group"]["transactions"]}
    try:
        report = ValidatorReport.model_validate(parse_json_reply(raw) or {})
    except ValidationError:
        report, model_problems = None, ["validator: unreadable reply"]
    else:
        model_problems = [f"{v.seq}.{f}: mismatch" for v in report.transactions
                          for f in ("recipient", "amount", "asset", "source")
                          if getattr(v, f) == "mismatch"]
        model_problems += [f"{s}.all: not checked" for s in seqs - {v.seq for v in report.transactions}]
        model_problems += [f"{s}.all: not asked for" for s in report.extra]
        model_problems += [f"missing: {m}" for m in report.missing]
    code_problems = crosscheck(ParsedIntent.model_validate(state["parsed"]), state["group"],
                               _said(state), _bank_ctx(state["customer_id"]))

    problems = model_problems + code_problems
    validation = {"verdict": "frozen" if problems else "consistent",
                  "model": report.model_dump() if report else raw,
                  "model_problems": model_problems, "code_problems": code_problems}
    audit = [LogEntry("validation", validation)]
    if not problems:
        return {"validation": validation, "audit": _log(*audit)}
    fields = sorted({p.split(":")[0] for p in problems})
    text = copy.frozen(fields)
    audit += [LogEntry("frozen", {"fields": fields, "text": text}),
              LogEntry("answer", {"text": text})]
    return {"validation": validation, "outcome": "frozen", "audit": _log(*audit)}


def risk_node(state: DctaState) -> dict:
    """Precheck with the gateway's own rules, so a payment that would be refused
    never reaches the confirm screen. The gateway's decision is the one that counts."""
    cid = state["customer_id"]
    ctx = {**_bank_ctx(cid), "limits": bank_client.limits(cid),
           "screening": bank_client.screening_list(), "now": datetime.now(timezone.utc)}
    result = evaluate(state["group"], ctx)
    audit = [LogEntry("risk.precheck", result)]
    if result["disposition"] == "deny":
        text = copy.denied(result["rules"])
        audit += [LogEntry("denied", {"text": text, "rules": result["rules"]}),
                  LogEntry("answer", {"text": text})]
        return {"risk": result, "outcome": "denied", "audit": _log(*audit)}
    return {"risk": result, "audit": _log(*audit)}


def confirm_node(state: DctaState) -> dict:
    """Gate: hand the draft to the customer's device and wait for the outcome.

    The browser asks the GATEWAY for the bytes to sign and submits the signature
    there directly; this process only learns "submitted" (then checks for itself
    in settle), "failed" or "cancelled". Nothing before interrupt() has effects.
    """
    result = interrupt({"type": "draft", "group_canonical": canonical(state["group"]),
                        "group_hash": state["group_hash"], "precheck": state["risk"],
                        "trace_id": f"{state.get('session_id', '')}:{state.get('turn', 0)}"})
    result = result if isinstance(result, dict) else {}
    audit = [LogEntry("confirm.shown", {"group_hash": state["group_hash"]})]
    if result.get("status") == "edit":
        return _review_edit(state, result, audit)
    if result.get("status") == "submitted" and result.get("challenge_id"):
        audit.append(LogEntry("confirm.submitted", {"challenge_id": result["challenge_id"]}))
        return {"decision": result, "audit": _log(*audit)}
    if result.get("status") == "failed":
        text, outcome = copy.gateway_refused(str(result.get("code", ""))), "denied"
        audit.append(LogEntry("confirm.failed", result))
    else:
        text, outcome = copy.CANCELLED, "cancelled"
        audit.append(LogEntry("confirm.cancelled", result))
    audit.append(LogEntry("answer", {"text": text}))
    return {"decision": result, "outcome": outcome, "audit": _log(*audit)}


def _review_edit(state: DctaState, result: dict, audit: list) -> dict:
    """The customer picked another source account on the review screen. It is a
    structured choice, like a tapped clarify option: pinned for resolve, and
    written into the transcript so the validator checks the new draft against
    it. The old draft is dropped; nothing was signed."""
    txns = state["group"]["transactions"]
    seq, acct = result.get("seq"), result.get("account_id")
    accounts = {a["account_id"]: a for a in _bank_ctx(state["customer_id"])["accounts"]}
    edits = state.get("review_edits", 0)
    ok = (isinstance(seq, int) and 1 <= seq <= len(txns) and acct in accounts
          and edits < MAX_REVIEW_EDITS)
    audit.append(LogEntry("confirm.edited", {"seq": seq, "field": "source",
                                             "account_id": acct, "accepted": ok}))
    if not ok:
        audit.append(LogEntry("answer", {"text": copy.REVIEW_EDITS_EXHAUSTED}))
        return {"decision": result, "outcome": "cancelled", "audit": _log(*audit)}
    said = copy.review_source_change(copy.describe_target(txns[seq - 1]),
                                     accounts[acct]["nickname"])
    return {"decision": result, "review_edits": edits + 1,
            "pins": {**state.get("pins", {}), f"{seq - 1}.source": acct},
            "transcript": [*state["transcript"], {"role": "customer", "text": said,
                                                  "choice_id": acct}],
            "group": {}, "group_hash": "", "validation": {}, "risk": {},
            "audit": _log(*audit)}


def settle_node(state: DctaState) -> dict:
    """Don't take the browser's word for it: fetch the receipt from the gateway
    and check it is for the draft this turn produced."""
    challenge_id = state["decision"]["challenge_id"]
    tx = bank_client.transaction(challenge_id) or {}
    receipt = tx.get("receipt") or {}
    if tx.get("status") != "posted" or receipt.get("group_hash") != state["group_hash"]:
        return {"outcome": "error", "audit": _log(
            LogEntry("settle", {"ok": False, "challenge_id": challenge_id,
                                "status": tx.get("status"), "expected": state["group_hash"],
                                "got": receipt.get("group_hash")}),
            LogEntry("answer", {"text": copy.SETTLE_MISMATCH}))}
    return {"outcome": "posted", "audit": _log(
        LogEntry("settle", {"ok": True, **receipt}),
        LogEntry("answer", {"text": copy.receipt(state["group"], receipt)}))}


# ── routing ─────────────────────────────────────────────────────────────────
def _after_parser_llm(state: DctaState) -> str:
    return "tools" if _last_msg_has_tool_calls(state) else "parse_check"


def _after_parse_check(state: DctaState) -> str:
    if state.get("outcome"):
        return END
    return "resolve" if state.get("parsed") else "parser_llm"   # else: repair attempt


def _after_resolve(state: DctaState) -> str:
    if state.get("outcome"):
        return END
    return "clarify" if state.get("clarify") else "validator"


def _after_clarify(state: DctaState) -> str:
    if state.get("outcome"):
        return END
    return "resolve" if state.get("parsed") else "parser"


def _after_validate(state: DctaState) -> str:
    return END if state.get("outcome") else "risk"


def _after_risk(state: DctaState) -> str:
    return END if state.get("outcome") else "confirm"


def _after_confirm(state: DctaState) -> str:
    if state.get("outcome"):
        return END
    return "resolve" if (state.get("decision") or {}).get("status") == "edit" else "settle"


# ══════════════════════════════════════════════════════════════════════════
# Build the StateGraph
#
#   intake → shield → parser → parser_llm ⇄ tools
#                         ↓
#                    parse_check ──(repair)──→ parser_llm
#                         ↓
#                      resolve ⇄ clarify ──(free text)──→ parser
#                         ↓                ↖
#        validator → validator_llm → validate_check → risk → confirm → settle
#                                         (source changed on review) ┘
# ══════════════════════════════════════════════════════════════════════════
builder = StateGraph(DctaState)

builder.add_node("intake", intake_node)
builder.add_node("shield", shield_node)
builder.add_node("parser", make_agent_node("parser", parser_input))
builder.add_node("parser_llm", make_llm_node("parser"))
# Only tools on some agent's allowlist can execute, whatever name a reply carries.
builder.add_node("tools", ToolNode(list({t.name: t for a in AGENTS.values()
                                         for t in a.tools}.values())))
builder.add_node("parse_check", parse_check_node)
builder.add_node("resolve", resolve_node)
builder.add_node("clarify", clarify_node)
builder.add_node("validator", make_agent_node("validator", validator_input))
builder.add_node("validator_llm", make_llm_node("validator"))
builder.add_node("validate_check", validate_check_node)
builder.add_node("risk", risk_node)
builder.add_node("confirm", confirm_node)
builder.add_node("settle", settle_node)

builder.set_entry_point("intake")
builder.add_edge("intake", "shield")
builder.add_conditional_edges("shield", lambda s: END if s.get("outcome") else "parser",
                              ["parser", END])
builder.add_edge("parser", "parser_llm")
builder.add_conditional_edges("parser_llm", _after_parser_llm, ["tools", "parse_check"])
builder.add_conditional_edges("tools", _route_tools_back)
builder.add_conditional_edges("parse_check", _after_parse_check, ["parser_llm", "resolve", END])
builder.add_conditional_edges("resolve", _after_resolve, ["clarify", "validator", END])
builder.add_conditional_edges("clarify", _after_clarify, ["resolve", "parser", END])
builder.add_edge("validator", "validator_llm")
builder.add_edge("validator_llm", "validate_check")
builder.add_conditional_edges("validate_check", _after_validate, ["risk", END])
builder.add_conditional_edges("risk", _after_risk, ["confirm", END])
builder.add_conditional_edges("confirm", _after_confirm, ["settle", "resolve", END])
builder.add_edge("settle", END)

# Checkpointer persists per-thread graph state so an interrupt (a gate) can pause
# across two customer interactions and resume from the pause point.
graph = builder.compile(checkpointer=MemorySaver())


if __name__ == "__main__":
    print("LangGraph compiled.")
    print("Agents:", list(AGENTS.keys()))
    print("Nodes:", list(graph.get_graph().nodes.keys()))
