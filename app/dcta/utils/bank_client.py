"""Read-only client for the gateway's query endpoints.

GET only. The agent process has no route to a write endpoint: signed
transactions go from the browser straight to the gateway. test_import_boundary
scans this file for any other HTTP verb.
"""

import httpx

from dcta.utils import config

_http: httpx.Client | None = None


def use(client: httpx.Client | None) -> None:
    """Swap the transport — tests pass a TestClient over an in-process gateway."""
    global _http
    _http = client


def _get(path: str, **params):
    global _http
    if _http is None:
        _http = httpx.Client(base_url=config.GATEWAY_URL, timeout=10)
    resp = _http.get(path, params=params or None)
    resp.raise_for_status()
    return resp.json()


def customer(cid: str) -> dict:
    return _get(f"/v1/customers/{cid}")


def accounts(cid: str) -> list[dict]:
    return _get(f"/v1/customers/{cid}/accounts")


def payees(cid: str) -> list[dict]:
    return _get(f"/v1/customers/{cid}/payees")


def bills(cid: str) -> list[dict]:
    return _get(f"/v1/customers/{cid}/bills")


def holdings(cid: str) -> list[dict]:
    return _get(f"/v1/customers/{cid}/holdings")


def activity(cid: str) -> list[dict]:
    return _get(f"/v1/customers/{cid}/activity")


def limits(cid: str) -> dict:
    return _get(f"/v1/customers/{cid}/limits")


def screening_list() -> list[dict]:
    return _get("/v1/screening-list")


def transaction(challenge_id: str) -> dict | None:
    """Status and receipt of a challenge; None if the gateway never issued it."""
    try:
        return _get(f"/v1/transactions/{challenge_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise


def gateway_audit() -> dict:
    return _get("/v1/audit")


def instruments(q: str = "") -> list[dict]:
    return _get("/v1/instruments", q=q)


def fx() -> dict[str, str]:
    return _get("/v1/fx")


def fx() -> dict[str, str]:
    return _get("/v1/fx")
