"""Guards the tolerant JSON parser used for one-shot model replies.

A model asked for a JSON object sometimes wraps it in a code fence, prepends stray
text, or (on some endpoints) starts the reply with a zero-width character. A bare
json.loads then throws away a reply that was actually correct. parse_json_reply
carves out the first balanced {...} object; on a genuine failure it returns None so
the caller falls back to a FIXED message — the raw model text is never surfaced.
No LLM / network.
"""

from dcta.graph import _first_json_object, parse_json_reply


def test_clean_json_passes_through():
    assert parse_json_reply('{"category":"transaction"}') == {"category": "transaction"}


def test_recovers_json_with_leading_noise():
    d = parse_json_reply('SS\n{"category":"transaction","transactions":[]}')
    assert d["category"] == "transaction"
    assert d["transactions"] == []


def test_trailing_junk_is_tolerated():
    d = parse_json_reply('{"category":"inquiry"}  \n done.')
    assert d["category"] == "inquiry"


def test_code_fence_is_stripped():
    d = parse_json_reply('```json\n{"category":"smalltalk"}\n```')
    assert d["category"] == "smalltalk"


def test_leading_zero_width_character_is_stripped():
    d = parse_json_reply('​{"category":"transaction"}')
    assert d == {"category": "transaction"}


def test_nested_braces_in_field():
    d = parse_json_reply('{"category":"transaction","evidence":"use {rest} of it"}')
    assert d["evidence"] == "use {rest} of it"


def test_unparseable_returns_none_not_raw():
    assert parse_json_reply("total nonsense no json here") is None
    assert parse_json_reply("") is None
    assert parse_json_reply(None) is None


def test_non_object_json_is_rejected():
    """A bare list or number parses as JSON but is not the object we asked for."""
    assert parse_json_reply("[1, 2, 3]") is None
    assert parse_json_reply("42") is None


def test_first_json_object_helper():
    assert _first_json_object("no braces") == ""
    assert _first_json_object('x {"a":1} y') == '{"a":1}'
    assert _first_json_object('{"a":{"b":2}} tail') == '{"a":{"b":2}}'
