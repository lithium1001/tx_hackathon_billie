"""Gateway HTTP app — the deterministic process. Run from the project root:

    py -m uvicorn gateway.app:app --port 8100

`create_app()` is a factory so tests and the eval harness can build a fresh,
isolated gateway (and ledger) per case. Write endpoints do not trust the caller's
origin, only signatures; CORS just lets the agent's page talk to it.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from dcta.core import audit as chain
from gateway.data import DataStore
from gateway.devices import Devices
from gateway.ledger import Ledger
from gateway.verify import Reject, Verifier

_ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"


class DeviceIn(BaseModel):
    customer_id: str
    jwk: dict


class ChallengeIn(BaseModel):
    group: str             # the canonical TransactionGroup string
    key_id: str
    trace_id: str = ""


def create_app(seed_dir: str | Path, state_dir: str | Path, *,
               clock: Callable[[], datetime] | None = None, ttl_s: int = 120,
               allowed_origins: tuple[str, ...] = ("http://127.0.0.1:8000",
                                                   "http://localhost:8000")) -> FastAPI:
    app = FastAPI(title="Bank gateway (Billie demo)")
    app.add_middleware(CORSMiddleware, allow_origins=list(allowed_origins),
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
    state_dir = Path(state_dir)
    audit_path = state_dir / "audit" / "gateway.jsonl"

    def audit(kind: str, payload: dict, trace_id: str = "") -> dict:
        return chain.append(audit_path, trace_id=trace_id, actor="gateway",
                            kind=kind, payload=payload)

    store = DataStore(seed_dir)
    ledger = Ledger(store, state_dir)
    devices = Devices()
    verifier = Verifier(store, ledger, devices, audit,
                        clock or (lambda: datetime.now(timezone.utc)), ttl_s)
    app.state.store, app.state.ledger, app.state.verifier = store, ledger, verifier
    app.state.audit_path = audit_path

    @app.exception_handler(Reject)
    def on_reject(request: Request, e: Reject):
        """Every refusal is answered with its code AND leaves a `reject` entry."""
        body = {"error": e.code, "detail": e.detail, **e.extra}
        audit("reject", {"path": request.url.path, "status": e.status, **body})
        return JSONResponse(body, status_code=e.status)

    def _customer(cid: str) -> dict:
        cust = store.customer(cid)
        if cust is None:
            raise HTTPException(404, "CUSTOMER_UNKNOWN")
        return cust

    # ── read-only queries ───────────────────────────────────────────────────
    @app.get("/v1/customers/{cid}")
    def get_customer(cid: str):
        return _customer(cid)

    @app.get("/v1/customers/{cid}/{table}")
    def get_customer_table(cid: str, table: str):
        _customer(cid)
        live = {"accounts": ledger.accounts, "holdings": ledger.holdings_of,
                "activity": ledger.activity}
        seed = {"payees": store.payees, "bills": store.bills, "limits": store.limits}
        if table not in live | seed:
            raise HTTPException(404, "NOT_FOUND")
        return (live | seed)[table](cid)

    @app.get("/v1/screening-list")
    def get_screening_list():
        return store.screening_list()

    @app.get("/v1/instruments")
    def get_instruments(q: str = ""):
        return store.instruments(q)

    @app.get("/v1/fx")
    def get_fx():
        return store.fx()

    @app.get("/v1/fx")
    def get_fx():
        return store.fx()

    # ── devices, challenges, execution ──────────────────────────────────────
    @app.post("/v1/devices")
    def register_device(body: DeviceIn):
        _customer(body.customer_id)
        try:
            key_id = devices.register(body.customer_id, body.jwk)
        except (ValueError, KeyError) as e:
            raise Reject(422, "KEY_INVALID", str(e))
        audit("device.register", {"customer_id": body.customer_id, "key_id": key_id,
                                  "jwk": body.jwk})
        return {"key_id": key_id}

    @app.post("/v1/challenges")
    def create_challenge(body: ChallengeIn):
        return verifier.issue(body.group, body.key_id, body.trace_id)

    @app.post("/v1/transactions")
    def submit_transaction(body: dict):
        return verifier.execute(body)

    @app.get("/v1/transactions/{challenge_id}")
    def get_transaction(challenge_id: str):
        ch = verifier.challenges.get(challenge_id)
        if ch is None:
            raise HTTPException(404, "CHALLENGE_UNKNOWN")
        status = "posted" if ch.receipt else ("void" if ch.used else "pending")
        return {"status": status, "group_hash": ch.group_hash, "receipt": ch.receipt}

    @app.get("/v1/audit")
    def get_audit():
        return {"entries": chain.read(audit_path), "broken_at": chain.verify(audit_path)}

    # ── out-of-band confirmation (mock hardware token / RM call-back) ───────
    @app.get("/v1/oob/pending")
    def oob_pending():
        now = verifier.clock()
        return [{"challenge_id": c.id, "customer_id": c.customer_id, "otp": c.otp,
                 "expires_at": c.expires_at.isoformat(), "summary": c.summary,
                 "rules": [r["detail"] for r in c.risk["rules"]]}
                for c in verifier.challenges.values()
                if c.otp and not c.receipt and not (c.used and c.otp_attempts >= 3)
                and c.expires_at > now]

    @app.get("/oob")
    def oob_page():
        return FileResponse(STATIC / "oob.html")

    @app.get("/oob.js")
    def oob_script():
        return FileResponse(STATIC / "oob.js", media_type="text/javascript")

    return app


# The gateway is its own process, so it reads its own settings rather than
# importing the agent's config module.
app = create_app(
    os.getenv("DCTA_DATA_DIR") or _ROOT / "data",
    os.getenv("DCTA_ARTIFACTS_DIR") or _ROOT / "artifacts",
    ttl_s=int(os.getenv("GATEWAY_CHALLENGE_TTL_S") or 120),
    allowed_origins=tuple(filter(None, (os.getenv("GATEWAY_ALLOWED_ORIGIN") or
                                        "http://127.0.0.1:8000,http://localhost:8000").split(","))))
