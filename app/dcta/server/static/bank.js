/* Billie — online banking panel: the signed-in customer's profile, accounts,
   statement, payees, limits and security log. Read-only: everything comes from
   /api/me, which reads the gateway's GET-only API. */
'use strict';

const Bank = (() => {
  const { esc, money, cents, fromCents, date, day, time, initials } = UI;
  const TABS = [['overview', 'Overview'], ['activity', 'Transactions'],
                ['payees', 'Payees'], ['security', 'Limits & security']];
  const TYPE_ICON = { transfer: '⇄', bill_payment: '🧾', buy_security: '📈', incoming: '↓' };
  const TYPE_WORD = { transfer: 'Transfer', bill_payment: 'Bill payment',
                      buy_security: 'Share purchase', incoming: 'Incoming' };
  const KYC = {
    verified: ['ok', '✓ Identity verified'],
    failed: ['bad', '✕ Identity check failed'],
  };

  let tab = 'overview';
  let filter = 'all';
  let last = null;      // [root, ctx] of the latest render, for show()

  function show(next) {
    tab = next;
    filter = 'all';
    if (last) render(...last);
  }

  function render(root, ctx) {
    last = [root, ctx];
    const { me, persona, keyId, newTxns } = ctx;
    if (!me) { root.innerHTML = signedOut(persona); return; }

    const total = me.accounts.reduce((s, a) => s + cents(a.balance), 0n);
    const invest = me.holdings.reduce((s, h) => s + cents(h.value_sgd), 0n);
    const [kycTone, kycText] = KYC[me.kyc_status] || ['warn', `Identity: ${me.kyc_status}`];

    root.innerHTML = `
      <section class="bk-hero">
        <div class="bk-avatar">${esc(initials(me.name))}</div>
        <div class="bk-who">
          <div class="bk-name">${esc(me.name)}</div>
          <div class="bk-meta">Customer ${esc(me.customer_id)} · since ${esc(me.member_since ? date(me.member_since) : '—')}
            · mobile •••• ${esc(me.mobile_last4)}</div>
          <div class="bk-badges">
            <span class="pill pill-${kycTone}">${esc(kycText)}</span>
            <span class="pill ${keyId ? 'pill-ok' : 'pill-bad'}">${keyId ? '🔑 Device key bound' : '✕ Device key not registered'}</span>
            <span class="pill pill-plain">Session ${esc(me.session_id)}</span>
          </div>
        </div>
        <div class="bk-total">
          <div class="bk-total-label">Total balance</div>
          <div class="bk-total-amt amount">${esc(money(fromCents(total)))}</div>
          <div class="bk-total-sub amount">+ investments ${esc(money(fromCents(invest)))}</div>
        </div>
      </section>
      ${me.kyc_status !== 'verified' ? `<div class="bk-alert bad" role="alert"><b>Payments are switched off.</b>
        Your identity verification did not pass, so the bank will decline every payment.
        Visit a branch with your NRIC to verify again.</div>` : ''}
      <nav class="bk-tabs" role="tablist">${TABS.map(([id, label]) =>
        `<button class="bk-tab${tab === id ? ' active' : ''}" data-tab="${id}" role="tab">${esc(label)}</button>`).join('')}</nav>
      <section class="bk-body">${
        tab === 'overview' ? overview(me, newTxns)
        : tab === 'activity' ? activity(me, newTxns)
        : tab === 'payees' ? payees(me) : security(me, keyId)}</section>`;

    root.querySelectorAll('.bk-tab').forEach((b) => {
      b.onclick = () => { tab = b.dataset.tab; render(root, ctx); };
    });
    root.querySelectorAll('[data-filter]').forEach((b) => {
      b.onclick = () => { filter = b.dataset.filter; render(root, ctx); };
    });
    root.querySelectorAll('[data-goto]').forEach((b) => {
      b.onclick = () => { tab = b.dataset.goto; render(root, ctx); };
    });
  }

  function signedOut(persona) {
    return `<section class="bk-empty">
      <div class="bk-logo">Bank</div>
      <h2>Online banking</h2>
      <p>You are signed out. Sign in on the phone to see accounts, transactions and payees.</p>
      ${persona ? `<div class="bk-persona"><div class="bk-persona-k">Selected persona</div>
        <div class="bk-persona-n">${esc(persona.name)} · ${esc(persona.id)}</div>
        <div class="bk-persona-b">${esc(persona.blurb)}</div></div>` : ''}
    </section>`;
  }

  function overview(me, newTxns) {
    const accounts = me.accounts.map((a) => `
      <div class="acct${a.is_default ? ' default' : ''}">
        <div class="acct-top"><span class="acct-name">${esc(a.nickname)}</span>
          ${a.is_default ? '<span class="pill pill-plain">Default</span>' : ''}</div>
        <div class="acct-num">${esc(a.type === 'current' ? 'Current' : 'Savings')} ·· ${esc(a.last4)}
          ${a.joint_holder ? ` · joint with ${esc(a.joint_holder)}` : ''}</div>
        <div class="acct-bal amount">${esc(money(a.balance, a.currency))}</div>
        <div class="acct-sub">Available balance</div>
      </div>`).join('');
    const noDefault = !me.accounts.some((a) => a.is_default);
    const bills = me.bills.map((b) => {
      const days = Math.ceil((new Date(b.due_date) - new Date()) / 864e5);
      return `<div class="row">
        <div class="row-icon">🧾</div>
        <div class="row-main"><div class="row-title">${esc(b.biller)}</div>
          <div class="row-sub">Ref ${esc(b.reference)} · due ${esc(date(b.due_date))}</div></div>
        <div class="row-amt"><div class="amount">${esc(money(b.amount_due))}</div>
          <div class="row-sub ${days <= 7 ? 'soon' : ''}">${days >= 0 ? `in ${days} day${days === 1 ? '' : 's'}` : 'overdue'}</div></div>
      </div>`;
    }).join('') || '<div class="muted">No bills due.</div>';
    const holdings = me.holdings.map((h) => `<div class="row">
        <div class="row-icon">📈</div>
        <div class="row-main"><div class="row-title">${esc(h.name)}</div>
          <div class="row-sub">${esc(h.symbol)} · ${esc(h.market)} · ${esc(trimQty(h.quantity))} units @ ${esc(money(h.price, h.currency))}</div></div>
        <div class="row-amt amount">${esc(money(h.value_sgd))}</div>
      </div>`).join('') || '<div class="muted">No investments yet.</div>';
    const recent = me.activity.slice(0, 4).map((r) => txnRow(r, me, newTxns || new Set())).join('');
    return `
      <h3 class="bk-h">Accounts</h3>
      ${noDefault ? '<div class="bk-alert warn">No default account is set, so the assistant will ask which account to pay from.</div>' : ''}
      <div class="acct-grid">${accounts}</div>
      <div class="bk-cols">
        <div><h3 class="bk-h">Upcoming bills</h3><div class="card-list">${bills}</div></div>
        <div><h3 class="bk-h">Investments</h3><div class="card-list">${holdings}</div></div>
      </div>
      <h3 class="bk-h">Recent transactions <button class="link" data-goto="activity">See all →</button></h3>
      <div class="card-list">${recent || '<div class="muted">No transactions yet.</div>'}</div>`;
  }

  function trimQty(q) { return String(q).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, ''); }

  function acctLabel(me, id) {
    const a = me.accounts.find((x) => x.account_id === id);
    return a ? `${a.nickname} ·· ${a.last4}` : id;
  }

  function txnRow(r, me, fresh) {
    const incoming = !r.amount.startsWith('-');
    const sub = `${TYPE_WORD[r.type] || r.type}${r.own_transfer ? ' (own account)' : ''} · ${acctLabel(me, r.account_id)} · ${time(r.ts)}`;
    return `<div class="row txn${fresh.has(r.id) ? ' fresh' : ''}">
      <div class="row-icon ${incoming ? 'in' : ''}">${esc(TYPE_ICON[r.type] || '•')}</div>
      <div class="row-main"><div class="row-title">${esc(r.counterparty)}
        ${fresh.has(r.id) ? '<span class="pill pill-ok">NEW · via assistant</span>' : ''}</div>
        <div class="row-sub">${esc(sub)}</div></div>
      <div class="row-amt amount ${incoming ? 'pos' : 'neg'}">${esc(incoming ? '+' + money(r.amount, r.currency) : money(r.amount, r.currency))}</div>
    </div>`;
  }

  function activity(me, newTxns) {
    const fresh = newTxns || new Set();
    const rows = me.activity.filter((r) => filter === 'all' || r.account_id === filter);
    const chips = [['all', 'All accounts'], ...me.accounts.map((a) => [a.account_id, `${a.nickname} ·· ${a.last4}`])]
      .map(([id, label]) => `<button class="chip${filter === id ? ' active' : ''}" data-filter="${esc(id)}">${esc(label)}</button>`).join('');
    const groups = [];
    rows.forEach((r) => {
      const d = day(r.ts);
      if (!groups.length || groups[groups.length - 1].d !== d) groups.push({ d, rows: [] });
      groups[groups.length - 1].rows.push(r);
    });
    return `<div class="chip-row">${chips}</div>
      ${groups.map((g) => `<div class="day-head">${esc(g.d)}</div>
        <div class="card-list">${g.rows.map((r) => txnRow(r, me, fresh)).join('')}</div>`).join('')
        || '<div class="muted">No transactions for this account.</div>'}`;
  }

  function payees(me) {
    return `<p class="bk-note">Payees are saved by you in advance. The assistant can only pay these
      people, your own accounts, your billers, or buy listed shares.</p>
      <div class="card-list">${me.payees.map((p) => `<div class="row">
        <div class="row-icon av">${esc(initials(p.name))}</div>
        <div class="row-main"><div class="row-title">${esc(p.name)}
          ${p.aliases.map((a) => `<span class="pill pill-plain">“${esc(a)}”</span>`).join('')}</div>
          <div class="row-sub">${esc(p.bank)} ·· ${esc(p.account_last4)} · added ${esc(date(p.added_at))}</div>
          ${p.screened ? '<div class="row-flag bad">⛔ On a screening list: payments will be declined</div>'
            : !p.paid_before ? '<div class="row-flag warn">⚠ Never paid before: first payment needs a code from your other device</div>' : ''}
        </div>
      </div>`).join('')}</div>`;
  }

  function security(me, keyId) {
    const used = cents(me.limits.used_today);
    const limit = cents(me.limits.daily);
    const pct = limit > 0n ? Number((used * 100n) / limit) : 0;
    const events = me.security.map((e) => `<div class="sec-ev ${esc(e.tone)}">
        <span class="sec-time">${esc(time(e.ts))}</span><span>${esc(e.text)}</span></div>`).join('')
      || '<div class="muted">No sign-in events in this demo run yet.</div>';
    return `
      <h3 class="bk-h">Daily transfer limit</h3>
      <div class="limit">
        <div class="limit-top"><span>Sent to others today</span>
          <span class="amount"><b>${esc(money(me.limits.used_today))}</b> of ${esc(money(me.limits.daily))}</span></div>
        <div class="bar"><div class="bar-fill${pct >= 80 ? ' hot' : ''}" style="width:${Math.min(pct, 100)}%"></div></div>
        <div class="limit-sub amount">${esc(money(fromCents(limit - used > 0n ? limit - used : 0n)))} left today ·
          transfers between your own accounts don't count</div>
      </div>
      <h3 class="bk-h">How your payments are protected</h3>
      <ul class="protect">
        <li><b>You approve every payment</b> on this phone. The assistant can only prepare a draft.</li>
        <li><b>Signed by this device.</b> The approval is a cryptographic signature from a key that never leaves the phone.</li>
        <li><b>Extra code</b> from your other device for payments of ${esc(money(me.limits.step_up_amount))} or more, a first payment to a payee, or unusual activity.</li>
        <li><b>Drain protection.</b> If an account holds S$50,000 or more, moving out more than half of it within 24 hours needs that extra code.</li>
        <li><b>Approvals expire</b> after 2 minutes and can be used only once.</li>
      </ul>
      <h3 class="bk-h">This device</h3>
      <div class="card-list"><div class="row"><div class="row-icon">🔑</div>
        <div class="row-main"><div class="row-title">${keyId ? 'Signing key registered with the bank' : 'No signing key registered'}</div>
          <div class="row-sub mono">${esc(keyId || '—')} · ECDSA P-256 · private key cannot be exported</div></div></div></div>
      <h3 class="bk-h">Recent security activity</h3>
      <div class="sec-list">${events}</div>`;
  }

  return { render, show };
})();
