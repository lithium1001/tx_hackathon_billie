"""An unreachable network must degrade silently, not stall and not stop the run.

The token-cost price lookup reaches the public internet, and on a host without it
the lookup cannot succeed. The requirement is that nobody has to know: no flag to
set, no stall, and above all no failed turn, which never needed the price.

The design is a per-process circuit breaker (utils.netguard) rather than a config
flag, so these tests pin down the two halves of that bargain:

* it OPENS on evidence the network has no path (DNS failure, refused, no route),
  and every later optional call then short-circuits instantly, and
* it does NOT open on a timeout or an HTTP error, which say nothing about
  reachability — otherwise one slow response on a healthy host would permanently
  disable the lookup.

Network is asserted-against by monkeypatching the transport to explode: if a guard
regresses, the test fails loudly instead of quietly reaching the internet.
"""
import socket

import pytest

from dcta.utils import config, netguard
from dcta.utils.telemetry import pricing


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Breaker and price cache are process-global; each test needs a clean slate."""
    netguard.reset()
    monkeypatch.setattr(pricing, "_PRICE_TABLE", None)
    monkeypatch.setattr(config, "HAS_PRICE_CATALOG", True)
    yield
    netguard.reset()


def _dns_error() -> Exception:
    """The shape urllib produces: an OSError wrapping a gaierror."""
    inner = socket.gaierror(-2, "Name or service not known")
    outer = OSError("<urlopen error [Errno -2] Name or service not known>")
    outer.__cause__ = inner
    return outer


# ── what opens the breaker, and what must not ────────────────────────────
def test_dns_failure_opens_the_breaker():
    assert netguard.note_failure(_dns_error()) is True
    assert netguard.is_offline() is True


def test_breaker_reports_opening_only_once():
    assert netguard.note_failure(_dns_error()) is True
    assert netguard.note_failure(_dns_error()) is False


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    ValueError("HTTP Error 500: Internal Server Error"),
])
def test_slow_or_failing_host_does_not_open_the_breaker(exc):
    assert netguard.note_failure(exc) is False
    assert netguard.is_offline() is False


# ── the price lookup behind it ───────────────────────────────────────────
def test_unreachable_network_leaves_cost_blank_and_stops_retrying(monkeypatch):
    calls = []

    def unreachable(*a, **k):
        calls.append(1)
        raise _dns_error()

    monkeypatch.setattr(pricing.urllib.request, "urlopen", unreachable)

    assert pricing.estimate("vendor/model", 1000, 200) is None
    assert netguard.is_offline() is True
    assert pricing.estimate("vendor/model", 1000, 200) is None
    assert len(calls) == 1          # cached empty table: no second attempt


def test_no_lookup_when_endpoint_has_no_catalogue(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a network call escaped the offline guard")

    monkeypatch.setattr(pricing.urllib.request, "urlopen", boom)
    monkeypatch.setattr(config, "HAS_PRICE_CATALOG", False)

    assert pricing.estimate("default", 1000, 200) is None


def test_no_lookup_once_offline(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a network call escaped the offline guard")

    monkeypatch.setattr(pricing.urllib.request, "urlopen", boom)
    netguard.note_failure(_dns_error())

    assert pricing.estimate("vendor/model", 1000, 200) is None


def test_known_price_is_applied(monkeypatch):
    monkeypatch.setattr(pricing, "_PRICE_TABLE", {"vendor/model": (1e-6, 2e-6)})
    assert pricing.estimate("vendor/model", 1000, 500) == pytest.approx(0.002)
    assert pricing.estimate("unknown/model", 1000, 500) is None
    assert pricing.estimate("", 1000, 500) is None
