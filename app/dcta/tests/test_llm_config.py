"""Endpoint configuration and the LLM client's use of it.

A silent regression here points the whole app at the wrong backend or the wrong
credential, which looks like a bad model rather than a misconfiguration.

config's resolvers are called directly with a patched _secret so the developer's
own .env cannot mask a broken default. No LLM / network.
"""
import pytest

from dcta.utils import config, llm


@pytest.fixture
def env(monkeypatch):
    """A _secret() that reads only from the dict this returns, then the default."""
    values: dict[str, str] = {}
    monkeypatch.setattr(config, "_secret",
                        lambda name, default="": values.get(name) or default)
    return values


# ── endpoint resolution ──────────────────────────────────────────────────
def test_defaults(env):
    env["DCTA_LLM_API_KEY"] = "sk-test"
    base_url, model, api_key, verify = config._resolve_endpoint()
    assert base_url == "https://openrouter.ai/api/v1"
    assert model
    assert api_key == "sk-test"
    assert verify is True            # verification stays ON unless switched off


def test_explicit_endpoint_and_model(env):
    env["DCTA_LLM_BASE_URL"] = "https://gw.example/v1/"
    env["DCTA_MODEL"] = "some-model"
    env["DCTA_LLM_API_KEY"] = "k"
    base_url, model, _, _ = config._resolve_endpoint()
    assert base_url == "https://gw.example/v1"      # trailing slash stripped
    assert model == "some-model"


def test_key_has_no_fallback_to_other_variables(env):
    """Only DCTA_LLM_API_KEY is ever sent. A fallback to some other provider's key
    would ship that credential to whatever host the base URL names."""
    env["DCTA_LLM_BASE_URL"] = "https://gw.example/v1"
    env["OPENAI_API_KEY"] = "sk-other"
    env["OPENROUTER_API_KEY"] = "sk-or-other"
    assert config._resolve_endpoint()[2] == ""


def test_missing_key_names_the_variable_to_set(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    msg = config._endpoint_error()
    assert "DCTA_LLM_API_KEY" in msg
    assert config.BASE_URL in msg

    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    assert config._endpoint_error() == ""


def test_make_llm_fails_fast_without_a_key(monkeypatch):
    """Failing at construction names the variable; failing at the first call would
    surface as an opaque 401 several nodes into a graph run."""
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    with pytest.raises(ValueError, match="DCTA_LLM_API_KEY"):
        llm.make_llm()


def test_tls_verification_can_be_switched_off_explicitly(env):
    env["DCTA_VERIFY_SSL"] = "0"
    assert config._resolve_endpoint()[3] is False


# ── throttle / budget ────────────────────────────────────────────────────
def test_free_tier_model_is_throttled_by_default(env, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "vendor/model:free")
    is_free, interval, retries = config._resolve_throttle()
    assert is_free is True and interval == 3.5 and retries == 3

    monkeypatch.setattr(config, "MODEL", "vendor/model")
    assert config._resolve_throttle()[:2] == (False, 0.0)


def test_explicit_throttle_override_wins(env, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "vendor/model:free")
    env["DCTA_MIN_INTERVAL"] = "0.5"
    env["DCTA_MAX_RETRIES"] = "1"
    assert config._resolve_throttle() == (True, 0.5, 1)


def test_limits_default_to_uncapped(env):
    assert config._resolve_limits() == (0, 0)
    env["DCTA_CONTEXT_LIMIT"] = "32768"
    env["DCTA_MAX_TOKENS"] = "2048"
    assert config._resolve_limits() == (32768, 2048)


def test_llm_reads_throttle_live_not_frozen(monkeypatch):
    """MIN_INTERVAL is read off the config module on every call, so changing it
    takes effect without re-importing the client."""
    slept = []
    monkeypatch.setattr(llm.time, "sleep", slept.append)
    monkeypatch.setattr(llm, "_LAST_CALL_TIME", llm.time.time())

    monkeypatch.setattr(config, "MIN_INTERVAL", 0)
    llm._throttle()
    assert slept == []

    monkeypatch.setattr(config, "MIN_INTERVAL", 5.0)
    llm._throttle()
    assert slept and 0 < slept[0] <= 5.0


# ── learning the context window ──────────────────────────────────────────
_OVERFLOW = ("This model's maximum context length is 32768 tokens. However, you "
             "requested 8192 output tokens and your prompt contains at least 24577 "
             "input tokens")


def test_learns_context_limit_from_overflow_error(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_LIMIT", 0)
    assert llm._learn_context_limit(ValueError(_OVERFLOW)) is True
    assert config.CONTEXT_LIMIT == 32768


def test_learning_only_ever_tightens(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_LIMIT", 16000)
    assert llm._learn_context_limit(ValueError(_OVERFLOW)) is False
    assert config.CONTEXT_LIMIT == 16000


def test_unrelated_errors_do_not_touch_the_limit(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_LIMIT", 0)
    assert llm._learn_context_limit(ValueError("rate limited")) is False
    assert config.CONTEXT_LIMIT == 0


# ── retry classification / text clean-up ─────────────────────────────────
@pytest.mark.parametrize("exc, expected", [
    (ValueError({"error": {"code": 504, "message": "The operation was aborted"}}), True),
    (TimeoutError("request timed out"), True),
    (ValueError("Invalid API key"), False),
    (KeyError("payee"), False),
])
def test_retry_only_on_transient_gateway_errors(exc, expected):
    assert llm._is_retryable(exc) is expected


def test_think_spans_and_role_tokens_are_stripped():
    assert llm._strip_think("<think>plan</think>\n{\"a\": 1}") == '{"a": 1}'
    assert llm._strip_role_tokens("answer<|eot_id|>") == "answer"
    # ordinary prose containing the word is left alone
    assert llm._strip_role_tokens("the assistant will call you") == "the assistant will call you"


def test_outgoing_text_is_sanitised():
    assert llm._sanitize_text("a\x07b") == "ab"                 # control char dropped
    assert llm._sanitize_text("C:\\path") == "C:\\\\path"        # lone backslash escaped
