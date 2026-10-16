/* Billie — trace & audit pane (for engineers and auditors; collapsible).

   Execution graph: one directed graph per customer turn. The fixed topology of
   the agent graph is drawn faintly; the path this turn actually took is drawn
   on top, with the visit order on each node and the outcome on the last one.

   Audit chain: both hash chains of this session as a two-lane DAG per turn.
   Solid arrows are hash links (prev_hash → hash) inside one chain; dashed
   arrows are the references that tie the two chains together (group_hash,
   challenge_id, audit_hash). */
'use strict';

const Trace = (() => {
  const { esc } = UI;
  const $ = (id) => document.getElementById(id);
  const STORE_KEY = 'dcta.traceCollapsed';

  // ── the agent graph's topology ───────────────────────────────────────────
  const NODES = {
    intake:    { c: 0, r: 0, label: 'Input', sub: 'text / voice', kind: 'code' },
    shield:    { c: 1, r: 0, label: 'Shield', sub: 'input screen', kind: 'code' },
    parser:    { c: 2, r: 0, label: 'Parser', sub: 'LLM', kind: 'llm' },
    tools:     { c: 3, r: 0, label: 'Read tools', sub: 'GET only', kind: 'code' },
    clarify:   { c: 3, r: 1, label: 'Ask customer', sub: 'gate', kind: 'gate' },
    resolve:   { c: 2, r: 1, label: 'Resolve', sub: 'code', kind: 'code' },
    validator: { c: 1, r: 1, label: 'Validator', sub: 'LLM + code', kind: 'llm' },
    risk:      { c: 0, r: 1, label: 'Risk rules', sub: 'code', kind: 'code' },
    confirm:   { c: 0, r: 2, label: 'Review', sub: 'customer gate', kind: 'gate' },
    device:    { c: 1, r: 2, label: 'Device signs', sub: 'ES256', kind: 'bank' },
    gateway:   { c: 2, r: 2, label: 'Gateway', sub: 'verify · post', kind: 'bank' },
    settle:    { c: 3, r: 2, label: 'Settle', sub: 'check receipt', kind: 'code' },
  };
  const EDGES = [['intake', 'shield'], ['shield', 'parser'], ['parser', 'tools'], ['tools', 'parser'],
                 ['parser', 'resolve'], ['resolve', 'clarify'], ['clarify', 'resolve'], ['clarify', 'parser'],
                 ['resolve', 'validator'], ['validator', 'risk'], ['risk', 'confirm'],
                 ['confirm', 'device'], ['device', 'gateway'], ['gateway', 'settle'], ['confirm', 'resolve']];
  const EDGE_SET = new Set(EDGES.map((e) => e.join('>')));
  const W = 400, NW = 84, NH = 36;
  const pos = (n) => ({ x: 50 + NODES[n].c * 100, y: 28 + NODES[n].r * 82 });

  const OUTCOME = {
    posted:    ['ok', '✓ Paid', 'The bank posted the payment and the receipt matched the draft.'],
    frozen:    ['frozen', '⛔ Frozen', "The draft didn't match what the customer said."],
    denied:    ['bad', '✕ Declined', ''],
    blocked:   ['block', '🛡 Blocked', 'The input screen stopped an instruction aimed at the system.'],
    cancelled: ['plain', '■ Cancelled', 'No money moved.'],
    replied:   ['plain', '💬 Answered', 'Not a payment: answered from a template.'],
    error:     ['bad', '⚠ Error', 'The turn stopped. No money moved.'],
    waiting:   ['wait', '⏸ Waiting for the customer', ''],
  };

  const state = { turns: [], tab: 'graph', open: new Set(), chainOpen: new Set(),
                  expanded: new Set(), groupHash: '', me: null };

  // ── turn bookkeeping, fed by app.js ──────────────────────────────────────
  const cur = () => state.turns[state.turns.length - 1];

  function visit(node) {
    const t = cur();
    if (!t || !NODES[node]) return;
    const last = t.path[t.path.length - 1];
    if (last === node) return;
    // A tool loop returns to the parser without a new event: show the hop.
    if (last === 'tools' && node !== 'parser' && node !== 'tools') t.path.push('parser');
    t.path.push(node);
  }

  function setOutcome(kind, text) {
    const t = cur();
    if (t) { t.outcome = kind; if (text) t.outcomeText = text; }
  }

  function onEvent(ev) {
    switch (ev.event) {
      case 'turn': {
        state.turns.push({ num: ev.num, msg: ev.user_msg, path: [], outcome: null, outcomeText: '',
                           cards: [], tools: {}, active: true });
        state.open = new Set([ev.num]);
        break;
      }
      case 'thinking_open': visit(ev.agent); break;
      case 'tool_call': visit('tools'); logTool(ev.call_id, ev.name, ev.args, undefined); break;
      case 'tool_result': logTool(ev.call_id, ev.name, undefined, ev.result); break;
      case 'a2a': logCard({ type: 'a2a', role: ev.role, from: ev.from, to: ev.to, content: ev.content, is_final: ev.is_final }); break;
      case 'routed': break;
      case 'shield':
        logEvent(`Security screen (${ev.source}): ${ev.verdict}`, ev.verdict === 'withheld'
          ? `${ev.fields} bank data field(s) withheld from the model` : (ev.reasons || []).join(', ') || 'nothing found');
        break;
      case 'clarify': visit('clarify'); setOutcome('waiting', ev.question); logEvent('Asked the customer', ev.question); break;
      case 'draft': visit('confirm'); setOutcome('waiting', 'Review and approve on the phone');
        logEvent(`Draft ready · precheck ${ev.precheck.disposition}`, `group_hash ${ev.group_hash}`); break;
      case 'frozen': setOutcome('frozen', ev.text); logEvent('Frozen', ev.text); break;
      case 'denied': setOutcome('denied', ev.text); logEvent('Declined', ev.text); break;
      case 'blocked': setOutcome('blocked', ev.text); logEvent('Blocked', ev.text); break;
      case 'receipt': setOutcome('posted', `Receipt ${ev.receipt_id} · audit_hash ${String(ev.audit_hash).slice(0, 12)}…`);
        logEvent('Posted by the bank', `receipt ${ev.receipt_id} · audit ${ev.audit_hash}`); break;
      case 'answer': {
        const t = cur();
        if (t && (!t.outcome || t.outcome === 'waiting')) {
          const cancelled = /^Cancelled/.test(ev.text);
          setOutcome(cancelled ? 'cancelled' : 'replied', ev.text);
        }
        break;
      }
      case 'error': setOutcome('error', ev.text); break;
      case 'done': { const t = cur(); if (t) t.active = false; break; }
      default: break;
    }
  }

  function logCard(c) { const t = cur(); if (t) t.cards.push(c); }
  function logEvent(title, detail) { logCard({ type: 'event', title, detail }); }
  function logTool(id, name, args, result) {
    const t = cur();
    if (!t) return;
    const k = id || 't' + t.cards.length;
    if (!t.tools[k]) { t.tools[k] = { type: 'tool', name, args: {}, result: null }; t.cards.push(t.tools[k]); }
    if (args !== undefined) t.tools[k].args = args;
    if (result !== undefined) t.tools[k].result = result;
  }

  // ── execution graph ──────────────────────────────────────────────────────
  function edgePath(a, b) {
    const p = pos(a), q = pos(b);
    const dx = Math.sign(q.x - p.x), dy = Math.sign(q.y - p.y);
    const twoWay = EDGE_SET.has(`${b}>${a}`) && dy === 0;
    const off = twoWay ? (dx > 0 ? -6 : 6) : 0;
    if (dy === 0) return `M${p.x + dx * NW / 2} ${p.y + off} L${q.x - dx * (NW / 2 + 3)} ${q.y + off}`;
    if (dx === 0) return `M${p.x} ${p.y + dy * NH / 2} L${q.x} ${q.y - dy * (NH / 2 + 3)}`;
    const x1 = p.x + dx * 18, y1 = p.y + dy * NH / 2;
    const x2 = q.x - dx * (NW / 2 + 3), y2 = q.y;
    return `M${x1} ${y1} C${x1} ${y2}, ${x1} ${y2}, ${x2} ${y2}`;
  }

  function graphSvg(t) {
    const id = `g${t.num}`;
    const walked = new Map();
    for (let i = 1; i < t.path.length; i++) {
      const k = `${t.path[i - 1]}>${t.path[i]}`;
      if (EDGE_SET.has(k)) walked.set(k, (walked.get(k) || 0) + 1);
    }
    const order = {};
    t.path.forEach((n, i) => { (order[n] = order[n] || []).push(i + 1); });
    const last = t.path[t.path.length - 1];
    const [tone] = OUTCOME[t.outcome] || ['plain'];

    const edges = EDGES.map(([a, b]) => {
      const k = `${a}>${b}`;
      const on = walked.has(k);
      return `<path d="${edgePath(a, b)}" class="ge${on ? ' on' : ''}${k === 'confirm>resolve' || k === 'clarify>parser' ? ' back' : ''}"
               marker-end="url(#${id}-${on ? 'on' : 'off'})"/>`;
    }).join('');

    const nodes = Object.keys(NODES).map((n) => {
      const { x, y } = pos(n);
      const N = NODES[n];
      const seen = order[n];
      const isLast = n === last;
      const cls = `gn ${N.kind}${seen ? ' seen' : ''}${isLast ? ` last t-${tone}${t.active ? ' live' : ''}` : ''}`;
      return `<g class="${cls}" transform="translate(${x - NW / 2},${y - NH / 2})">
        <rect width="${NW}" height="${NH}" rx="8"/>
        <text x="${NW / 2}" y="15" class="gl">${esc(N.label)}</text>
        <text x="${NW / 2}" y="28" class="gs">${esc(N.sub)}</text>
        ${seen ? `<g transform="translate(${NW - 6},-6)"><circle r="9" class="go"/><text class="got" y="3.5">${seen.length > 1 ? seen[0] + '+' : seen[0]}</text></g>` : ''}
      </g>`;
    }).join('');

    return `<svg class="gsvg" viewBox="0 0 ${W} 228" role="img" aria-label="Execution path for turn ${t.num}">
      <defs>
        <marker id="${id}-off" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" class="ma-off"/></marker>
        <marker id="${id}-on" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" class="ma-on"/></marker>
      </defs>
      <rect x="104" y="${164 - NH / 2 - 10}" width="192" height="${NH + 20}" rx="10" class="zone"/>
      ${edges}${nodes}
    </svg>`;
  }

  function renderGraph() {
    const body = $('graphBody');
    if (!state.turns.length) {
      body.innerHTML = `<div class="tr-empty">Each customer instruction appears here as a directed graph:
        which nodes ran, in what order, and where the turn ended.</div>${legend()}`;
      return;
    }
    body.innerHTML = state.turns.map((t) => {
      const open = state.open.has(t.num);
      const [tone, label, def] = OUTCOME[t.outcome] || ['wait', t.active ? '… Running' : '—', ''];
      return `<section class="turn${open ? ' open' : ''}">
        <header class="turn-head" data-turn="${t.num}">
          <span class="turn-num">Turn ${esc(t.num)}</span>
          <span class="turn-msg">“${esc((t.msg || '').slice(0, 70))}”</span>
          <span class="out out-${tone}">${esc(t.active && !t.outcome ? '… Running' : label)}</span>
        </header>
        ${open ? `<div class="turn-body">
          ${graphSvg(t)}
          <div class="turn-out out-${tone}">${esc(t.outcomeText || def)}</div>
          <div class="turn-path"><b>Path</b> ${t.path.map((n) => esc(NODES[n].label)).join(' → ') || '—'}</div>
          <details class="turn-ev"${state.expanded.has(t.num) ? ' open' : ''} data-ev="${t.num}">
            <summary>Events and model messages (${t.cards.length})</summary>
            ${t.cards.map(cardHtml).join('')}
          </details>
        </div>` : ''}
      </section>`;
    }).join('') + legend();
    body.querySelectorAll('.turn-head').forEach((h) => {
      h.onclick = () => {
        const n = Number(h.dataset.turn);
        state.open.has(n) ? state.open.delete(n) : state.open.add(n);
        renderGraph();
      };
    });
    body.querySelectorAll('details[data-ev]').forEach((d) => {
      d.ontoggle = () => { const n = Number(d.dataset.ev); d.open ? state.expanded.add(n) : state.expanded.delete(n); };
    });
    body.querySelectorAll('.lc-head').forEach((h) => { h.onclick = () => h.parentElement.classList.toggle('open'); });
    body.scrollTop = body.scrollHeight;
  }

  function legend() {
    return `<div class="legend">
      <span><i class="lg-code"></i>code (deterministic)</span>
      <span><i class="lg-llm"></i>LLM (output untrusted)</span>
      <span><i class="lg-gate"></i>waits for customer</span>
      <span><i class="lg-bank"></i>bank side · dashed box: no model past the signature</span>
      <span><i class="lg-on"></i>path taken · ① visit order</span>
    </div>`;
  }

  function cardHtml(c) {
    if (c.type === 'event') {
      return `<div class="lc ev"><b>${esc(c.title)}</b><span>${esc(c.detail)}</span></div>`;
    }
    if (c.type === 'tool') {
      let res = c.result;
      if (res != null) { try { res = JSON.stringify(JSON.parse(res), null, 2); } catch (e) { /* plain text */ } }
      return `<div class="lc tool"><div class="lc-head"><span class="lc-tag tool">TOOL</span>
          <span class="mono">${esc(c.name)}</span><span class="lc-caret">▾</span></div>
        <pre>${esc(JSON.stringify(c.args || {}, null, 2))}</pre>
        ${res != null ? `<pre class="res">${esc(res)}</pre>` : '<div class="lc-run">executing…</div>'}</div>`;
    }
    const label = c.is_final ? 'final' : (c.role || 'msg');
    const preview = (c.content || '').replace(/\n/g, ' ').slice(0, 90);
    return `<div class="lc a2a r-${esc(c.role)}"><div class="lc-head"><span class="lc-tag">${esc(label.toUpperCase())}</span>
        <span class="lc-route">${esc(c.from || '')} → ${esc(c.to || '')}</span><span class="lc-caret">▾</span></div>
      <div class="lc-prev">${esc(preview)}</div><pre>${esc(c.content || '')}</pre></div>`;
  }

  // ── audit chain DAG ──────────────────────────────────────────────────────
  const KIND = {
    'input.raw': 'Customer input', 'input.transcript': 'Voice transcript', shield: 'Security screen',
    tool_call: 'Tool call', a2a: 'Model message', 'draft.parsed': 'Parsed intent (model)',
    'draft.resolved': 'Resolved draft (code)', 'clarify.ask': 'Question to customer',
    'clarify.answer': "Customer's answer", validation: 'Independent validation', frozen: 'Frozen',
    'risk.precheck': 'Risk precheck', denied: 'Declined', blocked: 'Blocked', answer: 'Reply to customer',
    'confirm.shown': 'Shown for approval', 'confirm.edited': 'Source changed on review',
    'confirm.submitted': 'Customer approved', 'confirm.failed': 'Gateway refused',
    'confirm.cancelled': 'Customer cancelled', settle: 'Receipt verified',
    challenge: 'Approval challenge issued', signature: 'Device signature checked',
    'risk.decision': 'Risk decision (final)', stepup: 'Out-of-band code checked',
    'ledger.post': 'Posted to ledger', reject: 'Request rejected', 'device.register': 'Device key registered',
  };

  function detail(e) {
    const p = e.payload || {};
    switch (e.kind) {
      case 'input.raw': return `“${String(p.text || '').slice(0, 48)}” · ${p.channel}`;
      case 'shield': return `${p.source}: ${p.verdict}`;
      case 'draft.parsed': return p.ok ? `${(p.parsed && p.parsed.category) || ''}` : 'rejected, repair asked';
      case 'draft.resolved': return p.status === 'ok' ? `group ${String(p.group_hash).slice(0, 10)}…` : p.status;
      case 'validation': return p.verdict;
      case 'risk.precheck': case 'risk.decision': return p.disposition;
      case 'challenge': return `${p.challenge_id}${p.step_up ? ' · step-up' : ''}`;
      case 'signature': return p.verified ? 'ES256 ✓ valid' : 'ES256 ✗ invalid';
      case 'stepup': return p.ok ? 'code ✓' : `code ✗ (${p.attempts})`;
      case 'ledger.post': return `${(p.posted || []).length} payment(s) posted`;
      case 'reject': return `${p.status} ${p.error}`;
      case 'settle': return p.ok ? `receipt ${p.receipt_id}` : 'receipt mismatch';
      case 'confirm.submitted': return p.challenge_id;
      case 'confirm.edited': return `payment ${p.seq} → ${p.account_id}`;
      case 'clarify.ask': return p.question || '';
      default: return '';
    }
  }

  function chainNodes(entries) {
    // Collapse each run of model messages / tool calls from one actor into one node.
    const out = [];
    entries.forEach((e) => {
      const prev = out[out.length - 1];
      const chatty = e.kind === 'a2a' || e.kind === 'tool_call';
      if (chatty && prev && prev.group && prev.lane === e.lane && prev.actor === e.actor) {
        prev.items.push(e); return;
      }
      out.push(chatty ? { group: true, lane: e.lane, actor: e.actor, items: [e], ts: e.ts }
                      : { ...e, items: [e] });
    });
    return out;
  }

  async function renderChain() {
    const body = $('chainBody');
    let data;
    try { data = await (await fetch('/api/audit')).json(); } catch (e) { data = null; }
    if (!data) { body.innerHTML = '<div class="tr-empty">Audit chain unavailable — is the gateway running?</div>'; return; }

    const all = [
      ...data.agent.entries.map((e) => ({ ...e, lane: 'agent', bad: data.agent.broken_at != null && e.seq >= data.agent.broken_at })),
      ...data.gateway.entries.map((e) => ({ ...e, lane: 'gateway', bad: data.gateway.broken_at != null && e.seq >= data.gateway.broken_at })),
    ].sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
    const traces = [];
    all.forEach((e) => {
      let t = traces.find((x) => x.id === e.trace_id);
      if (!t) { t = { id: e.trace_id, entries: [] }; traces.push(t); }
      t.entries.push(e);
    });
    if (traces.length && !state.chainOpen.size) state.chainOpen.add(traces[traces.length - 1].id);

    const status = (name, file, c) => (c.broken_at == null
      ? `<div class="cs ok">✓ <b>${name} chain intact</b><span>${c.entries.length} entries this session · ${file}</span></div>`
      : `<div class="cs bad">✗ <b>${name} chain broken at #${esc(c.broken_at)}</b><span>an entry was changed after it was written; every hash after it no longer matches</span></div>`);

    body.innerHTML = `<div class="cs-wrap">${status('Agent', 'agent.jsonl', data.agent)}${status('Gateway', 'gateway.jsonl', data.gateway)}</div>
      <div class="ac-legend"><span><i class="al-hash"></i>hash link inside a chain</span>
        <span><i class="al-ref"></i>reference across chains</span>
        ${state.groupHash ? `<span><i class="al-hl"></i>carries group_hash ${esc(state.groupHash.slice(0, 10))}…</span>` : ''}</div>` +
      (traces.length ? traces.map(traceHtml).join('') : '<div class="tr-empty">No audit entries in this session yet.</div>');

    body.querySelectorAll('.ac-head').forEach((h) => {
      h.onclick = () => {
        const id = h.dataset.trace;
        state.chainOpen.has(id) ? state.chainOpen.delete(id) : state.chainOpen.add(id);
        renderChain();
      };
    });
    body.querySelectorAll('.ac-node').forEach((n) => {
      n.onclick = () => { n.classList.toggle('open'); drawAll(); };
    });
    drawAll();
    // A broken chain is the news: bring its first bad entry into view.
    const bad = body.querySelector('.ac-node.bad');
    if (bad) bad.scrollIntoView({ block: 'center' });
    else body.scrollTop = body.scrollHeight;
  }

  function traceHtml(t) {
    const open = state.chainOpen.has(t.id);
    const turn = String(t.id).split(':').pop();
    const input = t.entries.find((e) => e.kind === 'input.raw');
    const nodes = chainNodes(t.entries);
    const rows = nodes.map((n, i) => {
      const e = n.items[0];
      const linked = state.groupHash && n.items.some((x) => JSON.stringify(x.payload).includes(state.groupHash));
      const bad = n.items.some((x) => x.bad);
      const title = n.group ? `${e.actor} ⇄ model · ${n.items.length} message${n.items.length > 1 ? 's' : ''}` : (KIND[e.kind] || e.kind);
      const meta = n.group ? n.items.map((x) => x.kind === 'tool_call' ? `tool ${x.payload.name}` : x.payload.role).join(', ') : detail(e);
      const seqs = n.items.length > 1 ? `#${n.items[0].seq}–${n.items[n.items.length - 1].seq}` : `#${e.seq}`;
      const full = n.items.map((x) => ({ seq: x.seq, kind: x.kind, actor: x.actor, ts: x.ts,
                                         prev_hash: x.prev_hash, hash: x.hash, payload: x.payload }));
      return `<div class="ac-row ${n.lane}">
        <div class="ac-node ${n.lane}${n.group ? ' grp' : ''}${linked ? ' linked' : ''}${bad ? ' bad' : ''}" data-i="${i}"
             data-kind="${esc(e.kind)}" data-hash="${esc(e.hash)}" data-ref="${esc(refKey(e))}">
          <div class="an-top"><span class="an-title">${esc(title)}</span><span class="an-seq mono">${esc(seqs)}</span></div>
          <div class="an-meta">${esc(meta)}</div>
          <div class="an-hash mono">${bad ? '✗ hash mismatch · ' : ''}${esc(String(n.items[n.items.length - 1].hash).slice(0, 12))}</div>
          <pre class="an-pre">${esc(JSON.stringify(full.length === 1 ? full[0] : full, null, 2).slice(0, 4000))}</pre>
        </div></div>`;
    }).join('');
    return `<section class="ac-trace${open ? ' open' : ''}">
      <header class="ac-head" data-trace="${esc(t.id)}">
        <span class="turn-num">Turn ${esc(turn)}</span>
        <span class="turn-msg">${input ? `“${esc(String(input.payload.text || '').slice(0, 60))}”` : ''}</span>
        <span class="mono ac-tid">trace ${esc(String(t.id).slice(0, 8))}…:${esc(turn)}</span>
      </header>
      ${open ? `<div class="ac-lanes"><div>Agent chain</div><div>Gateway chain</div></div>
        <div class="ac-graph">${rows}<svg class="ac-svg"></svg></div>` : ''}
    </section>`;
  }

  // What ties an entry to the other chain.
  function refKey(e) {
    const p = e.payload || {};
    if (e.kind === 'draft.resolved' && p.group_hash) return `gh:${p.group_hash}`;
    if (e.kind === 'challenge') return `gh:${p.group_hash}|ch:${p.challenge_id}`;
    if (e.kind === 'confirm.submitted') return `ch:${p.challenge_id}`;
    if (e.kind === 'settle' && p.audit_hash) return `ah:${p.audit_hash}`;
    return '';
  }

  function drawAll() {
    requestAnimationFrame(() => document.querySelectorAll('.ac-graph').forEach(draw));
  }

  function draw(graph) {
    const svg = graph.querySelector('.ac-svg');
    const nodes = [...graph.querySelectorAll('.ac-node')];
    const W2 = graph.clientWidth, H2 = graph.scrollHeight;
    svg.setAttribute('viewBox', `0 0 ${W2} ${H2}`);
    svg.setAttribute('width', W2); svg.setAttribute('height', H2);
    const box = (n) => ({ x: n.offsetLeft, y: n.offsetTop, w: n.offsetWidth, h: n.offsetHeight });
    let out = `<defs>
      <marker id="ah-h" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" class="ma-h"/></marker>
      <marker id="ah-r" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L8 4L0 8z" class="ma-r"/></marker></defs>`;

    // Hash links: consecutive nodes in the same lane.
    ['agent', 'gateway'].forEach((lane) => {
      const ns = nodes.filter((n) => n.classList.contains(lane));
      for (let i = 1; i < ns.length; i++) {
        const a = box(ns[i - 1]), b = box(ns[i]);
        const x = a.x + 14;
        const bad = ns[i].classList.contains('bad');
        out += `<path d="M${x} ${a.y + a.h} L${x} ${b.y - 3}" class="eh${bad ? ' bad' : ''}" marker-end="url(#ah-h)"/>`;
      }
    });

    // Cross-chain references.
    const by = (pred) => nodes.find(pred);
    const refs = (n) => (n.dataset.ref || '').split('|').filter(Boolean);
    const links = [];
    nodes.filter((n) => n.dataset.kind === 'draft.resolved' && n.dataset.ref).forEach((d) => {
      const gh = refs(d)[0];
      const ch = by((n) => n.dataset.kind === 'challenge' && refs(n).includes(gh));
      if (ch) links.push([d, ch, 'group_hash']);
    });
    nodes.filter((n) => n.dataset.kind === 'challenge').forEach((c) => {
      const chId = refs(c).find((r) => r.startsWith('ch:'));
      const sub = by((n) => n.dataset.kind === 'confirm.submitted' && refs(n).includes(chId));
      if (sub) links.push([c, sub, 'challenge_id']);
    });
    nodes.filter((n) => n.dataset.kind === 'ledger.post').forEach((p) => {
      const st = by((n) => n.dataset.kind === 'settle' && n.dataset.ref === `ah:${p.dataset.hash}`);
      if (st) links.push([p, st, 'audit_hash']);
    });
    links.forEach(([from, to, label]) => {
      const a = box(from), b = box(to);
      const rightward = a.x < b.x;
      const x1 = rightward ? a.x + a.w : a.x, y1 = a.y + a.h / 2;
      const x2 = rightward ? b.x - 3 : b.x + b.w + 3, y2 = b.y + b.h / 2;
      const mx = (x1 + x2) / 2;
      const pw = label.length * 6 + 10;
      out += `<path d="M${x1} ${y1} C${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}" class="er" marker-end="url(#ah-r)"/>
        <g transform="translate(${mx},${(y1 + y2) / 2})"><rect x="${-pw / 2}" y="-8" width="${pw}" height="16" rx="8" class="er-pill"/>
        <text class="er-t" y="3.5">${label}</text></g>`;
    });
    svg.innerHTML = out;
  }

  // ── pane chrome ─────────────────────────────────────────────────────────
  function setCollapsed(on) {
    $('trace').classList.toggle('collapsed', on);
    try { localStorage.setItem(STORE_KEY, on ? '1' : '0'); } catch (e) { /* storage blocked */ }
    if (!on) { $('traceDot').hidden = true; render(); }
  }

  function showTab(tab) {
    state.tab = tab;
    $('tabGraph').classList.toggle('active', tab === 'graph');
    $('tabChain').classList.toggle('active', tab === 'chain');
    $('graphBody').hidden = tab !== 'graph';
    $('chainBody').hidden = tab !== 'chain';
    render();
  }

  function render() {
    if ($('trace').classList.contains('collapsed')) { $('traceDot').hidden = !state.turns.length; return; }
    if (state.tab === 'graph') renderGraph(); else renderChain();
  }

  function setSession(me) {
    state.me = me;
    $('sessionLine').textContent = me
      ? `Session ${me.session_id} · ${me.customer_id} ${me.name} · trace id = session:turn`
      : 'No session';
  }

  function setGroupHash(h) { state.groupHash = h || ''; }

  function reset() {
    state.turns = []; state.open = new Set(); state.chainOpen = new Set(); state.expanded = new Set();
    state.groupHash = '';
    render();
  }

  function init() {
    let collapsed = false;
    try { collapsed = localStorage.getItem(STORE_KEY) === '1'; } catch (e) { /* storage blocked */ }
    $('trace').classList.toggle('collapsed', collapsed);
    $('traceCollapse').onclick = () => setCollapsed(true);
    $('traceExpand').onclick = () => setCollapsed(false);
    $('tabGraph').onclick = () => showTab('graph');
    $('tabChain').onclick = () => showTab('chain');
    window.addEventListener('resize', drawAll);
    render();
  }

  return { init, onEvent, visit, setOutcome, render, renderChain, showTab, setSession, setGroupHash, reset };
})();
