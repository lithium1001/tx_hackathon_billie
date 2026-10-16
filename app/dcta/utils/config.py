"""Centralised configuration — every secret / environment read lives here.

Importing this module triggers a single ``load_dotenv()`` so every other module
(llm, tools, telemetry) sees the same values without re-reading ``.env``. To
change a default, set the corresponding variable in ``.env`` (or the shell) — do
not hardcode it elsewhere, and do not call ``os.getenv`` from another module.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# <repo>/dcta/utils/config.py -> the project root is two levels above the package.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _secret(name: str, default: str = "") -> str:
    """Read one variable from the environment / .env, else the default.

    The single read path, so a test can patch it and be sure the developer's own
    .env cannot mask a broken default.
    """
    return os.getenv(name) or default


# ── LLM endpoint ─────────────────────────────────────────────────────────
# Which client talks to the model. "openai" = any OpenAI-compatible
# /chat/completions endpoint (the only backend implemented in utils/llm.py).
LLM_BACKEND = _secret("DCTA_LLM_BACKEND", "openai").strip().lower()

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "google/gemma-4-31b-it"


def _resolve_endpoint() -> tuple[str, str, str, bool]:
    """(base_url, model, api_key, verify_ssl) for the OpenAI-compatible backend.

    The credential has NO fallback to any other variable. A fallback would send
    one provider's key to whatever host DCTA_LLM_BASE_URL names — a credential
    leak to a third party on nothing more than a typo — and would equally turn a
    forgotten key into a confusing 401 instead of a clear "not configured". A
    missing key is reported by _endpoint_error() when the client is built.
    """
    base_url = _secret("DCTA_LLM_BASE_URL", _DEFAULT_BASE_URL).strip().rstrip("/")
    model = _secret("DCTA_MODEL", _DEFAULT_MODEL)
    api_key = _secret("DCTA_LLM_API_KEY")
    # verify=False accepts ANY certificate — it disables the check that the peer
    # is who it claims to be. Acceptable on a trusted private network; never on a
    # public endpoint. Prefer pointing DCTA_SSL_CA_BUNDLE at the private CA file,
    # which keeps verification on.
    verify = _secret("DCTA_VERIFY_SSL", "1") != "0"
    return base_url, model, api_key, verify


def _endpoint_error() -> str:
    """Describe a missing credential for the configured endpoint, else "".

    Returned rather than raised: importing config must stay side-effect-free for
    the test suite and for tooling that never touches the LLM. make_llm()
    surfaces this at the point the credential is actually needed.
    """
    if not LLM_API_KEY:
        return (f"DCTA_LLM_API_KEY is not set, but the configured LLM endpoint is "
                f"{BASE_URL}. Set DCTA_LLM_API_KEY in .env.")
    return ""


BASE_URL, MODEL, LLM_API_KEY, VERIFY_SSL = _resolve_endpoint()

# The validator runs as a second, independent engine. Giving it its own model
# keeps the two from failing in the same place; unset, it shares MODEL.
VALIDATOR_MODEL = _secret("DCTA_VALIDATOR_MODEL") or MODEL

# Optional private-CA bundle. Preferred over DCTA_VERIFY_SSL=0: verification
# stays on, it just trusts this CA too. Takes precedence when both are set.
SSL_CA_BUNDLE = _secret("DCTA_SSL_CA_BUNDLE", "")

# True when the endpoint is the public catalogue whose per-token prices
# telemetry/pricing.py can look up. Any other endpoint leaves the cost column blank.
HAS_PRICE_CATALOG = BASE_URL.startswith(_DEFAULT_BASE_URL)


# ── Rate-limit guard ─────────────────────────────────────────────────────
# Minimum seconds between LLM requests. A ":free" model tier enforces roughly one
# request every 3 seconds, so those default to 3.5s to stay clear of 429s; every
# other model runs unthrottled unless DCTA_MIN_INTERVAL says otherwise.
#
# NOTE: MIN_INTERVAL caps request *frequency*, not token spend — lowering it does
# not save tokens. To control cost, send fewer / smaller requests and set a
# credit cap with the provider.
def _resolve_throttle() -> tuple[bool, float, int]:
    """(is_free_model, min_interval_s, max_retries) for the current endpoint."""
    is_free = MODEL.strip().endswith(":free")
    interval = float(_secret("DCTA_MIN_INTERVAL", "3.5" if is_free else "0"))
    # Retries on a transient gateway error (429/5xx), with 2s/4s/8s backoff in
    # utils/llm.py.
    retries = int(_secret("DCTA_MAX_RETRIES", "3"))
    return is_free, interval, retries


IS_FREE_MODEL, MIN_INTERVAL, MAX_RETRIES = _resolve_throttle()


# ── Context / output budget ──────────────────────────────────────────────
# Some endpoints serve a far smaller context window than a large commercial API,
# and reject an oversized request with a 400 such as:
#
#   "This model's maximum context length is 32768 tokens. However, you requested
#    8192 output tokens and your prompt contains at least 24577 input tokens"
#
# Note what that message says: the OUTPUT reservation counts against the SAME
# budget as the input. If max_tokens is never set, the endpoint applies its own
# default and silently reserves that much of the window before a single message
# is sent. Setting DCTA_MAX_TOKENS reclaims that space.
#
# 0 means "impose no client-side cap" — the provider default applies.
# CONTEXT_LIMIT is the *total* window; utils/llm.py lowers it on its own the
# first time an overflow error names the real figure.
def _resolve_limits() -> tuple[int, int]:
    """(context_limit, max_output_tokens); 0 = uncapped."""
    ctx = int(_secret("DCTA_CONTEXT_LIMIT", "0"))
    out = int(_secret("DCTA_MAX_TOKENS", "0"))
    return ctx, out


CONTEXT_LIMIT, MAX_OUTPUT_TOKENS = _resolve_limits()


# ── Locations ────────────────────────────────────────────────────────────
# Run artifacts (runs.csv and friends) are runtime data, not source: gitignored.
ARTIFACTS_DIR = Path(_secret("DCTA_ARTIFACTS_DIR", str(PROJECT_ROOT / "artifacts")))

# Gateway read-only API (utils/bank_client.py). The agent never calls a write endpoint.
GATEWAY_URL = _secret("DCTA_GATEWAY_URL", "http://127.0.0.1:8100").rstrip("/")

# Demo-only controls (the audit-tamper switch). Off unless DCTA_DEMO=1.
DEMO = _secret("DCTA_DEMO", "0") == "1"
