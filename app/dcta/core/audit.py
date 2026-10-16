"""Hash-chained, append-only audit log (DESIGN §3.5, §6.1).

Each line is one entry; `hash = sha256(prev_hash + canonical(entry without hash))`,
so editing, dropping or reordering any line breaks every hash after it. Two
chains use this module, each written by its own process: the agent's (one write
point, the stream driver) and the gateway's.
"""

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64

_lock = threading.Lock()
_heads: dict[str, tuple[int, str]] = {}      # path → (last seq, last hash)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(body: dict) -> str:
    return hashlib.sha256((body["prev_hash"] + _canon(body)).encode("utf-8")).hexdigest()


def read(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append(path: str | Path, *, trace_id: str, actor: str, kind: str, payload) -> dict:
    """Append one entry and return it (with its seq and hash)."""
    path = Path(path)
    with _lock:
        head = _heads.get(str(path)) if path.exists() else None
        if head is None:
            last = (read(path) or [{"seq": 0, "hash": GENESIS}])[-1]
            head = (last["seq"], last["hash"])
        # Round-trip through JSON first, so what is hashed is exactly what a
        # later verify() reads back (Decimals, tuples… become plain JSON).
        body = json.loads(json.dumps({
            "seq": head[0] + 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "trace_id": trace_id, "actor": actor, "kind": kind, "payload": payload,
            "prev_hash": head[1]}, default=str))
        entry = {**body, "hash": _digest(body)}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _heads[str(path)] = (entry["seq"], entry["hash"])
    return entry


def verify(path: str | Path) -> int | None:
    """None if the chain is intact, else the seq of the first broken entry."""
    prev, seq = GENESIS, 1
    for e in read(path):
        body = {k: v for k, v in e.items() if k != "hash"}
        if e.get("seq") != seq or e.get("prev_hash") != prev or _digest(body) != e.get("hash"):
            return seq
        prev, seq = e["hash"], seq + 1
    return None
