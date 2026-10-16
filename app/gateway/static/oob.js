// Polls the gateway for challenges that need an out-of-band code. All server
// strings go in through textContent.
async function refresh() {
  const list = document.getElementById('list');
  let items = [];
  try {
    items = await (await fetch('/v1/oob/pending')).json();
  } catch (e) {
    return;
  }
  list.replaceChildren();
  if (!items.length) {
    const p = document.createElement('p');
    p.className = 'meta';
    p.textContent = 'No payments are waiting for a code.';
    list.append(p);
    return;
  }
  for (const it of items) {
    const card = document.createElement('div');
    card.className = 'card';
    const otp = document.createElement('div');
    otp.className = 'otp';
    otp.textContent = it.otp;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `${it.customer_id} · ${it.challenge_id} · expires ${new Date(it.expires_at).toLocaleTimeString()}`;
    const ul = document.createElement('ul');
    for (const line of [...it.summary, ...it.rules]) {
      const li = document.createElement('li');
      li.textContent = line;
      ul.append(li);
    }
    card.append(otp, meta, ul);
    list.append(card);
  }
}
refresh();
setInterval(refresh, 2000);
