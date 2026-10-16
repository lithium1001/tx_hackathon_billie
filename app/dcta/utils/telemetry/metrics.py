"""RunMetrics — collect operational KPIs for one graph.stream() call and
append them as a single row to artifacts/runs.csv.

Design:
  - Granularity: one graph.stream() == one CSV row (run_id). A transaction that
    pauses at a gate spans several streams (the chat turn + each resume); each is
    its own row, tagged with session/turn/is_resume so rows can be aggregated
    back into a transaction downstream.
  - Single append-only file: artifacts/runs.csv, fixed schema, Excel-openable.
  - Two hook points feed one row:
      * token usage  — collected in utils.llm._generate (the ONLY place every
        LLM call passes through; the stream events do NOT carry token usage).
      * latency / loop count / tool calls / path / audit / interrupted / error —
        collected in server.stream._drive from the events already flowing.

The token side uses a thread-local accumulator (`token_counter`) so a stream
handler can .reset() before driving the graph and .read() after, summing tokens
across every LLM call the graph made in between — without threading a metrics
object down into the LLM client.
"""

from __future__ import annotations

import csv
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dcta.utils.config import ARTIFACTS_DIR, BASE_URL, MODEL
from dcta.utils.telemetry import pricing

# runs.csv is a run artifact, not source. Read live by write() (never bound as a
# default argument) so a test can point it at a temp directory.
RUNS_CSV = ARTIFACTS_DIR / "runs.csv"

# Fixed column order. The eval harness MUST write the same header so
# live-monitoring rows and offline-eval rows land in one table. Golden-set columns
# are left blank by live monitoring and filled by the harness — keeping them here
# means the schema never diverges. New columns go at the END of their group;
# _migrate_header() widens an existing file on the next write.
FIELDNAMES = [
    "run_id",
    "ts",              # ISO-8601 UTC start time
    "source",          # "server" (live) | "eval" (harness)
    "session_id",
    "customer_id",
    "turn",            # customer message number within the session
    "is_resume",       # 1 if this stream resumes a paused gate, else 0
    # ── operational (no labels needed) ──
    "model",           # LLM model id used this stream (config.MODEL)
    "endpoint",        # base_url that served it (config.BASE_URL). Recorded because
                       # `model` alone cannot identify the backend: a single-model
                       # gateway reports its model as "default", and two runs against
                       # different backends would otherwise be indistinguishable in
                       # the CSV — which breaks comparability of any eval batch.
    "latency_s",       # wall-clock of the stream
    "tokens_prompt",
    "tokens_completion",
    "tokens_total",
    "cost_est_usd",    # estimated $ from published per-token prices (blank if unknown;
                       # 0 for :free). Blank is normal and not an error.
                       # A LOWER BOUND on the real bill, not the bill. Tokens are only
                       # counted when a call RETURNS usage, so two classes of spend are
                       # invisible here: (a) gateway retries — utils/llm.py re-sends the
                       # whole prompt on a 504/429 and the provider may charge for the
                       # failed attempt, which reports no usage; (b) requests rejected
                       # on context length — the prompt was transmitted, but the 400
                       # carries no usage. Report the invoice figure and cite this
                       # column as the successful-call floor.
    "llm_calls",       # number of LLM invocations this stream
    "loop_count",      # agent-node revisits (tool-loop iterations); 0 = no re-entry
    "tool_calls",      # number of tool_call events
    "node_visits",     # total agent-llm node visits this stream
    "path",            # ">"-joined agent path, e.g. "parser>validator"
    "audit_entries",   # total audit entries appended to state["audit"] this stream
    "nodes_audited",   # how many DISTINCT nodes emitted >=1 audit entry
    "nodes_total",     # how many DISTINCT nodes ran (audited or not)
    "audit_complete",  # 1 if every node that ran also logged; else 0 (the KPI)
    "interrupted",     # 1 if the stream paused at a gate, else 0
    "tools_called",    # "|"-joined tool names actually invoked, in first-call order
    "error",           # error text if the stream raised, else ""
    # ── golden-set columns (blank for live rows; filled by the eval harness) ──
    "case_id",         # which golden case this row is. REQUIRED for pass^k: k trials
                       # of one case share a case_id and differ by trial.
    "trial",           # 1..k — which repeat this is. 1 for deterministic single runs.
    "success",         # 1 | 0 | "" — end-to-end task success (eval only)
]


# ══════════════════════════════════════════════════════════════════════════
# Token accumulator — written by utils.llm._generate, read by the stream layer.
# ══════════════════════════════════════════════════════════════════════════
class _TokenCounter:
    """Process-wide, thread-local token accumulator.

    utils.llm._generate calls .add(usage) after every LLM response; the stream
    handler calls .reset() before graph.stream and .read() after to get the sum
    for that one stream. Thread-local so concurrent SSE requests don't mix.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _bucket(self) -> dict:
        b = getattr(self._local, "bucket", None)
        if b is None:
            b = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
            self._local.bucket = b
        return b

    def reset(self) -> None:
        self._local.bucket = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}

    def add(self, prompt: int = 0, completion: int = 0, total: int = 0) -> None:
        b = self._bucket()
        b["prompt"] += prompt or 0
        b["completion"] += completion or 0
        # Fall back to prompt+completion if the provider omitted a total.
        b["total"] += total or ((prompt or 0) + (completion or 0))
        b["calls"] += 1

    def read(self) -> dict:
        return dict(self._bucket())


# The single shared instance. Import as `from dcta.utils.telemetry import token_counter`.
token_counter = _TokenCounter()


def _migrate_header(path: Path, columns: list | None = None) -> None:
    """Widen an existing CSV written before a column was added.

    DictWriter only emits the header for a NEW file, so appending a wider row to
    an older CSV silently misaligns it: the surplus value lands under DictReader's
    None key and every consumer reads shifted data. Rewrite the file once with the
    current columns, leaving new ones blank on historical rows.

    Blank is the honest value — it means "this run predates the column", so it is
    never guessed. No-op when the header already matches, so the cost is one
    header read per write.

    `columns` defaults to runs.csv's FIELDNAMES; any other append-only CSV with the
    same widening problem passes its own list, so there is one copy of this logic.
    """
    cols = list(columns) if columns else FIELDNAMES
    try:
        if not path.exists():
            return
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            header = list(rows[0].keys()) if rows else None
        if header is None or header == cols:
            return
        if not set(header) - set(cols):             # only ever widen, never drop
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    r.pop(None, None)              # stray surplus from a prior append
                    w.writerow({k: r.get(k, "") for k in cols})
            tmp.replace(path)
            print(f"  [metrics] widened {path.name} to {len(cols)} columns")
    except Exception as e:  # noqa: BLE001 — monitoring must not break the turn
        print(f"  [metrics] header migration skipped: {e}")


# ══════════════════════════════════════════════════════════════════════════
# RunMetrics — accumulates one row, then .write() appends it.
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class RunMetrics:
    source: str = "server"
    session_id: str = ""
    customer_id: str = ""
    turn: int = 0
    is_resume: bool = False
    model: str = MODEL   # LLM model id in effect for this stream
    endpoint: str = BASE_URL   # which backend served it — see FIELDNAMES note

    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    tool_calls: int = 0
    interrupted: bool = False
    error: str = ""

    # node_visits[agent] = how many times that agent's *_llm node ran this stream.
    # loop_count = sum(visits-1) over agents seen >1 time (a tool loop re-enters
    # the same llm node); path is the ordered list of agents as they first appear.
    _node_visits: dict = field(default_factory=dict)
    _path: list = field(default_factory=list)

    # Audit-log completeness. The convention is that EVERY node appends at least
    # one entry to state["audit"]. A node that runs silently produces a gap in the
    # trail a human auditor would follow — so the KPI is not "how many entries" but
    # "did every node that ran actually log". We track the set of nodes seen and
    # the subset that emitted at least one entry.
    audit_entries: int = 0
    _nodes_ran: set = field(default_factory=set)
    _nodes_logged: set = field(default_factory=set)

    # Which tools were actually invoked, in first-call order — the evidence trail
    # when a case fails.
    _tools_called: list = field(default_factory=list)

    # Golden-set labels (eval rows only; the harness sets these).
    case_id: str = ""
    trial: int = 1
    success: object = ""          # 1 | 0 | "" — "" means "not an eval row"

    _t0: float = field(default_factory=time.perf_counter)

    # ── event hooks (called by the stream driver) ──────────────────────────
    def visit_node(self, agent: str) -> None:
        """Record that agent's llm node ran (called once per node output)."""
        if not agent:
            return
        if agent not in self._node_visits:
            self._path.append(agent)
        self._node_visits[agent] = self._node_visits.get(agent, 0) + 1

    # Nodes NOT bound by the audit convention, so excluded from the completeness
    # denominator. LangGraph's built-in ToolNode ("tools") returns only messages
    # and cannot append to state["audit"] — but the tool call it ran is already
    # audited by the adjacent *_llm node (the AIMessage's tool_calls in and the
    # ToolMessage result out both land in that node's audit). Counting `tools` as
    # an unaudited node would pin audit_complete below 100% for every turn that
    # uses a tool, measuring a structural fact about a framework component rather
    # than a gap in OUR trail — which is the KPI.
    _AUDIT_EXEMPT_NODES = frozenset({"tools"})

    def saw_node(self, node: str) -> None:
        """Record that a graph node bound by the audit convention produced output.

        Every node WE author must log. The built-in ToolNode is exempt (see
        _AUDIT_EXEMPT_NODES); its work is audited by the adjacent *_llm node.
        """
        if node and node not in self._AUDIT_EXEMPT_NODES:
            self._nodes_ran.add(node)

    def add_audit_entries(self, node: str, n: int) -> None:
        """Record that `node` emitted n audit entries (n may be 0)."""
        if not node:
            return
        self.audit_entries += n
        if n > 0:
            self._nodes_logged.add(node)

    def add_tool_call(self, name: str = "", args: dict | None = None, n: int = 1) -> None:
        """Record one tool invocation and its name.

        `args` is accepted so the caller's shape stays stable when an
        authorisation check on the arguments is added here.
        """
        self.tool_calls += n
        if name:
            self._tools_called.append(name)

    # ── derived fields ─────────────────────────────────────────────────────
    @property
    def latency_s(self) -> float:
        return round(time.perf_counter() - self._t0, 3)

    @property
    def node_visits(self) -> int:
        return sum(self._node_visits.values())

    @property
    def loop_count(self) -> int:
        return sum(v - 1 for v in self._node_visits.values() if v > 1)

    @property
    def path(self) -> str:
        return ">".join(self._path)

    @property
    def nodes_total(self) -> int:
        return len(self._nodes_ran)

    @property
    def nodes_audited(self) -> int:
        return len(self._nodes_logged & self._nodes_ran)

    @property
    def audit_complete(self) -> bool:
        """Every node that ran also logged at least one audit entry.

        Vacuously true when no node ran (an errored stream), which is why the
        raw nodes_audited/nodes_total are written alongside — the report should
        aggregate the ratio over rows where nodes_total > 0.
        """
        return not (self._nodes_ran - self._nodes_logged)

    @property
    def unaudited_nodes(self) -> list:
        """Nodes that ran without logging — the actual defect, for debugging."""
        return sorted(self._nodes_ran - self._nodes_logged)

    @property
    def tools_called(self) -> list:
        """Distinct tools invoked, in first-call order.

        De-duplicated because the question is "was the needed tool used", not
        "how many times" — a tool loop calling one tool twice is not two tools.
        """
        seen, out = set(), []
        for t in self._tools_called:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    # ── persistence ────────────────────────────────────────────────────────
    def to_row(self) -> dict:
        # Read the clock BEFORE estimating cost: the first estimate in a process
        # fetches the price catalogue, and that wait is not part of the stream.
        latency = self.latency_s
        tok = token_counter.read()
        cost = pricing.estimate(self.model, tok["prompt"], tok["completion"])
        return {
            "run_id": self.run_id,
            "ts": self.ts,
            "source": self.source,
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "turn": self.turn,
            "is_resume": int(self.is_resume),
            "model": self.model,
            "endpoint": self.endpoint,
            "latency_s": latency,
            "tokens_prompt": tok["prompt"],
            "tokens_completion": tok["completion"],
            "tokens_total": tok["total"],
            "cost_est_usd": "" if cost is None else cost,
            "llm_calls": tok["calls"],
            "loop_count": self.loop_count,
            "tool_calls": self.tool_calls,
            "node_visits": self.node_visits,
            "path": self.path,
            "audit_entries": self.audit_entries,
            "nodes_audited": self.nodes_audited,
            "nodes_total": self.nodes_total,
            "audit_complete": int(self.audit_complete),
            "interrupted": int(self.interrupted),
            "tools_called": "|".join(self.tools_called),
            "error": self.error,
            # Golden-set columns. Blank on live rows (no labels to score against);
            # the eval harness populates them.
            "case_id": self.case_id,
            "trial": self.trial,
            "success": self.success,
        }

    def write(self, path: Path | str | None = None) -> None:
        """Append this run as one row, writing the header if the file is new.

        Best-effort: a metrics write must never break a live turn, so any I/O
        error is swallowed (printed, not raised).
        """
        path = Path(path) if path else RUNS_CSV
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not path.exists() or path.stat().st_size == 0
            if not new_file:
                _migrate_header(path)
            with path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDNAMES)
                if new_file:
                    w.writeheader()
                w.writerow(self.to_row())
        except Exception as e:  # noqa: BLE001 — monitoring must not break the turn
            print(f"  [metrics] failed to write run row: {e}")
