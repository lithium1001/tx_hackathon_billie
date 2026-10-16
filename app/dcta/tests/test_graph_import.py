"""Smoke test for the graph module.

The compiled graph must import cleanly WITHOUT an LLM credential, register every
loaded agent, and produce a well-formed initial state via new_state(). Does NOT
call the LLM (no network), so it is safe to run offline.
"""


def test_graph_compiles():
    from dcta.graph import graph

    nodes = set(graph.get_graph().nodes.keys())
    for core in ("intake", "parser", "parser_llm", "tools", "parse_check"):
        assert core in nodes, f"missing node {core}"


def test_new_state_shape():
    from dcta.graph import DctaState, new_state

    st = new_state("C001", "s1", "Send fifty to John", channel="voice", turn=2)
    assert set(st.keys()) == set(DctaState.__annotations__.keys())
    assert st["customer_id"] == "C001"
    assert st["raw_text"] == "Send fifty to John"
    assert st["channel"] == "voice"
    assert st["turn"] == 2
    assert st["audit"] == []


def test_validator_has_no_tools():
    """The validator is read-only by construction: an empty allowlist, not a prompt."""
    from dcta.graph import _AGENT_TOOLS

    assert _AGENT_TOOLS["validator"] == []


def test_agent_segment_hides_earlier_agents():
    """Each agent sees only its own segment — what makes the validator independent."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from dcta.graph import _agent_segment

    msgs = [
        SystemMessage(content="parser prompt"), HumanMessage(content="parser input"),
        AIMessage(content="parser output"),
        SystemMessage(content="validator prompt"), HumanMessage(content="validator input"),
    ]
    seg = _agent_segment(msgs)
    assert [m.content for m in seg] == ["validator prompt", "validator input"]
