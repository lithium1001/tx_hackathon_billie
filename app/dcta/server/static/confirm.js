/* Billie — the review-and-approve sheet.
   Its only input is the `signing_input` string the GATEWAY issued: it is parsed
   and rendered by the fixed template below, and the very same string is what
   gets signed. It never reads SSE events or chat text and renders no markdown,
   and every value goes in through textContent. What you see is what you sign.

   The customer can change which account a payment comes from. That does not
   edit these bytes: it hands the choice back to the agent, which drafts again,
   re-validates, and the gateway issues a fresh approval request. */
'use strict';

const Confirm = (() => {
  const { el, money, cents, fromCents } = UI;
  const HOLD_MS = 1000;   // press-and-hold stands in for biometrics (mock)
  const WARN_S = 30;
  const DANGER_S = 10;

  const TYPES = { transfer: ['⇄', 'Transfer'], bill_payment: ['🧾', 'Bill payment'],
                  buy_security: ['📈', 'Buy shares'] };
  const FLAGS = {
    default_source: 'Paid from your default account because you did not name one. Change it above if needed.',
    first_time_payee: 'First payment to this payee.',
    fx_indicative: 'Indicative price and FX rate; the final number of shares is set at execution.',
    shield_flag: 'Your message was flagged by our security screen, so extra verification is needed.',
  };
  // The bank's eight checks, in the order the customer reads them.
  const CHECKS = [
    ['KYC_STATUS', 'Identity verified (KYC)'], ['SCREENING', 'Recipient screening'],
    ['FIRST_TIME_PAYEE', 'Known payee'], ['DAILY_LIMIT', 'Daily transfer limit'],
    ['STEP_UP_AMOUNT', 'Large-payment check'], ['VELOCITY', 'Payment frequency'],
    ['DRAIN', 'Account drain protection'], ['SHIELD_FLAG', 'Message security screen'],
  ];
  const STEPS = ['Understood', 'Double-checked', 'Safety checks', 'Your approval', 'Bank pays'];

  const $ = (id) => document.getElementById(id);

  function accountOf(id, accounts) { return (accounts || []).find((x) => x.account_id === id); }
  function accountLabel(id, accounts) {
    const a = accountOf(id, accounts);
    return a ? `${a.nickname} ·· ${a.last4}` : id;
  }

  function target(t, accounts) {
    if (t.instrument) return `${t.instrument.name} (${t.instrument.symbol}) · ${t.instrument.market}`;
    const c = t.creditor;
    if (c.kind === 'own_account') return `Your ${accountLabel(c.id, accounts)}`;
    if (c.kind === 'biller') return `${c.name} ·· ${c.account_last4}`;
    return `${c.name} · ${c.bank} ·· ${c.account_last4}`;
  }

  function basisLine(t, txns, accounts) {
    const b = t.basis;
    if (b.kind === 'remainder') {
      const src = (accountOf(t.debtor_account, accounts) || {}).nickname || t.debtor_account;
      const less = (b.less || []).map((x) => money(x)).join(' − ');
      return `“the rest” = ${src} balance ${money(b.balance_at_draft)}${less ? ' − ' + less : ''}`;
    }
    if (b.kind === 'fraction') {
      const of = txns[b.of_seq - 1];
      return `${b.ratio} × payment ${b.of_seq} (${money(of.amount, of.currency)})`;
    }
    if (b.kind === 'bill_due') return 'Full amount due on this bill';
    return null;
  }

  async function sha256Hex(text) {
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  /* open(draft, ctx) → Promise of {status: 'submitted', challenge_id}
                                 | {status: 'failed', code, challenge_id}
                                 | {status: 'edit', seq, account_id, challenge_id}
                                 | {status: 'cancelled'} */
  function open(draft, ctx) {
    return new Promise((resolve) => {
      const sheet = $('sheet');
      const backdrop = $('sheetBackdrop');
      let timer = null;
      let holdTimer = null;
      let settled = false;
      let ch = null;

      function close(result) {
        if (settled) return;
        settled = true;
        clearInterval(timer);
        clearTimeout(holdTimer);
        sheet.hidden = true;
        backdrop.hidden = true;
        sheet.replaceChildren();
        document.removeEventListener('keydown', onKey);
        resolve(result);
      }

      function onKey(e) {
        if (e.key === 'Escape') close({ status: 'cancelled' });
        if (e.key !== 'Tab') return;       // keep focus inside the dialog
        const f = sheet.querySelectorAll('button:not([disabled]), input, select');
        if (!f.length) return;
        const first = f[0];
        const last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }

      function loading(text) {
        sheet.replaceChildren(stepper(3), el('div', 'c-loading', text));
      }

      function request() {
        loading('Asking the bank for a one-time approval request…');
        Device.challenge(ctx.gatewayUrl, draft.group_canonical, ctx.keyId, draft.trace_id).then((r) => {
          if (!r.ok) { close({ status: 'failed', code: r.body.error, challenge_id: '' }); return; }
          ch = r.body;
          render();
        });
      }

      sheet.hidden = false;
      backdrop.hidden = false;
      document.addEventListener('keydown', onKey);
      request();

      function render() {
        clearInterval(timer);
        // THE source of everything shown: the bytes the device will sign.
        const env = JSON.parse(ch.signing_input);
        const txns = env.group.transactions;
        const accounts = ctx.me.accounts;
        sheet.replaceChildren();

        // ── header: title + countdown ring ────────────────────────────────
        const head = el('div', 'c-head');
        const titles = el('div', 'c-titles');
        const title = el('h2', 'c-title', 'Review and approve');
        title.id = 'sheetTitle';
        titles.append(title, el('div', 'c-subtitle', 'Check every line. Nothing moves until you approve.'));
        const ring = countdownRing();
        head.append(titles, ring.node);
        sheet.append(head, stepper(3));

        // ── the payments ─────────────────────────────────────────────────
        txns.forEach((t) => sheet.append(txnCard(t, txns, accounts)));

        // ── balance impact and total ─────────────────────────────────────
        const short = [];
        const impact = el('div', 'c-impact');
        impact.append(el('div', 'c-sec', 'Balance after payment'));
        [...new Set(txns.map((t) => t.debtor_account))].forEach((id) => {
          const a = accountOf(id, accounts);
          const out = txns.filter((t) => t.debtor_account === id).reduce((s, t) => s + cents(t.amount), 0n);
          const before = cents(a ? a.balance : '0');
          const after = before - out;
          const row = el('div', 'c-impact-row' + (after < 0n ? ' short' : ''));
          row.append(el('span', null, accountLabel(id, accounts)),
                     el('span', 'amount', `${money(fromCents(before))} → ${money(fromCents(after))}`));
          impact.append(row);
          if (after < 0n) short.push(accountLabel(id, accounts));
        });
        const total = txns.reduce((s, t) => s + cents(t.amount), 0n);
        const tot = el('div', 'c-total');
        tot.append(el('span', null, `Total · ${txns.length} payment${txns.length > 1 ? 's' : ''}`),
                   el('span', 'amount', money(fromCents(total))));
        impact.append(tot, el('div', 'c-policy', '⛓ All or nothing: if any payment fails, none of them is made.'));
        if (short.length) {
          impact.append(el('div', 'c-short', `Not enough money in ${short.join(', ')}. Choose another account above, or cancel.`));
        }
        sheet.append(impact);

        // ── the bank's checks ────────────────────────────────────────────
        sheet.append(checksBlock(ch.risk));

        // ── step-up code ─────────────────────────────────────────────────
        let otp = null;
        if (ch.step_up) {
          const box = el('div', 'c-stepup');
          box.append(el('div', 'c-stepup-title', '🔐 Extra verification needed'));
          ch.risk.rules.filter((r) => r.result === 'step_up')
            .forEach((r) => box.append(el('div', 'c-stepup-why', '• ' + r.detail)));
          const lbl = el('label', 'c-otp-label', "Enter the 6-digit code shown on your other device (security token)");
          otp = el('input', 'c-otp amount');
          Object.assign(otp, { inputMode: 'numeric', maxLength: 6, autocomplete: 'one-time-code', id: 'otpInput',
                               placeholder: '••••••' });
          lbl.htmlFor = 'otpInput';
          box.append(lbl, otp, el('div', 'c-fine', 'The code comes straight from the bank, never through the chat.'));
          sheet.append(box);
        }

        // ── what is being signed ─────────────────────────────────────────
        const sig = el('details', 'c-sign');
        sig.append(el('summary', null, '🔏 You are signing exactly what is shown above'));
        const dl = el('dl', 'c-sign-dl');
        const hashDd = el('dd', 'mono', 'computing…');
        dl.append(el('dt', null, 'Approval request'), el('dd', 'mono', env.challenge_id),
                  el('dt', null, 'Content hash'), hashDd,
                  el('dt', null, 'Draft hash'), el('dd', 'mono', env.group_hash.slice(0, 24) + '…'),
                  el('dt', null, 'Device key'), el('dd', 'mono', env.key_id),
                  el('dt', null, 'One-time nonce'), el('dd', 'mono', env.nonce.slice(0, 16) + '…'));
        sig.append(dl, el('div', 'c-fine', 'This screen is drawn from the bank\'s approval request, not from the chat. '
          + 'The signature covers these exact bytes, so any change after you approve is rejected by the bank.'));
        sha256Hex(ch.signing_input).then((h) => { hashDd.textContent = `sha256 ${h.slice(0, 32)}…`; });
        sheet.append(sig);

        // ── actions (pinned) ─────────────────────────────────────────────
        const error = el('div', 'c-error');
        error.setAttribute('role', 'alert');
        const hold = el('button', 'c-hold');
        hold.append(el('span', 'c-hold-fill'), el('span', 'c-hold-text', '◉  Press and hold to approve'));
        const cancel = el('button', 'c-cancel', 'Cancel — nothing will be sent');
        const actions = el('div', 'c-actions');
        actions.append(error, hold, cancel);
        sheet.append(actions);
        cancel.onclick = () => close({ status: 'cancelled' });
        if (short.length) { hold.disabled = true; error.textContent = 'Approval is off until every account can cover its payments.'; }

        const release = () => { clearTimeout(holdTimer); hold.classList.remove('holding'); };
        hold.addEventListener('pointerdown', (e) => {
          e.preventDefault();
          if (hold.disabled) return;
          if (otp && !/^\d{6}$/.test(otp.value.trim())) {
            error.textContent = 'Enter the 6-digit code from your other device first.';
            otp.focus();
            return;
          }
          hold.classList.add('holding');
          holdTimer = setTimeout(approve, HOLD_MS);
        });
        ['pointerup', 'pointerleave', 'pointercancel'].forEach((t) => hold.addEventListener(t, release));

        async function approve() {
          release();
          hold.disabled = true;
          hold.querySelector('.c-hold-text').textContent = 'Signing on this device…';
          error.textContent = '';
          const signature = await Device.sign(ctx.me.customer_id, ch.signing_input);
          hold.querySelector('.c-hold-text').textContent = 'Sending to the bank…';
          const r = await Device.submit(ctx.gatewayUrl, ch.challenge_id, ch.signing_input,
                                        signature, otp && otp.value.trim());
          if (r.ok) { close({ status: 'submitted', challenge_id: ch.challenge_id }); return; }
          if (r.body.error === 'STEP_UP_REQUIRED' && r.body.attempts_left > 0) {
            error.textContent = `${Device.explain(r.body.error)} ${r.body.attempts_left} tries left.`;
            hold.disabled = false;
            hold.querySelector('.c-hold-text').textContent = '◉  Press and hold to approve';
            if (otp) { otp.value = ''; otp.focus(); }
            return;
          }
          close({ status: 'failed', code: r.body.error, challenge_id: ch.challenge_id });
        }

        // ── countdown ────────────────────────────────────────────────────
        const issued = Date.parse(env.issued_at);
        const expires = Date.parse(env.expires_at);
        const span = Math.max(1, expires - issued);
        const tick = () => {
          const ms = Math.max(0, expires - Date.now());
          const s = Math.round(ms / 1000);
          ring.set(ms / span, s);
          if (s > 0) return;
          clearInterval(timer);
          release();
          actions.replaceChildren(expiredBlock());
        };
        tick();
        timer = setInterval(tick, 250);
        (otp || hold).focus();

        function expiredBlock() {
          const box = el('div', 'c-expired');
          box.append(el('div', 'c-expired-title', '⏱ This approval request has expired'),
                     el('div', 'c-fine', 'For your safety, approvals last 2 minutes and can be used once. Nothing was sent.'));
          const again = el('button', 'c-hold', 'Request a new approval');
          again.onclick = () => request();
          const stop = el('button', 'c-cancel', 'Cancel');
          stop.onclick = () => close({ status: 'cancelled' });
          box.append(again, stop);
          setTimeout(() => again.focus(), 0);
          return box;
        }

        function txnCard(t, all, accts) {
          const [icon, label] = TYPES[t.type] || ['•', t.type];
          const card = el('div', 'c-txn');
          const top = el('div', 'c-txn-head');
          top.append(el('span', 'c-icon', icon), el('span', 'c-type', `${t.seq} · ${label}`));
          card.append(top, el('div', 'c-amount amount', money(t.amount, t.currency)));
          const basis = basisLine(t, all, accts);
          if (basis) card.append(el('div', 'c-basis amount', basis));

          const dl = el('dl', 'c-fields');
          const toOwn = t.creditor && t.creditor.kind === 'own_account' ? t.creditor.id : null;
          const options = accts.filter((a) => a.account_id !== toOwn);
          const fromDd = el('dd');
          if (options.length > 1) {
            const sel = el('select', 'c-from');
            sel.setAttribute('aria-label', `Pay payment ${t.seq} from`);
            options.forEach((a) => {
              const o = el('option', null, `${a.nickname} ·· ${a.last4} — ${money(a.balance)}`);
              o.value = a.account_id;
              o.selected = a.account_id === t.debtor_account;
              sel.append(o);
            });
            sel.onchange = () => {
              sel.disabled = true;
              close({ status: 'edit', seq: t.seq, account_id: sel.value, challenge_id: ch.challenge_id });
            };
            fromDd.append(sel);
          } else {
            fromDd.textContent = accountLabel(t.debtor_account, accts);
          }
          dl.append(el('dt', null, 'From'), fromDd, el('dt', null, 'To'), el('dd', null, target(t, accts)));
          card.append(dl);
          (t.flags || []).forEach((f) => card.append(el('div', 'c-flag', '⚠ ' + (FLAGS[f] || f))));
          return card;
        }
      }
    });
  }

  function stepper(active) {
    const ol = el('ol', 'c-steps');
    STEPS.forEach((s, i) => {
      const li = el('li', i < active ? 'done' : i === active ? 'now' : '');
      li.append(el('span', 'c-step-dot', i < active ? '✓' : String(i + 1)), el('span', 'c-step-t', s));
      ol.append(li);
    });
    return ol;
  }

  function countdownRing() {
    const R = 24, C = 2 * Math.PI * R;
    const wrap = el('div', 'c-timer');
    wrap.setAttribute('role', 'timer');
    wrap.innerHTML = `<svg viewBox="0 0 60 60" aria-hidden="true"><circle cx="30" cy="30" r="${R}" class="ct-bg"/>
      <circle cx="30" cy="30" r="${R}" class="ct-fg" stroke-dasharray="${C}" stroke-dashoffset="0" transform="rotate(-90 30 30)"/></svg>`;
    const label = el('div', 'c-timer-t amount', '2:00');
    const sub = el('div', 'c-timer-s', 'left');
    wrap.append(label, sub);
    const fg = wrap.querySelector('.ct-fg');
    return {
      node: wrap,
      set(frac, s) {
        fg.setAttribute('stroke-dashoffset', String(C * (1 - Math.max(0, Math.min(1, frac)))));
        label.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
        wrap.classList.toggle('warn', s <= WARN_S && s > DANGER_S);
        wrap.classList.toggle('danger', s <= DANGER_S);
        wrap.setAttribute('aria-label', `${s} seconds left to approve`);
        sub.textContent = s ? 'left' : 'expired';
      },
    };
  }

  function checksBlock(risk) {
    const hits = {};
    (risk.rules || []).forEach((r) => { (hits[r.rule] = hits[r.rule] || []).push(r); });
    const stepUps = CHECKS.filter(([k]) => (hits[k] || []).some((r) => r.result === 'step_up')).length;
    const passed = CHECKS.length - Object.keys(hits).length;
    const box = el('details', 'c-checks');
    const sum = el('summary');
    sum.append(el('span', 'c-checks-t', '🛡 Bank safety checks'),
               el('span', 'c-checks-n', `${passed} passed${stepUps ? ` · ${stepUps} need${stepUps > 1 ? '' : 's'} your code` : ''}`));
    box.append(sum);
    const ul = el('ul', 'c-check-list');
    CHECKS.forEach(([k, label]) => {
      const rs = hits[k] || [];
      const worst = rs.some((r) => r.result === 'deny') ? 'deny' : rs.length ? 'step_up' : 'pass';
      const li = el('li', 'ck-' + worst);
      li.append(el('span', 'ck-i', worst === 'pass' ? '✓' : worst === 'deny' ? '✕' : '!'),
                el('span', 'ck-l', label),
                el('span', 'ck-d', worst === 'pass' ? 'passed' : rs.map((r) => r.detail).join('; ')));
      ul.append(li);
    });
    box.append(ul);
    if (stepUps) box.open = true;
    return box;
  }

  return { open, money };
})();
