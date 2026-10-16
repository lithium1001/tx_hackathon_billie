/* Billie — small shared helpers for every view. Server strings are attacker-
   controlled (payee names, memos), so anything bound for innerHTML goes
   through esc() first. */
'use strict';

const UI = (() => {
  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // Amounts are decimal strings; group the digits without going through float.
  function money(amount, currency) {
    const s = String(amount == null ? '0' : amount);
    const neg = s.startsWith('-');
    const [int, frac = '00'] = s.replace(/^[-+]/, '').split('.');
    const grouped = int.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const cur = !currency || currency === 'SGD' ? 'S$' : currency + ' ';
    return `${neg ? '−' : ''}${cur}${grouped}.${frac.padEnd(2, '0').slice(0, 2)}`;
  }

  // Exact decimal arithmetic in cents.
  function cents(amount) {
    const s = String(amount || '0');
    const neg = s.startsWith('-');
    const [int, frac = '0'] = s.replace(/^[-+]/, '').split('.');
    const c = BigInt(int || '0') * 100n + BigInt(frac.padEnd(2, '0').slice(0, 2));
    return neg ? -c : c;
  }
  function fromCents(c) {
    const neg = c < 0n;
    const a = neg ? -c : c;
    return `${neg ? '-' : ''}${a / 100n}.${String(a % 100n).padStart(2, '0')}`;
  }

  const DATE = new Intl.DateTimeFormat('en-SG', { day: 'numeric', month: 'short', year: 'numeric' });
  const DAY = new Intl.DateTimeFormat('en-SG', { weekday: 'short', day: 'numeric', month: 'short' });
  const TIME = new Intl.DateTimeFormat('en-SG', { hour: '2-digit', minute: '2-digit' });
  const date = (iso) => DATE.format(new Date(iso));
  const day = (iso) => DAY.format(new Date(iso));
  const time = (iso) => TIME.format(new Date(iso));

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // "LIM WEI JIE" → "Lim Wei Jie": greet by the whole name, never guess which part is the given name.
  const title = (name) => String(name || '').toLowerCase().replace(/\b\p{L}/gu, (c) => c.toUpperCase());

  const initials = (name) => String(name || '?').split(/\s+/).filter(Boolean).slice(0, 2)
    .map((w) => w[0]).join('');

  return { esc, money, cents, fromCents, date, day, time, el, initials, title };
})();
