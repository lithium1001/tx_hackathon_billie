"""Pytest setup for the whole suite.

Adds the project root to sys.path so any test can ``import dcta.*`` regardless of
the directory pytest is launched from. Tests live in two places by design:

    dcta/tests/   developer safety net   — regression guards, free and offline
    dcta/eval/    reportable deliverable — the golden-set numbers

Run from the project root with:

    py -m pytest                          # everything
    py -m pytest -q dcta/tests/test_audit_completeness.py
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture(autouse=True)
def _no_live_llm(monkeypatch, request):
    """Fail any test that sends a request to the paid LLM endpoint.

    The test suite must be free and offline; only the eval harness spends money.
    This is not hypothetical — the moment a helper that a test already exercises
    grows a model call, that test silently starts billing on every run. A test
    that needs the LLM must inject a stub instead.

    Patches the request path, not client construction: some modules build a client
    without ever calling it, and failing those would be a false alarm.
    """
    if request.node.get_closest_marker("live_llm"):
        return                                     # opt out explicitly, if ever needed

    import dcta.utils.llm as _llm

    def _boom(self, messages, stop=None, run_manager=None, **kw):
        raise AssertionError(
            "This test sent a request to the LLM. Tests must be free and offline - "
            "inject a stub client instead."
        )

    monkeypatch.setattr(_llm.ThrottledChatOpenAI, "_generate", _boom, raising=False)


@pytest.fixture(autouse=True)
def _runs_csv_in_tmp(monkeypatch, tmp_path):
    """Keep test runs out of the real artifacts/runs.csv.

    Every stream writes a metrics row on exit, so any test that drives the graph
    would otherwise append to the same file live traffic and the eval write to.
    """
    from dcta.utils.telemetry import metrics

    monkeypatch.setattr(metrics, "RUNS_CSV", tmp_path / "runs.csv")


@pytest.fixture(autouse=True)
def _artifacts_in_tmp(monkeypatch, tmp_path):
    """Audit chains and other run output go to a per-test directory."""
    from dcta.utils import config

    monkeypatch.setattr(config, "ARTIFACTS_DIR", tmp_path / "artifacts")


@pytest.fixture(autouse=True)
def _no_price_lookup(monkeypatch):
    """Writing a metrics row estimates cost, which fetches a public price catalogue
    on first use. Pre-seed an empty table so no test reaches the network for it; a
    test of the lookup itself resets this to None."""
    from dcta.utils.telemetry import pricing

    monkeypatch.setattr(pricing, "_PRICE_TABLE", {})


SEED_DIR = os.path.join(_ROOT, "data")


@pytest.fixture
def bank(tmp_path):
    """An in-process gateway over the seed data, wired into bank_client.

    Yields the TestClient so a test can also hit the gateway directly.
    """
    from fastapi.testclient import TestClient

    from dcta.utils import bank_client
    from gateway.app import create_app

    client = TestClient(create_app(SEED_DIR, tmp_path / "gateway"))
    bank_client.use(client)
    yield client
    bank_client.use(None)


class StubLLM:
    """Plays back scripted AIMessages in order; records what each call was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


@pytest.fixture
def stub_llm(monkeypatch):
    """`stub_llm(reply, ...)` makes every agent's model play back these replies.
    A str reply becomes an AIMessage; pass an AIMessage for tool calls."""
    import dcta.graph
    from langchain_core.messages import AIMessage

    def install(*replies):
        stub = StubLLM(AIMessage(content=r) if isinstance(r, str) else r for r in replies)
        monkeypatch.setattr(dcta.graph, "get_llm", lambda model=None: stub)
        return stub
    return install
