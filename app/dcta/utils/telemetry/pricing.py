"""Optional per-token $ cost for the telemetry CSV's cost_est_usd column.

Entirely cosmetic: this fills in one reporting column and nothing in the app
reads it back. A blank cost column is a normal, healthy state — it is what you
get on an endpoint with no published per-token price and on a host without
public internet. Neither is an error and neither affects a run.

When the app IS routed through the public model catalogue, that catalogue is the
authoritative and moving source of its own prices, so we do not hardcode a rate
table: on first use we fetch it once and cache each model's prompt/completion
rate (USD per token) for the process lifetime. cost_est is then:

    prompt_tokens * prompt_price + completion_tokens * completion_price

`:free` models publish "0"/"0", so cost_est is genuinely $0 for those. Any
failure — unreachable network, parse error, unknown model — makes estimate()
return None and the caller leaves the column blank. Pricing must never break a
run or the metrics write.
"""

from __future__ import annotations

import json
import urllib.request

_MODELS_URL = "https://openrouter.ai/api/v1/models"

# model id -> (prompt_price_per_token, completion_price_per_token) as floats.
# None = not fetched yet; {} = fetched but empty (still cached, no re-fetch).
_PRICE_TABLE: dict[str, tuple[float, float]] | None = None


def _pricing_enabled() -> bool:
    """Whether we may fetch the price catalogue at all.

    False when the configured endpoint is not the one the catalogue describes,
    and False once utils.netguard has established that this host cannot reach the
    public internet. Cost then stays blank, which is the honest answer rather
    than a guess.

    Any failure to read config is treated as "not enabled": the safe default is
    to make no outbound request, not to attempt one.
    """
    try:
        # Read through the MODULE, never from-import the flag: a test may rebind
        # it, and a from-import would freeze whatever value was current at import.
        from dcta.utils import config, netguard
        if netguard.is_offline():
            return False
        return bool(config.HAS_PRICE_CATALOG)
    except Exception:  # noqa: BLE001 — telemetry must never break on an import
        return False


def _load_table() -> dict[str, tuple[float, float]]:
    """Fetch and parse the model pricing table once; cache for the process.

    Best-effort: any network/parse failure caches an empty table so we don't
    hammer the endpoint on every run, and estimate() then returns None.
    """
    global _PRICE_TABLE
    if _PRICE_TABLE is not None:
        return _PRICE_TABLE
    table: dict[str, tuple[float, float]] = {}
    if not _pricing_enabled():
        _PRICE_TABLE = table
        return table
    try:
        req = urllib.request.Request(_MODELS_URL, headers={"User-Agent": "dcta-telemetry"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for m in data.get("data", []):
            mid = m.get("id")
            pricing = m.get("pricing") or {}
            if not mid:
                continue
            try:
                prompt = float(pricing.get("prompt", "0") or 0)
                completion = float(pricing.get("completion", "0") or 0)
            except (TypeError, ValueError):
                continue
            table[mid] = (prompt, completion)
    except Exception as e:  # noqa: BLE001 — pricing lookup must never raise
        # An unreachable network trips the shared breaker, so any other optional
        # outbound feature skips its attempt too instead of paying its own timeout
        # to learn the same thing.
        from dcta.utils import netguard
        if netguard.note_failure(e):
            # Said once per process, and only for a genuinely unreachable network.
            # This is a blank optional column — the LLM endpoint is configured
            # separately and is unaffected.
            print("  [info] no public internet from this host - the cost column "
                  "is unavailable.")
    _PRICE_TABLE = table
    return table


def estimate(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimated USD cost for one run's token usage, or None if price unknown.

    None (not 0.0) signals "price unavailable" so the caller can leave the CSV
    column blank; a genuine free model returns 0.0 because its rates are "0".
    """
    if not model:
        return None
    rates = _load_table().get(model)
    if rates is None:
        return None
    prompt_price, completion_price = rates
    return round((prompt_tokens or 0) * prompt_price
                 + (completion_tokens or 0) * completion_price, 6)
