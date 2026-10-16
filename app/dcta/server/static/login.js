/* Billie — the phone's sign-in screens (mock identity provider, see
   server/auth.py): Face ID or PIN, a locked account, and a new device that must
   be confirmed with an SMS code. Every outcome is said in words, not colour alone. */
'use strict';

const Login = (() => {
  const { esc } = UI;
  const PIN_LEN = 6;

  // view: home | pin | otp | locked
  let view = 'home';
  let pin = '';
  let method = 'biometric';
  let message = null;          // {tone, text}
  let otpTo = '';
  let locked = null;           // 'bank' | 'pin'
  let busy = false;
  let ctx = null;              // {root, persona, onSignedIn}

  function reset() {
    view = 'home'; pin = ''; method = 'biometric'; message = null; otpTo = ''; locked = null; busy = false;
  }

  function render(root, persona, onSignedIn) {
    ctx = { root, persona, onSignedIn };
    paint();
  }

  function paint() {
    const { root, persona } = ctx;
    if (!persona) {
      root.innerHTML = `<div class="lg-wrap"><div class="lg-logo">Bank</div>
        <p class="lg-hint">Choose a persona in the demo bar above to start.</p></div>`;
      return;
    }
    const head = `<div class="lg-logo">Bank</div>
      <div class="lg-with">with <b>Billie</b>, your payment assistant</div>
      <div class="lg-hello">Good to see you,</div>
      <div class="lg-name">${esc(persona.name)}</div>
      <div class="lg-id">User ID ${esc(persona.id)} ·  •••• ${esc(persona.mobile)}</div>`;
    const msg = message ? `<div class="lg-msg ${esc(message.tone)}" role="alert">${esc(message.text)}</div>` : '';

    if (view === 'locked') {
      root.innerHTML = `<div class="lg-wrap">${head}
        <div class="lg-locked" role="alert">
          <div class="lg-locked-icon">🔒</div>
          <div class="lg-locked-title">Digital banking access is locked</div>
          <p>${locked === 'bank'
            ? 'The bank has locked online and mobile access to this profile for your protection.'
            : 'There were 3 incorrect PIN attempts, so access is locked to keep your money safe.'}</p>
          <p><b>No payments can be made</b> from this app until it is unlocked.</p>
          <ul><li>Call the 24-hour hotline and verify your identity, or</li>
              <li>visit any branch with your NRIC.</li></ul>
        </div>
        <button class="lg-link" id="lgBack">← Back</button></div>`;
      root.querySelector('#lgBack').onclick = () => { view = 'home'; message = null; paint(); };
      return;
    }

    if (view === 'otp') {
      root.innerHTML = `<div class="lg-wrap">${head}
        <div class="lg-card">
          <div class="lg-card-title">📱 Confirm this new device</div>
          <p class="lg-p">This phone has not been used with your account before. We sent a 6-digit code
            by SMS to <b>${esc(otpTo)}</b>.</p>
          <input class="lg-otp amount" id="lgOtp" inputmode="numeric" maxlength="6" autocomplete="one-time-code"
                 aria-label="SMS code" placeholder="••••••">
          ${msg}
          <button class="lg-primary" id="lgOtpGo" ${busy ? 'disabled' : ''}>Confirm device</button>
          <p class="lg-fine">Never share this code. The bank will never ask for it by phone.</p>
        </div>
        <button class="lg-link" id="lgBack">Cancel</button></div>`;
      const box = root.querySelector('#lgOtp');
      box.focus();
      const go = () => submit({ otp: box.value.trim() });
      root.querySelector('#lgOtpGo').onclick = go;
      box.onkeydown = (e) => { if (e.key === 'Enter') go(); };
      root.querySelector('#lgBack').onclick = () => { view = 'home'; message = null; paint(); };
      return;
    }

    if (view === 'pin') {
      const dots = Array.from({ length: PIN_LEN }, (_, i) => `<span class="pin-dot${i < pin.length ? ' on' : ''}"></span>`).join('');
      const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '', '0', '⌫']
        .map((k) => k ? `<button class="pin-key" data-k="${k}" ${busy ? 'disabled' : ''}>${k}</button>` : '<span></span>').join('');
      root.innerHTML = `<div class="lg-wrap">${head}
        <div class="lg-sub">Enter your 6-digit PIN</div>
        <div class="pin-dots" aria-label="${pin.length} of ${PIN_LEN} digits entered">${dots}</div>
        ${msg}
        <div class="pin-pad">${keys}</div>
        <button class="lg-link" id="lgBio">Use Face ID instead</button></div>`;
      root.querySelectorAll('.pin-key').forEach((b) => { b.onclick = () => key(b.dataset.k); });
      root.querySelector('#lgBio').onclick = () => { view = 'home'; message = null; paint(); };
      return;
    }

    root.innerHTML = `<div class="lg-wrap">${head}
      <button class="lg-face${busy ? ' scanning' : ''}" id="lgFace" ${busy ? 'disabled' : ''} aria-label="Sign in with Face ID">
        <span class="lg-face-icon">☺</span></button>
      <div class="lg-sub">${busy ? 'Scanning…' : 'Tap to sign in with Face ID'}</div>
      ${msg}
      <button class="lg-link" id="lgPin">Use PIN instead</button>
      <p class="lg-fine">Demo: Face ID is simulated. The demo PIN is 123456.</p></div>`;
    root.querySelector('#lgFace').onclick = () => {
      method = 'biometric';
      busy = true; message = null; paint();
      setTimeout(() => submit({}), 700);
    };
    root.querySelector('#lgPin').onclick = () => { view = 'pin'; pin = ''; message = null; paint(); };
  }

  function key(k) {
    if (busy) return;
    if (k === '⌫') pin = pin.slice(0, -1);
    else if (pin.length < PIN_LEN) pin += k;
    paint();
    if (pin.length === PIN_LEN) { method = 'pin'; submit({}); }
  }

  async function submit(extra) {
    busy = true;
    const body = { customer_id: ctx.persona.id, method, ...(method === 'pin' ? { pin } : {}), ...extra };
    let resp, data;
    try {
      resp = await fetch('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                         body: JSON.stringify(body) });
      data = await resp.json();
    } catch (e) {
      busy = false; message = { tone: 'bad', text: "Couldn't reach the bank. Check your connection and try again." };
      paint(); return;
    }
    busy = false;
    if (resp.ok) { const done = ctx.onSignedIn; reset(); done(data); return; }
    pin = '';
    switch (data.error) {
      case 'PIN_WRONG':
        message = { tone: 'warn', text: `Incorrect PIN. ${data.attempts_left} attempt${data.attempts_left === 1 ? '' : 's'} left before access is locked.` };
        view = 'pin'; break;
      case 'LOCKED':
        locked = data.reason; view = 'locked'; message = null; break;
      case 'OTP_REQUIRED':
        otpTo = data.sent_to; view = 'otp'; message = null; break;
      case 'OTP_WRONG':
        message = { tone: 'warn', text: `That code is not right. ${data.attempts_left} attempt${data.attempts_left === 1 ? '' : 's'} left.` };
        view = 'otp'; break;
      case 'OTP_EXPIRED':
        message = { tone: 'bad', text: 'That code has expired or was entered wrongly too often. Sign in again to get a new one.' };
        view = 'home'; break;
      default:
        message = { tone: 'bad', text: 'Sign-in failed. Please try again.' };
    }
    paint();
  }

  return { render, reset };
})();
