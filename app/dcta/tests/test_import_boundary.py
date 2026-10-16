"""A9 / P1–P2: the boundaries are structural, so they are checked on the syntax tree.

- The agent package never imports the gateway (tests and the eval harness, which
  run a gateway in-process on purpose, are not the agent).
- The gateway and the shared core import no LLM library and nothing from dcta
  except dcta.core.
- bank_client.py makes GET requests only, and nothing in the agent package
  touches the ledger's write function.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LLM_LIBS = ("langchain", "langgraph", "openai", "tiktoken")


def _py(*parts, skip=()):
    return [p for p in ROOT.joinpath(*parts).rglob("*.py")
            if not any(s in p.parts for s in skip)]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_agent_never_imports_the_gateway():
    for p in _py("dcta", skip=("tests", "eval")):
        bad = {m for m in _imports(p) if m.split(".")[0] == "gateway"}
        assert not bad, f"{p.relative_to(ROOT)} imports {bad}"


def test_gateway_and_core_have_no_llm_and_only_core_from_dcta():
    for p in _py("gateway") + _py("dcta", "core"):
        for m in _imports(p):
            assert m.split(".")[0] not in LLM_LIBS, f"{p.relative_to(ROOT)} imports {m}"
            if m.split(".")[0] == "dcta":
                assert m.startswith("dcta.core"), f"{p.relative_to(ROOT)} imports {m}"


def test_bank_client_only_gets():
    tree = ast.parse((ROOT / "dcta" / "utils" / "bank_client.py").read_text(encoding="utf-8"))
    verbs = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not verbs & {"post", "put", "patch", "delete", "request", "send", "stream"}


def test_agent_has_no_way_to_post_to_the_ledger():
    for p in _py("dcta", skip=("tests",)):
        assert "post_group" not in p.read_text(encoding="utf-8"), p.relative_to(ROOT)
