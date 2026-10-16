/* Billie — the customer's OTHER device (mock): SMS codes for confirming a new
   device, and out-of-band codes the gateway issues for payments that need a
   step-up. Neither passes through the assistant: SMS codes come from the mock
   sign-in service, payment codes straight from the gateway. */
'use strict';

const Inbox = (() => {
  const { esc, time, money } = UI;
  const POLL_MS = 2000;
  let seen = new Set();
  let items = [];
  let open = false;
  let ctx = { gatewayUrl: '', customerId: '' };

  function init() {
    $id('inboxBtn').onclick = () => toggle(!open);
    document.addEventListener('click', (e) => {
      if (open && !e.target.closest('#inbox') && !e.target.closest('#inboxBtn')) toggle(false);
    });
    setInterval(poll, POLL_MS);
  }

  const $id = (id) => document.getElementById(id);

  function configure(next) { ctx = { ...ctx, ...next }; poll(); }

  function toggle(on) {
    open = on;
    $id('inbox').hidden = !on;
    $id('inboxBtn').setAttribute('aria-expanded', String(on));
    if (on) { items.forEach((i) => seen.add(i.key)); paint(); }
  }

  async function poll() {
    const next = [];
    try {
      const sms = await (await fetch('/api/mock/sms')).json();
      sms.forEach((m) => next.push({ key: 'sms:' + m.code + m.ts, kind: 'sms', ts: m.ts,
                                     title: `SMS to ${m.to}`, code: m.code, text: m.text }));
    } catch (e) { /* server restarting */ }
    if (ctx.gatewayUrl && ctx.customerId) {
      try {
        const pend = await (await fetch(ctx.gatewayUrl + '/v1/oob/pending')).json();
        pend.filter((p) => p.customer_id === ctx.customerId).forEach((p) => next.push({
          key: 'oob:' + p.challenge_id, kind: 'oob', ts: new Date().toISOString(),
          title: 'Bank security token · payment approval', code: p.otp,
          text: [...p.summary, ...p.rules].join('\n'), expires: p.expires_at }));
      } catch (e) { /* gateway down: the sheet already says so */ }
    }
    const fresh = next.filter((i) => !seen.has(i.key));
    items = next;
    if (fresh.length && !open) toggle(true);           // a new code pops the device open
    else if (open) { items.forEach((i) => seen.add(i.key)); paint(); }
    const badge = $id('inboxBadge');
    const unseen = items.filter((i) => !seen.has(i.key)).length;
    badge.hidden = !unseen;
    badge.textContent = String(unseen);
  }

  function paint() {
    const root = $id('inbox');
    root.innerHTML = `<div class="ib-head">📱 Customer's other device <span class="ib-note">mock · not part of the app</span></div>` +
      (items.length ? items.map((i) => `<div class="ib-item ${i.kind}">
        <div class="ib-title">${esc(i.title)}</div>
        <div class="ib-code amount">${esc(i.code)}</div>
        <div class="ib-text">${esc(i.text)}</div>
        ${i.expires ? `<div class="ib-exp">expires ${esc(time(i.expires))}</div>` : ''}
      </div>`).join('') : '<div class="ib-empty">No messages. Codes appear here when the bank sends one.</div>');
  }

  return { init, configure };
})();
