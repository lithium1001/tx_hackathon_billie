"""FastAPI app — serves the Billie front-end and drives the graph.

Run from the project root:
    py -m uvicorn dcta.server.app:app --reload
then open http://127.0.0.1:8000

This is a thin transport layer. All agent logic lives in graph.py. The chat
endpoints stream graph events as Server-Sent Events so the front-end can render
live thinking blocks and whichever gate the graph pauses at.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import Response

from dcta.core import audit as audit_chain
from dcta.core import risk
from dcta.server import auth, stream
from dcta.utils import bank_client, config

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Billie")

# The session id lives in an httponly cookie the server mints. Request bodies
# never carry an identity: who the customer is comes from the session, not from
# anything the page (or a model) can put in a payload.
_SID_COOKIE = "sid"
_SID_MAX_AGE = 60 * 60 * 12


def _sid(request: Request) -> tuple[str, bool]:
    """(session id, was it just minted?) for this request."""
    sid = request.cookies.get(_SID_COOKIE) or ""
    if sid:
        return sid, False
    return uuid.uuid4().hex, True


def _sse_response(request: Request, make_stream) -> StreamingResponse:
    """Wrap an SSE generator, stamping the session cookie on a first visit."""
    sid, is_new = _sid(request)
    resp = StreamingResponse(make_stream(sid), media_type="text/event-stream")
    if is_new:
        resp.set_cookie(_SID_COOKIE, sid, max_age=_SID_MAX_AGE,
                        httponly=True, samesite="lax")
    return resp


class NoCacheStaticFiles(StaticFiles):
    """Serve the frontend with no-cache headers so the browser always picks up the
    latest app.js / styles.css during development (avoids stale-JS confusion)."""

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


# ── API: session ────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    customer_id: str
    method: str = "biometric"      # "biometric" (mock Face ID) | "pin"
    pin: str | None = None
    otp: str | None = None         # the SMS code, when a new device must be confirmed


class CustomerRef(BaseModel):
    customer_id: str


def _json(request: Request, body, sid: str) -> JSONResponse:
    """A JSON reply that stamps the session cookie when `sid` was just minted.
    Takes the sid the handler already used: calling _sid() again on a first
    visit would mint a second, different one."""
    resp = JSONResponse(body)
    if sid != request.cookies.get(_SID_COOKIE):
        resp.set_cookie(_SID_COOKIE, sid, max_age=_SID_MAX_AGE, httponly=True, samesite="lax")
    return resp


_CENT = Decimal("0.01")


def _holding_values(holdings: list[dict], instruments: list[dict], fx: dict) -> list[dict]:
    by_symbol = {i["symbol"]: i for i in instruments}
    out = []
    for h in holdings:
        i = by_symbol.get(h["symbol"])
        if i is None:
            continue
        rate = Decimal(fx.get(f"{i['currency']}SGD", "1")) if i["currency"] != "SGD" else 1
        out.append({"symbol": i["symbol"], "name": i["name"], "market": i["market"],
                    "quantity": h["quantity"], "price": i["price"], "currency": i["currency"],
                    "value_sgd": str((Decimal(h["quantity"]) * Decimal(i["price"]) * rate)
                                     .quantize(_CENT))})
    return out


def _activity_rows(activity: list[dict], names: dict, own: set[str]) -> list[dict]:
    """Ledger history as a statement: one signed row per account movement."""
    rows = []
    for h in activity:
        incoming = h["type"] == "incoming"
        rows.append({"id": h["txn_id"], "ts": h["ts"], "type": h["type"],
                     "account_id": h["creditor_id"] if incoming else h["debtor_account"],
                     "counterparty": (h.get("memo") or "Incoming transfer") if incoming
                     else names.get(h["creditor_id"], h["creditor_id"]),
                     "own_transfer": h["creditor_id"] in own and not incoming,
                     "amount": h["amount"] if incoming else f"-{h['amount']}",
                     "currency": h.get("currency", "SGD")})
    return rows


def _me(cid: str, sid: str = "") -> dict:
    """Everything the customer's banking screens show, read through the
    gateway's GET-only API."""
    cust = bank_client.customer(cid)
    accounts, payees = bank_client.accounts(cid), bank_client.payees(cid)
    bills, activity = bank_client.bills(cid), bank_client.activity(cid)
    instruments = bank_client.instruments()
    own = {a["account_id"] for a in accounts}
    names = {**{a["account_id"]: f"{a['nickname']} ·· {a['last4']}" for a in accounts},
             **{p["payee_id"]: p["name"] for p in payees},
             **{b["bill_id"]: b["biller"] for b in bills},
             **{i["symbol"]: i["name"] for i in instruments}}
    paid = {h["creditor_id"] for h in activity}
    screened = {s["name"].strip().upper() for s in bank_client.screening_list()}
    limits = bank_client.limits(cid)
    return {
        "customer_id": cid, "name": cust["name"], "kyc_status": cust["kyc_status"],
        "member_since": cust.get("member_since", ""), "mobile_last4": cust.get("mobile_last4", ""),
        "session_id": sid[:8],
        "accounts": [{**{k: a.get(k, "") for k in ("account_id", "nickname", "type", "last4",
                                                    "currency", "balance", "joint_holder")},
                      "is_default": a["account_id"] == cust["default_account"]}
                     for a in accounts],
        "payees": [{**{k: p[k] for k in ("payee_id", "name", "bank", "account_last4", "added_at")},
                    "aliases": [x for x in p.get("alias", "").split("|") if x],
                    "paid_before": p["payee_id"] in paid,
                    "screened": p["name"].strip().upper() in screened}
                   for p in payees],
        "bills": [{k: b[k] for k in ("bill_id", "biller", "reference", "amount_due", "due_date")}
                  for b in bills],
        "holdings": _holding_values(bank_client.holdings(cid), instruments, bank_client.fx()),
        "activity": _activity_rows(activity, names, own),
        "limits": {"daily": str(risk.daily_limit(limits)),
                   "used_today": str(risk.sent_today(activity, own, datetime.now(timezone.utc))),
                   "step_up_amount": str(risk.STEP_UP_AMOUNT)},
        "security": auth.events(cid),
        "gateway_url": config.GATEWAY_URL, "demo": config.DEMO}


@app.post("/api/login")
def api_login(req: LoginRequest, request: Request):
    """Mock sign-in (see auth.py). On success the customer id is kept
    server-side; no other endpoint accepts a customer id from the page."""
    try:
        cust = bank_client.customer(req.customer_id)
    except httpx.HTTPStatusError:
        raise HTTPException(400, "unknown customer")
    sid, _ = _sid(request)
    try:
        auth.sign_in(sid, cust, req.method, req.pin, req.otp)
    except auth.LoginError as e:
        resp = _json(request, {"error": e.code, **e.extra}, sid)
        resp.status_code = e.status
        return resp
    # A successful sign-in starts a fresh session id (no session fixation), so
    # this customer's turns and audit trail never share a trace id with what
    # happened in the browser before.
    stream.reset_session(sid)
    new_sid = uuid.uuid4().hex
    stream._session(new_sid)["customer_id"] = req.customer_id
    return _json(request, _me(req.customer_id, new_sid), new_sid)


@app.post("/api/logout")
def api_logout(request: Request):
    sid, _ = _sid(request)
    stream.reset_session(sid)
    return _json(request, {"signed_out": True}, sid)


@app.get("/api/me")
def api_me(request: Request):
    sid, _ = _sid(request)
    cid = stream.SESSIONS.get(sid, {}).get("customer_id")
    return _json(request, _me(cid, sid) if cid else None, sid)


# ── API: mock channels around sign-in (demo only, no effect on money) ───────
@app.get("/api/mock/sms")
def api_mock_sms(request: Request):
    """The customer's phone inbox: SMS codes sent to this session."""
    sid, _ = _sid(request)
    return _json(request, auth.sms(sid), sid)


@app.post("/api/mock/reset-login")
def api_mock_reset_login(req: CustomerRef):
    """Unlock after wrong PINs and forget the confirmed new device."""
    auth.reset(req.customer_id)
    return {"reset": req.customer_id}


@app.post("/api/reset")
def api_reset(request: Request):
    """Clear this session's conversation state, keeping who is logged in. Plain
    JSON, not SSE. Abandons any gate still waiting."""
    sid, _ = _sid(request)
    cid = stream.SESSIONS.get(sid, {}).get("customer_id")
    out = stream.reset_session(sid)
    if cid:
        stream._session(sid)["customer_id"] = cid
    return out


# ── API: chat and the two gates ─────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    channel: str = "text"      # "text" | "voice"
    asr: dict | None = None    # alternative transcripts + confidences (voice)


class ClarifyRequest(BaseModel):
    choice_id: str | None = None   # a tapped option, or…
    text: str | None = None        # …a typed / spoken answer


class ConfirmRequest(BaseModel):
    challenge_id: str = ""
    status: str                    # "submitted" | "failed" | "edit"
    code: str | None = None        # the gateway's refusal code, when failed
    seq: int | None = None         # edit: which payment…
    account_id: str | None = None  # …to pay from which of the customer's accounts


@app.post("/api/chat")
def api_chat(req: ChatRequest, request: Request):
    return _sse_response(request, lambda sid: stream.stream_chat(
        sid, req.message, channel=req.channel, asr=req.asr))


@app.post("/api/clarify")
def api_clarify(req: ClarifyRequest, request: Request):
    return _sse_response(request, lambda sid: stream.stream_resume(
        sid, req.model_dump(exclude_none=True)))


@app.post("/api/confirm")
def api_confirm(req: ConfirmRequest, request: Request):
    """The device says it submitted (or the gateway refused, or the customer
    changed a source account on the review screen). settle checks the receipt
    with the gateway itself; this claim alone moves nothing."""
    return _sse_response(request, lambda sid: stream.stream_resume(
        sid, req.model_dump(exclude_none=True)))


@app.post("/api/cancel")
def api_cancel(request: Request):
    return _sse_response(request, lambda sid: stream.stream_resume(sid, {"status": "cancelled"}))


# ── API: audit ──────────────────────────────────────────────────────────────
@app.get("/api/audit")
def api_audit(request: Request):
    """Both chains, each with its own verification, entries of this session only."""
    sid, _ = _sid(request)
    path = stream.agent_chain_path()
    gw = bank_client.gateway_audit()
    mine = lambda e: str(e.get("trace_id", "")).startswith(f"{sid}:")  # noqa: E731
    return {"agent": {"broken_at": audit_chain.verify(path),
                      "entries": [e for e in audit_chain.read(path) if mine(e)]},
            "gateway": {"broken_at": gw["broken_at"],
                        "entries": [e for e in gw["entries"] if mine(e)]}}


if config.DEMO:
    @app.post("/api/demo/tamper")
    def api_demo_tamper(request: Request):
        """DEMO ONLY (DCTA_DEMO=1): edit this session's latest drafted amount in
        the agent chain on disk, so the panel can show verification catching it."""
        sid, _ = _sid(request)
        path = stream.agent_chain_path()
        lines = path.read_text(encoding="utf-8").splitlines()
        for i in range(len(lines) - 1, -1, -1):
            e = json.loads(lines[i])
            mine = e["trace_id"].startswith(f"{sid}:")
            if mine and e["kind"] == "draft.resolved" and e["payload"].get("group"):
                e["payload"]["group"]["transactions"][0]["amount"] = "1.00"
                lines[i] = json.dumps(e, ensure_ascii=False)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return {"tampered_seq": e["seq"]}
        raise HTTPException(404, "no drafted payment in this session yet")


# ── index.html with content-hashed asset versions ──────────────────────────
# The page references /app.js?v=N and /styles.css?v=N. Rewriting N to a hash of
# the current file contents on every request means the browser is *forced* to
# refetch whenever the JS/CSS actually changes (a fixed ?v= stamp would let a
# stale cached copy linger across edits). Served by an explicit route registered
# BEFORE the static mount so it wins over the directory's index.html.
def _file_hash(name: str) -> str:
    p = STATIC_DIR / name
    return hashlib.sha1(p.read_bytes()).hexdigest()[:8] if p.is_file() else "0"


def _page_html(page: str) -> str:
    html = (STATIC_DIR / page).read_text(encoding="utf-8")
    # Stamp each asset with a hash of ITS OWN content (independent stamps), so a
    # change to only one js/css file still busts that one file's cache.
    return re.sub(
        r'/((?:\w+)\.(?:js|css))\?v=[^"\']*',
        lambda m: f'/{m.group(1)}?v={_file_hash(m.group(1))}',
        html,
    )


if STATIC_DIR.is_dir():
    @app.get("/", response_class=HTMLResponse)
    @app.get("/index.html", response_class=HTMLResponse)
    def index():
        resp = HTMLResponse(_page_html("index.html"))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        # Scripts only from this origin, no inline script; the page may talk to
        # itself and to the gateway (signed transactions go there directly).
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' {config.GATEWAY_URL}; img-src 'self' data:; "
            "frame-ancestors 'none'")
        return resp

    # static frontend (mounted last so /api/* and the index route take precedence)
    app.mount("/", NoCacheStaticFiles(directory=str(STATIC_DIR), html=True), name="static")
