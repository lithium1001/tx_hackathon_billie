"""Mock sign-in (DEMO ONLY).

A real bank's identity provider owns sign-in; this stands in for it so the demo
can show the failures a customer actually meets: a wrong PIN, an account locked
after too many wrong PINs (or locked by the bank), and a new device that has to
be confirmed with a one-time SMS code before it can be used.

State is in memory and lives as long as the server process. Nothing here can
move money: a session only names whose drafts the agent may prepare, and every
payment still needs a signature from the device key the gateway knows.
"""

import secrets
import threading
import time
from datetime import datetime, timezone

from dcta.core import copy

DEMO_PIN = "123456"
MAX_PIN_ATTEMPTS = 3
MAX_OTP_ATTEMPTS = 3
OTP_TTL_S = 300
MAX_EVENTS = 20

_lock = threading.Lock()
_wrong_pins: dict[str, int] = {}     # customer → consecutive wrong PINs
_trusted: set[str] = set()           # customers whose new device has been confirmed
_otps: dict[str, dict] = {}          # session → pending SMS code
_sms: dict[str, list] = {}           # session → mock SMS inbox, newest first
_events: dict[str, list] = {}        # customer → security events, newest first


class LoginError(Exception):
    def __init__(self, status: int, code: str, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event(cid: str, text: str, tone: str = "info") -> None:
    log = _events.setdefault(cid, [])
    log.insert(0, {"ts": _now(), "text": text, "tone": tone})
    del log[MAX_EVENTS:]


def sign_in(sid: str, cust: dict, method: str, pin: str | None = None,
            otp: str | None = None) -> None:
    """Return quietly on success; raise LoginError with the reason otherwise."""
    cid = cust["customer_id"]
    with _lock:
        if cust.get("login_status") == "locked":
            _event(cid, "Sign-in refused: digital access locked by the bank", "bad")
            raise LoginError(423, "LOCKED", reason="bank")
        if _wrong_pins.get(cid, 0) >= MAX_PIN_ATTEMPTS:
            _event(cid, "Sign-in refused: locked after incorrect PINs", "bad")
            raise LoginError(423, "LOCKED", reason="pin")

        if method == "pin":
            if pin != DEMO_PIN:
                wrong = _wrong_pins[cid] = _wrong_pins.get(cid, 0) + 1
                left = MAX_PIN_ATTEMPTS - wrong
                if left <= 0:
                    _event(cid, f"Locked after {MAX_PIN_ATTEMPTS} incorrect PINs", "bad")
                    raise LoginError(423, "LOCKED", reason="pin")
                _event(cid, f"Incorrect PIN ({left} attempt{'s' if left > 1 else ''} left)", "warn")
                raise LoginError(401, "PIN_WRONG", attempts_left=left)
        elif method != "biometric":
            raise LoginError(400, "METHOD_UNKNOWN")
        _wrong_pins.pop(cid, None)

        if cust.get("login_status") == "new_device" and cid not in _trusted:
            _confirm_new_device(sid, cust, otp)

        _event(cid, f"Signed in with {'Face ID' if method == 'biometric' else 'PIN'}", "good")


def _confirm_new_device(sid: str, cust: dict, otp: str | None) -> None:
    cid, to = cust["customer_id"], f"•••• {cust.get('mobile_last4', '')}"
    pending = _otps.get(sid)
    if not otp:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        _otps[sid] = {"customer_id": cid, "code": code, "expires": time.time() + OTP_TTL_S,
                      "attempts": 0}
        _sms.setdefault(sid, []).insert(0, {"ts": _now(), "to": to, "code": code,
                                            "text": copy.sms_new_device(code)})
        _event(cid, "New device: one-time code sent by SMS", "warn")
        raise LoginError(428, "OTP_REQUIRED", sent_to=to)
    if not pending or pending["customer_id"] != cid or time.time() > pending["expires"]:
        _otps.pop(sid, None)
        raise LoginError(410, "OTP_EXPIRED")
    if otp.strip() != pending["code"]:
        pending["attempts"] += 1
        left = MAX_OTP_ATTEMPTS - pending["attempts"]
        _event(cid, "Incorrect SMS code for new device", "warn")
        if left <= 0:
            _otps.pop(sid, None)
            raise LoginError(410, "OTP_EXPIRED")
        raise LoginError(401, "OTP_WRONG", attempts_left=left)
    _otps.pop(sid, None)
    _trusted.add(cid)
    _event(cid, "New device confirmed with SMS code and bound to this account", "good")


def sms(sid: str) -> list[dict]:
    return list(_sms.get(sid, []))


def events(cid: str) -> list[dict]:
    return list(_events.get(cid, []))


def reset(cid: str) -> None:
    """Demo control: forget wrong PINs, the trusted device and the event log."""
    with _lock:
        _wrong_pins.pop(cid, None)
        _trusted.discard(cid)
        _events.pop(cid, None)
